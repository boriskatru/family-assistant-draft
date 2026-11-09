from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError

from assistant.event_timing import normalise_event_timing
from assistant.reminder_defaults import event_reminder_due, event_reminder_minutes


USER = SimpleNamespace(timezone='Europe/Moscow')


def test_date_only_create_defaults_to_one_day_and_nonblocking():
    data = normalise_event_timing(USER, {'all_day': True, 'start_date': '2030-12-24'})
    assert data['start_date'] == date(2030, 12, 24)
    assert data['end_date'] == date(2030, 12, 25)
    assert data['starts_at'].hour == data['ends_at'].hour == 0
    assert data['blocks_time'] is False
    assert data['starts_at'].utcoffset() == timedelta(hours=3)


def test_all_day_duration_respects_dst_and_can_block_explicitly():
    data = normalise_event_timing(USER, {'all_day': True, 'start_date': '2030-03-31',
        'timezone': 'Europe/Berlin', 'blocks_time': True})
    elapsed = data['ends_at'].astimezone(timezone.utc) - data['starts_at'].astimezone(timezone.utc)
    assert elapsed == timedelta(hours=23)
    assert data['blocks_time'] is True


def test_moving_all_day_date_preserves_day_count_and_explicit_blocking():
    data = normalise_event_timing(USER, {'all_day': True, 'start_date': '2030-12-24',
        'end_date': '2030-12-27', 'blocks_time': True})
    event = SimpleNamespace(**data)
    updated = normalise_event_timing(USER, {'start_date': '2031-01-02'}, event)
    assert updated['end_date'] == date(2031, 1, 5) and updated['blocks_time']
    assert normalise_event_timing(USER, {'description': 'Instructions'}, event) == {}


@pytest.mark.parametrize('data', [
    {'all_day': True},
    {'all_day': True, 'start_date': '2030-02-30'},
    {'all_day': True, 'start_date': '2030-12-24', 'end_date': '2030-12-24'},
    {'all_day': True, 'start_date': '2030-12-24', 'end_date': '2030-12-23'},
    {'all_day': True, 'start_date': '2030-12-24', 'starts_at': '2030-12-24T12:00:00+03:00'},
    {'all_day': True, 'start_date': '2030-12-24', 'end_timezone': 'Asia/Tokyo'},
    {'all_day': False, 'start_date': '2030-12-24'},
    {'starts_at': '2030-12-24T12:00:00+03:00'},
    {'starts_at': '2030-12-24T12:00:00+03:00', 'ends_at': '2030-12-24T11:00:00+03:00'},
    {'all_day': True, 'start_date': '2030-12-24', 'blocks_time': 'false'},
])
def test_invalid_or_contradictory_date_time_input_is_rejected(data):
    with pytest.raises(ValidationError):
        normalise_event_timing(USER, data)


def test_timed_transition_requires_explicit_times_and_clears_dates():
    event = SimpleNamespace(**normalise_event_timing(USER, {'all_day': True, 'start_date': '2030-12-24'}))
    with pytest.raises(ValidationError):
        normalise_event_timing(USER, {'all_day': False}, event)
    updated = normalise_event_timing(USER, {'all_day': False,
        'starts_at': '2030-12-24T12:00:00+03:00', 'ends_at': '2030-12-24T13:00:00+03:00'}, event)
    assert updated['start_date'] is updated['end_date'] is None
    assert updated['blocks_time'] is True


def test_all_day_default_reminder_at_nine_and_custom_offsets_from_midnight():
    event = SimpleNamespace(**normalise_event_timing(USER, {'all_day': True, 'start_date': '2030-12-24'}))
    assert event_reminder_minutes(event) == 0
    assert event_reminder_due(event, 0) == datetime(2030, 12, 24, 9, tzinfo=ZoneInfo('Europe/Moscow'))
    assert event_reminder_due(event, 1440, 'custom') == event.starts_at - timedelta(days=1)


def test_custom_reminder_offset_is_elapsed_time_across_dst():
    event = SimpleNamespace(**normalise_event_timing(USER, {'all_day': True,
        'start_date': '2030-04-01', 'timezone': 'Europe/Berlin'}))
    due = event_reminder_due(event, 1440, 'custom')
    assert event.starts_at.astimezone(timezone.utc) - due == timedelta(hours=24)
    assert due.astimezone(ZoneInfo('Europe/Berlin')).hour == 23
