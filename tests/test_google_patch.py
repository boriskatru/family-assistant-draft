from copy import deepcopy

import httpx
import pytest

from assistant.event_timing import normalise_event_timing
from assistant.integrations import google
from assistant.models import CalendarBinding, CalendarConnection, CalendarSource, Event


pytestmark = pytest.mark.django_db


def _timing(person, all_day, event=None):
    values = ({'all_day': True, 'start_date': '2030-12-24'} if all_day else {
        'all_day': False,
        'starts_at': '2030-12-24T12:00:00+03:00',
        'ends_at': '2030-12-24T13:00:00+03:00',
    })
    return normalise_event_timing(person, values, event)


def _bound_event(person, all_day):
    connection = CalendarConnection.objects.create(person=person, encrypted_tokens='unused')
    source = CalendarSource.objects.create(connection=connection, calendar_id='test-calendar', writable=True)
    event = Event.objects.create(owner=person, title='День рождения', **_timing(person, all_day))
    event.participants.add(person)
    binding = CalendarBinding.objects.create(source=source, event=event, external_id='google-event',
        etag='v1', snapshot=google._snapshot(event), last_version=event.version)
    remote = {**google._export_payload(event), 'id': binding.external_id, 'etag': 'v1',
        'status': 'confirmed', 'recurringEventId': 'birthday-series',
        'originalStartTime': deepcopy(google._export_payload(event)['start']),
        'attendees': [{'email': 'guest@example.org'}],
        'conferenceData': {'entryPoints': [{'entryPointType': 'video', 'uri': 'https://meet.example.org/test'}]}}
    remote['extendedProperties']['private']['other_app'] = 'keep'
    return event, binding, remote


def _merge_patch(target, patch):
    """Google's documented PATCH semantics: recursive merge, null deletes."""
    result = deepcopy(target)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict):
            result[key] = _merge_patch(result.get(key, {}), value)
        else:
            result[key] = deepcopy(value)
    return result


@pytest.mark.parametrize(('before_all_day', 'after_all_day'), [
    (False, True), (True, False), (False, False), (True, True),
])
def test_patch_converts_timing_without_mixed_date_fields_and_preserves_provider_data(
        people, monkeypatch, before_all_day, after_all_day):
    person, _ = people
    event, binding, remote = _bound_event(person, before_all_day)
    for field, value in _timing(person, after_all_day, event).items():
        setattr(event, field, value)
    event.title = 'Обновлённое событие'
    event.version += 1
    event.sync_status = 'pending'
    event.save()
    calls = []
    provider = deepcopy(remote)

    def request(connection, method, path, **kwargs):
        nonlocal provider
        calls.append(method)
        if method == 'GET':
            return httpx.Response(200, json=provider)
        assert method == 'PATCH'
        assert kwargs['etag'] == 'v1'
        updated = _merge_patch(provider, kwargs['body'])
        for boundary in ('start', 'end'):
            assert ('date' in updated[boundary]) != ('dateTime' in updated[boundary]), (
                'A Google PATCH must not leave both date and dateTime on the event')
        provider = {**updated, 'etag': 'v2'}
        return httpx.Response(200, json=provider)

    monkeypatch.setattr(google, '_request', request)
    google.sync_event(event.pk)
    event.refresh_from_db()
    binding.refresh_from_db()
    assert calls == ['GET', 'PATCH']
    assert event.sync_status == 'synced' and binding.last_version == event.version
    assert binding.etag == 'v2' and not binding.conflict
    assert provider['summary'] == event.title
    field = 'date' if after_all_day else 'dateTime'
    assert provider['start'][field] == google._export_payload(event)['start'][field]
    assert provider['end'][field] == google._export_payload(event)['end'][field]
    if after_all_day:
        assert 'timeZone' not in provider['start'] and 'timeZone' not in provider['end']
    for key in ('recurringEventId', 'originalStartTime', 'attendees', 'conferenceData'):
        assert provider[key] == remote[key]
    assert provider['extendedProperties']['private']['other_app'] == 'keep'
    assert event.meeting_url == 'https://meet.example.org/test'
    google.sync_event(event.pk)
    assert calls == ['GET', 'PATCH', 'GET']


def test_timing_conversion_keeps_conflict_on_concurrent_provider_edit(people, monkeypatch):
    person, _ = people
    event, binding, remote = _bound_event(person, False)
    for field, value in _timing(person, True, event).items():
        setattr(event, field, value)
    event.version += 1
    event.sync_status = 'pending'
    event.save()
    original_snapshot = deepcopy(binding.snapshot)

    def request(connection, method, path, **kwargs):
        if method == 'GET':
            return httpx.Response(200, json=remote)
        assert method == 'PATCH' and kwargs['etag'] == 'v1'
        return httpx.Response(412, json={})

    monkeypatch.setattr(google, '_request', request)
    monkeypatch.setattr(google, '_notify', lambda *args, **kwargs: None)
    with pytest.raises(google.GoogleRetryableError):
        google.sync_event(event.pk)
    binding.refresh_from_db()
    event.refresh_from_db()
    assert binding.conflict and binding.snapshot == original_snapshot
    assert binding.last_version == event.version - 1
    assert event.all_day and event.sync_status != 'synced'
