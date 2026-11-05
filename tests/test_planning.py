from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError

from assistant.models import ActionRequest, Checklist, ChecklistItem, Event, Task, Trip, TripSegment
from assistant.planning import accept_plan, build_daily_plan, check_trip, plan_permission_refs, render_daily_plan

pytestmark = pytest.mark.django_db
UTC = timezone.utc
DAY = date(2030, 1, 7)  # Monday


def moment(hour, minute=0, day=DAY, zone='UTC'):
    return datetime.combine(day, datetime.min.time(), ZoneInfo(zone)).replace(hour=hour, minute=minute)


@pytest.fixture
def users(people, monkeypatch):
    from assistant import planning
    monkeypatch.setattr(planning.timezone, 'now', lambda: moment(0, day=DAY-timedelta(days=1)))
    for p in people:
        p.timezone='UTC'; p.save()
    return people


def task(person, title='Task', **kwargs):
    obj=Task.objects.create(owner=person, assignee=person, title=title, **kwargs)
    obj.participants.add(person)
    return obj


def event(person, start, end, title='Event', **kwargs):
    obj=Event.objects.create(owner=person, title=title, starts_at=start, ends_at=end, timezone=person.timezone, **kwargs)
    obj.participants.add(person)
    return obj


def proposals(plan):
    return [i for i in plan.items if i['kind']=='task']


def test_shared_visibility_alone_does_not_make_attendance(users):
    a,b=users
    event(a,moment(7),moment(22),visibility='shared')
    task(b)
    plan=build_daily_plan(b,DAY)
    assert not [i for i in plan.items if i['kind']=='event']
    assert datetime.fromisoformat(proposals(plan)[0]['starts_at'])==moment(7)


def test_joint_task_respects_private_calendar_without_leaking_title(users):
    a,b=users
    event(b,moment(7),moment(10),title='Confidential medical visit')
    t=task(a,visibility='shared');t.participants.add(b)
    plan=build_daily_plan(a,DAY)
    assert datetime.fromisoformat(proposals(plan)[0]['starts_at'])==moment(10)
    assert 'Confidential' not in str(plan.items)
    assert 'Confidential' not in render_daily_plan(a,DAY)


def test_work_sleep_buffers_and_daily_cap(users):
    a,_=users
    a.profile={'work_start':'10:00','work_end':'13:00','max_daily_minutes':120};a.save()
    event(a,moment(10),moment(11),buffer_after=30)
    task(a,domain='work',estimated_minutes=90,remaining_minutes=90,allow_split=True)
    blocks=proposals(build_daily_plan(a,DAY))
    assert len(blocks)==1
    assert datetime.fromisoformat(blocks[0]['starts_at'])==moment(11,30)
    assert datetime.fromisoformat(blocks[0]['ends_at'])==moment(12,30)


def test_priority_dependencies_and_breaks(users):
    a,_=users
    first=task(a,'First',priority=5)
    after=task(a,'After',priority=1);after.dependencies.add(first)
    blocks=proposals(build_daily_plan(a,DAY))
    assert [i['title'] for i in blocks]==['First','After']
    assert datetime.fromisoformat(blocks[1]['starts_at'])==moment(8,10)
    result=accept_plan(a,DAY)
    assert len(result)==2
    assert all(e.status=='confirmed' for e in result)
    assert accept_plan(a,DAY)==[]


def test_pending_and_completed_dependencies_are_not_silently_ignored(users):
    a,b=users
    upstream=task(b,'Private prerequisite')
    downstream=task(a,'Cannot do yet');downstream.dependencies.add(upstream)
    plan=build_daily_plan(a,DAY)
    assert proposals(plan)==[]
    assert 'Private prerequisite' not in str(plan.items)
    upstream.status='done';upstream.save()
    assert len(proposals(build_daily_plan(a,DAY)))==1


def test_draft_does_not_reserve_and_accept_rechecks_conflicts(users):
    a,_=users
    task(a)
    build_daily_plan(a,DAY)
    assert Event.objects.count()==0
    event(a,moment(7),moment(8))
    with pytest.raises(ValidationError,match='Расписание'):
        accept_plan(a,DAY)
    assert Event.objects.count()==1


def test_accept_rechecks_changed_limit_and_task_version(users):
    a,_=users
    t=task(a)
    build_daily_plan(a,DAY)
    a.profile={'max_daily_minutes':30};a.save()
    with pytest.raises(ValidationError,match='лимит'):
        accept_plan(a,DAY)
    a.profile={};a.save()
    t.version+=1;t.save()
    with pytest.raises(ValidationError,match='Задача изменилась'):
        accept_plan(a,DAY)
    assert not Event.objects.exists()


@pytest.mark.parametrize('accepting_participant', [0, 1])
def test_either_participant_can_accept_a_joint_task_block(users, accepting_participant):
    a,b=users
    t=task(a,visibility='shared');t.participants.add(b)
    build_daily_plan(a,DAY)
    build_daily_plan(b,DAY)
    result=accept_plan(users[accepting_participant],DAY)
    assert len(result)==1 and result[0].status=='confirmed'
    assert not ActionRequest.objects.filter(target_id=result[0].pk).exists()
    assert set(result[0].reminders.values_list('recipient_id','status'))=={(a.pk,'pending'),(b.pk,'pending')}
    with pytest.raises(ValidationError, match='размещена'):
        accept_plan(users[1-accepting_participant],DAY)
    assert proposals(build_daily_plan(a,DAY))==[]
    assert proposals(build_daily_plan(b,DAY))==[]


def test_accept_rechecks_dependency_that_was_reopened(users):
    a,_=users
    prerequisite=task(a,'Prerequisite',status='done')
    downstream=task(a,'Downstream');downstream.dependencies.add(prerequisite)
    build_daily_plan(a,DAY)
    prerequisite.status='todo';prerequisite.save()
    with pytest.raises(ValidationError,match='предварительные'):
        accept_plan(a,DAY)


def test_dependency_finishes_only_after_its_last_existing_block(users):
    a,_=users
    first=task(a,'Prerequisite',estimated_minutes=120,remaining_minutes=120)
    task_event=event(a,moment(10,day=DAY+timedelta(days=1)),moment(11,day=DAY+timedelta(days=1)),task=first)
    later=task(a,'Dependent');later.dependencies.add(first)
    blocks=proposals(build_daily_plan(a,DAY))
    assert [i['title'] for i in blocks]==['Prerequisite']
    task_event.status='proposed';task_event.save()
    assert [i['title'] for i in proposals(build_daily_plan(a,DAY))]==['Prerequisite']


def test_day_limits_follow_each_participants_timezone(users):
    a,b=users
    b.timezone='Pacific/Honolulu';b.profile={'max_daily_minutes':60};b.save()
    # Jan 7 UTC morning is Jan 6 Hawaii evening, whose daily load is already full.
    event(b,moment(1),moment(2))
    t=task(a,visibility='shared');t.participants.add(b)
    blocks=proposals(build_daily_plan(a,DAY))
    assert datetime.fromisoformat(blocks[0]['starts_at'])==moment(17)


def test_restricted_event_details_and_personal_packing(users):
    a,b=users
    e=event(a,moment(12),moment(13),title='Secret',visibility='busy_only',location='Hidden')
    e.participants.add(b)
    checklist=Checklist.objects.create(event=e)
    ChecklistItem.objects.create(checklist=checklist,text='Secret document')
    text=render_daily_plan(b,DAY)
    assert 'Занят' in text and 'Secret' not in text and 'Hidden' not in text
    own=event(b,moment(14),moment(15))
    packing=Checklist.objects.create(event=own)
    ChecklistItem.objects.create(checklist=packing,text='Паспорт',assignee=b)
    ChecklistItem.objects.create(checklist=packing,text='Камера',assignee=a)
    text=render_daily_plan(b,DAY)
    assert '☐ Паспорт' in text and 'Камера' not in text


def test_trip_checks_explicit_transfer_and_baggage(users):
    a,_=users
    trip=Trip.objects.create(owner=a,title='Trip')
    left=event(a,moment(8),moment(10),title='Flight',trip=trip)
    right=event(a,moment(11),moment(12),title='Train',trip=trip)
    TripSegment.objects.create(trip=trip,event=left,baggage_minutes=30)
    TripSegment.objects.create(trip=trip,event=right,transfer_minutes=40,checkin_minutes=10)
    check=check_trip(trip,user=a)[0]
    assert check['available_minutes']==60 and check['required_minutes']==80
    assert check['deficit_minutes']==20 and check['source']=='manual'


def test_shared_trip_does_not_expose_private_children(users):
    a,b=users
    trip=Trip.objects.create(owner=a,title='Shared trip',visibility='shared')
    left=event(a,moment(8),moment(10),title='Secret itinerary',trip=trip)
    right=event(a,moment(11),moment(12),title='Shared itinerary',trip=trip,visibility='shared')
    TripSegment.objects.create(trip=trip,event=left)
    TripSegment.objects.create(trip=trip,event=right)
    assert check_trip(trip,user=b)==[]
    assert check_trip(trip)==[]
    assert check_trip(trip,user=b,shared_context=True)==[]
    own = check_trip(trip,user=a,shared_context=True)
    assert len(own) == 1
    assert own[0]['from_event_id'] == str(left.pk)
    assert own[0]['to_event_id'] == str(right.pk)
    assert check_trip(trip,user=a)[0]['from']=='Secret itinerary'


def test_rendered_plan_keeps_exact_acl_sources_after_visibility_change(users):
    a,b=users
    trip=Trip.objects.create(owner=a,title='Route',visibility='shared')
    previous=event(a,moment(22,day=DAY-timedelta(days=1)),moment(23,day=DAY-timedelta(days=1)),trip=trip,visibility='shared')
    current=event(a,moment(9),moment(10),trip=trip,visibility='shared')
    current.participants.add(b)
    TripSegment.objects.create(trip=trip,event=previous)
    TripSegment.objects.create(trip=trip,event=current)
    checklist=Checklist.objects.create(trip=trip)
    ChecklistItem.objects.create(checklist=checklist,text='Passport',assignee=b)
    todo=task(b)
    busy=event(a,moment(13),moment(14),visibility='busy_only');busy.participants.add(b)
    render_daily_plan(b,DAY)
    refs=plan_permission_refs(b,DAY)
    pairs={(r['kind'],r['id']) for r in refs}
    assert {('event',str(previous.pk)),('event',str(current.pk)),('trip',str(trip.pk)),
            ('checklist',str(checklist.pk)),('task',str(todo.pk))} <= pairs
    assert {'kind':'event','id':str(busy.pk),'mode':'busy'} in refs
    current.visibility='private';current.save()
    trip.visibility='private';trip.save()
    assert plan_permission_refs(b,DAY)==refs
