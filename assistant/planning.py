"""Deterministic day planning. Persistent drafts never reserve calendar time."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from dateutil.tz import datetime_exists, resolve_imaginary
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import DailyPlan, Event, Person, Task

UTC = dt_timezone.utc


def _day(value):
    return date.fromisoformat(value) if isinstance(value, str) else value


def _clock(value, default):
    try:
        return time.fromisoformat(str(value))
    except (TypeError, ValueError):
        return time.fromisoformat(default)


def _at(day, clock, zone):
    local = datetime.combine(day, clock, zone)
    if not datetime_exists(local):
        local = resolve_imaginary(local)
    return local.astimezone(UTC)


def _minutes(start, end):
    return max(0, int((end - start).total_seconds() // 60))


def _merge(intervals):
    result = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def _subtract(windows, busy):
    result = list(windows)
    for bstart, bend in _merge(busy):
        next_result = []
        for start, end in result:
            if bend <= start or bstart >= end:
                next_result.append((start, end))
            else:
                if bstart > start:
                    next_result.append((start, min(bstart, end)))
                if bend < end:
                    next_result.append((max(bend, start), end))
        result = next_result
    return result


def _intersection(left, right):
    return _merge((max(a, c), min(b, d)) for a, b in left for c, d in right if max(a, c) < min(b, d))


def attending_events(person, start, end):
    """Ownership is a fallback only for legacy events without participant rows."""
    return Event.objects.filter(status='confirmed', starts_at__lt=end, ends_at__gt=start).filter(
        Q(participants=person) | Q(owner=person, participants__isnull=True)
    ).distinct()


def _base_windows(person, day, domain='personal'):
    zone = ZoneInfo(person.timezone)
    profile = person.profile or {}
    begin = _at(day, time.min, zone)
    end = _at(day + timedelta(days=1), time.min, zone)
    sleep_start = _clock(profile.get('sleep_start'), '23:00')
    sleep_end = _clock(profile.get('sleep_end'), '07:00')
    sleeps = []
    if sleep_start > sleep_end:
        sleeps = [(begin, _at(day, sleep_end, zone)), (_at(day, sleep_start, zone), end)]
    elif sleep_start < sleep_end:
        sleeps = [(_at(day, sleep_start, zone), _at(day, sleep_end, zone))]
    windows = _subtract([(begin, end)], sleeps)
    if domain == 'work':
        work_start = _clock(profile.get('work_start'), '09:00')
        work_end = _clock(profile.get('work_end'), '18:00')
        work_days = profile.get('work_days', [0, 1, 2, 3, 4])
        work = []
        for actual_day in (day - timedelta(days=1), day):
            if actual_day.weekday() not in work_days or work_start == work_end:
                continue
            work.append((_at(actual_day, work_start, zone), _at(actual_day + timedelta(days=int(work_end < work_start)), work_end, zone)))
        windows = _intersection(windows, work)
    return windows


def _busy(person, start, end):
    # Read only blocking times for other people's private events; never copy details.
    events = list(attending_events(person, start - timedelta(days=1), end + timedelta(days=1)))
    intervals = [(e.starts_at - timedelta(minutes=e.buffer_before), e.ends_at + timedelta(minutes=e.buffer_after)) for e in events]
    return intervals, events


def _available(person, day, domain, planned, now):
    zone = ZoneInfo(person.timezone)
    lower = _at(day, time.min, zone)
    upper = _at(day + timedelta(days=1), time.min, zone)
    busy, _ = _busy(person, lower, upper)
    windows = _subtract(_base_windows(person, day, domain), busy + planned.get(str(person.pk), []))
    # Never make a new proposal in the past.
    return [(max(s, now), e) for s, e in windows if e > max(s, now)]


def _participants(task):
    persons = {str(p.pk): p for p in task.participants.all()}
    persons[str(task.assignee_id)] = task.assignee
    return list(persons.values())


def _daily_load(person, day):
    zone = ZoneInfo(person.timezone)
    begin, end = _at(day, time.min, zone), _at(day + timedelta(days=1), time.min, zone)
    return sum(_minutes(max(e.starts_at, begin), min(e.ends_at, end))
               for e in attending_events(person, begin, end))


def _split_local_days(windows, actors):
    """Keep every proposed block inside one local day for each participant."""
    result = []
    for start, end in windows:
        points = {start, end}
        for actor in actors:
            zone = ZoneInfo(actor.timezone)
            day = start.astimezone(zone).date() + timedelta(days=1)
            boundary = _at(day, time.min, zone)
            while boundary < end:
                if boundary > start:
                    points.add(boundary)
                day += timedelta(days=1)
                boundary = _at(day, time.min, zone)
        ordered = sorted(points)
        result.extend(zip(ordered, ordered[1:]))
    return result


def _event_item(event, person):
    from .services import can_view
    details = can_view(event, person)
    # Even for a corrupt relation with a private participant, redact defensively.
    return {
        'kind': 'event', 'event_id': str(event.pk), 'version': event.version,
        'title': event.title if details else 'Занят',
        'starts_at': event.starts_at.isoformat(), 'ends_at': event.ends_at.isoformat(),
        'state': 'confirmed', 'location': event.location if details else '',
        'meeting_url': event.meeting_url if details else '',
        'permission_mode': 'busy' if not details and event.visibility=='busy_only' else 'full',
    }


@transaction.atomic
def build_daily_plan(person, day):
    """Return a stored, reproducible proposal; a rebuild invalidates old acceptance."""
    from .services import can_view
    day = _day(day)
    zone = ZoneInfo(person.timezone)
    begin = _at(day, time.min, zone)
    end = _at(day + timedelta(days=1), time.min, zone)
    now = timezone.now()
    items = [_event_item(e, person) for e in attending_events(person, begin, end).order_by('starts_at')]
    confirmed = list(attending_events(person, begin, end))
    for previous, following in zip(sorted(confirmed, key=lambda e:e.starts_at), sorted(confirmed, key=lambda e:e.starts_at)[1:]):
        if previous.ends_at + timedelta(minutes=previous.buffer_after + following.buffer_before) > following.starts_at:
            items.append({'kind':'problem', 'title':'Пересечение событий или недостаточный запас времени', 'state':'problem'})

    candidates = list(Task.objects.filter(status__in=['todo', 'in_progress']).filter(
        Q(assignee=person) | Q(participants=person)
    ).select_related('assignee').prefetch_related('participants', 'dependencies').distinct())
    candidates = [t for t in candidates if can_view(t, person)]
    candidates.sort(key=lambda t:(t.due_at is None, t.due_at or datetime.max.replace(tzinfo=UTC), t.priority, t.created_at))
    planned = {}
    load = {}
    finishes = {}
    unscheduled = list(candidates)
    while unscheduled:
        progress = False
        for task in list(unscheduled):
            dependencies = list(task.dependencies.all())
            if any(d.status != 'done' and str(d.pk) not in finishes for d in dependencies):
                continue
            actor_list = _participants(task)
            # Shared visibility does not make the other household member a participant.
            windows = [(begin, end)]
            for actor in actor_list:
                # The requested calendar day belongs to the plan recipient. Intersect the
                # same UTC span against all local dates touched for every participant.
                actor_zone = ZoneInfo(actor.timezone)
                start_day = begin.astimezone(actor_zone).date()
                end_day = (end - timedelta(microseconds=1)).astimezone(actor_zone).date()
                actor_windows = []
                cursor = start_day
                while cursor <= end_day:
                    actor_windows += _available(actor, cursor, task.domain, planned, now)
                    cursor += timedelta(days=1)
                windows = _intersection(windows, actor_windows)
            dependency_end = max((finishes[str(d.pk)] for d in dependencies if d.status != 'done'), default=begin)
            windows = [(max(s, dependency_end), e) for s, e in windows if e > max(s, dependency_end)]
            if task.due_at:
                windows = [(s, min(e, task.due_at)) for s, e in windows if min(e, task.due_at) > s]
            reservations = list(Event.objects.filter(task=task, status__in=['confirmed','proposed'], ends_at__gt=now))
            booked = sum(_minutes(e.starts_at, e.ends_at) for e in reservations)
            awaiting_approval = any(e.status=='proposed' for e in reservations)
            if awaiting_approval:
                items.append({'kind':'unscheduled','task_id':str(task.pk),'title':task.title,
                              'state':'problem','reason':'Часть времени по задаче ждёт согласования участника'})
            needed = max(0, task.remaining_minutes - booked)
            if needed == 0:
                # A fully scheduled prerequisite still blocks dependents until its last slot.
                if reservations and not awaiting_approval:
                    finishes[str(task.pk)] = max(e.ends_at for e in reservations)
                unscheduled.remove(task)
                progress = True
                continue
            minimum = max(int((a.profile or {}).get('min_block_minutes', 25)) for a in actor_list)
            breaks = max(int((a.profile or {}).get('break_minutes', 10)) for a in actor_list)
            slots = []
            left = needed
            for start, stop in _split_local_days(windows, actor_list):
                if slots:
                    start = max(start, slots[-1][1] + timedelta(minutes=breaks))
                    if start >= stop:
                        continue
                keys = [(str(a.pk), start.astimezone(ZoneInfo(a.timezone)).date()) for a in actor_list]
                for actor, key in zip(actor_list, keys):
                    if key not in load:
                        load[key] = _daily_load(actor, key[1])
                cap = min(max(0, int((a.profile or {}).get('max_daily_minutes', 480)) - load[key])
                          for a, key in zip(actor_list, keys))
                capacity = min(_minutes(start, stop), cap)
                if capacity <= 0:
                    continue
                if not task.allow_split and capacity < left:
                    continue
                amount = min(left, capacity)
                if amount < min(minimum, left):
                    continue
                slots.append((start, start + timedelta(minutes=amount)))
                for key in keys:
                    load[key] += amount
                left -= amount
                if left == 0 or not task.allow_split:
                    break
            if not slots:
                reason = 'Недостаточно свободного времени или превышен дневной лимит'
                if task.due_at and task.due_at <= now:
                    reason = 'Дедлайн уже прошёл; нужен новый срок'
                items.append({'kind':'unscheduled','task_id':str(task.pk),'title':task.title,'state':'problem','reason':reason})
            else:
                for start, stop in slots:
                    items.append({'kind':'task','task_id':str(task.pk),'task_version':task.version,'title':task.title,
                                  'starts_at':start.isoformat(),'ends_at':stop.isoformat(),'state':'proposed',
                                  'participants':[str(a.pk) for a in actor_list], 'domain':task.domain,
                                  'visibility':task.visibility})
                    for actor in actor_list:
                        key = str(actor.pk)
                        planned.setdefault(key, []).append((start, stop + timedelta(minutes=breaks)))
                if left:
                    items.append({'kind':'unscheduled','task_id':str(task.pk),'title':task.title,'state':'problem','reason':f'Осталось разместить {left} мин.'})
                elif not awaiting_approval:
                    finishes[str(task.pk)] = max([slots[-1][1]] + [e.ends_at for e in reservations])
            unscheduled.remove(task)
            progress = True
        if not progress:
            for task in unscheduled:
                items.append({'kind':'unscheduled','task_id':str(task.pk),'title':task.title,'state':'problem','reason':'Предварительные задачи ещё не выполнены или не помещаются в план'})
            break
    items.sort(key=lambda i:(i.get('starts_at', '9999'), i.get('title', '')))
    plan, created = DailyPlan.objects.get_or_create(person=person, day=day)
    if not created:
        plan.version += 1
    plan.items = items
    plan.status = 'draft'
    plan.save()
    return plan


def daily_plan_message(person, day):
    """Rebuild on every render to avoid leaking revoked access from an old snapshot."""
    from .services import can_view
    plan = build_daily_plan(person, day)
    zone = ZoneInfo(person.timezone)
    lines = [f'План на {plan.day:%d.%m.%Y} ({person.timezone})']
    confirmed = [i for i in plan.items if i['kind']=='event']
    proposed = [i for i in plan.items if i['kind']=='task']
    problems = [i for i in plan.items if i['kind'] in ['problem','unscheduled']]
    refs = {}
    def source(kind, identifier, mode=None):
        ref = {'kind':kind, 'id':str(identifier)}
        if mode == 'busy':
            ref['mode'] = mode
        key = (kind,str(identifier))
        if key not in refs or 'mode' not in ref:
            refs[key] = ref
    for item in confirmed:
        source('event', item['event_id'], item.get('permission_mode'))
    for item in proposed + problems:
        if item.get('task_id'):
            source('task', item['task_id'])
    for heading, group in [('События:',confirmed),('Предлагаемые дела:',proposed)]:
        lines.append('\n'+heading)
        if not group:
            lines.append('— Нет')
        for item in group:
            start = datetime.fromisoformat(item['starts_at']).astimezone(zone)
            end = datetime.fromisoformat(item['ends_at']).astimezone(zone)
            text = f'• {start:%H:%M}–{end:%H:%M} {item["title"]}'
            if item.get('location'):
                text += f' · {item["location"]}'
            lines.append(text)
            if item.get('meeting_url'):
                lines.append(item['meeting_url'])
    packing = []
    trips = {}
    for event in attending_events(person, _at(plan.day,time.min,zone), _at(plan.day+timedelta(days=1),time.min,zone)):
        if not can_view(event, person):
            continue
        checklists = list(event.checklists.all())
        if event.trip_id and can_view(event.trip, person):
            checklists += list(event.trip.checklists.all())
            trips[event.trip_id] = event.trip
        for checklist in checklists:
            for entry in checklist.items.filter(checked_at__isnull=True):
                if entry.assignee_id in [None, person.pk]:
                    source('checklist',checklist.pk)
                    if checklist.trip_id:
                        source('trip',checklist.trip_id)
                    label = entry.text + (f' — {entry.quantity}' if entry.quantity else '')
                    if label not in packing:
                        packing.append(label)
    if packing:
        lines.append('\nВзять с собой:')
        lines += ['☐ '+x for x in packing]
    logistics = []
    for trip in trips.values():
        for transfer in check_trip(trip, user=person):
            following = Event.objects.get(pk=transfer['to_event_id'])
            if following.starts_at.astimezone(zone).date() != plan.day:
                continue
            source('trip',trip.pk)
            source('event',transfer['from_event_id'])
            source('event',transfer['to_event_id'])
            text = (f'{transfer["from"]} → {transfer["to"]}: '
                    f'окно {transfer["available_minutes"]} мин., '
                    f'нужен запас {transfer["required_minutes"]} мин.')
            if transfer['deficit_minutes']:
                text += f' — не хватает {transfer["deficit_minutes"]} мин.'
            logistics.append(text)
    if logistics:
        lines.append('\nЛогистика (заданные вручную длительности):')
        lines += ['• '+text for text in logistics]
    if problems:
        lines.append('\nТребует внимания:')
        lines += ['• '+i['title']+(f': {i["reason"]}' if i.get('reason') else '') for i in problems]
    if proposed:
        lines.append('\nДля принятия блоков: /accept YYYY-MM-DD')
    # Store the sources of this exact rendered snapshot. Re-querying visibility
    # later could silently omit a source whose access was revoked in between.
    permission_refs = list(refs.values())
    plan.items.append({'kind':'permission_refs','refs':permission_refs})
    plan.save(update_fields=['items','updated_at'])
    return {'text': '\n'.join(lines), 'permission_refs': permission_refs}


def render_daily_plan(person, day):
    return daily_plan_message(person, day)['text']


def plan_permission_refs(person, day):
    """ACL references for the latest rendered plan, without rebuilding its draft."""
    plan = DailyPlan.objects.get(person=person, day=_day(day))
    for item in reversed(plan.items):
        if item.get('kind')=='permission_refs':
            return item['refs']
    # A build-only draft has no checklist/logistics text yet.
    refs = []
    for item in plan.items:
        if item.get('event_id'):
            ref = {'kind':'event','id':item['event_id']}
            if item.get('permission_mode')=='busy':
                ref['mode']='busy'
            refs.append(ref)
        if item.get('task_id'):
            refs.append({'kind':'task','id':item['task_id']})
    return refs


@transaction.atomic
def accept_plan(person, day):
    """Validate the stored draft, lock its attendees, and book their task blocks."""
    from .services import can_view, create_event
    day = _day(day)
    plan = DailyPlan.objects.select_for_update().get(person=person, day=day)
    if plan.status == 'accepted':
        return []
    proposals = [i for i in plan.items if i.get('kind')=='task' and i.get('state')=='proposed']
    person_ids = {str(person.pk)}
    for item in proposals:
        person_ids.update(item.get('participants', []))
    # Serialize concurrent acceptance of two people's joint plan in a fixed order.
    list(Person.objects.select_for_update().filter(pk__in=person_ids).order_by('pk'))
    staged = {}
    proposed_minutes = {}
    dependency_finishes = {}
    loads = {}
    for item in proposals:
        task = Task.objects.select_for_update().get(pk=item['task_id'])
        if not can_view(task, person) or task.status not in ['todo','in_progress']:
            raise PermissionDenied('Задача недоступна или уже завершена. Перестройте план.')
        if task.version != item['task_version']:
            raise ValidationError('Задача изменилась. Перестройте план.')
        actors = _participants(task)
        if set(item['participants']) != {str(a.pk) for a in actors}:
            raise ValidationError('Состав участников изменился. Перестройте план.')
        if str(person.pk) not in {str(a.pk) for a in actors}:
            raise PermissionDenied('Вы не участвуете в задаче.')
        start, end = datetime.fromisoformat(item['starts_at']), datetime.fromisoformat(item['ends_at'])
        if start < timezone.now():
            raise ValidationError('Предложенное время уже прошло. Перестройте план.')
        if task.due_at and end > task.due_at:
            raise ValidationError('Дедлайн изменился. Перестройте план.')
        for dependency in task.dependencies.exclude(status='done'):
            finish = dependency_finishes.get(str(dependency.pk))
            if finish is None:
                scheduled = list(Event.objects.filter(task=dependency, status='confirmed', ends_at__gt=timezone.now()))
                if sum(_minutes(e.starts_at,e.ends_at) for e in scheduled) >= dependency.remaining_minutes and scheduled:
                    finish = max(e.ends_at for e in scheduled)
            if finish is None or finish > start:
                raise ValidationError('Изменились предварительные задачи. Перестройте план.')
        task_key = str(task.pk)
        reservations = list(Event.objects.filter(task=task,status__in=['confirmed','proposed'],ends_at__gt=timezone.now()))
        booked = sum(_minutes(e.starts_at,e.ends_at) for e in reservations)
        proposed_minutes[task_key] = proposed_minutes.get(task_key,0) + _minutes(start,end)
        if booked + proposed_minutes[task_key] > task.remaining_minutes:
            raise ValidationError('Задача уже размещена в расписании. Перестройте план.')
        if booked + proposed_minutes[task_key] == task.remaining_minutes and all(e.status=='confirmed' for e in reservations):
            dependency_finishes[task_key] = max([end] + [e.ends_at for e in reservations])
        breaks = max(int((a.profile or {}).get('break_minutes',10)) for a in actors)
        for actor in actors:
            local_day = start.astimezone(ZoneInfo(actor.timezone)).date()
            windows = _available(actor, local_day, task.domain, staged, timezone.now())
            if not any(s <= start and end <= e for s,e in windows):
                raise ValidationError('Расписание или ограничения изменились. Перестройте план.')
            load_key = (str(actor.pk),local_day)
            if load_key not in loads:
                loads[load_key] = _daily_load(actor,local_day)
            loads[load_key] += _minutes(start,end)
            if loads[load_key] > int((actor.profile or {}).get('max_daily_minutes',480)):
                raise ValidationError('Изменился дневной лимит нагрузки. Перестройте план.')
            staged.setdefault(str(actor.pk),[]).append((start,end+timedelta(minutes=breaks)))
    result = []
    for item in proposals:
        task = Task.objects.get(pk=item['task_id'])
        event = create_event(person, title=task.title, task_id=str(task.pk), project_id=str(task.project_id) if task.project_id else None,
                             trip_id=str(task.trip_id) if task.trip_id else None, starts_at=item['starts_at'], ends_at=item['ends_at'],
                             timezone=person.timezone, end_timezone=person.timezone, domain=task.domain,
                             visibility=task.visibility, participants=item['participants'], flexible=True, kind='task',
                             buffer_after=max(int((a.profile or {}).get('break_minutes',10)) for a in _participants(task)))
        item['state'] = event.status
        item['event_id'] = str(event.pk)
        result.append(event)
    plan.status = 'accepted'
    plan.version += 1
    plan.save(update_fields=['items','status','version','updated_at'])
    return result


def check_trip(trip, user=None, shared_context=False):
    """Feasibility based on explicit manual segment buffers, with no invented routes."""
    from .services import can_view
    def readable(event):
        return can_view(event,user,shared_context=shared_context) if user else event.visibility=='shared'
    segments = list(trip.segments.select_related('event').filter(event__status='confirmed').order_by('event__starts_at','position'))
    results = []
    for left,right in zip(segments,segments[1:]):
        # A shared container never grants access to a private child. Do not
        # connect across a hidden intermediate leg and invent a feasible route.
        if not readable(left.event) or not readable(right.event):
            continue
        available = int((right.event.starts_at-left.event.ends_at).total_seconds()//60)
        required = left.baggage_minutes + right.transfer_minutes + right.checkin_minutes
        results.append({'from_id':str(left.pk),'to_id':str(right.pk),'from_event_id':str(left.event_id),'to_event_id':str(right.event_id),
                        'from':left.event.title,'to':right.event.title,
                        'available_minutes':available,'required_minutes':required,
                        'deficit_minutes':max(0,required-available),'status':'conflict' if available < required else 'feasible',
                        'source':'manual','note':'Проверка по заданным запасам; дорожный трафик и задержки не проверены.'})
    return results
