from datetime import timedelta
from unittest.mock import Mock

import httpx
import pytest
from cryptography.fernet import Fernet
from django.utils import timezone

from assistant.integrations import google
from assistant.models import CalendarConnection, CalendarSource, Event, Person

pytestmark = pytest.mark.django_db


@pytest.fixture
def source(settings, monkeypatch):
    settings.TOKEN_ENCRYPTION_KEY = Fernet.generate_key().decode()
    settings.GOOGLE_CLIENT_ID = 'client'
    settings.GOOGLE_CLIENT_SECRET = 'secret'
    settings.GOOGLE_REDIRECT_URI = 'https://example.test/callback'
    monkeypatch.setattr(google, '_notify', Mock())
    person = Person.objects.create(name='Test', telegram_id=991, chat_id=991)
    tokens = {'access_token': 'old-access', 'refresh_token': 'keep-refresh',
              'expires_at': timezone.now().timestamp() + 3600}
    conn = CalendarConnection.objects.create(person=person, encrypted_tokens=google._encrypt(tokens))
    return CalendarSource.objects.create(connection=conn, calendar_id='private-calendar', writable=True)


@pytest.mark.parametrize('status,reason,retryable', [
    (400, 'badRequest', False), (403, 'forbiddenForNonOrganizer', False),
    (403, 'insufficientPermissions', False), (403, 'dailyLimitExceeded', False),
    (403, 'rateLimitExceeded', True), (403, 'userRateLimitExceeded', True),
    (429, 'rateLimitExceeded', True), (503, 'backendError', True),
])
def test_google_http_errors_keep_safe_metadata(source, monkeypatch, status, reason, retryable):
    body = {'error': {'message': 'PRIVATE EVENT access-secret',
                      'errors': [{'reason': reason, 'message': 'PRIVATE EVENT access-secret'}]}}
    monkeypatch.setattr(httpx, 'request', Mock(return_value=httpx.Response(status, json=body)))
    with pytest.raises(google.GoogleIntegrationError) as caught:
        google._request(source.connection, 'PATCH', '/calendars/private-calendar/events/private-event', body={})
    error = caught.value
    assert isinstance(error, google.GoogleRetryableError) is retryable
    assert (error.status_code, error.reason, error.operation) == (status, reason, 'event_update')
    assert 'PRIVATE' not in str(error) and 'access-secret' not in str(error)
    assert 'private-calendar' not in str(error) and 'private-event' not in str(error)


@pytest.mark.parametrize('payload', [
    {'error': {'errors': [{'reason': 'PRIVATE TOKEN'}]}},
    {'error': {'errors': 'PRIVATE TOKEN'}}, ['PRIVATE TOKEN'], {'error': None},
])
def test_unknown_error_details_are_not_exposed(source, monkeypatch, payload):
    monkeypatch.setattr(httpx, 'request', Mock(return_value=httpx.Response(400, json=payload)))
    with pytest.raises(google.GoogleIntegrationError) as caught:
        google._request(source.connection, 'GET', '/users/me/calendarList')
    assert caught.value.reason is None
    assert caught.value.operation == 'calendar_list'
    assert 'PRIVATE' not in str(caught.value)


def test_non_json_google_error_keeps_status(source, monkeypatch):
    monkeypatch.setattr(httpx, 'request', Mock(return_value=httpx.Response(503, text='PRIVATE HTML')))
    with pytest.raises(google.GoogleRetryableError) as caught:
        google._request(source.connection, 'GET', '/users/me/calendarList')
    assert caught.value.status_code == 503
    assert caught.value.reason is None


@pytest.mark.parametrize('status,reason', [(400, 'invalid_client'), (401, 'invalid_client'), (400, 'invalid_request')])
def test_oauth_config_error_does_not_disable_user_connection(source, monkeypatch, status, reason):
    conn = source.connection
    tokens = google._decrypt(conn)
    tokens['expires_at'] = 0
    conn.encrypted_tokens = google._encrypt(tokens)
    conn.save()
    monkeypatch.setattr(httpx, 'post', Mock(return_value=httpx.Response(status, json={'error': reason})))
    with pytest.raises(google.GoogleIntegrationError) as caught:
        google._access_token(conn)
    assert not isinstance(caught.value, google.GoogleRetryableError)
    assert caught.value.operation == 'token_refresh'
    assert caught.value.reason == reason
    conn.refresh_from_db()
    assert conn.enabled and google._decrypt(conn)['refresh_token'] == 'keep-refresh'
    google._notify.assert_not_called()


def test_stale_401_cannot_overwrite_rotated_refresh_token(source, monkeypatch):
    conn = source.connection

    def unauthorized(*args, **kwargs):
        newer = CalendarConnection.objects.get(pk=conn.pk)
        tokens = google._decrypt(newer)
        tokens.update(access_token='new-access', refresh_token='new-refresh')
        newer.encrypted_tokens = google._encrypt(tokens)
        newer.save()
        return httpx.Response(401, json={'error': {'errors': [{'reason': 'authError'}]}})

    monkeypatch.setattr(httpx, 'request', unauthorized)
    with pytest.raises(google.GoogleRetryableError):
        google._request(conn, 'GET', '/users/me/calendarList')
    conn.refresh_from_db()
    tokens = google._decrypt(conn)
    assert tokens['refresh_token'] == 'new-refresh'
    assert tokens['access_token'] == 'new-access'
    assert tokens['expires_at'] > timezone.now().timestamp()


def test_401_expires_only_current_access_token(source, monkeypatch):
    monkeypatch.setattr(httpx, 'request', Mock(return_value=httpx.Response(401)))
    with pytest.raises(google.GoogleRetryableError) as caught:
        google._request(source.connection, 'GET', '/users/me/calendarList')
    source.connection.refresh_from_db()
    assert google._decrypt(source.connection)['expires_at'] == 0
    assert source.connection.enabled
    assert caught.value.status_code == 401


@pytest.mark.parametrize('transient', [False, True])
def test_sync_all_preserves_cause_and_continues_other_events(source, monkeypatch, transient):
    now = timezone.now()
    events = [Event.objects.create(owner=source.connection.person, title='Hidden event',
              starts_at=now, ends_at=now + timedelta(hours=1), sync_status='pending') for _ in range(2)]
    failure_type = google.GoogleRetryableError if transient else google.GoogleIntegrationError
    original = failure_type('Safe message', status_code=503 if transient else 400,
                            reason='backendError' if transient else 'badRequest', operation='event_update')
    monkeypatch.setattr(google, 'sync_source', Mock())
    sync_event = Mock(side_effect=[original, None])
    monkeypatch.setattr(google, 'sync_event', sync_event)
    with pytest.raises(google.GoogleIntegrationError) as caught:
        google.sync_all()
    assert isinstance(caught.value, google.GoogleRetryableError) is transient
    assert caught.value.__cause__ is original
    assert caught.value.status_code == original.status_code
    assert caught.value.reason == original.reason
    assert caught.value.operation == 'event_update'
    assert caught.value.failure_count == 1
    assert caught.value.retryable_count == int(transient)
    assert {call.args[0] for call in sync_event.call_args_list} == {e.pk for e in events}
    google._notify.assert_not_called()


def test_mixed_sync_failures_retry_transient_part_without_losing_permanent_cause(source, monkeypatch):
    now = timezone.now()
    Event.objects.create(owner=source.connection.person, title='Hidden event',
                         starts_at=now, ends_at=now + timedelta(hours=1), sync_status='pending')
    monkeypatch.setattr(google, 'sync_source', Mock(side_effect=google.GoogleRetryableError(
        'Temporary', status_code=503, reason='backendError', operation='calendar_sync')))
    monkeypatch.setattr(google, 'sync_event', Mock(side_effect=google.GoogleIntegrationError(
        'Permanent', status_code=403, reason='forbiddenForNonOrganizer', operation='event_update')))
    with pytest.raises(google.GoogleRetryableError) as caught:
        google.sync_all()
    assert caught.value.failure_count == 2
    assert caught.value.retryable_count == 1
    assert caught.value.status_code == 403
    assert caught.value.reason == 'forbiddenForNonOrganizer'
    google._notify.assert_not_called()
