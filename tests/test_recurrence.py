from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError, PermissionDenied

from assistant.models import ActionRequest, Checklist, ChecklistItem, Event, Job, OccurrenceOverride, Reminder, Task
from assistant.services import create_event
from assistant.recurrence import (create_rule_from_event, create_rule_from_task,
                                 materialize_recurrences, occurrence_key, on_task_completed)

pytestmark=pytest.mark.django_db
UTC=timezone.utc


def event(person,start,**kwargs):
    result=Event.objects.create(owner=person,title='Series',starts_at=start,ends_at=start+timedelta(hours=1),timezone=person.timezone,**kwargs)
    result.participants.add(person)
    return result


def test_rrule_preserves_wall_time_across_dst_and_is_idempotent(people):
    a,_=people;a.timezone='Europe/Berlin';a.save()
    start=datetime(2030,3,24,9,tzinfo=ZoneInfo(a.timezone))
    first=create_event(a, title='Series', starts_at=start, ends_at=start+timedelta(hours=1))
    rule=create_rule_from_event(a,first,rrule='FREQ=WEEKLY;COUNT=3')
    assert materialize_recurrences(start)==2
    assert materialize_recurrences(start)==0
    occurrences=list(Event.objects.filter(recurrence=rule).order_by('starts_at'))
    assert [e.starts_at.astimezone(ZoneInfo(a.timezone)).hour for e in occurrences]==[9,9,9]
    assert [e.starts_at.hour for e in occurrences]==[8,7,7]
    assert all(e.reminders.count()==1 for e in occurrences[1:])


def test_occurrence_cancel_move_and_fresh_checklists(people):
    a,_=people
    start=datetime(2030,1,1,9,tzinfo=UTC)
    first=event(a,start)
    checklist=Checklist.objects.create(event=first,title='Packing')
    ChecklistItem.objects.create(checklist=checklist,text='Passport',checked_at=start,checked_by=a)
    rule=create_rule_from_event(a,first,rrule='FREQ=DAILY;COUNT=4')
    OccurrenceOverride.objects.create(rule=rule,occurrence_key=occurrence_key(start+timedelta(days=1)),action='cancel')
    key=occurrence_key(start+timedelta(days=2))
    moved=start+timedelta(days=2,hours=2)
    OccurrenceOverride.objects.create(rule=rule,occurrence_key=key,action='modify',changes={'starts_at':moved.isoformat(),'ends_at':(moved+timedelta(hours=1)).isoformat()})
    assert materialize_recurrences(start)==2
    moved_event=Event.objects.get(recurrence=rule,occurrence_key=key)
    assert moved_event.starts_at==moved
    assert moved_event.checklists.get().items.get().checked_at is None
    assert moved_event.checklists.get().items.get().checked_by is None
    assert first.checklists.get().items.get().checked_at==start
    assert materialize_recurrences(start)==0


def test_cycle_two_on_two_off(people):
    a,_=people
    start=datetime(2030,1,1,9,tzinfo=UTC)
    first=event(a,start)
    rule=create_rule_from_event(a,first,mode='cycle',cycle=[True,True,False,False])
    rule.until=start+timedelta(days=7);rule.save()
    assert materialize_recurrences(start)==3
    assert [e.starts_at.day for e in Event.objects.filter(recurrence=rule).order_by('starts_at')]==[1,2,5,6]


def test_after_completion_uses_actual_date_month_end_and_one_successor(people):
    a,_=people;a.timezone='UTC';a.save()
    original=Task.objects.create(owner=a,assignee=a,title='Filter replacement',due_at=datetime(2030,1,15,10,tzinfo=UTC))
    original.participants.add(a)
    create_rule_from_task(a,original,interval=1,unit='month')
    original.refresh_from_db()
    original.status='done';original.completed_at=datetime(2030,1,31,10,tzinfo=UTC);original.save()
    successor=on_task_completed(original)
    assert successor.due_at==datetime(2030,2,28,10,tzinfo=UTC)
    assert on_task_completed(original).pk==successor.pk
    assert Task.objects.filter(previous_task=original).count()==1
    assert successor.status=='todo' and successor.remaining_minutes==60
    assert successor.reminders.get().due_at==successor.due_at-timedelta(minutes=15)
    successor.status='cancelled';successor.save()
    original.completed_at=datetime(2030,2,2,10,tzinfo=UTC);original.save()
    restored=on_task_completed(original)
    assert restored.pk==successor.pk and restored.due_at==datetime(2030,3,2,10,tzinfo=UTC)
    assert restored.status=='todo'


def test_all_day_occurrences_span_actual_dst_day(people):
    a,_=people;a.timezone='Europe/Berlin';a.save()
    zone=ZoneInfo(a.timezone)
    first=event(a,datetime(2030,3,30,tzinfo=zone),all_day=True,start_date=date(2030,3,30),end_date=date(2030,3,31))
    first.ends_at=datetime(2030,3,31,tzinfo=zone);first.save()
    rule=create_rule_from_event(a,first,rrule='FREQ=DAILY;COUNT=2')
    materialize_recurrences(first.starts_at)
    following=Event.objects.filter(recurrence=rule).order_by('starts_at').last()
    assert following.start_date==date(2030,3,31) and following.end_date==date(2030,4,1)
    assert following.ends_at-following.starts_at==timedelta(hours=23)


def test_calendar_tasks_get_distinct_due_dates_and_defaults(people):
    a,_=people
    start=datetime(2030,1,1,9,tzinfo=UTC)
    original=Task.objects.create(owner=a,assignee=a,title='Monthly task',due_at=start,estimated_minutes=45,remaining_minutes=20)
    original.participants.add(a)
    original_reminder=Reminder.objects.create(task=original,recipient=a,due_at=start-timedelta(minutes=15),status='queued')
    rule=create_rule_from_task(a,original,mode='calendar',rrule='FREQ=MONTHLY;COUNT=3')
    original.refresh_from_db(); original_reminder.refresh_from_db()
    assert original_reminder.status=='pending' and original_reminder.object_version==original.version
    assert materialize_recurrences(start)==2
    assert list(Task.objects.filter(recurrence=rule).order_by('due_at').values_list('remaining_minutes',flat=True))==[20,45,45]
    assert materialize_recurrences(start)==0


@pytest.mark.parametrize('remind_minutes', [[30], []])
def test_joint_event_series_includes_both_in_each_occurrence(people, settings, remind_minutes):
    a,b=people
    start=datetime(2030,1,1,9,tzinfo=UTC)
    first=create_event(a,title='Театр',visibility='shared',participants=[a.pk,b.pk],
                       starts_at=start,ends_at=start+timedelta(hours=1),remind_minutes=remind_minutes)
    rule=create_rule_from_event(a,first,rrule='FREQ=DAILY;COUNT=3')
    settings.ALLOWED_TELEGRAM_IDS=(a.telegram_id,)
    assert materialize_recurrences(start)==0
    settings.ALLOWED_TELEGRAM_IDS=(a.telegram_id,b.telegram_id)
    assert materialize_recurrences(start)==2 and materialize_recurrences(start)==0
    for occurrence in Event.objects.filter(recurrence=rule):
        assert occurrence.status=='confirmed' and set(occurrence.participants.all())=={a,b}
        assert set(occurrence.reminders.values_list('recipient_id','offset_minutes','status')) == {
            (person.pk,offset,'pending') for person in (a,b) for offset in remind_minutes
        }
    assert not ActionRequest.objects.exists()
    assert Job.objects.filter(kind='telegram_send').count()==1


def test_joint_event_series_cannot_schedule_only_for_other_or_revoked_user(people, settings):
    a,b=people
    first=event(a,datetime(2030,1,1,9,tzinfo=UTC),visibility='shared')
    first.participants.set([b])
    with pytest.raises(ValidationError,match='с участием обоих'):
        create_rule_from_event(a,first,rrule='FREQ=DAILY')
    first.participants.add(a)
    settings.ALLOWED_TELEGRAM_IDS = (a.telegram_id,)
    with pytest.raises(PermissionDenied,match='не разрешён'):
        create_rule_from_event(a,first,rrule='FREQ=DAILY')
    first.refresh_from_db()
    assert first.recurrence_id is None


@pytest.mark.parametrize('rrule',['FREQ=SECONDLY','FREQ=DAILY;BYHOUR=1','FREQ=DAILY\nRRULE:FREQ=YEARLY','garbage','FREQ=DAILY;INTERVAL=0','FREQ=DAILY;INTERVAL=-1','FREQ=DAILY;COUNT=0'])
def test_rejects_unbounded_dense_or_invalid_rules(people,rrule):
    a,_=people
    first=event(a,datetime(2030,1,1,9,tzinfo=UTC))
    with pytest.raises(ValidationError):
        create_rule_from_event(a,first,rrule=rrule)
