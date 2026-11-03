from datetime import timedelta
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock

import httpx
import pytest
from cryptography.fernet import Fernet
from django.utils import timezone

from assistant.integrations import google
from assistant.models import (CalendarConnection, CalendarSource, CalendarBinding,
                              Event, OAuthState, Person, Reminder)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def configured(settings, monkeypatch):
    settings.TOKEN_ENCRYPTION_KEY = Fernet.generate_key().decode()
    settings.GOOGLE_CLIENT_ID = 'test-client'
    settings.GOOGLE_CLIENT_SECRET = 'test-secret'
    settings.GOOGLE_REDIRECT_URI = 'https://example.org/oauth/google/callback/'
    monkeypatch.setattr(google, '_notify', Mock())


@pytest.fixture
def person():
    return Person.objects.create(name='Борис', telegram_id=1, chat_id=1)


@pytest.fixture
def source(person):
    tokens = {'access_token': 'access-secret', 'refresh_token': 'refresh-secret',
              'expires_at': timezone.now().timestamp() + 3600, '_account_id': 'boris@example.org'}
    conn = CalendarConnection.objects.create(person=person, encrypted_tokens=google._encrypt(tokens))
    return CalendarSource.objects.create(connection=conn, calendar_id='boris@example.org', writable=True)


@pytest.fixture
def event(person):
    start = timezone.now().replace(microsecond=0) + timedelta(days=1)
    e = Event.objects.create(owner=person, title='Встреча', starts_at=start,
                             ends_at=start + timedelta(hours=1), sync_status='pending')
    e.participants.add(person)
    return e


def response(code, payload=None):
    return httpx.Response(code, json=payload or {})


def remote_for(event, etag='v1', **extra):
    obj = google._export_payload(event)
    obj.update(id='fa' + event.pk.hex, etag=etag, status='confirmed')
    obj.update(extra)
    return obj


def bind(source, event, etag='v1'):
    return CalendarBinding.objects.create(source=source, event=event,
        external_id='fa' + event.pk.hex, etag=etag, last_version=event.version,
        snapshot=google._snapshot(event))


def test_oauth_state_is_hashed_one_time_and_tokens_encrypted(person, monkeypatch):
    url = google.authorization_url(person)
    state = parse_qs(urlparse(url).query)['state'][0]
    row = OAuthState.objects.get(person=person)
    assert row.state_hash != state
    post = Mock(return_value=response(200, {'access_token':'access', 'refresh_token':'refresh', 'expires_in':3600}))
    monkeypatch.setattr(httpx, 'post', post)
    monkeypatch.setattr(httpx, 'get', Mock(return_value=response(200, {'id':'boris@example.org'})))
    conn = google.complete_oauth(state, 'auth-code')
    assert 'refresh' not in conn.encrypted_tokens
    assert google._decrypt(conn)['refresh_token'] == 'refresh'
    with pytest.raises(google.GoogleIntegrationError, match='использована'):
        google.complete_oauth(state, 'auth-code')
    assert post.call_count == 1


def test_expired_state_rejected_before_network(person, monkeypatch):
    state = parse_qs(urlparse(google.authorization_url(person)).query)['state'][0]
    OAuthState.objects.update(expires_at=timezone.now()-timedelta(seconds=1))
    post = Mock()
    monkeypatch.setattr(httpx, 'post', post)
    with pytest.raises(google.GoogleIntegrationError, match='истекла'):
        google.complete_oauth(state, 'code')
    post.assert_not_called()


def test_reauthorization_same_account_keeps_bindings(source, event, monkeypatch):
    binding = bind(source, event)
    state = parse_qs(urlparse(google.authorization_url(source.connection.person)).query)['state'][0]
    monkeypatch.setattr(httpx, 'post', Mock(return_value=response(200, {'access_token':'new', 'refresh_token':'new-refresh'})))
    monkeypatch.setattr(httpx, 'get', Mock(return_value=response(200, {'id':'boris@example.org'})))
    google.complete_oauth(state, 'code')
    assert CalendarBinding.objects.filter(pk=binding.pk).exists()


def test_readonly_calendar_cannot_be_selected_for_writes(source, monkeypatch):
    monkeypatch.setattr(google, '_request', Mock(return_value=response(200, {'id':'readonly','accessRole':'reader'})))
    with pytest.raises(google.GoogleIntegrationError, match='права записи'):
        google.select_calendar(source.connection.person, 'readonly')
    assert not CalendarSource.objects.filter(calendar_id='readonly').exists()


def test_private_google_event_in_shared_calendar_stays_private(source, event):
    source.role='shared'; source.save()
    item=remote_for(event, id='external1', visibility='private')
    google._import_item(source,item)
    imported=CalendarBinding.objects.get(external_id='external1').event
    assert imported.visibility=='private'
    assert not imported.editors.exists()
    assert list(imported.participants.values_list('pk',flat=True))==[source.connection.person.pk]


def test_simultaneous_edits_preserve_both_versions(source, event):
    binding=bind(source,event)
    external=remote_for(event, etag='v2', summary='Google change')
    event.title='Local change'; event.version+=1; event.save()
    google._import_item(source,external)
    event.refresh_from_db(); binding.refresh_from_db()
    assert event.title=='Local change'
    assert event.sync_status=='conflict'
    assert binding.conflict['remote']['title']=='Google change'
    assert binding.conflict['local']['title']=='Local change'


def test_remote_move_replaces_pending_reminder(source,event):
    binding=bind(source,event)
    old=Reminder.objects.create(event=event,recipient=event.owner,
        due_at=event.starts_at-timedelta(minutes=15),offset_minutes=15)
    external=remote_for(event,etag='v2')
    external['start']['dateTime']=(event.starts_at+timedelta(hours=2)).isoformat()
    external['end']['dateTime']=(event.ends_at+timedelta(hours=2)).isoformat()
    google._import_item(source,external)
    event.refresh_from_db();old.refresh_from_db();binding.refresh_from_db()
    assert old.status=='cancelled'
    new=event.reminders.get(status='pending')
    assert new.object_version==event.version==2
    assert new.due_at==event.starts_at-timedelta(minutes=15)
    assert binding.last_version==2


def test_private_event_never_exported_to_shared_only(source,event,monkeypatch):
    source.role='shared';source.save()
    request=Mock()
    monkeypatch.setattr(google,'_request',request)
    google.sync_event(event.pk)
    request.assert_not_called()
    assert not event.calendar_bindings.exists()


def test_event_creation_uses_stable_id_and_second_sync_does_not_insert(source,event,monkeypatch):
    remote=remote_for(event)
    state={'exists':False}
    methods=[]
    def request(conn,method,path,**kwargs):
        methods.append(method)
        if method=='GET':
            return response(200,remote) if state['exists'] else response(404)
        if method=='POST':
            assert kwargs['body']['id']=='fa'+event.pk.hex
            assert 'attendees' not in kwargs['body']
            assert kwargs['params']['sendUpdates']=='none'
            state['exists']=True
            return response(200,remote)
        raise AssertionError(method)
    monkeypatch.setattr(google,'_request',request)
    google.sync_event(event.pk)
    google.sync_event(event.pk)
    assert methods.count('POST')==1
    assert CalendarBinding.objects.filter(event=event).count()==1


def test_remote_deletion_does_not_destroy_event_linked_data(source,event):
    binding=bind(source,event)
    google._import_item(source,{'id':binding.external_id,'status':'cancelled','etag':'deleted'})
    event.refresh_from_db()
    assert event.status=='cancelled'
    assert CalendarBinding.objects.filter(pk=binding.pk).exists()


def test_expired_cursor_restarts_full_sync(source,monkeypatch):
    source.sync_token=google.json.dumps({'token':'expired','full_date':timezone.now().date().isoformat()});source.save()
    calls=[]
    def request(conn,method,path,**kwargs):
        calls.append(kwargs['params'])
        if len(calls)==1:
            return response(410)
        return response(200,{'items':[],'nextSyncToken':'fresh'})
    monkeypatch.setattr(google,'_request',request)
    google.sync_source(source)
    source.refresh_from_db()
    assert calls[0]['syncToken']=='expired'
    assert 'timeMin' not in calls[0]
    assert 'timeMin' in calls[1] and 'syncToken' not in calls[1]
    assert google._cursor(source)['token']=='fresh'


def test_meet_pending_retry_reuses_event(source,event,monkeypatch):
    state={'created':False,'ready':False,'posts':0}
    def request(conn,method,path,**kwargs):
        if method=='GET' and not state['created']:
            return response(404)
        if method=='POST':
            state['created']=True;state['posts']+=1
            assert kwargs['body']['conferenceData']['createRequest']['requestId']
        result=remote_for(event)
        if state['ready']:
            result['hangoutLink']='https://meet.google.com/abc-defg-hij'
            result['conferenceData']={'createRequest':{'status':{'statusCode':'success'}}}
        else:
            result['conferenceData']={'createRequest':{'status':{'statusCode':'pending'}}}
        return response(200,result)
    monkeypatch.setattr(google,'_request',request)
    with pytest.raises(google.GoogleRetryableError,match='ещё создаётся'):
        google.sync_event(event.pk,create_meet=True)
    state['ready']=True
    google.sync_event(event.pk,create_meet=True)
    event.refresh_from_db()
    assert event.meeting_url=='https://meet.google.com/abc-defg-hij'
    assert state['posts']==1


def test_conflict_resolution_remote_updates_local(source,event,monkeypatch):
    binding=bind(source,event)
    external=remote_for(event,etag='v2',summary='Remote wins')
    event.title='Local';event.version+=1;event.save()
    google._import_item(source,external)
    monkeypatch.setattr(google,'_request',Mock(return_value=response(200,external)))
    google.resolve_conflict(event.owner,event.pk,'remote')
    event.refresh_from_db();binding.refresh_from_db()
    assert event.title=='Remote wins'
    assert not binding.conflict


def test_invalid_grant_persists_disabled_state(source,monkeypatch):
    conn=source.connection
    tokens=google._decrypt(conn);tokens['expires_at']=0
    conn.encrypted_tokens=google._encrypt(tokens);conn.save()
    monkeypatch.setattr(httpx,'post',Mock(return_value=response(400,{'error':'invalid_grant'})))
    with pytest.raises(google.GoogleIntegrationError,match='повторного подключения'):
        google._access_token(conn)
    conn.refresh_from_db()
    assert conn.enabled is False


def test_private_import_in_shared_calendar_is_not_deleted(source,event,monkeypatch):
    source.role='shared';source.save()
    google._import_item(source,remote_for(event,id='private-external',visibility='private'))
    imported=CalendarBinding.objects.get(external_id='private-external').event
    request=Mock()
    monkeypatch.setattr(google,'_request',request)
    google.sync_event(imported.pk)
    request.assert_not_called()
    assert CalendarBinding.objects.filter(event=imported).exists()


def test_google_privacy_reduction_hides_existing_shared_event(source,event):
    source.role='shared';source.save()
    event.visibility='shared';event.save()
    bind(source,event)
    google._import_item(source,remote_for(event,etag='v2',visibility='private'))
    event.refresh_from_db()
    assert event.visibility=='private'


def test_google_privacy_reduction_hides_conflicting_shared_event(source,event):
    source.role='shared';source.save()
    event.visibility='shared';event.save()
    binding=bind(source,event)
    external=remote_for(event,etag='v2',visibility='private',summary='Remote')
    event.title='Local';event.version+=1;event.save()
    google._import_item(source,external)
    event.refresh_from_db();binding.refresh_from_db()
    assert event.visibility=='private'
    assert event.sync_status=='conflict'
    assert binding.conflict['local']['title']=='Local'
    assert binding.conflict['remote']['title']=='Remote'


@pytest.mark.parametrize('status',['sent','missed','summarized'])
def test_remote_move_recreates_already_delivered_or_missed_reminder(source,event,status):
    bind(source,event)
    old=Reminder.objects.create(event=event,recipient=event.owner,
        due_at=event.starts_at-timedelta(minutes=30),offset_minutes=30,status=status)
    external=remote_for(event,etag='v2')
    external['start']['dateTime']=(event.starts_at+timedelta(hours=2)).isoformat()
    external['end']['dateTime']=(event.ends_at+timedelta(hours=2)).isoformat()
    google._import_item(source,external)
    event.refresh_from_db();old.refresh_from_db()
    assert old.status==status
    new=event.reminders.get(status='pending')
    assert new.object_version==event.version==2
    assert new.due_at==event.starts_at-timedelta(minutes=30)


def test_remote_move_does_not_restore_latest_cancelled_reminder(source,event):
    event.version=2;event.save()
    bind(source,event)
    Reminder.objects.create(event=event,recipient=event.owner,object_version=1,
        due_at=event.starts_at-timedelta(minutes=15),status='sent')
    cancelled=Reminder.objects.create(event=event,recipient=event.owner,object_version=2,
        due_at=event.starts_at-timedelta(minutes=15),status='cancelled')
    external=remote_for(event,etag='v2')
    external['start']['dateTime']=(event.starts_at+timedelta(hours=2)).isoformat()
    external['end']['dateTime']=(event.ends_at+timedelta(hours=2)).isoformat()
    google._import_item(source,external)
    cancelled.refresh_from_db()
    assert cancelled.status=='cancelled'
    assert not event.reminders.filter(status='pending').exists()


def test_remote_move_creates_one_reminder_for_configuration_history(source,event):
    event.version=3;event.save()
    bind(source,event)
    for version,status in [(1,'sent'),(2,'missed'),(3,'summarized')]:
        Reminder.objects.create(event=event,recipient=event.owner,object_version=version,
            due_at=event.starts_at-timedelta(minutes=15),status=status)
    external=remote_for(event,etag='v2')
    external['start']['dateTime']=(event.starts_at+timedelta(hours=2)).isoformat()
    external['end']['dateTime']=(event.ends_at+timedelta(hours=2)).isoformat()
    google._import_item(source,external)
    assert event.reminders.filter(status='pending').count()==1


def test_oauth_callback_shows_actual_calendar_command(monkeypatch,rf):
    from assistant import google_views
    monkeypatch.setattr(google_views,'complete_oauth',Mock())
    result=google_views.oauth_callback(rf.get('/oauth/google/callback/',{'state':'s','code':'c'}))
    text=result.content.decode()
    assert '/calendar ID | personal | rw' in text
    assert '/calendar ID | personal | ro' in text
    assert result['Cache-Control']=='no-store'
