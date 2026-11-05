from datetime import datetime,timedelta,timezone as dtz
from unittest.mock import patch
import pytest
from django.core.exceptions import PermissionDenied,ValidationError
from django.utils import timezone
from assistant.models import Event,Task,ChecklistItem,IncomingUpdate,Job,ActionRequest,Document
from assistant import services as s
from assistant.queue import enqueue,claim,finish
from assistant.runtime import schedule_tick,handle_job


def event(user,**kw):
    start=timezone.now()+timedelta(days=2)
    return s.create_event(user,title='Встреча',starts_at=start,ends_at=start+timedelta(hours=1),**kw)


def test_private_busy_group(people):
    a,b=people;e=event(a)
    assert not s.can_view(e,b)
    assert not s.visible(Event.objects.all(),b).exists()
    e.visibility='busy_only';e.save()
    assert s.safe_event(e,b)['title']=='Занят'
    assert 'location' not in s.safe_event(e,b)
    assert s.safe_event(e,b,True)=={}
    assert list(s.visible(Event.objects.all(),a,True,True)) == [e]
    assert s.safe_event(e,a,True)['title'] == e.title


@pytest.mark.parametrize('organiser', [0, 1])
def test_either_attendee_can_book_for_both_but_edits_still_need_approval(people, organiser):
    a, b = people[organiser], people[1-organiser]
    e = event(a, visibility='shared', participants=[str(a.pk), str(b.pk)])
    assert e.status=='confirmed'
    assert set(e.participants.all()) == {a, b}
    assert set(e.reminders.filter(status='pending').values_list('recipient_id', flat=True)) == {a.pk, b.pk}
    assert not ActionRequest.objects.filter(target_id=e.pk).exists()
    assert Job.objects.get(kind='google_event').payload['event_id'] == str(e.pk)
    notice = Job.objects.get(kind='telegram_send')
    assert notice.payload['chat_id'] == b.chat_id and not notice.payload.get('reply_markup')
    assert a.name in notice.payload['text'] and e.title in notice.payload['text']
    assert notice.payload['permission_refs'] == [{'kind': 'event', 'id': str(e.pk)}]
    req2=s.update_event(a,e.id,{'starts_at':(e.starts_at+timedelta(hours=1)).isoformat(),'ends_at':(e.ends_at+timedelta(hours=1)).isoformat()})
    assert isinstance(req2,ActionRequest)
    with pytest.raises(PermissionDenied):s.approve_action(a,req2.id)
    before=e.starts_at;s.approve_action(b,req2.id);e.refresh_from_db()
    assert e.starts_at==before+timedelta(hours=1)
    assert all(r.object_version==e.version for r in e.reminders.all())
    version=e.version
    s.approve_action(b,req2.id);e.refresh_from_db();assert e.version==version


def test_booking_only_for_the_other_person_still_requires_their_agreement(people):
    a,b=people
    e=event(a,visibility='shared',participants=[str(b.pk)])
    assert e.status=='proposed' and e.reminders.get().status=='paused'
    req=ActionRequest.objects.get(target_id=e.pk,kind='confirm_event',approver=b)
    assert Job.objects.filter(dedup_key=f'action:{req.pk}').exists()
    assert not Job.objects.filter(dedup_key__startswith='joint-event:').exists()
    s.approve_action(b,req.pk)
    e.refresh_from_db()
    assert e.status=='confirmed' and e.reminders.get().status=='pending'


@pytest.mark.parametrize('access_revoked', [False, True])
def test_joint_notice_rechecks_privacy_and_is_sent_once(people, access_revoked):
    a,b=people
    e=event(a,visibility='shared',participants=[a.pk,b.pk])
    notice=Job.objects.get(kind='telegram_send')
    if access_revoked:
        e.participants.remove(b)
        s.share_entity(a,'event',e.pk,'private')
    with patch('assistant.runtime._tg',return_value={'message_id':1}) as telegram:
        handle_job(notice)
        handle_job(notice)
    telegram.assert_called_once()
    method,out=telegram.call_args.args
    assert method=='sendMessage' and out['chat_id']==b.chat_id
    assert (e.title in out['text']) is (not access_revoked)


def test_shared_event_is_editable_even_without_participation(people):
    a,b=people;e=event(a,visibility='shared')
    assert s.can_view(e,b) and s.can_edit(e,b)
    assert list(e.participants.all())==[a] and not Job.objects.filter(kind='telegram_send').exists()
    request=s.update_event(b,e.id,{'title':'Обновлено'})
    assert request.initiator==b and request.approver==a
    s.approve_action(a,request.pk)
    e.refresh_from_db()
    assert e.title=='Обновлено' and e.owner==a


def test_checklist_each_and_set_not_toggle(people):
    a,b=people;t=s.create_trip(a,title='Поездка',visibility='shared',participants=[str(a.id),str(b.id)])
    c=s.make_checklist(a,'trip',t.id,'Паспорт каждому, Камера')
    assert c.items.count()==3
    item=c.items.get(assignee=b)
    s.set_checklist_item(b,item.id,True);item.refresh_from_db();v=item.version
    s.set_checklist_item(b,item.id,True);item.refresh_from_db();assert item.version==v
    s.set_checklist_item(b,item.id,False);item.refresh_from_db();assert item.checked_at is None


def test_documents_no_link_acl_widening(people,tmp_path,settings):
    a,b=people;settings.MEDIA_ROOT=tmp_path;e=event(a,visibility='shared')
    d=s.save_document(a,'../../ticket.pdf',b'%PDF-1.0\nfake','application/pdf')
    assert d.original_name=='ticket.pdf'
    s.link_document(a,d.id,'event',e.id)
    with pytest.raises(PermissionDenied):s.get_entity('document',d.id,b)
    same=s.save_document(a,'other.pdf',b'%PDF-1.0\nfake','application/pdf');assert same.id==d.id


def test_identical_image_with_a_different_caption_creates_a_private_document(people, tmp_path, settings):
    owner, spouse = people
    settings.MEDIA_ROOT = tmp_path
    args = (owner, 'poster.jpg', b'image', 'image/jpeg')
    poster = s.save_document(*args, metadata={'telegram_caption': 'invented by OCR', 'title': 'Lyceum'})
    assert poster.metadata == {'title': 'Lyceum'}
    s.share_entity(owner, 'document', poster.pk, 'shared')

    caption = ' Личная заметка к афише\nТолько для меня. '
    annotated = s.save_document(*args, telegram_caption=caption)
    assert annotated.pk != poster.pk
    assert annotated.telegram_caption == caption
    assert s.save_document(*args, telegram_caption=caption).pk == annotated.pk
    assert s.save_document(*args).pk == poster.pk
    assert s.get_entity('document', poster.pk, spouse).telegram_caption == ''
    with pytest.raises(PermissionDenied):
        s.get_entity('document', annotated.pk, spouse)


def test_stale_reminder_does_not_send(people):
    a,b=people;e=event(a)
    r=e.reminders.first()
    j=enqueue('reminder',{'reminder_id':str(r.id),'object_version':e.version},'test-reminder',queue='notifications')
    r.status='queued';r.save();e.version+=1;e.save()
    with patch('assistant.runtime._tg') as tg:handle_job(j);tg.assert_not_called()


def test_task_assignment_and_dependencies(people):
    a,b=people
    with pytest.raises(ValidationError):s.create_task(a,title='Для Анны',assignee_id=str(b.id))
    dep=s.create_task(a,title='Сначала')
    task=s.create_task(a,title='Потом',dependencies=[str(dep.id)])
    with pytest.raises(ValidationError):s.complete_task(a,task.id)
    s.complete_task(a,dep.id);s.complete_task(a,task.id);task.refresh_from_db();assert task.status=='done'


def test_explicit_timezone(people):
    a,_=people
    e=s.create_event(a,title='Токио',starts_at='2026-11-20T15:00',ends_at='2026-11-20T16:00',timezone='Asia/Tokyo')
    assert e.starts_at.utcoffset()==timedelta(hours=9)


def test_queue_dedup_lease_and_fenced_finish(db):
    j=enqueue('test',{},'unique')
    assert enqueue('test',{},'unique').id==j.id
    first=claim('service');assert first and claim('service') is None
    Job.objects.filter(pk=j.pk).update(locked_until=timezone.now()-timedelta(seconds=1))
    second=claim('service');assert second.lock_token!=first.lock_token
    assert not finish(first)
    assert finish(second)


def test_webhook_auth_dedup(client,settings,db):
    settings.TELEGRAM_WEBHOOK_SECRET='secret'
    data={'update_id':42,'message':{'text':'x'}}
    assert client.post('/telegram/webhook/',data,content_type='application/json').status_code==403
    for _ in range(2):
        assert client.post('/telegram/webhook/',data,content_type='application/json',HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN='secret').status_code==200
    assert IncomingUpdate.objects.count()==1 and Job.objects.count()==1
