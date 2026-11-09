"""Google OAuth/Calendar adapter. Network calls are isolated for mocked tests.

Draft deliberately exports local recurrence instances as individual events and
imports Google series as occurrences in a moving 30-day/180-day window.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from assistant.models import (
    AuditLog, CalendarBinding, CalendarConnection, CalendarSource, Event,
    OAuthState, Person, Reminder,
)

from assistant.reminder_defaults import event_reminder_minutes, event_reminder_due

API = 'https://www.googleapis.com/calendar/v3'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
SCOPE = 'https://www.googleapis.com/auth/calendar'
TIMEOUT = 25.0
PAST_DAYS = 30
FUTURE_DAYS = 180


class GoogleIntegrationError(ValueError):
    """Safe error text: no token/Google response body is exposed."""

    provider = 'google_calendar'

    def __init__(self, message, *, status_code=None, reason=None, operation=None,
                 failure_count=None, retryable_count=None):
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.operation = operation
        self.failure_count = failure_count
        self.retryable_count = retryable_count


class GoogleRetryableError(GoogleIntegrationError):
    pass


# Only machine codes from this allowlist may leave the HTTP adapter. Google
# messages/URLs can contain event data, account names or credentials.
_GOOGLE_REASONS = frozenset({
    'badRequest', 'invalid', 'required', 'forbidden', 'insufficientPermissions',
    'forbiddenForNonOrganizer', 'notFound', 'gone', 'deleted', 'conditionNotMet',
    'duplicate', 'rateLimitExceeded', 'userRateLimitExceeded', 'quotaExceeded',
    'dailyLimitExceeded', 'calendarUsageLimitsExceeded', 'backendError',
    'internalError', 'authError', 'updatedMinTooLongAgo', 'fullSyncRequired',
    'invalid_grant', 'invalid_client', 'unauthorized_client', 'invalid_request',
    'unsupported_grant_type', 'invalid_scope', 'access_denied',
    'temporarily_unavailable', 'server_error',
})
_RETRYABLE_REASONS = frozenset({
    'rateLimitExceeded', 'userRateLimitExceeded', 'backendError',
    'internalError', 'temporarily_unavailable', 'server_error',
})


def _response_reason(response):
    try:
        payload = response.json()
    except (ValueError, UnicodeError):
        return None
    error = payload.get('error') if isinstance(payload, dict) else None
    if isinstance(error, str):
        return error if error in _GOOGLE_REASONS else None
    if isinstance(error, dict):
        errors = error.get('errors', [])
        if isinstance(errors, list):
            for item in errors[:20]:
                reason = item.get('reason') if isinstance(item, dict) else None
                if isinstance(reason, str) and reason in _GOOGLE_REASONS:
                    return reason
    return None


def _operation(method, path):
    if path == '/users/me/calendarList':
        return 'calendar_list'
    if '/events/' in path:
        return {'GET': 'event_get', 'PATCH': 'event_update',
                'PUT': 'event_update', 'DELETE': 'event_delete'}.get(method)
    if path.endswith('/events'):
        return 'calendar_sync' if method == 'GET' else 'event_insert'
    return None


def _response_error(response, operation):
    status, reason = response.status_code, _response_reason(response)
    retryable = status == 429 or status >= 500 or reason in _RETRYABLE_REASONS
    error_type = GoogleRetryableError if retryable else GoogleIntegrationError
    if retryable:
        message = 'Google временно недоступен или ограничил запросы; действие будет повторено.'
    elif reason == 'forbiddenForNonOrganizer':
        message = 'Google разрешает изменение этих полей только организатору события.'
    elif status == 403:
        message = 'Google отклонил запрос: проверьте права календаря и квоты проекта.'
    elif status == 400:
        message = 'Google отклонил параметры запроса.'
    elif status == 404:
        message = 'Событие или календарь Google не найдены либо недоступны.'
    else:
        message = f'Google отклонил запрос (HTTP {status}).'
    return error_type(message, status_code=status, reason=reason, operation=operation)


def _cipher():
    key = getattr(settings, 'TOKEN_ENCRYPTION_KEY', '')
    if not key:
        raise GoogleIntegrationError('Не задан TOKEN_ENCRYPTION_KEY для Google.')
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise GoogleIntegrationError('TOKEN_ENCRYPTION_KEY должен быть ключом Fernet.') from exc


def _encrypt(tokens):
    return _cipher().encrypt(json.dumps(tokens).encode()).decode()


def _decrypt(connection):
    try:
        return json.loads(_cipher().decrypt(connection.encrypted_tokens.encode()))
    except (InvalidToken, json.JSONDecodeError) as exc:
        raise GoogleIntegrationError('Не удалось прочитать Google-токены. Проверьте ключ шифрования.') from exc


def _configuration():
    client_id = getattr(settings, 'GOOGLE_CLIENT_ID', '')
    client_secret = getattr(settings, 'GOOGLE_CLIENT_SECRET', '')
    redirect = getattr(settings, 'GOOGLE_REDIRECT_URI', '')
    if not all([client_id, client_secret, redirect]):
        raise GoogleIntegrationError('Сначала настройте GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET и GOOGLE_REDIRECT_URI.')
    if not redirect.startswith('https://') and not redirect.startswith('http://localhost'):
        raise GoogleIntegrationError('Google callback должен использовать HTTPS.')
    _cipher()
    return client_id, client_secret, redirect


def authorization_url(person):
    client_id, _, redirect = _configuration()
    state = secrets.token_urlsafe(32)
    OAuthState.objects.create(
        person=person, state_hash=hashlib.sha256(state.encode()).hexdigest(),
        expires_at=timezone.now() + timedelta(minutes=10),
    )
    return AUTH_URL + '?' + urlencode({
        'client_id': client_id, 'redirect_uri': redirect, 'response_type': 'code',
        'scope': SCOPE, 'access_type': 'offline', 'prompt': 'consent',
        'state': state, 'include_granted_scopes': 'true',
    })


def complete_oauth(state, code):
    """Consume state before network exchange. Retry starts a fresh authorization."""
    client_id, client_secret, redirect = _configuration()
    if not state or not code or len(state) > 500 or len(code) > 5000:
        raise GoogleIntegrationError('Неполный ответ Google. Запустите /google заново.')
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    with transaction.atomic():
        record = OAuthState.objects.select_for_update().filter(state_hash=state_hash).first()
        if not record or record.used_at or record.expires_at <= timezone.now():
            raise GoogleIntegrationError('Ссылка авторизации истекла или уже использована. Запустите /google заново.')
        record.used_at = timezone.now()
        record.save(update_fields=['used_at', 'updated_at'])
        person = record.person
    try:
        response = httpx.post(TOKEN_URL, data={
            'code': code, 'client_id': client_id, 'client_secret': client_secret,
            'redirect_uri': redirect, 'grant_type': 'authorization_code',
        }, timeout=TIMEOUT, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise GoogleRetryableError('Google временно недоступен. Запустите авторизацию снова.') from exc
    if response.status_code != 200:
        raise GoogleIntegrationError('Google отклонил авторизацию. Проверьте настройки и запустите /google заново.')
    tokens = response.json()
    if not tokens.get('access_token') or not tokens.get('refresh_token'):
        # Never silently retain a refresh token from a possibly different account.
        raise GoogleIntegrationError('Google не выдал фоновый доступ. Отзовите доступ приложения в Google и подключите заново.')
    tokens['expires_at'] = timezone.now().timestamp() + int(tokens.get('expires_in', 3600))
    try:
        identity = httpx.get(API + '/calendars/primary', headers={'Authorization': 'Bearer ' + tokens['access_token']}, timeout=TIMEOUT, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise GoogleRetryableError('Не удалось проверить Google-аккаунт. Повторите подключение.') from exc
    if identity.status_code != 200 or not identity.json().get('id'):
        raise GoogleIntegrationError('Не удалось проверить основной Google-календарь.')
    tokens['_account_id'] = identity.json()['id']
    previous = CalendarConnection.objects.filter(person=person).first()
    previous_account = _decrypt(previous).get('_account_id') if previous else None
    with transaction.atomic():
        connection, created = CalendarConnection.objects.update_or_create(
            person=person, defaults={'encrypted_tokens': _encrypt(tokens), 'enabled': True},
        )
        # OAuth account can change: never reuse sources selected for old credentials.
        if not created and previous_account != tokens['_account_id']:
            connection.sources.all().delete()
    return connection


def _access_token(connection):
    if not connection.enabled:
        raise GoogleIntegrationError('Google отключён. Выполните /google для повторного подключения.')
    tokens = _decrypt(connection)
    if tokens.get('access_token') and float(tokens.get('expires_at', 0)) > timezone.now().timestamp() + 60:
        return tokens['access_token']
    client_id, client_secret, _ = _configuration()
    # Row lock prevents two workers from racing a refresh-token rotation.
    with transaction.atomic():
        locked = CalendarConnection.objects.select_for_update().get(pk=connection.pk)
        if not locked.enabled:
            raise GoogleIntegrationError('Google отключён. Выполните /google для повторного подключения.',
                                         reason='invalid_grant', operation='token_refresh')
        tokens = _decrypt(locked)
        if tokens.get('access_token') and float(tokens.get('expires_at', 0)) > timezone.now().timestamp() + 60:
            connection.encrypted_tokens = locked.encrypted_tokens
            return tokens['access_token']
        try:
            response = httpx.post(TOKEN_URL, data={
                'client_id': client_id, 'client_secret': client_secret,
                'refresh_token': tokens.get('refresh_token', ''), 'grant_type': 'refresh_token',
            }, timeout=TIMEOUT, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise GoogleRetryableError('Google временно недоступен.',
                                       reason='network_error', operation='token_refresh') from exc
        if response.status_code in (400, 401) and _response_reason(response) == 'invalid_grant':
            locked.enabled = False
            locked.save(update_fields=['enabled', 'updated_at'])
            # Raise outside atomic to persist the disabled flag.
            result = None
        elif response.status_code != 200:
            # A bad OAuth client/configuration is not a revoked user grant.
            # Keep the refresh token so fixing settings does not require consent again.
            raise _response_error(response, 'token_refresh')
        else:
            new = response.json()
            if not new.get('access_token'):
                raise GoogleIntegrationError('Google вернул неполный токен.')
            tokens.update(new)
            tokens['expires_at'] = timezone.now().timestamp() + int(new.get('expires_in', 3600))
            locked.encrypted_tokens = _encrypt(tokens)
            locked.save(update_fields=['encrypted_tokens', 'updated_at'])
            connection.encrypted_tokens = locked.encrypted_tokens
            result = tokens['access_token']
    if result is None:
        connection.enabled = False
        _notify(connection.person, 'Доступ Google отозван или истёк. Подключите календарь заново: /google.', f'google-auth-{connection.pk}')
        raise GoogleIntegrationError('Google требует повторного подключения: /google.',
                                     status_code=response.status_code, reason='invalid_grant', operation='token_refresh')
    return result


def _request(connection, method, path, *, params=None, body=None, etag=None, allowed=()):
    access_token = _access_token(connection)
    operation = _operation(method, path)
    headers = {'Authorization': 'Bearer ' + access_token}
    if etag:
        headers['If-Match'] = etag
    try:
        response = httpx.request(method, API + path, params=params, json=body, headers=headers,
                                timeout=TIMEOUT, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise GoogleRetryableError('Сбой соединения с Google; действие будет повторено.',
                                   reason='network_error', operation=operation) from exc
    if response.status_code in allowed or 200 <= response.status_code < 300:
        return response
    if response.status_code == 401:
        # Another worker may already have refreshed/rotated the credentials.
        # Never overwrite its fresh token with the stale in-memory connection.
        with transaction.atomic():
            locked = CalendarConnection.objects.select_for_update().get(pk=connection.pk)
            tokens = _decrypt(locked)
            if tokens.get('access_token') == access_token:
                tokens['expires_at'] = 0
                locked.encrypted_tokens = _encrypt(tokens)
                locked.save(update_fields=['encrypted_tokens', 'updated_at'])
            connection.encrypted_tokens = locked.encrypted_tokens
        raise GoogleRetryableError('Доступ Google обновляется; действие будет повторено.',
                                   status_code=401, reason='authError', operation=operation)
    raise _response_error(response, operation)


def _notify(person, text, key):
    if person.chat_id:
        from assistant.queue import enqueue
        enqueue('telegram_send', {'chat_id': person.chat_id, 'text': text}, key, queue='notifications')


def list_calendars(person):
    try:
        connection = CalendarConnection.objects.get(person=person, enabled=True)
    except CalendarConnection.DoesNotExist as exc:
        raise GoogleIntegrationError('Сначала подключите Google: /google.') from exc
    calendars, page = [], None
    while True:
        params = {'maxResults': 250}
        if page:
            params['pageToken'] = page
        data = _request(connection, 'GET', '/users/me/calendarList', params=params).json()
        calendars.extend({key: item.get(key) for key in ['id', 'summary', 'accessRole', 'primary']}
                         for item in data.get('items', []))
        page = data.get('nextPageToken')
        if not page:
            break
    return calendars


def select_calendar(person, calendar_id, role='personal', writable=True):
    if role not in ('personal', 'shared'):
        raise GoogleIntegrationError('Роль календаря: personal либо shared.')
    try:
        connection = CalendarConnection.objects.get(person=person, enabled=True)
    except CalendarConnection.DoesNotExist as exc:
        raise GoogleIntegrationError('Сначала подключите Google: /google.') from exc
    info = _request(connection, 'GET', '/users/me/calendarList/' + quote(calendar_id, safe='')).json()
    if info.get('accessRole') not in ('owner', 'writer', 'reader'):
        raise GoogleIntegrationError('Календарь должен разрешать чтение подробностей событий.')
    if writable and info.get('accessRole') not in ('owner', 'writer'):
        raise GoogleIntegrationError('Нет права записи в выбранный календарь.')
    canonical = info.get('id', calendar_id)
    with transaction.atomic():
        other = CalendarSource.objects.filter(calendar_id=canonical).exclude(connection=connection).first()
        if other:
            if role == 'shared' and other.role == 'shared':
                return other  # One import/write owner for common calendar.
            raise GoogleIntegrationError('Этот календарь уже подключён другим пользователем.')
        source = CalendarSource.objects.filter(connection=connection, calendar_id=canonical).first()
        if source and source.role != role and source.calendarbinding_set.exists():
            raise GoogleIntegrationError('Роль календаря с уже синхронизированными событиями меняется вручную администратором.')
        if role == 'shared' and CalendarSource.objects.filter(role='shared').exclude(pk=source.pk if source else None).exists():
            raise GoogleIntegrationError('Общий календарь уже выбран. В черновике поддерживается один общий календарь.')
        source, _ = CalendarSource.objects.update_or_create(
            connection=connection, calendar_id=canonical, defaults={'role': role, 'writable': bool(writable)},
        )
    pending = Event.objects.filter(sync_status='local').exclude(status='proposed')
    if role == 'personal':
        pending = pending.filter(owner=person)
    else:
        pending = pending.filter(visibility='shared')
    pending.update(sync_status='pending')
    return source


def _path(source, external_id=None):
    result = '/calendars/' + quote(source.calendar_id, safe='') + '/events'
    return result + '/' + quote(external_id, safe='') if external_id else result


def _zone(name):
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError, TypeError):
        return ZoneInfo('UTC')


def _remote_fields(item, source, event=None):
    if item.get('status') == 'cancelled':
        return {'status': 'cancelled'}
    start, end = item.get('start', {}), item.get('end', {})
    tz_name = start.get('timeZone') or source.connection.person.timezone
    end_tz = end.get('timeZone') or tz_name
    all_day = bool(start.get('date'))
    try:
        if all_day:
            start_date, end_date = date.fromisoformat(start['date']), date.fromisoformat(end['date'])
            starts = datetime.combine(start_date, time.min, _zone(tz_name))
            ends = datetime.combine(end_date, time.min, _zone(end_tz))
        else:
            start_date = end_date = None
            starts = datetime.fromisoformat(start['dateTime'].replace('Z', '+00:00'))
            ends = datetime.fromisoformat(end['dateTime'].replace('Z', '+00:00'))
            if starts.tzinfo is None:
                starts = starts.replace(tzinfo=_zone(tz_name))
            if ends.tzinfo is None:
                ends = ends.replace(tzinfo=_zone(end_tz))
    except (KeyError, ValueError, TypeError) as exc:
        raise GoogleIntegrationError('У события Google некорректная дата; импорт остановлен без потери курсора.') from exc
    if ends <= starts:
        raise GoogleIntegrationError('У события Google конец не позже начала.')
    meeting = item.get('hangoutLink', '')
    if not meeting:
        for entry in item.get('conferenceData', {}).get('entryPoints', []):
            if entry.get('entryPointType') == 'video':
                meeting = entry.get('uri', '')
                break
    # A provider-side privacy change must also remove shared visibility locally.
    # Personal calendar sharing is never inferred from its Google ACL.
    visibility = event.visibility if event else 'private'
    if source.role == 'shared':
        visibility = 'private' if item.get('visibility') == 'private' else 'shared'
    elif item.get('visibility') == 'private' and visibility != 'busy_only':
        visibility = 'private'
    result = {
        'title': (item.get('summary') or 'Без названия')[:300],
        'description': item.get('description', ''), 'location': item.get('location', '')[:500],
        'starts_at': starts, 'ends_at': ends, 'timezone': tz_name, 'end_timezone': end_tz,
        'all_day': all_day, 'start_date': start_date, 'end_date': end_date,
        'blocks_time': item.get('transparency', 'opaque') != 'transparent',
        'status': 'confirmed', 'meeting_url': meeting, 'visibility': visibility,
    }
    # Provider metadata may preserve a workload override, but it cannot grant
    # visibility, bot access or membership in the family.
    properties = item.get('extendedProperties', {}).get('private', {})
    load = properties.get('family_assistant_counts_toward_load')
    if load in ('true', 'false', 'auto'):
        result['counts_toward_load'] = {'true': True, 'false': False, 'auto': None}[load]
    return result


def _snapshot(event):
    keys = ['title', 'description', 'location', 'starts_at', 'ends_at', 'timezone', 'end_timezone',
            'all_day', 'start_date', 'end_date', 'blocks_time', 'counts_toward_load', 'status', 'meeting_url', 'visibility']
    result = {}
    for key in keys:
        value = getattr(event, key)
        if isinstance(value, datetime):
            value = value.astimezone(dt_timezone.utc).isoformat()
        elif isinstance(value, date):
            value = value.isoformat()
        result[key] = value
    result['participants'] = sorted(str(pk) for pk in event.participants.values_list('pk', flat=True))
    return result


def _baseline_snapshot(snapshot, event=None):
    """Old saved baselines predate availability; migration kept them blocking.

    Keep an empty recovery baseline empty: it means no sync was acknowledged.
    Do not mutate persisted history merely to compare with the current schema.
    """
    if not snapshot:
        return snapshot
    baseline = {'blocks_time': True, 'counts_toward_load': None, **snapshot}
    if 'participants' not in baseline and event is not None:
        baseline['participants'] = sorted(str(pk) for pk in event.participants.values_list('pk', flat=True))
    return baseline


def _wire_snapshot(fields, event):
    result = _snapshot(event)
    for key, value in fields.items():
        if isinstance(value, datetime):
            value = value.astimezone(dt_timezone.utc).isoformat()
        elif isinstance(value, date):
            value = value.isoformat()
        result[key] = value
    return result


def _export_payload(event):
    body = {'summary': event.title, 'description': event.description, 'location': event.location,
            'visibility': 'default' if event.visibility == 'shared' else 'private',
            'transparency': 'opaque' if event.blocks_time else 'transparent',
            'extendedProperties': {'private': {
                'family_assistant_id': str(event.pk),
                'family_assistant_counts_toward_load': (
                    'auto' if event.counts_toward_load is None else str(event.counts_toward_load).lower()),
            }}}
    if event.all_day:
        if not event.start_date or not event.end_date:
            raise GoogleIntegrationError('Для события на весь день нужны начальная и конечная даты.')
        body['start'] = {'date': event.start_date.isoformat()}
        body['end'] = {'date': event.end_date.isoformat()}
    else:
        body['start'] = {'dateTime': event.starts_at.isoformat(), 'timeZone': event.timezone}
        body['end'] = {'dateTime': event.ends_at.isoformat(), 'timeZone': event.end_timezone}
    return body


def _patch_payload(event, remote):
    body = _export_payload(event)
    # Google merges nested PATCH objects. Omitting the old representation of
    # start/end leaves both date and dateTime after an all-day/timed conversion.
    # Explicit null removes those fields without replacing provider metadata.
    for boundary in ('start', 'end'):
        previous = remote.get(boundary) or {}
        obsolete = ('dateTime', 'timeZone') if event.all_day and previous.get('dateTime') else ()
        if not event.all_day:
            obsolete = ('date',)
        for field in obsolete:
            if field in previous:
                body[boundary][field] = None
    return body


def _missing_shared_participants(binding, event, *, newly_shared=False):
    """Repair only old Google-origin imports, never explicit app attendance.

    A shared calendar declares family attendance for its provider-origin events.
    Once initialized, the snapshot records participants, so a later explicit
    local edit can remove someone without the next import adding them again.
    """
    if binding.source.role != 'shared' or event.visibility != 'shared':
        return []
    if not newly_shared and 'participants' in (binding.snapshot or {}):
        return []
    # App-created records already have deliberately selected attendance. Google
    # IDs created by this adapter also cover conflict-resolution replacement IDs.
    if binding.external_id.startswith('fa') and len(binding.external_id) == 34:
        return []
    existing = set(event.participants.values_list('pk', flat=True))
    return list(Person.objects.filter(telegram_id__in=settings.ALLOWED_TELEGRAM_IDS).exclude(pk__in=existing))


def _participant_reminders(event, added):
    """Give new attendees future reminders without reviving disabled settings."""
    if event.status != 'confirmed':
        return
    latest = {}
    for reminder in event.reminders.filter(recipient_id=event.owner_id).order_by('-object_version', '-created_at'):
        latest.setdefault((reminder.offset_minutes, reminder.kind), reminder)
    configs = [(r.offset_minutes, r.kind) for r in latest.values()
               if r.status not in ('disabled', 'cancelled')]
    if not latest:
        configs = [(event_reminder_minutes(event), 'reminder')]
    now = timezone.now()
    for person in added:
        for offset, kind in configs:
            due_at = event_reminder_due(event, offset, kind)
            if due_at > now:
                Reminder.objects.get_or_create(event=event, recipient=person,
                    offset_minutes=offset, kind=kind, object_version=event.version,
                    defaults={'due_at': due_at})


def _repair_shared_participants(binding, event, *, new=False, newly_shared=False):
    added = _missing_shared_participants(binding, event, newly_shared=newly_shared)
    if not added:
        return 0
    before = _snapshot(event)
    old_version = event.version
    event.participants.add(*added)
    if not new:
        event.version += 1
        event.save(update_fields=['version', 'updated_at'])
    # This is a local attendance repair, not a pending remote content edit.
    # Advance an acknowledged version only if no other edit is outstanding.
    if binding.last_version == old_version:
        binding.last_version = event.version
    if binding.snapshot:
        binding.snapshot = {**binding.snapshot,
                            'participants': _snapshot(event)['participants']}
        binding.save(update_fields=['snapshot', 'last_version', 'updated_at'])
    _participant_reminders(event, added)
    AuditLog.objects.create(actor=binding.source.connection.person,
        action='google_participants_repair', target_type='event', target_id=event.pk,
        before=before, after=_snapshot(event))
    return len(added)


def repair_shared_calendar_participants(*, apply=False, source=None):
    """Offline, repeatable upgrade repair; default is a read-only count."""
    bindings = CalendarBinding.objects.filter(source__role='shared', event__visibility='shared')
    if source is not None:
        bindings = bindings.filter(source=source)
    result = {'events': 0, 'participants_added': 0}
    for identifier in bindings.values_list('pk', flat=True).iterator():
        with transaction.atomic():
            binding = CalendarBinding.objects.select_related('source__connection__person').select_for_update().get(pk=identifier)
            event = Event.objects.select_for_update().get(pk=binding.event_id)
            missing = _missing_shared_participants(binding, event)
            if not missing:
                continue
            result['events'] += 1
            result['participants_added'] += len(missing)
            if apply:
                _repair_shared_participants(binding, event)
    return result


def _private_attendance(event):
    """A provider privacy reduction must also remove private busy-time leaks."""
    removed = event.participants.exclude(pk=event.owner_id)
    event.reminders.filter(recipient_id__in=removed.values('pk'),
                           status__in=['pending', 'queued', 'paused']).update(status='cancelled')
    event.participants.set([event.owner_id])


def _mark_conflict(binding, event, remote):
    binding.conflict = {'local': _snapshot(event), 'remote': remote}
    binding.save(update_fields=['conflict', 'updated_at'])
    # Privacy reductions take effect even while conflicting content is awaiting
    # a choice. Choosing a content version must not keep private details shared.
    if remote.get('visibility') == 'private' and event.visibility == 'shared':
        event.visibility = 'private'
        event.version += 1
        _private_attendance(event)
    event.sync_status = 'conflict'
    event.save(update_fields=['sync_status', 'visibility', 'version', 'updated_at'])
    _notify(event.owner, f'Конфликт Google Calendar для события {event.id}. '
            'Обе версии сохранены. Выберите /resolve ' + str(event.id) + ' local или remote.',
            f'google-conflict-{binding.pk}-{event.version}-{hashlib.sha256(json.dumps(remote,sort_keys=True).encode()).hexdigest()[:12]}')


def _refresh_reminders(event):
    # One policy for local edits and Google imports: metadata edits preserve
    # delivered history, moved events get new schedules, explicit disables stay.
    from assistant.reminder_actions import refresh_event_reminders
    refresh_event_reminders(event, clone_pending=True)


def _valid_source(event, source):
    return (source.role == 'shared' and event.visibility == 'shared') or (
        source.role == 'personal' and source.connection.person_id == event.owner_id)


def _recover_binding(source, item):
    """Recover a deterministic app-created ID after POST succeeded but DB acknowledgement failed."""
    external_id = item.get('id', '')
    if not (external_id.startswith('fa') and len(external_id) == 34):
        return None
    try:
        event = Event.objects.get(pk=external_id[2:])
    except (Event.DoesNotExist, ValidationError):
        return None
    if not _valid_source(event, source):
        return None
    # This ID is scoped to source. We never trust arbitrary extendedProperties IDs.
    binding, _ = CalendarBinding.objects.get_or_create(source=source, external_id=external_id,
        defaults={'event': event, 'last_version': 0, 'snapshot': {}})
    return binding


def _import_item(source, item):
    external_id = item.get('id')
    if not external_id:
        return
    with transaction.atomic():
        binding = CalendarBinding.objects.select_for_update().filter(source=source, external_id=external_id).first()
        if not binding:
            binding = _recover_binding(source, item)
        if not binding and item.get('status') == 'cancelled':
            return
        if binding:
            event = Event.objects.select_for_update().get(pk=binding.event_id)
            if item.get('visibility') != 'private':
                _repair_shared_participants(binding, event)
            if binding.conflict:
                return  # Explicit resolution is required, never auto-overwrite.
            fields = _remote_fields(item, source, event)
            remote = _wire_snapshot(fields, event)
            local = _snapshot(event)
            baseline = _baseline_snapshot(binding.snapshot, event)
            remote_changed = item.get('etag', '') != binding.etag and remote != baseline
            local_changed = event.version != binding.last_version and local != baseline
            if remote_changed and local_changed and remote != local and binding.last_version:
                _mark_conflict(binding, event, remote)
                return
            if local_changed and (remote == baseline or item.get('etag', '') == binding.etag):
                return  # Local edit awaits outbound synchronization.
            if remote_changed and remote != local:
                before = local
                if 'location' in fields and fields['location'] != event.location:
                    # The remote text is a new choice, not the saved directory
                    # snapshot. Never keep an unrelated old place reference.
                    event.place_id = None
                    event.place_snapshot = {}
                for key, value in fields.items():
                    setattr(event, key, value)
                event.version += 1
                event.sync_status = 'synced'
                event.save()
                if event.visibility == 'private' and source.role == 'shared':
                    _private_attendance(event)
                elif before['visibility'] == 'private' and event.visibility == 'shared':
                    _repair_shared_participants(binding, event, new=True, newly_shared=True)
                _refresh_reminders(event)
                AuditLog.objects.create(actor=source.connection.person, action='google_import',
                    target_type='event', target_id=event.pk, before=before, after=_snapshot(event))
            else:
                event.sync_status = 'synced'
                event.save(update_fields=['sync_status', 'updated_at'])
        else:
            fields = _remote_fields(item, source)
            event = Event.objects.create(owner=source.connection.person,
                                         sync_status='synced', **fields)
            event.participants.add(source.connection.person)
            binding = CalendarBinding.objects.create(source=source, event=event, external_id=external_id)
            _repair_shared_participants(binding, event, new=True)
            offset = event_reminder_minutes(event)
            due_at = event_reminder_due(event, offset)
            if due_at > timezone.now():
                Reminder.objects.create(event=event, recipient=event.owner, offset_minutes=offset,
                    due_at=due_at, object_version=event.version)
        binding.etag = item.get('etag', '')
        binding.last_version = event.version
        binding.snapshot = _snapshot(event)
        binding.save()


def _cursor(source):
    if not source.sync_token:
        return {}
    try:
        result = json.loads(source.sync_token)
        return result if isinstance(result, dict) else {'token': source.sync_token}
    except json.JSONDecodeError:
        return {'token': source.sync_token}


def sync_source(source):
    cursor = _cursor(source)
    now = timezone.now()
    full = not cursor.get('token') or cursor.get('full_date') != now.date().isoformat()
    lower, upper = now - timedelta(days=PAST_DAYS), now + timedelta(days=FUTURE_DAYS)
    params = {'maxResults': 250, 'singleEvents': 'true', 'showDeleted': 'true'}
    if full:
        params.update(timeMin=lower.isoformat(), timeMax=upper.isoformat())
    else:
        params['syncToken'] = cursor['token']
    seen, next_token, pages = set(), None, 0
    while True:
        response = _request(source.connection, 'GET', _path(source), params=params, allowed=(410,))
        if response.status_code == 410:
            source.sync_token = ''
            source.save(update_fields=['sync_token', 'updated_at'])
            if full:
                raise GoogleIntegrationError('Google отклонил полный запрос синхронизации.')
            return sync_source(source)
        payload = response.json()
        for item in payload.get('items', []):
            seen.add(item.get('id'))
            _import_item(source, item)
        pages += 1
        if pages >= 100:
            raise GoogleIntegrationError('Слишком много событий для одного прогона. Уменьшите объём выбранного календаря.')
        page = payload.get('nextPageToken')
        if page:
            params['pageToken'] = page
        else:
            next_token = payload.get('nextSyncToken')
            break
    # Missing entries on full refresh may have moved outside the window or been
    # deleted. Fetch each ID rather than interpreting absence as deletion.
    if full:
        for binding in source.calendarbinding_set.select_related('event').filter(
                event__starts_at__gte=lower, event__starts_at__lt=upper, event__status='confirmed'):
            if binding.external_id in seen:
                continue
            response = _request(source.connection, 'GET', _path(source, binding.external_id), allowed=(404, 410))
            item = {'id': binding.external_id, 'status': 'cancelled', 'etag': 'deleted'} if response.status_code in (404, 410) else response.json()
            _import_item(source, item)
    source.sync_token = json.dumps({'token': next_token, 'full_date': now.date().isoformat() if full else cursor.get('full_date')})
    source.last_sync = now
    source.save(update_fields=['sync_token', 'last_sync', 'updated_at'])


def _select_destination(event):
    if event.visibility == 'shared':
        shared = CalendarSource.objects.select_related('connection__person').filter(
            role='shared', writable=True, connection__enabled=True).first()
        if shared:
            return shared
    return CalendarSource.objects.select_related('connection__person').filter(
        connection__person=event.owner, connection__enabled=True, role='personal', writable=True).first()


def _create_meet(body, event):
    body['conferenceData'] = {'createRequest': {
        'requestId': hashlib.sha256(('meet:' + str(event.pk)).encode()).hexdigest(),
        'conferenceSolutionKey': {'type': 'hangoutsMeet'},
    }}


def sync_event(event_id, create_meet=False):
    event = Event.objects.select_related('owner').get(pk=event_id)
    if event.status == 'proposed':
        return
    binding = event.calendar_bindings.select_related('source__connection__person').first()
    if binding and binding.conflict:
        return
    source = binding.source if binding else _select_destination(event)
    if not source:
        if create_meet:
            raise GoogleIntegrationError('Для создания Meet выберите доступный для записи Google-календарь.')
        return
    if not source.writable:
        if binding and event.version != binding.last_version:
            event.sync_status = 'error'
            event.save(update_fields=['sync_status', 'updated_at'])
            _notify(event.owner, f'Календарь события {event.id} доступен только для чтения. Изменение сохранено только в боте.', f'google-readonly-{event.id}-{event.version}')
        return
    if not _valid_source(event, source):
        if binding.snapshot.get('visibility') != 'shared':
            # A private Google-origin item may exist in a shared calendar. Keep
            # its local copy private, but do not delete or write it automatically.
            if _snapshot(event) != _baseline_snapshot(binding.snapshot, event):
                Event.objects.filter(pk=event.pk).update(sync_status='error')
                _notify(event.owner, f'Личное событие {event.id} находится в общем Google-календаре. '
                        'Изменение сохранено в боте; перенесите событие в личный календарь Google.',
                        f'google-private-source-{event.id}-{event.version}')
            if create_meet:
                raise GoogleIntegrationError('Перенесите личное событие из общего Google-календаря в личный для создания Meet.')
            return
        # Privacy changed: erase former common-calendar copy before moving.
        response = _request(source.connection, 'DELETE', _path(source, binding.external_id),
                            etag=binding.etag, params={'sendUpdates': 'none'}, allowed=(404, 410, 412))
        if response.status_code == 412:
            _mark_conflict(binding, event, {'reason': 'privacy_move_remote_changed'})
            return
        binding.delete()
        binding = None
        source = _select_destination(event)
        if not source:
            Event.objects.filter(pk=event.pk).update(sync_status='local')
            return
    external_id = binding.external_id if binding else 'fa' + event.pk.hex
    remote_response = _request(source.connection, 'GET', _path(source, external_id), allowed=(404, 410))
    remote = None if remote_response.status_code in (404, 410) else remote_response.json()
    if binding and remote:
        remote_fields = _remote_fields(remote, source, event)
        remote_snap = _wire_snapshot(remote_fields, event)
        if remote.get('etag') != binding.etag:
            baseline = _baseline_snapshot(binding.snapshot, event)
            local_changed = _snapshot(event) != baseline
            remote_changed = remote_snap != baseline
            if local_changed and remote_changed and remote_snap != _snapshot(event):
                _mark_conflict(binding, event, remote_snap)
                return
            if remote_changed and not local_changed:
                _import_item(source, remote)
                event.refresh_from_db()
                binding.refresh_from_db()
                if not _valid_source(event, source):
                    return
    if event.status == 'cancelled':
        if remote:
            response = _request(source.connection, 'DELETE', _path(source, external_id),
                etag=remote.get('etag'), params={'sendUpdates': 'none'}, allowed=(404, 410, 412))
            if response.status_code == 412:
                _mark_conflict(binding, event, {'reason': 'concurrent_delete'})
                return
        if binding:
            binding.snapshot = _snapshot(event)
            binding.last_version = event.version
            binding.save()
        Event.objects.filter(pk=event.pk).update(sync_status='synced')
        return
    if binding and not remote and binding.last_version:
        # An external deletion wins only when there is no pending local edit.
        if event.version != binding.last_version:
            _mark_conflict(binding, event, {'status': 'cancelled'})
        else:
            _import_item(source, {'id': external_id, 'status': 'cancelled', 'etag': 'deleted'})
        return
    body = _patch_payload(event, remote) if remote else _export_payload(event)
    conference = (remote or {}).get('conferenceData', {})
    need_write = not remote or not binding or _snapshot(event) != _baseline_snapshot(binding.snapshot, event)
    if create_meet and not event.meeting_url and not conference:
        _create_meet(body, event)
        need_write = True
    if need_write:
        params = {'sendUpdates': 'none', 'conferenceDataVersion': 1}
        if remote:
            response = _request(source.connection, 'PATCH', _path(source, external_id),
                                body=body, params=params, etag=remote.get('etag'), allowed=(412,))
        else:
            body['id'] = external_id
            response = _request(source.connection, 'POST', _path(source), body=body, params=params, allowed=(409,))
            if response.status_code == 409:
                response = _request(source.connection, 'GET', _path(source, external_id))
        if response.status_code == 412:
            if binding:
                _mark_conflict(binding, event, {'reason': 'concurrent_write'})
            raise GoogleRetryableError('Событие изменилось в Google во время записи; повторите синхронизацию.')
        remote = response.json()
    fields = _remote_fields(remote, source, event)
    if fields.get('meeting_url'):
        Event.objects.filter(pk=event.pk).update(meeting_url=fields['meeting_url'])
        event.meeting_url = fields['meeting_url']
    event.sync_status = 'synced'
    event.save(update_fields=['sync_status', 'updated_at'])
    binding, _ = CalendarBinding.objects.update_or_create(source=source, event=event,
        defaults={'external_id': external_id, 'etag': remote.get('etag', ''), 'last_version': event.version,
                  'snapshot': _snapshot(event), 'conflict': {}})
    status = remote.get('conferenceData', {}).get('createRequest', {}).get('status', {}).get('statusCode')
    if create_meet and not event.meeting_url:
        if status == 'failure':
            raise GoogleIntegrationError('Google не смог создать Meet. Проверьте, разрешены ли конференции для календаря.')
        raise GoogleRetryableError('Google Meet ещё создаётся; проверка будет повторена.')


def resolve_conflict(person, event_id, choice):
    """Explicit owner resolution. Bot /resolve command may call this helper."""
    if choice not in ('local', 'remote'):
        raise GoogleIntegrationError('Выберите local или remote.')
    event = Event.objects.get(pk=event_id, owner=person)
    binding = event.calendar_bindings.select_related('source__connection__person').filter(~Q(conflict={})).first()
    if not binding:
        raise GoogleIntegrationError('Конфликт для события не найден.')
    response = _request(binding.source.connection, 'GET', _path(binding.source, binding.external_id), allowed=(404, 410))
    remote = {'id': binding.external_id, 'status': 'cancelled', 'etag': 'deleted'} if response.status_code in (404, 410) else response.json()
    if choice == 'remote':
        binding.conflict = {}
        binding.snapshot = _snapshot(event)
        binding.last_version = event.version
        binding.etag = ''
        binding.save()
        _import_item(binding.source, remote)
    else:
        binding.conflict = {}
        binding.etag = remote.get('etag', '')
        binding.snapshot = _wire_snapshot(_remote_fields(remote, binding.source, event), event)
        binding.last_version = event.version - 1
        binding.save()
        if remote.get('status') == 'cancelled':
            # A deleted provider ID must not be reused. Persist replacement ID before
            # POST, so a lost acknowledgement still has a deterministic retry target.
            binding.external_id = 'fa' + uuid.uuid4().hex
            binding.etag = ''
            binding.last_version = 0
            binding.snapshot = {}
            binding.save()
        sync_event(event.pk)
    return Event.objects.get(pk=event.pk)


def sync_all():
    failures = []
    for source in CalendarSource.objects.select_related('connection__person').filter(connection__enabled=True):
        try:
            sync_source(source)
        except GoogleIntegrationError as exc:
            failures.append(exc)
    for event in Event.objects.exclude(status='proposed').filter(
            Q(sync_status__in=['pending', 'error']) | Q(calendar_bindings__isnull=False)).distinct():
        try:
            sync_event(event.pk)
        except GoogleIntegrationError as exc:
            failures.append(exc)
    if failures:
        retryable_count = sum(isinstance(exc, GoogleRetryableError) for exc in failures)
        representative = next((exc for exc in failures if not isinstance(exc, GoogleRetryableError)), failures[0])
        error_type = GoogleRetryableError if retryable_count else GoogleIntegrationError
        raise error_type(f'Не все операции синхронизации Google выполнены ({len(failures)}).',
                         status_code=representative.status_code, reason=representative.reason,
                         operation=representative.operation or 'calendar_sync',
                         failure_count=len(failures), retryable_count=retryable_count) from representative
