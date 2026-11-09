"""Editable reminder schedules with delivered-history preservation."""
from datetime import timedelta
from typing import Literal
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import Event, Person, Reminder, Task
from .reminder_defaults import event_reminder_due, task_reminder_due


class ReminderSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')
    recipient_id: UUID | None = Field(default=None, description='Null selects the requesting user.')
    offset_minutes: int = Field(default=0, ge=0, le=525600)
    local_time: str | None = Field(default=None, pattern=r'^([01]\d|2[0-3]):[0-5]\d$',
        description='Clock time in the event timezone (task: owner timezone), on its date, minus offset_minutes. E.g. 11:00 with offset 0.')


class UpdateRemindersArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['event', 'task']
    record_id: UUID
    expected_version: int = Field(ge=1)
    mode: Literal['add', 'remove', 'replace', 'disable']
    reminders: list[ReminderSpec] = Field(default_factory=list, max_length=20)
    recipient_id: UUID | None = Field(default=None, description='For replace/disable: null selects requesting user; pass another permitted attendee explicitly to change theirs.')

    @model_validator(mode='after')
    def schedule(self):
        if self.mode in ('add', 'remove') and not self.reminders:
            raise ValueError('Specify reminder configurations to add/remove.')
        return self


def _latest(obj):
    rows = {}
    for row in obj.reminders.order_by('-object_version', '-created_at', '-id'):
        rows.setdefault((row.recipient_id, row.offset_minutes, row.kind), row)
    return rows


def refresh_event_reminders(event, restore_cancelled=False, clone_pending=False):
    """Fence queued jobs, retain sent reminders on metadata changes, never revive disabled rows."""
    rows = _latest(event)
    keep = [r.pk for r in rows.values()]
    event.reminders.exclude(pk__in=keep).filter(status__in=['pending', 'queued', 'paused']).update(status='cancelled')
    attendees = set(event.participants.values_list('pk', flat=True))
    # A newly added attendee inherits the current schedule, unless the event has no reminders.
    known_recipients = {r.recipient_id for r in rows.values()}
    schedule = {(r.offset_minutes, r.kind) for r in rows.values() if r.status not in ('cancelled', 'disabled', 'detached')}
    for recipient in attendees - known_recipients:
        for offset, reminder_kind in schedule:
            new = Reminder.objects.create(event=event, recipient_id=recipient, offset_minutes=offset,
                kind=reminder_kind, due_at=event_reminder_due(event, offset, reminder_kind),
                object_version=event.version, status='pending' if event.status == 'confirmed' else 'paused')
            rows[(recipient, offset, reminder_kind)] = new
    for row in rows.values():
        due = event_reminder_due(event, row.offset_minutes, row.kind)
        if row.status == 'disabled' or (row.status == 'cancelled' and not restore_cancelled):
            continue
        if row.recipient_id not in attendees:
            if row.status in ('sent', 'missed', 'summarized'):
                Reminder.objects.create(event=event, recipient_id=row.recipient_id, offset_minutes=row.offset_minutes,
                    kind=row.kind, due_at=row.due_at, object_version=event.version, status='detached')
                continue
            row.status = 'detached'
        elif event.status == 'cancelled':
            if row.status in ('pending', 'queued', 'paused'):
                row.status = 'cancelled'
        elif event.status != 'confirmed':
            if row.status in ('pending', 'queued', 'paused'):
                row.status = 'paused'
        elif due != row.due_at and (clone_pending or row.status in ('sent', 'missed', 'summarized')):
            # A timing move gets a new delivery row; preserve delivery history and fence old queued jobs.
            if row.status not in ('sent', 'missed', 'summarized'):
                row.status = 'cancelled'
                row.save(update_fields=['status', 'updated_at'])
            Reminder.objects.create(event=event, recipient_id=row.recipient_id, offset_minutes=row.offset_minutes,
                kind=row.kind, due_at=due, object_version=event.version, status='pending')
            continue
        elif due != row.due_at or row.status in ('pending', 'queued', 'paused', 'cancelled', 'detached'):
            row.due_at = due
            row.status = 'pending'
        row.object_version = event.version
        row.save(update_fields=['status', 'due_at', 'object_version', 'updated_at'])


@transaction.atomic
def update_reminders(user, kind, record_id, expected_version, mode, reminders=None, recipient_id=None, shared_context=False):
    from . import services
    data = UpdateRemindersArgs.model_validate(dict(kind=kind, record_id=record_id, expected_version=expected_version,
        mode=mode, reminders=reminders or [], recipient_id=recipient_id))
    services.get_entity(kind, record_id, user, shared_context)
    model = Event if kind == 'event' else Task
    obj = model.objects.select_for_update().get(pk=record_id)
    if not services.can_edit(obj, user):
        raise PermissionDenied('Нет прав на изменение напоминаний.')
    if obj.version != expected_version:
        raise ValidationError('Запись уже изменилась, откройте её заново.')
    people = set(obj.participants.values_list('pk', flat=True))
    if kind == 'task':
        people |= {obj.owner_id, obj.assignee_id}
    target = data.recipient_id or user.pk
    specs = []
    for item in data.reminders:
        recipient = item.recipient_id or user.pk
        if recipient not in people or not Person.objects.filter(pk=recipient, telegram_id__in=services.settings.ALLOWED_TELEGRAM_IDS).exists():
            raise PermissionDenied('Напоминание доступно только разрешённому участнику.')
        specs.append((recipient, item.offset_minutes, f'at:{item.local_time}' if item.local_time else 'custom'))
    if mode in ('replace', 'disable') and target not in people:
        raise PermissionDenied('Напоминание доступно только участнику.')
    if mode == 'replace' and any(recipient != target for recipient, _, _ in specs):
        raise ValidationError('При замене укажите напоминания только выбранного участника.')
    if kind == 'task' and obj.due_at is None and mode in ('add', 'replace') and specs:
        raise ValidationError('Сначала укажите срок задачи для напоминания.')
    obj.version += 1
    obj.save(update_fields=['version', 'updated_at'])
    def disable(row):
        if row.status in ('sent', 'missed', 'summarized'):
            Reminder.objects.create(**{kind: obj}, recipient_id=row.recipient_id, offset_minutes=row.offset_minutes,
                kind=row.kind, due_at=row.due_at, object_version=obj.version, status='disabled')
        else:
            row.status = 'disabled'
            row.object_version = obj.version
            row.save(update_fields=['status', 'object_version', 'updated_at'])
    if mode in ('replace', 'disable'):
        for row in _latest(obj).values():
            if row.recipient_id == target:
                disable(row)
    if mode == 'remove':
        for recipient, offset, reminder_kind in specs:
            kinds = ('custom', 'reminder') if reminder_kind == 'custom' else (reminder_kind,)
            for row in _latest(obj).values():
                if row.recipient_id == recipient and row.offset_minutes == offset and row.kind in kinds:
                    disable(row)
    if mode in ('add', 'replace'):
        latest = _latest(obj)
        for recipient, offset, reminder_kind in dict.fromkeys(specs):
            old = latest.get((recipient, offset, reminder_kind))
            if mode == 'add' and old and old.status not in ('disabled', 'cancelled'):
                continue
            due = event_reminder_due(obj, offset, reminder_kind) if kind == 'event' else task_reminder_due(obj, offset, reminder_kind)
            status = ('pending' if obj.status == 'confirmed' else 'paused') if kind == 'event' else ('pending' if obj.status in ('todo', 'in_progress') else 'disabled')
            Reminder.objects.create(**{kind: obj}, recipient_id=recipient, offset_minutes=offset,
                kind=reminder_kind, due_at=due, object_version=obj.version, status=status)
    if kind == 'event':
        refresh_event_reminders(obj)
    else:
        obj.reminders.filter(status__in=['pending', 'queued']).update(object_version=obj.version, status='pending')
    services._audit(user, obj, 'update_reminders', {'mode': mode})
    return obj
