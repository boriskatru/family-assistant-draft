"""Local-time recurrence expansion with stable occurrence identities."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone as dt_timezone
from itertools import islice
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from dateutil.rrule import rrulestr
from dateutil.tz import datetime_exists, resolve_imaginary
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (Checklist, ChecklistItem, Event, OccurrenceOverride, Person,
                     RecurrenceRule, Reminder, Task)

from .reminder_defaults import event_reminder_minutes, event_reminder_due, task_reminder_due

UTC = dt_timezone.utc
HORIZON_DAYS = 180
MAX_OCCURRENCES_PER_RULE = 1000


def occurrence_key(moment):
    return moment.astimezone(UTC).isoformat()


def _datetime(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if timezone.is_naive(result):
        raise ValidationError('Время повторения должно содержать часовой пояс.')
    return result


def _checked_rrule(value, start):
    value = str(value).strip().upper()
    if value.startswith('RRULE:'):
        value = value[6:]
    if '\n' in value or '\r' in value:
        raise ValidationError('Укажите одно правило RRULE.')
    parts = dict(x.split('=', 1) for x in value.split(';') if '=' in x)
    if parts.get('FREQ') not in {'DAILY','WEEKLY','MONTHLY','YEARLY'}:
        raise ValidationError('В черновике доступны DAILY, WEEKLY, MONTHLY, YEARLY.')
    if any(key in parts for key in ('BYSECOND','BYMINUTE','BYHOUR')):
        raise ValidationError('Время берётся из исходного события; BYSECOND/BYMINUTE/BYHOUR не поддерживаются.')
    for key in ('INTERVAL','COUNT'):
        if key in parts:
            try:
                valid = 1 <= int(parts[key]) <= 1000000
            except ValueError:
                valid = False
            if not valid:
                raise ValidationError(f'{key}: ожидается положительное целое число до 1000000.')
    try:
        iterator = rrulestr(value, dtstart=start)
        next(iter(iterator), None)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValidationError('Некорректное правило повторения.') from exc
    return value


def _reminder_template(obj):
    configs={}
    for r in obj.reminders.order_by('-object_version','-created_at'):
        configs.setdefault((r.recipient_id,r.offset_minutes,r.kind),r)
    return [{'recipient_id':str(r.recipient_id),'offset_minutes':r.offset_minutes,'kind':r.kind}
            for r in configs.values() if r.status not in ('cancelled','disabled','detached')]


def _event_template(event):
    fields = ['title','description','domain','visibility','location','kind','flexible',
              'buffer_before','buffer_after','timezone','end_timezone','packing_notes','place_snapshot','blocks_time','counts_toward_load']
    result = {f:getattr(event,f) for f in fields}
    result.update(duration_minutes=int((event.ends_at-event.starts_at).total_seconds()//60),
                  participants=[str(p.pk) for p in event.participants.all()],
                  project_id=str(event.project_id) if event.project_id else None,
                  task_id=str(event.task_id) if event.task_id else None,
                  trip_id=str(event.trip_id) if event.trip_id else None,
                  place_id=str(event.place_id) if event.place_id else None,
                  all_day=event.all_day,
                  duration_days=(event.end_date-event.start_date).days if event.all_day and event.start_date and event.end_date else 1,
                  reminders=_reminder_template(event),
                  checklists=[{'title':c.title,'source_text':c.source_text,
                               'items':[{'text':i.text,'quantity':i.quantity,'assignee_id':str(i.assignee_id) if i.assignee_id else None,'position':i.position} for i in c.items.all()]}
                              for c in event.checklists.all()])
    # Generate a distinct Meet for each future call; do not copy an access URL.
    return result


def _task_template(task):
    fields = ['title','description','domain','visibility','priority','estimated_minutes','allow_split','category']
    result = {f:getattr(task,f) for f in fields}
    result.update(remaining_minutes=task.estimated_minutes,
                  assignee_id=str(task.assignee_id), participants=[str(p.pk) for p in task.participants.all()],
                  project_id=str(task.project_id) if task.project_id else None,
                  trip_id=str(task.trip_id) if task.trip_id else None,
                  parent_id=str(task.parent_id) if task.parent_id else None,
                  dependencies=[str(d.pk) for d in task.dependencies.all()],
                  reminders=_reminder_template(task),
                  reminders_disabled=task.reminders.filter(status='disabled').exists(),
                  not_before_offset_seconds=int((task.due_at-task.not_before).total_seconds()) if task.due_at and task.not_before else None)
    return result


@transaction.atomic
def create_rule_from_event(user, event, rrule='', mode='calendar', cycle=None):
    from .services import can_edit
    event = Event.objects.select_for_update().get(pk=event.pk)
    if not can_edit(event,user):
        raise PermissionDenied('Нет прав на создание серии этого события.')
    if event.status != 'confirmed':
        raise ValidationError('Сначала подтвердите событие.')
    if event.recurrence_id and event.recurrence.active:
        raise ValidationError('Событие уже относится к серии.')
    attendees = set(event.participants.values_list('telegram_id', flat=True))
    if event.owner_id != user.pk or attendees - {user.telegram_id}:
        if not attendees.issubset(settings.ALLOWED_TELEGRAM_IDS):
            raise PermissionDenied('Участник не разрешён')
        if user.telegram_id not in attendees or event.visibility != 'shared' or len(attendees) != 2:
            raise ValidationError('Для совместной серии нужно общее событие с участием обоих супругов.')
    zone = ZoneInfo(event.timezone)
    if mode == 'calendar':
        rrule = _checked_rrule(rrule,event.starts_at.astimezone(zone))
        cycle = []
    elif mode == 'cycle':
        if not isinstance(cycle,list) or not 1 <= len(cycle) <= 366 or not any(cycle) or any(type(v) is not bool for v in cycle):
            raise ValidationError('Цикл должен быть списком рабочих/выходных дней, например [true,true,false,false].')
        rrule = ''
    else:
        raise ValidationError('События поддерживают режимы calendar и cycle.')
    # Configuring a shared recurrence does not transfer the original record or
    # its future occurrences to the spouse who requested the change.
    rule = RecurrenceRule.objects.create(owner=event.owner,title=event.title,visibility=event.visibility,domain=event.domain,
                                         kind='event',mode=mode,dtstart=event.starts_at,timezone=event.timezone,
                                         rrule=rrule,cycle=cycle,template=_event_template(event))
    event.recurrence = rule
    event.occurrence_key = occurrence_key(event.starts_at)
    event.version += 1
    event.save(update_fields=['recurrence','occurrence_key','version','updated_at'])
    from .services import reschedule_event
    reschedule_event(event)
    return rule


@transaction.atomic
def create_rule_from_task(user,task,mode='after_completion',interval=1,unit='month',rrule=''):
    from .services import can_edit
    task = Task.objects.select_for_update().get(pk=task.pk)
    if not can_edit(task,user):
        raise PermissionDenied('Нет прав на создание серии этой задачи.')
    if task.recurrence_id and task.recurrence.active:
        raise ValidationError('Задача уже относится к серии.')
    if mode not in ['after_completion','calendar']:
        raise ValidationError('Для задач доступны calendar и after_completion.')
    if unit not in ['day','week','month'] or not 1 <= int(interval) <= 365:
        raise ValidationError('Интервал: 1–365 day/week/month.')
    start = task.due_at or timezone.now()
    if mode=='calendar':
        rrule = _checked_rrule(rrule,start.astimezone(ZoneInfo(task.owner.timezone)))
    rule = RecurrenceRule.objects.create(owner=task.owner,title=task.title,visibility=task.visibility,domain=task.domain,
                                         kind='task',mode=mode,dtstart=start,timezone=task.owner.timezone,
                                         interval=interval,unit=unit,rrule=rrule,template=_task_template(task))
    task.recurrence=rule
    task.occurrence_key=occurrence_key(start) if mode=='calendar' else f'first:{task.pk}'
    task.version+=1
    task.save(update_fields=['recurrence','occurrence_key','version','updated_at'])
    task.reminders.filter(status__in=['pending','queued']).update(object_version=task.version,status='pending')
    if task.status=='done':
        on_task_completed(task)
    return rule


def _occurrences(rule, lower, upper):
    zone = ZoneInfo(rule.timezone)
    anchor = rule.dtstart.astimezone(zone)
    if rule.until:
        upper=min(upper,rule.until)
    if upper<lower:
        return
    if rule.mode=='calendar':
        value = _checked_rrule(rule.rrule, anchor)
        iterator=rrulestr(value,dtstart=anchor).xafter(lower.astimezone(zone),inc=True)
        for moment in islice(iterator,MAX_OCCURRENCES_PER_RULE):
            if not datetime_exists(moment):
                moment=resolve_imaginary(moment)
            if moment.astimezone(UTC)>upper:
                break
            yield moment.astimezone(UTC)
    elif rule.mode=='cycle':
        pattern=rule.cycle
        if not isinstance(pattern,list) or not pattern or not any(pattern):
            return
        cursor=max(anchor.date(),lower.astimezone(zone).date())
        last=upper.astimezone(zone).date()
        for _ in range(MAX_OCCURRENCES_PER_RULE):
            if cursor>last:
                break
            ordinal=(cursor-anchor.date()).days
            if pattern[ordinal % len(pattern)]:
                moment=datetime.combine(cursor,anchor.timetz().replace(tzinfo=None),zone)
                if not datetime_exists(moment):
                    moment=resolve_imaginary(moment)
                as_utc=moment.astimezone(UTC)
                if lower<=as_utc<=upper:
                    yield as_utc
            cursor+=timedelta(days=1)


def _new_reminders(obj, template):
    moment=obj.starts_at if isinstance(obj,Event) else obj.due_at
    if moment is None:
        return
    templates=template.get('reminders',[])
    if not templates:
        if isinstance(obj, Event):
            if 'reminders' in template:
                return  # An explicitly empty event schedule also applies to each occurrence.
            offset = event_reminder_minutes(obj)
            recipients = obj.participants.values_list('pk', flat=True)
            templates = [{'recipient_id': str(pk), 'offset_minutes': offset} for pk in recipients]
        else:
            if template.get('reminders_disabled'):
                return
            templates = [{'recipient_id': str(obj.owner_id), 'offset_minutes': 15}]
    for data in templates:
        recipient=Person.objects.filter(pk=data.get('recipient_id')).first()
        if recipient is None:
            continue
        offset=max(0,min(int(data.get('offset_minutes',15)),525600))
        Reminder.objects.create(**{'event' if isinstance(obj,Event) else 'task':obj},recipient=recipient,
                                due_at=event_reminder_due(obj,offset,data.get('kind','reminder')) if isinstance(obj,Event) else task_reminder_due(obj,offset,data.get('kind','reminder')),offset_minutes=offset,
                                object_version=obj.version,kind=data.get('kind','reminder'),status='pending')


def _clone_task(rule,moment,key,previous_task=None):
    template=rule.template
    defaults={f:template[f] for f in ['title','description','domain','visibility','priority','estimated_minutes','remaining_minutes','allow_split','category','assignee_id','project_id','trip_id','parent_id'] if f in template}
    defaults.update(owner=rule.owner, title=template.get('title',rule.title),assignee_id=template.get('assignee_id',str(rule.owner_id)),
                    recurrence=rule,occurrence_key=key,due_at=moment,status='todo')
    if previous_task:
        defaults['previous_task']=previous_task
    if template.get('not_before_offset_seconds') is not None:
        defaults['not_before'] = moment-timedelta(seconds=int(template['not_before_offset_seconds']))
    task=Task.objects.create(**defaults)
    task.participants.set(Person.objects.filter(pk__in=template.get('participants',[])))
    task.dependencies.set(Task.objects.filter(pk__in=template.get('dependencies',[])))
    _new_reminders(task,template)
    return task


def _clone_event(rule,moment,key,override):
    template=rule.template
    values={f:template[f] for f in ['title','description','domain','visibility','location','kind','flexible','buffer_before','buffer_after','timezone','end_timezone','packing_notes','project_id','task_id','trip_id','place_id','place_snapshot','blocks_time','counts_toward_load'] if f in template}
    zone=ZoneInfo(rule.timezone)
    values.update(owner=rule.owner,title=template.get('title',rule.title),recurrence=rule,occurrence_key=key,
                  starts_at=moment,ends_at=moment+timedelta(minutes=max(1,int(template.get('duration_minutes',60)))),
                  timezone=rule.timezone,status='confirmed')
    if template.get('all_day'):
        local_day=moment.astimezone(zone).date()
        end_day=local_day+timedelta(days=max(1,int(template.get('duration_days',1))))
        values.update(all_day=True,start_date=local_day,end_date=end_day,
                      starts_at=datetime.combine(local_day,datetime.min.time(),zone).astimezone(UTC),
                      ends_at=datetime.combine(end_day,datetime.min.time(),zone).astimezone(UTC))
    if override:
        for name,value in override.changes.items():
            if name in {'starts_at','ends_at'}:
                values[name]=_datetime(value)
            elif name in {'start_date','end_date'}:
                values[name]=date.fromisoformat(value) if value is not None else None
            elif name in {'title','description','location','status','visibility','flexible','meeting_url','buffer_before','buffer_after','place_id','place_snapshot','all_day','blocks_time','timezone','end_timezone'}:
                values[name]=value
    if values['ends_at']<=values['starts_at']:
        raise ValidationError('Некорректная длительность повторения.')
    event=Event.objects.create(**values)
    participant_ids = override.changes.get('participants', template.get('participants', [])) if override else template.get('participants', [])
    participants=list(Person.objects.filter(pk__in=participant_ids, telegram_id__in=settings.ALLOWED_TELEGRAM_IDS)) or [rule.owner]
    event.participants.set(participants)
    for data in template.get('checklists',[]):
        checklist=Checklist.objects.create(event=event,title=data.get('title','Что взять'),source_text=data.get('source_text',''))
        for index,item in enumerate(data.get('items',[])):
            ChecklistItem.objects.create(checklist=checklist,text=str(item['text'])[:300],quantity=str(item.get('quantity',''))[:50],
                                         assignee=Person.objects.filter(pk=item.get('assignee_id')).first() if item.get('assignee_id') else None,
                                         position=item.get('position',index))
    _new_reminders(event,template)
    from .services import reschedule_event
    reschedule_event(event)
    return event


def materialize_recurrences(now=None):
    """Expand only a finite forward horizon; each rule is locked independently."""
    now=now or timezone.now()
    if timezone.is_naive(now):
        raise ValidationError('now должен содержать timezone.')
    lower=now-timedelta(days=1)
    upper=now+timedelta(days=HORIZON_DAYS)
    count=0
    for rule_id in RecurrenceRule.objects.filter(active=True).exclude(mode='after_completion').values_list('pk',flat=True):
        with transaction.atomic():
            rule=RecurrenceRule.objects.select_for_update().select_related('owner').get(pk=rule_id)
            if not rule.active:
                continue
            participants = rule.template.get('participants', [])
            if rule.kind == 'event' and len(participants) > 1:
                allowed = Person.objects.filter(pk__in=participants, telegram_id__in=settings.ALLOWED_TELEGRAM_IDS)
                if allowed.count() != len(participants):
                    continue
            for moment in _occurrences(rule,lower,upper):
                key=occurrence_key(moment)
                model=Event if rule.kind=='event' else Task
                if model.objects.filter(recurrence=rule,occurrence_key=key).exists():
                    continue
                override=OccurrenceOverride.objects.filter(rule=rule,occurrence_key=key).first()
                if override and override.action=='cancel':
                    continue
                if rule.kind=='event':
                    _clone_event(rule,moment,key,override)
                elif rule.kind=='task':
                    _clone_task(rule,moment,key)
                else:
                    raise ValidationError('Неизвестный тип серии.')
                count+=1
    return count


@transaction.atomic
def on_task_completed(task):
    """Idempotently create one successor from actual completion, not old due date."""
    if task.status!='done' or not task.completed_at or not task.recurrence_id:
        return None
    rule=RecurrenceRule.objects.select_for_update().get(pk=task.recurrence_id)
    if not rule.active or rule.mode!='after_completion':
        return None
    if rule.unit not in ['day','week','month'] or rule.interval<1:
        raise ValidationError('Некорректное правило повторения после выполнения.')
    local=task.completed_at.astimezone(ZoneInfo(rule.timezone))
    future=local+relativedelta(**{rule.unit+'s':rule.interval})
    if not datetime_exists(future):
        future=resolve_imaginary(future)
    moment=future.astimezone(UTC)
    if rule.until and moment>rule.until:
        return None
    existing=Task.objects.select_for_update().filter(previous_task=task).first()
    if existing:
        if existing.status=='cancelled':
            old_version = existing.version
            if existing.not_before and existing.due_at:
                existing.not_before += moment-existing.due_at
            existing.status='todo';existing.due_at=moment;existing.completed_at=None
            existing.remaining_minutes=existing.estimated_minutes;existing.version+=1
            existing.save()
            for reminder in existing.reminders.exclude(status='disabled'):
                reminder.due_at=task_reminder_due(existing,reminder.offset_minutes,reminder.kind)
                reminder.status='pending';reminder.object_version=existing.version;reminder.save()
            from .services import _audit
            _audit(task.owner, existing, 'restore_successor', {'version': old_version})
        return existing
    return _clone_task(rule,moment,f'after:{task.pk}',previous_task=task)
