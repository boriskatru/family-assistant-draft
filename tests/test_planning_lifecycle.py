"""Regression scenarios for workload, release dates and stale task reservations."""
from datetime import date, datetime, timedelta, timezone

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from assistant.models import Event, Job, Reminder, Task, Trip, TripSegment
from assistant.planning import _daily_load, accept_plan, build_daily_plan
from assistant.planning_actions import cancel_task_reservations, reconcile_task_reservations
from assistant.services import complete_task

pytestmark = pytest.mark.django_db
DAY = date(2030, 1, 7)


def at(hour, day=DAY):
    return datetime.combine(day, datetime.min.time(), timezone.utc).replace(hour=hour)


@pytest.fixture
def users(people, monkeypatch):
    from assistant import planning
    monkeypatch.setattr(planning.timezone, 'now', lambda: at(0, DAY-timedelta(days=1)))
    for person in people:
        person.timezone = 'UTC'
        person.save()
    return people


def task(person, **fields):
    result = Task.objects.create(owner=person, assignee=person, title='Подготовить подарки', **fields)
    result.participants.add(person)
    return result


def event(person, start=at(10), end=at(11), **fields):
    result = Event.objects.create(owner=person, title='Занятость', starts_at=start,
        ends_at=end, timezone='UTC', **fields)
    result.participants.add(person)
    return result


def proposed(plan):
    return [item for item in plan.items if item['kind'] == 'task']


def test_long_trip_does_not_use_up_free_mornings_daily_workload(users):
    boris, _ = users
    event(boris, at(13), at(16))
    event(boris, at(16), at(14, DAY+timedelta(days=2)), kind='travel')
    task(boris)
    assert _daily_load(boris, DAY) == 180
    blocks = proposed(build_daily_plan(boris, DAY))
    assert len(blocks) == 1
    assert datetime.fromisoformat(blocks[0]['starts_at']) == at(7)
    assert len(accept_plan(boris, DAY)) == 1


def test_travel_still_blocks_occupied_time(users):
    boris, _ = users
    event(boris, at(7), at(17), kind='travel')
    task(boris)
    blocks = proposed(build_daily_plan(boris, DAY))
    assert datetime.fromisoformat(blocks[0]['starts_at']) == at(17)


@pytest.mark.parametrize('segment_kind', ['transport', 'flight', 'train', 'car', 'bus', 'walk', 'stay', 'hotel'])
def test_trip_segment_excluded_even_when_event_has_legacy_meeting_kind(users, segment_kind):
    boris, _ = users
    trip = Trip.objects.create(owner=boris, title='Поездка')
    hotel = event(boris, at(16), at(12, DAY+timedelta(days=1)), trip=trip)
    TripSegment.objects.create(trip=trip, event=hotel, kind=segment_kind)
    assert _daily_load(boris, DAY) == 0


def test_load_override_can_count_work_on_a_journey_or_exclude_another_event(users):
    boris, _ = users
    event(boris, at(7), at(9), kind='travel', counts_toward_load=True)
    event(boris, at(10), at(12), counts_toward_load=False)
    assert _daily_load(boris, DAY) == 120


def test_overlapping_events_count_each_minute_once(users):
    boris, _ = users
    boris.profile = {'max_daily_minutes': 420}
    boris.save()
    event(boris, at(7), at(11))
    event(boris, at(9), at(13))
    task(boris)
    assert _daily_load(boris, DAY) == 360
    blocks = proposed(build_daily_plan(boris, DAY))
    assert len(blocks) == 1
    assert datetime.fromisoformat(blocks[0]['starts_at']) == at(13)
    assert len(accept_plan(boris, DAY)) == 1


def test_workload_clipped_to_local_day_before_merging(users):
    boris, _ = users
    boris.timezone = 'Europe/Moscow'
    boris.save()
    event(boris, at(20, DAY-timedelta(days=1)), at(2))
    event(boris, at(0), at(4))
    assert _daily_load(boris, DAY) == 420  # UTC 21:00 yesterday through 04:00 today.


def test_future_release_date_does_not_schedule_or_raise_urgent_warning(users):
    boris, _ = users
    todo = task(boris, not_before=at(12, DAY+timedelta(days=10)), due_at=at(12, DAY+timedelta(days=20)))
    plan = build_daily_plan(boris, DAY)
    assert proposed(plan) == []
    assert not [item for item in plan.items if item['kind'] == 'unscheduled']
    assert any(item['kind'] == 'deferred' and item['task_id'] == str(todo.pk) for item in plan.items)


def test_release_time_is_honoured_inside_day_and_on_accept(users):
    boris, _ = users
    task(boris, not_before=at(15), due_at=at(18))
    blocks = proposed(build_daily_plan(boris, DAY))
    assert datetime.fromisoformat(blocks[0]['starts_at']) == at(15)
    assert accept_plan(boris, DAY)[0].starts_at == at(15)


def test_accept_rechecks_release_even_if_admin_changes_without_version_bump(users):
    boris, _ = users
    todo = task(boris)
    build_daily_plan(boris, DAY)
    Task.objects.filter(pk=todo.pk).update(not_before=at(15))
    with pytest.raises(ValidationError, match='начала задачи'):
        accept_plan(boris, DAY)
    assert not Event.objects.exists()


def test_completion_frees_future_planner_blocks_and_preserves_linked_meeting(users):
    boris, _ = users
    todo = task(boris)
    build_daily_plan(boris, DAY)
    block = accept_plan(boris, DAY)[0]
    meeting = event(boris, at(15), at(16), task=todo, kind='meeting')
    complete_task(boris, todo.pk)
    block.refresh_from_db()
    meeting.refresh_from_db()
    assert block.status == 'cancelled'
    assert meeting.status == 'confirmed'
    assert not block.reminders.filter(status__in=['pending', 'queued', 'paused']).exists()
    assert Job.objects.filter(kind='google_event', payload__event_id=str(block.pk), payload__isnull=False).exists()


def test_cancel_generated_blocks_preserves_started_and_past_work(users):
    boris, _ = users
    todo = task(boris)
    started = event(boris, at(23, DAY-timedelta(days=2)), at(1, DAY-timedelta(days=1)), task=todo, kind='task', flexible=True)
    future = event(boris, task=todo, kind='task', flexible=True)
    cancelled = cancel_task_reservations(todo, boris, 'task_completed')
    assert [item.pk for item in cancelled] == [future.pk]
    started.refresh_from_db()
    assert started.status == 'confirmed'
    assert cancel_task_reservations(todo, boris) == []


def test_reconcile_removes_blocks_outside_new_window(users):
    boris, _ = users
    todo = task(boris, not_before=at(12), due_at=at(17))
    early = event(boris, at(10), at(11), task=todo, kind='task', flexible=True)
    valid = event(boris, at(13), at(14), task=todo, kind='task', flexible=True)
    late = event(boris, at(17), at(18), task=todo, kind='task', flexible=True)
    assert {item.pk for item in reconcile_task_reservations(todo, boris)} == {early.pk, late.pk}
    valid.refresh_from_db()
    assert valid.status == 'confirmed'


def test_reservation_cleanup_does_not_grant_access_to_private_task(users):
    boris, anna = users
    todo = task(boris)
    block = event(boris, task=todo, kind='task', flexible=True)
    with pytest.raises(PermissionDenied):
        cancel_task_reservations(todo, anna)
    block.refresh_from_db()
    assert block.status == 'confirmed'


def test_trip_activity_segment_still_counts_as_workload(users):
    boris, _ = users
    trip = Trip.objects.create(owner=boris, title='Поездка')
    activity = event(boris, at(10), at(11), trip=trip)
    TripSegment.objects.create(trip=trip, event=activity, kind='activity')
    assert _daily_load(boris, DAY) == 60
