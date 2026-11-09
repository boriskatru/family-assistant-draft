from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from unittest.mock import Mock

import httpx
import pytest

from assistant import services
from assistant.event_timing import normalise_event_timing
from assistant.integrations import google
from assistant.models import CalendarBinding, CalendarConnection, CalendarSource, Event, Reminder
from assistant.recurrence import create_rule_from_event, materialize_recurrences

pytestmark = pytest.mark.django_db


def saved_event(person, **values):
    timing = normalise_event_timing(person, {'all_day': True, 'start_date': '2030-12-24', **values})
    event = Event.objects.create(owner=person, title='День рождения Коли', **timing)
    event.participants.add(person)
    return event


def source(person):
    connection = CalendarConnection.objects.create(person=person, encrypted_tokens='unused')
    return CalendarSource.objects.create(connection=connection, calendar_id='test-calendar', role='shared')


def test_google_all_day_roundtrip_preserves_date_and_free_status(people):
    person, _ = people
    event = saved_event(person)
    payload = google._export_payload(event)
    assert payload['start'] == {'date': '2030-12-24'}
    assert payload['end'] == {'date': '2030-12-25'}
    assert payload['transparency'] == 'transparent'
    fields = google._remote_fields(payload, source(person))
    assert fields['all_day'] and fields['blocks_time'] is False
    assert fields['start_date'] == event.start_date and fields['end_date'] == event.end_date
    assert google._snapshot(event)['blocks_time'] is False


def test_google_timed_transparency_import_does_not_change_privacy(people):
    person, _ = people
    calendar = source(person)
    fields = google._remote_fields({'summary': 'Private note', 'visibility': 'private',
        'transparency': 'transparent', 'start': {'dateTime': '2030-12-24T12:00:00+03:00'},
        'end': {'dateTime': '2030-12-24T13:00:00+03:00'}}, calendar)
    assert not fields['all_day'] and not fields['blocks_time'] and fields['visibility'] == 'private'
    fields = google._remote_fields({'start': {'date': '2030-12-24'}, 'end': {'date': '2030-12-25'}}, calendar)
    assert fields['blocks_time'] is True


def test_google_import_and_move_use_morning_reminder_for_date_only(people):
    person, _ = people
    calendar = source(person)
    payload = {'id': 'birthday', 'etag': 'v1', 'summary': 'День рождения Коли',
        'start': {'date': '2030-12-24'}, 'end': {'date': '2030-12-25'}, 'transparency': 'transparent'}
    google._import_item(calendar, payload)
    event = CalendarBinding.objects.get(source=calendar, external_id='birthday').event
    assert set(event.reminders.values_list('recipient_id', flat=True)) == {p.pk for p in people}
    assert event.reminders.get(recipient=person).due_at.astimezone(ZoneInfo(person.timezone)).hour == 9
    google._import_item(calendar, {**payload, 'etag': 'v2',
        'start': {'date': '2030-12-25'}, 'end': {'date': '2030-12-26'}})
    event.refresh_from_db()
    assert event.reminders.filter(status='pending').count() == 2
    reminder = event.reminders.get(status='pending', recipient=person)
    assert reminder.due_at.astimezone(ZoneInfo(person.timezone)).date() == date(2030, 12, 25)
    assert reminder.due_at.astimezone(ZoneInfo(person.timezone)).hour == 9
    assert not event.blocks_time


def test_nonblocking_all_day_recurrence_preserves_dates_and_actual_dst_span(people):
    person, _ = people
    event = saved_event(person, start_date='2030-03-30', timezone='Europe/Berlin')
    Reminder.objects.create(event=event, recipient=person, offset_minutes=0,
        due_at=datetime(2030, 3, 30, 9, tzinfo=ZoneInfo('Europe/Berlin')))
    rule = create_rule_from_event(person, event, rrule='FREQ=DAILY;COUNT=3')
    assert rule.template['blocks_time'] is False
    materialize_recurrences(datetime(2030, 3, 30, tzinfo=timezone.utc))
    following = Event.objects.get(recurrence=rule, start_date=date(2030, 3, 31))
    assert not following.blocks_time and following.end_date == date(2030, 4, 1)
    assert following.ends_at - following.starts_at == timedelta(hours=23)
    assert following.reminders.get().due_at.astimezone(ZoneInfo('Europe/Berlin')).hour == 9


def test_service_can_create_and_move_date_only_event_without_clock_times(people):
    person, spouse = people
    event = services.create_event(person, title='День рождения Коли', all_day=True,
        start_date='2030-12-24', visibility='shared', participants=[person.pk, spouse.pk])
    assert event.start_date == date(2030, 12, 24) and not event.blocks_time
    assert event.reminders.count() == 2
    assert all(r.due_at.astimezone(ZoneInfo('Europe/Moscow')).hour == 9 for r in event.reminders.all())
    services.update_event(spouse, event.pk, {'start_date': '2030-12-25'}, expected_version=event.version)
    event.refresh_from_db()
    assert event.start_date == date(2030, 12, 25) and event.end_date == date(2030, 12, 26)
    assert event.reminders.filter(status='pending').count() == 2
    assert all(r.due_at.astimezone(ZoneInfo('Europe/Moscow')).day == 25
               for r in event.reminders.filter(status='pending'))


@pytest.mark.parametrize('transparency', ['opaque', 'transparent'])
def test_old_google_snapshot_does_not_acknowledge_pending_local_edit(people, transparency):
    person, _ = people
    calendar = source(person)
    event = saved_event(person, blocks_time=True)
    event.visibility = 'shared'
    event.save()
    baseline = google._snapshot(event)
    baseline.pop('blocks_time')
    remote = {**google._export_payload(event), 'id': 'legacy', 'etag': 'same',
              'transparency': transparency}
    binding = CalendarBinding.objects.create(source=calendar, event=event,
        external_id='legacy', etag='same', last_version=event.version, snapshot=baseline)
    event.title = 'Изменено в боте'
    event.version += 1
    event.sync_status = 'pending'
    event.save()
    google._import_item(calendar, remote)
    binding.refresh_from_db()
    event.refresh_from_db()
    assert event.title == 'Изменено в боте' and event.sync_status == 'pending'
    assert binding.last_version == event.version - 1
    assert binding.snapshot == baseline


def test_old_google_snapshot_without_changes_does_not_trigger_patch(people, monkeypatch):
    person, _ = people
    calendar = source(person)
    calendar.writable = True
    calendar.save()
    event = saved_event(person, blocks_time=True)
    event.visibility = 'shared'
    event.save()
    baseline = google._snapshot(event)
    baseline.pop('blocks_time')
    remote = {**google._export_payload(event), 'id': 'legacy', 'etag': 'same'}
    CalendarBinding.objects.create(source=calendar, event=event,
        external_id='legacy', etag='same', last_version=event.version, snapshot=baseline)
    request = Mock(return_value=httpx.Response(200, json=remote))
    monkeypatch.setattr(google, '_request', request)
    google.sync_event(event.pk)
    request.assert_called_once()
    assert request.call_args.args[1] == 'GET'
    assert CalendarBinding.objects.get(event=event).snapshot['blocks_time'] is True
