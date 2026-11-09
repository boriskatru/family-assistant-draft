from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from assistant import services
from assistant.models import Event, OccurrenceOverride, Reminder, Task
from assistant.recurrence import create_rule_from_event, create_rule_from_task, materialize_recurrences, occurrence_key
from assistant.recurrence_actions import update_recurrence, stop_recurrence
from assistant.reminder_actions import update_reminders, refresh_event_reminders

pytestmark = pytest.mark.django_db
UTC = timezone.utc
START = datetime(2030, 1, 1, 9, tzinfo=UTC)


def event(owner, **kwargs):
    return services.create_event(owner, title='Call', starts_at=START, ends_at=START+timedelta(hours=1), **kwargs)


@pytest.mark.parametrize('status', ['sent', 'missed', 'summarized'])
def test_description_edit_keeps_delivery_and_move_creates_new_row(people, status):
    owner, _ = people
    obj = event(owner)
    original = obj.reminders.get()
    original.status = status
    original.save()
    services.update_event(owner, obj.pk, {'description': 'Address details'}, obj.version)
    original.refresh_from_db()
    assert original.status == status and obj.reminders.count() == 1
    obj.refresh_from_db()
    services.update_event(owner, obj.pk, {'starts_at': START+timedelta(days=1), 'ends_at': START+timedelta(days=1, hours=1)}, obj.version)
    original.refresh_from_db()
    assert original.status == status
    assert obj.reminders.filter(status='pending').get().due_at == START+timedelta(days=1, minutes=-15)


def test_disable_preserves_sent_history_and_survives_move(people):
    owner, _ = people
    obj = event(owner)
    row = obj.reminders.get()
    row.status = 'sent'; row.save()
    obj = update_reminders(owner, 'event', obj.pk, obj.version, 'disable')
    row.refresh_from_db()
    assert row.status == 'sent'
    assert obj.reminders.filter(status='disabled').count() == 1
    services.update_event(owner, obj.pk, {'starts_at': START+timedelta(days=1), 'ends_at': START+timedelta(days=1, hours=1)}, obj.version)
    assert not obj.reminders.filter(status='pending').exists()


def test_remove_default_offset_and_same_day_clock(people):
    owner, _ = people
    obj = event(owner)
    obj = update_reminders(owner, 'event', obj.pk, obj.version, 'remove', [{'offset_minutes': 15}])
    assert not obj.reminders.filter(status='pending').exists()
    birthday = services.create_event(owner, title='Birthday', all_day=True, start_date='2030-12-24', blocks_time=False)
    birthday = update_reminders(owner, 'event', birthday.pk, birthday.version, 'replace', [{'local_time': '11:00'}])
    active = birthday.reminders.get(status='pending')
    assert active.due_at.astimezone(ZoneInfo(owner.timezone)).isoformat().startswith('2030-12-24T11:00')
    rule = create_rule_from_event(owner, birthday, 'FREQ=YEARLY;COUNT=2')
    materialize_recurrences(datetime(2031, 12, 1, tzinfo=UTC))
    next_birthday = Event.objects.filter(recurrence=rule).order_by('starts_at').last()
    assert next_birthday.reminders.get().due_at.astimezone(ZoneInfo(owner.timezone)).hour == 11


def test_reminder_version_and_privacy(people):
    owner, spouse = people
    obj = event(owner)
    with pytest.raises(PermissionDenied):
        update_reminders(spouse, 'event', obj.pk, obj.version, 'disable')
    with pytest.raises(ValidationError):
        update_reminders(owner, 'event', obj.pk, obj.version+1, 'disable')
    assert obj.reminders.get().status == 'pending'


def test_reminder_new_attendee_inherits_but_empty_schedule_stays_empty(people):
    owner, spouse = people
    obj = event(owner, visibility='shared')
    obj.participants.add(spouse)
    refresh_event_reminders(obj)
    assert set(obj.reminders.values_list('recipient_id', flat=True)) == {owner.pk, spouse.pk}
    empty = event(owner, visibility='shared', remind_minutes=[])
    empty.participants.add(spouse)
    refresh_event_reminders(empty)
    assert not empty.reminders.exists()


def test_shared_task_series_editable_by_spouse_and_owner_preserved(people):
    owner, spouse = people
    task = services.create_task(owner, title='Clean', visibility='shared', due_at=START)
    task.participants.set([owner, spouse])
    rule = create_rule_from_task(spouse, task, mode='calendar', rrule='FREQ=WEEKLY;COUNT=3')
    assert rule.owner_id == owner.pk
    task.refresh_from_db()
    result = update_recurrence(spouse, 'task', task.pk, task.version,
        {'local_time': '14:00'}, effective_from=START)
    first = Task.objects.get(pk=result['record_id'])
    assert first.owner_id == owner.pk and first.due_at.astimezone(ZoneInfo(owner.timezone)).hour == 14


def test_series_move_preserves_past_existing_ids_and_cancelled_exceptions(people):
    owner, _ = people
    obj = event(owner)
    rule = create_rule_from_event(owner, obj, 'FREQ=DAILY;COUNT=4')
    OccurrenceOverride.objects.create(rule=rule, occurrence_key=occurrence_key(START+timedelta(days=2)), action='cancel')
    materialize_recurrences(START)
    day2 = Event.objects.get(recurrence=rule, starts_at=START+timedelta(days=1))
    day2_id = day2.pk
    obj.refresh_from_db()
    result = update_recurrence(owner, 'event', obj.pk, obj.version,
        {'local_time': '14:00'}, effective_from=START+timedelta(days=1))
    obj.refresh_from_db(); day2.refresh_from_db()
    assert obj.starts_at == START and obj.recurrence_id == rule.pk
    assert day2.pk == day2_id and day2.starts_at.astimezone(ZoneInfo(owner.timezone)).hour == 14
    assert Event.objects.filter(recurrence_id=result['rule_id'], status='confirmed').count() == 2
    materialize_recurrences(START)
    assert Event.objects.filter(recurrence_id=result['rule_id'], status='confirmed').count() == 2
    assert OccurrenceOverride.objects.filter(rule_id=result['rule_id'], action='cancel').count() == 1


def test_stop_cancels_future_then_no_new_instances_and_can_reconfigure(people):
    owner, _ = people
    obj = event(owner)
    rule = create_rule_from_event(owner, obj, 'FREQ=DAILY;COUNT=4')
    materialize_recurrences(START)
    obj.refresh_from_db()
    with patch('assistant.recurrence_actions.timezone.now', return_value=START-timedelta(hours=1)):
        stop_recurrence(owner, 'event', obj.pk, obj.version, cancel_future=False)
    obj.refresh_from_db(); rule.refresh_from_db()
    assert not rule.active and obj.status == 'confirmed'
    new = create_rule_from_event(owner, obj, 'FREQ=WEEKLY;COUNT=2')
    assert new.pk != rule.pk


def test_stop_preserves_past_and_cancels_materialized_future(people):
    owner, _ = people
    obj = event(owner)
    rule = create_rule_from_event(owner, obj, 'FREQ=DAILY;COUNT=4')
    materialize_recurrences(START)
    obj.refresh_from_db()
    result = stop_recurrence(owner, 'event', obj.pk, obj.version, effective_from=START+timedelta(days=1))
    obj.refresh_from_db()
    assert obj.status == 'confirmed' and result['cancelled_occurrences'] == 3
    materialize_recurrences(START+timedelta(days=1))
    assert Event.objects.filter(recurrence=rule, status='confirmed').count() == 1


def test_annual_edit_finds_next_occurrence_outside_horizon(people):
    owner, _ = people
    obj = event(owner)
    create_rule_from_event(owner, obj, 'FREQ=YEARLY;COUNT=3')
    obj.refresh_from_db()
    result = update_recurrence(owner, 'event', obj.pk, obj.version,
        {'title': 'Annual'}, effective_from=START+timedelta(days=1))
    following = Event.objects.get(pk=result['record_id'])
    assert following.starts_at.year == 2031 and following.title == 'Annual'


def test_completed_future_due_task_is_not_moved_or_reopened(people):
    owner, _ = people
    task = services.create_task(owner, title='Task', due_at=START)
    rule = create_rule_from_task(owner, task, mode='calendar', rrule='FREQ=DAILY;COUNT=3')
    materialize_recurrences(START)
    future = Task.objects.get(recurrence=rule, due_at=START+timedelta(days=1))
    future.status = 'done'; future.completed_at = START; future.save()
    task.refresh_from_db()
    result = update_recurrence(owner, 'task', task.pk, task.version,
        {'local_time': '15:00'}, effective_from=START+timedelta(days=1))
    future.refresh_from_db()
    assert future.status == 'done' and future.due_at == START+timedelta(days=1)
    assert Task.objects.filter(recurrence_id=result['rule_id']).count() == 1


def test_after_completion_interval_updates_existing_successor(people):
    owner, _ = people
    task = services.create_task(owner, title='Filter', due_at=START)
    create_rule_from_task(owner, task, mode='after_completion', interval=1, unit='month')
    task.refresh_from_db()
    with patch('assistant.services.timezone.now', return_value=START):
        services.complete_task(owner, task.pk)
    successor = Task.objects.get(previous_task=task)
    original_id = successor.pk
    result = update_recurrence(owner, 'task', successor.pk, successor.version,
        {'interval': 2}, effective_from=START+timedelta(days=1))
    successor.refresh_from_db()
    assert successor.pk == original_id and successor.due_at.month == 3


def test_restore_event_reactivates_cancelled_but_not_disabled_reminders(people):
    owner, spouse = people
    obj = event(owner, visibility='shared', participants=[owner.pk, spouse.pk])
    obj = update_reminders(owner, 'event', obj.pk, obj.version, 'disable')
    obj = services.update_event(owner, obj.pk, {'status': 'cancelled'}, obj.version)
    assert obj.reminders.filter(recipient=spouse).get().status == 'cancelled'
    obj = services.update_event(owner, obj.pk, {'status': 'confirmed'}, obj.version)
    assert obj.reminders.filter(recipient=spouse).get().status == 'pending'
    assert obj.reminders.filter(recipient=owner).get().status == 'disabled'


def test_removed_and_readded_attendee_retains_schedule_and_delivery_history(people):
    owner, spouse = people
    obj = event(owner, visibility='shared', participants=[owner.pk, spouse.pk])
    sent = obj.reminders.get(recipient=spouse)
    sent.status = 'sent'; sent.save()
    obj = services.update_event(owner, obj.pk, {'participants': [str(owner.pk)]}, obj.version)
    sent.refresh_from_db()
    assert sent.status == 'sent'
    assert not obj.reminders.filter(recipient=spouse, status='pending').exists()
    obj = services.update_event(owner, obj.pk, {'participants': [str(owner.pk), str(spouse.pk)]}, obj.version)
    assert obj.reminders.filter(recipient=spouse, status='pending').count() == 1
