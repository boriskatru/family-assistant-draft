"""Transactional edits of the future part of a recurrence; past records stay intact."""
from copy import deepcopy
from datetime import datetime, time, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from dateutil.rrule import rrulestr
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import Event, OccurrenceOverride, RecurrenceRule, Task
from . import recurrence


class RecurrenceChanges(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rrule: str | None = None
    mode: Literal['calendar', 'cycle', 'after_completion'] | None = None
    starts_at: datetime | None = Field(default=None, description='Anchor of the first future occurrence, with UTC offset.')
    local_time: str | None = Field(default=None, pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    interval: int | None = Field(default=None, ge=1, le=365)
    unit: Literal['day', 'week', 'month'] | None = None
    cycle: list[bool] | None = Field(default=None, max_length=366)
    until: datetime | None = None
    clear_until: bool = False
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=10000)
    location: str | None = Field(default=None, max_length=500)
    duration_minutes: int | None = Field(default=None, ge=1, le=525600)

    @field_validator('starts_at', 'until')
    @classmethod
    def aware(cls, value):
        if value is not None and timezone.is_naive(value):
            raise ValueError('Specify a UTC offset.')
        return value

    @model_validator(mode='after')
    def nonempty(self):
        if self.clear_until and self.until is not None:
            raise ValueError('Cannot set and clear until.')
        if self.starts_at and self.local_time:
            raise ValueError('Specify starts_at or local_time, not both.')
        if not any(v is not None and v is not False for v in self.model_dump().values()):
            raise ValueError('Specify changes.')
        return self


class UpdateRecurrenceArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['event', 'task']
    record_id: UUID
    expected_version: int = Field(ge=1)
    changes: RecurrenceChanges
    effective_from: datetime | None = Field(default=None, description='Apply to occurrences starting at/after this moment; null means now. Past occurrences stay unchanged.')

    @field_validator('effective_from')
    @classmethod
    def aware(cls, value):
        return RecurrenceChanges.aware(value)


class StopRecurrenceArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['event', 'task']
    record_id: UUID
    expected_version: int = Field(ge=1)
    cancel_future: bool = Field(default=True, description='Cancel already materialized future occurrences as well. False stops generation but preserves existing records, useful before converting the source task.')
    effective_from: datetime | None = None

    @field_validator('effective_from')
    @classmethod
    def aware(cls, value):
        return RecurrenceChanges.aware(value)


def _locked(user, kind, record_id, expected_version, shared_context):
    from . import services
    services.get_entity(kind, record_id, user, shared_context)
    model = Event if kind == 'event' else Task
    obj = model.objects.select_for_update().get(pk=record_id)
    if not services.can_edit(obj, user):
        raise PermissionDenied('Нет прав на изменение серии.')
    if obj.version != expected_version:
        raise ValidationError('Запись уже изменилась, откройте её заново.')
    if not obj.recurrence_id:
        raise ValidationError('Запись не относится к серии.')
    rule = RecurrenceRule.objects.select_for_update().get(pk=obj.recurrence_id)
    return obj, rule


def _future(rule, cutoff):
    model, field = (Event, 'starts_at') if rule.kind == 'event' else (Task, 'due_at')
    return model.objects.select_for_update().filter(recurrence=rule, **{field + '__gte': cutoff}).order_by(field, 'id')


def _cancel(user, obj):
    from . import services
    if not services.can_edit(obj, user):
        raise PermissionDenied('Нет прав на изменение одного из будущих экземпляров серии.')
    if isinstance(obj, Event):
        if obj.status != 'cancelled':
            services.update_event(user, obj.pk, {'status': 'cancelled'}, obj.version)
    elif obj.status not in ('done', 'cancelled'):
        from .task_actions import update_task
        update_task(user, obj.pk, {'status': 'cancelled'}, obj.version)


def _bump(obj):
    from . import services
    obj.refresh_from_db()
    obj.version += 1
    obj.save(update_fields=['version', 'updated_at'])
    if isinstance(obj, Event):
        services.reschedule_event(obj)
    else:
        obj.reminders.filter(status__in=['pending', 'queued']).update(object_version=obj.version, status='pending')


@transaction.atomic
def stop_recurrence(user, kind, record_id, expected_version, cancel_future=True, effective_from=None, shared_context=False):
    data = StopRecurrenceArgs.model_validate(dict(kind=kind, record_id=record_id,
        expected_version=expected_version, cancel_future=cancel_future, effective_from=effective_from))
    obj, rule = _locked(user, kind, record_id, expected_version, shared_context)
    now = timezone.now()
    cutoff = data.effective_from or now
    if cutoff < now:
        raise ValidationError('effective_from не может быть в прошлом: прошедшие экземпляры сохраняются.')
    # A future cutoff keeps the series running until it, instead of stopping it prematurely.
    rule.until = min(rule.until, cutoff-timedelta(microseconds=1)) if rule.until else cutoff-timedelta(microseconds=1)
    rule.active = cutoff > timezone.now()
    rule.version += 1
    rule.save(update_fields=['active', 'until', 'version', 'updated_at'])
    count = 0
    if cancel_future:
        for instance in _future(rule, cutoff):
            if instance.status not in ('done', 'cancelled'):
                _cancel(user, instance)
                count += 1
    _bump(obj)
    from . import services
    services._audit(user, rule, 'stop_recurrence', {'effective_from': cutoff.isoformat()})
    return {'rule_id': str(rule.pk), 'record_id': str(obj.pk), 'version': obj.version,
            'cancelled_occurrences': count, 'active_until': rule.until.isoformat()}


@transaction.atomic
def update_recurrence(user, kind, record_id, expected_version, changes, effective_from=None, shared_context=False):
    from . import services
    data = UpdateRecurrenceArgs.model_validate(dict(kind=kind, record_id=record_id,
        expected_version=expected_version, changes=changes, effective_from=effective_from))
    obj, old = _locked(user, kind, record_id, expected_version, shared_context)
    if not old.active:
        raise ValidationError('Серия остановлена. Создайте новое правило для нужного события или задачи.')
    now = timezone.now()
    cutoff = data.effective_from or now
    if cutoff < now:
        raise ValidationError('effective_from не может быть в прошлом: прошедшие экземпляры сохраняются.')
    values = data.changes.model_dump(exclude_none=True)
    zone = ZoneInfo(old.timezone)
    instances = list(_future(old, cutoff))
    for instance in instances:
        if not services.can_edit(instance, user):
            raise PermissionDenied('Нет прав на изменение одного из будущих экземпляров серии.')
    if old.mode == 'after_completion':
        anchor = next((x.due_at for x in instances if x.status not in ('done', 'cancelled')), None) or max(old.dtstart, cutoff)
    else:
        if old.mode == 'calendar':
            anchor = rrulestr(old.rrule, dtstart=old.dtstart.astimezone(zone)).after(cutoff.astimezone(zone), inc=True)
            if anchor is not None and old.until and anchor > old.until:
                anchor = None
        else:
            anchor = next(recurrence._occurrences(old, cutoff, max(cutoff, old.dtstart)+timedelta(days=max(366, len(old.cycle)))), None)
    if values.get('mode', old.mode) == 'after_completion' and ('interval' in values or 'unit' in values) and 'starts_at' not in values:
        seed = obj if obj.status == 'done' else obj.previous_task
        if seed and seed.completed_at:
            anchor = seed.completed_at.astimezone(zone) + relativedelta(**{values.get('unit', old.unit)+'s': values.get('interval', old.interval)})
            anchor = max(anchor, cutoff)
    anchor = values.get('starts_at', anchor)
    if anchor is None:
        raise ValidationError('У серии нет будущих повторений; задайте starts_at для нового продолжения.')
    if values.get('local_time'):
        anchor = datetime.combine(anchor.astimezone(zone).date(), time.fromisoformat(values['local_time']), zone)
    if anchor < cutoff:
        raise ValidationError('Первое новое повторение должно быть не раньше effective_from.')
    template = deepcopy(old.template)
    for field in ('title', 'description', 'location', 'duration_minutes'):
        if field in values:
            if kind == 'task' and field in ('location', 'duration_minutes'):
                raise ValidationError('location/duration_minutes применяются к серии событий.')
            template[field] = values[field]
    if template.get('all_day') and ('local_time' in values or 'duration_minutes' in values):
        raise ValidationError('Для серии событий на дату время и длительность в минутах не применяются.')
    mode = values.get('mode', old.mode)
    if (kind == 'event' and mode == 'after_completion') or (kind == 'task' and mode == 'cycle'):
        raise ValidationError('Этот режим недоступен для данного типа записи.')
    rrule = values.get('rrule', old.rrule)
    cycle = values.get('cycle', old.cycle)
    if mode == 'calendar':
        rrule = recurrence._checked_rrule(rrule, anchor.astimezone(zone))
        # COUNT is the total series size. A time-only edit carries the remaining count forward.
        if 'rrule' not in values and 'COUNT=' in rrule:
            parts = dict(p.split('=', 1) for p in rrule.split(';'))
            consumed = 0
            # Count before the cutoff without the materialization horizon/cap: COUNT is a total, not a batch size.
            for old_moment in rrulestr(old.rrule, dtstart=old.dtstart.astimezone(zone)):
                if old_moment >= cutoff:
                    break
                consumed += 1
            remaining = max(0, int(parts['COUNT'])-consumed)
            if not remaining:
                raise ValidationError('Все повторы серии уже завершены; задайте новое правило.')
            parts['COUNT'] = str(remaining)
            rrule = ';'.join(f'{key}={value}' for key, value in parts.items())
        cycle = []
    elif mode == 'cycle':
        if not cycle or not any(cycle):
            raise ValidationError('Цикл должен содержать хотя бы один активный день.')
        rrule = ''
    until = None if values.get('clear_until') else values.get('until', old.until)
    if until and until < anchor:
        raise ValidationError('Конец серии раньше первого нового повторения.')
    rule = RecurrenceRule.objects.create(owner=old.owner, visibility=old.visibility, domain=old.domain,
        title=template.get('title', old.title), kind=kind, mode=mode, dtstart=anchor, timezone=old.timezone,
        rrule=rrule, cycle=cycle, interval=values.get('interval', old.interval), unit=values.get('unit', old.unit),
        until=until, template=template)
    # Map by the original theoretical slot, including overrides with no materialized row.
    # Otherwise a cancelled occurrence would reappear when the future rule is split.
    by_key = {instance.occurrence_key: instance for instance in instances}
    overrides = {o.occurrence_key: o for o in OccurrenceOverride.objects.filter(rule=old)}
    if old.mode == 'after_completion':
        old_slots = [(instance, False) for instance in instances]
    else:
        upper = max(cutoff, old.dtstart, anchor) + timedelta(days=recurrence.HORIZON_DAYS)
        old_slots = []
        for old_moment in recurrence._occurrences(old, cutoff, upper):
            old_key = recurrence.occurrence_key(old_moment)
            override = overrides.get(old_key)
            old_slots.append((by_key.get(old_key), bool(override and override.action == 'cancel')))
    reused = set()
    old.until = min(old.until, cutoff-timedelta(microseconds=1)) if old.until else cutoff-timedelta(microseconds=1)
    old.active = cutoff > timezone.now()
    old.version += 1
    old.save(update_fields=['until', 'active', 'version', 'updated_at'])
    if mode == 'after_completion':
        moments = [anchor] if any(x.status not in ('done', 'cancelled') for x in instances) else []
    else:
        moments = list(recurrence._occurrences(rule, max(cutoff, anchor), max(cutoff, anchor)+timedelta(days=recurrence.HORIZON_DAYS)))
    first = None
    updated_count = created_count = cancelled_count = 0
    for index, moment in enumerate(moments):
        instance, cancelled_slot = old_slots[index] if index < len(old_slots) else (None, False)
        key = recurrence.occurrence_key(moment)
        if instance:
            reused.add(instance.pk)
        if cancelled_slot or (instance is not None and instance.status in ('done', 'cancelled')):
            # Preserve completed/cancelled exceptions without moving history or regenerating them.
            OccurrenceOverride.objects.create(rule=rule, occurrence_key=key, action='cancel')
            continue
        if instance is None:
            created_count += 1
            instance = recurrence._clone_event(rule, moment, key, None) if kind == 'event' else recurrence._clone_task(rule, moment, key)
        else:
            updated_count += 1
            instance.recurrence = rule
            instance.occurrence_key = key
            for field in ('title', 'description', 'location'):
                if field in values:
                    setattr(instance, field, values[field])
            if kind == 'event':
                if instance.all_day:
                    days = max(1, int(template.get('duration_days', 1)))
                    instance.start_date = moment.astimezone(zone).date()
                    instance.end_date = instance.start_date + timedelta(days=days)
                    instance.starts_at = datetime.combine(instance.start_date, time.min, zone)
                    instance.ends_at = datetime.combine(instance.end_date, time.min, zone)
                else:
                    instance.starts_at = moment
                    instance.ends_at = moment + timedelta(minutes=int(template.get('duration_minutes', 60)))
                instance.version += 1
                instance.save()
                services.reschedule_event(instance)
            else:
                from .task_actions import _cancel_generated_blocks, _reschedule_task
                old_due = instance.due_at
                instance.due_at = moment
                if instance.not_before and old_due:
                    instance.not_before += moment-old_due
                instance.version += 1
                instance.save()
                _reschedule_task(instance, deadline_changed=old_due != moment)
                if old_due != moment:
                    _cancel_generated_blocks(user, instance)
        first = first or instance
    for instance in instances:
        if instance.pk not in reused:
            if instance.status not in ('done', 'cancelled'):
                cancelled_count += 1
            _cancel(user, instance)
    if not first and kind == 'task' and mode == 'after_completion':
        # The completed seed must point to the new rule so its next completion logic sees it.
        obj.recurrence = rule
        for field in ('title', 'description'):
            if field in values and obj.status not in ('done', 'cancelled'):
                setattr(obj, field, values[field])
        if 'starts_at' in values and obj.status not in ('done', 'cancelled'):
            obj.due_at = anchor
        obj.save()
        first = obj
    _bump(obj)
    if first:
        first.refresh_from_db()
    services._audit(user, rule, 'update_recurrence', {'previous_rule_id': str(old.pk), 'effective_from': cutoff.isoformat()})
    return {'rule_id': str(rule.pk), 'record_id': str(first.pk) if first else str(obj.pk),
        'source_record_id': str(obj.pk), 'version': first.version if first else obj.version,
        'updated_occurrences': updated_count, 'created_occurrences': created_count,
        'cancelled_occurrences': cancelled_count}
