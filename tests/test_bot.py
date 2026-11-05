"""Transport security, confirmation and durable processing tests; no live API calls."""
import asyncio
from datetime import timedelta
from unittest.mock import patch
import pytest
from aiogram.types import File, Update, WebhookInfo
from django.core.management import call_command
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from assistant import bot, services
from assistant.models import ActionRequest, ChecklistItem, Conversation, Document, Event, IncomingUpdate, Job, Project, Task, TripSegment

pytestmark=pytest.mark.django_db


@pytest.fixture(autouse=True)
def legacy_bot_mode(settings):
    # This module retains coverage of the opt-in legacy proposal path.
    settings.AGENT_ENABLED=False


def update(identifier=1,text='/help',user=1001,chat=None,shared=False,callback=None):
    message={'message_id':identifier,'from':{'id':user,'is_bot':False},'chat':{'id':chat or user,'type':'supergroup' if shared else 'private'}}
    payload={'update_id':identifier}
    if callback:
        payload['callback_query']={'id':f'callback-{identifier}','from':message['from'],'message':message,'data':callback}
    else:
        message['text']=text
        payload['message']=message
    return payload


def process_sync(payload,person,prepared=None):
    return bot._finish_sync(payload,bot._identity(payload),person.pk if person else None,prepared)


def replies():
    return list(Job.objects.filter(kind='telegram_send').order_by('created_at'))


def event(person,**kwargs):
    now=timezone.now()+timedelta(days=2)
    return services.create_event(person,title='Встреча',starts_at=now,ends_at=now+timedelta(hours=1),**kwargs)


def test_identity_rejects_wrong_group_and_private_sender(settings):
    settings.TELEGRAM_GROUP_ID=-500
    assert bot._identity(update(shared=True,chat=-499)) is None
    assert bot._identity(update(user=1001,chat=1002)) is None
    assert bot._identity(update(shared=True,chat=-500))[2] is True


@pytest.mark.django_db(transaction=True)
def test_polled_start_reaches_telegram_send(people, settings):
    person, _ = people
    settings.TELEGRAM_BOT_TOKEN = '1234:test'
    payload = update(text='/start', user=person.telegram_id)
    payload['message']['from']['first_name'] = 'Борис'
    payload['message']['date'] = 1700000000
    telegram_update = Update.model_validate(payload)
    no_webhook = WebhookInfo(url='', has_custom_certificate=False, pending_update_count=0)
    with patch.object(bot.Bot, 'get_webhook_info', return_value=no_webhook), \
         patch.object(bot.Bot, 'get_updates', side_effect=[[telegram_update], asyncio.CancelledError()]) as poll:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(bot.poll_forever())
    assert poll.await_args.kwargs['offset'] == telegram_update.update_id + 1
    stored = IncomingUpdate.objects.get(update_id=telegram_update.update_id)
    assert stored.payload['message']['from']['id'] == person.telegram_id

    call_command('runworker', queue='chat', once=True)
    with patch('assistant.runtime._tg', return_value={'message_id': 123}) as send:
        call_command('runworker', queue='notifications', once=True)
    send.assert_called_once_with('sendMessage', {'chat_id': person.telegram_id, 'text': bot.HELP})


def test_legacy_poll_senders_respect_private_chat_and_callback_actor(people):
    person, spouse = people
    payload = update(text='/start')
    message = payload['message']
    message['from_user'] = message.pop('from')
    assert bot._find_person(bot._identity(payload)) == person

    # A callback belongs to the user who pressed it, not to the message author.
    callback = {'from_user': {'id': spouse.telegram_id}, 'message': message}
    assert bot._identity({'callback_query': callback}) is None
    message['chat']['id'] = spouse.telegram_id
    assert bot._find_person(bot._identity({'callback_query': callback})) == spouse
    del callback['from_user']
    message['chat']['id'] = person.telegram_id
    assert bot._identity({'callback_query': callback}) is None


def test_onboarding_id_does_not_grant_access(people):
    stranger=update(text='/id',user=9000)
    process_sync(stranger,None)
    assert '9000' in replies()[0].payload['text']
    assert 'actor_id' not in replies()[0].payload
    process_sync(update(2,text='/task 偷看',user=9000),None)
    assert not Task.objects.exists()
    assert len(replies())==1


def test_update_is_persisted_once_and_mutation_commits_once(people):
    person,_=people
    payload=update(text='/task Купить билеты | - | 25 | private')
    assert bot.ingest_update(payload)
    assert not bot.ingest_update(payload)
    process_sync(payload,person)
    process_sync(payload,person)
    assert Task.objects.filter(title='Купить билеты').count()==1
    assert Job.objects.filter(kind='telegram_update').count()==1
    assert len(replies())==1
    assert replies()[0].payload['actor_id']==str(person.pk)
    assert IncomingUpdate.objects.get(update_id=1).payload['_bot_processed']


def test_expected_error_rolls_back_partial_mutation(people):
    person,_=people
    def bad_command(*args,**kwargs):
        Task.objects.create(owner=person,assignee=person,title='Must roll back')
        raise ValidationError('Invalid final field')
    with patch.object(bot,'dispatch_command',side_effect=bad_command):
        process_sync(update(),person)
    assert not Task.objects.exists()
    assert 'Invalid final field' in replies()[0].payload['text']


def test_group_owner_can_read_private_entity_but_spouse_cannot(people,settings):
    person,spouse=people
    settings.TELEGRAM_GROUP_ID=-500
    task=services.create_task(person,title='Личный секрет')
    payload=update(text=f'/show task {task.pk}',shared=True,chat=-500)
    process_sync(payload,person)
    assert 'Личный секрет' in replies()[0].payload['text']
    assert replies()[0].payload['shared_context'] is True
    bot.dispatch_command(person,-500,'/task Секрет | - | 30 | private',True)
    assert Task.objects.get(title='Секрет').visibility == 'private'
    with pytest.raises(PermissionDenied):
        bot.dispatch_command(spouse,-500,f'/show task {task.pk}',True)


def test_busy_listing_redacts_title_and_identifier(people):
    person,spouse=people
    hidden=event(person,visibility='busy_only')
    listing=bot.dispatch_command(spouse,spouse.chat_id,'/events')
    assert 'Занят' in listing['text']
    assert hidden.title not in listing['text']
    assert str(hidden.pk) not in listing['text']
    assert listing['permission_refs']==[{'kind':'event','id':str(hidden.pk),'mode':'busy'}]


def test_llm_write_requires_personal_expiring_confirmation(people):
    person,spouse=people
    action={'action':'create_task','args':{'title':'Купить паспортную обложку'},'reply':'Я всё создал!'}
    process_sync(update(text='Добавь дело'),person,{'action':action})
    assert not Task.objects.exists()
    proposal=replies()[0].payload
    assert 'Я всё создал!' not in proposal['text']
    callback=proposal['reply_markup']['inline_keyboard'][0][0]['callback_data']
    process_sync(update(2,user=spouse.telegram_id,callback=callback),spouse)
    assert not Task.objects.exists()
    process_sync(update(3,callback=callback),person)
    assert Task.objects.count()==1
    process_sync(update(4,callback=callback),person)
    assert Task.objects.count()==1
    conversation=Conversation.objects.get(person=person)
    assert conversation.pending=={}


def test_expired_confirmation_never_executes(people):
    person,_=people
    proposal=bot._record_pending(person,person.chat_id,{'action':'create_project','args':{'title':'Старый запрос'}})
    Conversation.objects.filter(person=person).update(expires_at=timezone.now()-timedelta(seconds=1))
    cb=proposal['reply_markup']['inline_keyboard'][0][0]['callback_data']
    process_sync(update(callback=cb),person)
    assert 'истёк' in replies()[0].payload['text']
    assert not Project.objects.exists()
    assert not Conversation.objects.get(person=person).pending


def test_checklist_desired_state_is_idempotent_and_rechecks_edit_permission(people):
    person,spouse=people
    item_event=event(person,visibility='shared')
    checklist=services.make_checklist(person,'event',item_event.pk,'Паспорт; Камера')
    item=checklist.items.first()
    for identifier in (1,2):
        process_sync(update(identifier,callback=f'item:{item.pk}:1'),person)
    item.refresh_from_db()
    assert item.checked_at
    assert item.version==2
    assert replies()[0].payload['edit_message_id']==1
    process_sync(update(3,user=spouse.telegram_id,callback=f'item:{item.pk}:0'),spouse)
    item.refresh_from_db()
    assert item.checked_at is None
    services.share_entity(person,'event',item_event.pk,'private')
    process_sync(update(4,user=spouse.telegram_id,callback=f'item:{item.pk}:1'),spouse)
    item.refresh_from_db()
    assert item.checked_at is None
    assert 'Нет доступа' in replies()[-1].payload['text']


def test_repeat_uses_template_with_reminders_and_original_occurrence(people):
    person,_=people
    first=event(person)
    services.make_checklist(person,'event',first.pk,'Паспорт')
    bot.dispatch_command(person,person.chat_id,f'/repeat event {first.pk} | FREQ=WEEKLY;BYDAY=MO')
    first.refresh_from_db()
    assert first.recurrence_id and first.occurrence_key
    assert first.recurrence.template
    assert first.version==2


def test_segment_allows_shared_trip_editor_and_denies_private_trip(people):
    person,spouse=people
    trip=services.create_trip(person,title='Путешествие')
    command=f'/segment {trip.pk} | 2026-12-01T10:00+03:00 | 2026-12-01T15:00+04:00 | transport | Москва | Дубай | Перелёт | 120,30,60'
    with pytest.raises(PermissionDenied):
        bot.dispatch_command(spouse,spouse.chat_id,command)
    services.share_entity(person,'trip',trip.pk,'shared')
    bot.dispatch_command(spouse,spouse.chat_id,command)
    segment=TripSegment.objects.get(trip=trip)
    assert segment.event.visibility=='shared'
    assert segment.checkin_minutes==120
    assert segment.transfer_minutes==60
    assert segment.event.ends_at-segment.event.starts_at==timedelta(hours=4)


def test_profile_validation_and_alias(people):
    person,_=people
    bot.dispatch_command(person,person.chat_id,'/profile work_days=0,2,4')
    person.refresh_from_db()
    assert person.profile['work_days']==[0,2,4]
    with pytest.raises(ValueError):
        bot.dispatch_command(person,person.chat_id,'/settings work_days 9')
    with pytest.raises(ValueError):
        bot.dispatch_command(person,person.chat_id,'/settings morning 08:30:45')
    with pytest.raises(PermissionDenied):
        bot.dispatch_command(person,-500,'/settings',shared=True)


def test_nonexistent_and_ambiguous_local_times_rejected(people):
    person,_=people
    person.timezone='Europe/Berlin'
    with pytest.raises(ValueError):
        bot._datetime('2026-03-29 02:30',person)
    with pytest.raises(ValueError):
        bot._datetime('2026-10-25 02:30',person)
    assert bot._datetime('2026-10-25T02:30+01:00',person).utcoffset()==timedelta(hours=1)


@pytest.mark.django_db(transaction=True)
def test_attachment_in_unknown_group_never_downloads(people,settings):
    person,_=people
    settings.TELEGRAM_GROUP_ID=-500
    payload=update(shared=True,chat=-501)
    payload['message'].pop('text')
    payload['message']['document']={'file_id':'secret','file_size':100,'file_name':'passport.pdf'}
    with patch.object(bot,'Bot') as client:
        asyncio.run(bot.process_update(payload))
        asyncio.run(bot.process_update(payload))
    client.assert_not_called()
    assert len(replies())==0
    assert not Document.objects.exists()


@pytest.mark.parametrize('actor_index', [0, 1])
def test_either_spouse_can_schedule_a_joint_event_via_command(people, actor_index):
    actor = people[actor_index]
    reply = bot.dispatch_command(actor, actor.chat_id, '/event 2026-12-02 14:00 | 60 | Семейный обед | Кафе | shared | 1001,1002')
    event = Event.objects.get(pk=reply['entity_id'])
    assert event.owner == actor and event.status == 'confirmed'
    assert set(event.participants.all()) == set(people)
    assert 'confirmed' in reply['text']
    assert not ActionRequest.objects.filter(target_id=event.pk).exists()
    assert not Job.objects.filter(dedup_key__startswith='action:').exists()


@pytest.mark.parametrize('button', ['approve', 'reject'])
def test_old_resolved_creation_buttons_report_the_actual_status(people, button):
    owner, spouse = people
    event = services.create_event(owner, title='Спектакль', visibility='shared',
                                  participants=[str(p.pk) for p in people], starts_at='2030-01-01T12:00:00+03:00',
                                  ends_at='2030-01-01T13:00:00+03:00')
    request = ActionRequest.objects.create(initiator=owner, approver=spouse, kind='confirm_event', status='superseded',
                                          target_type='event', target_id=event.pk, target_version=event.version,
                                          changes={'status': 'confirmed'}, expires_at=timezone.now())
    with pytest.raises(PermissionDenied):
        bot.dispatch_callback(owner, owner.chat_id, f'{button}:{request.pk}')
    result = bot.dispatch_callback(spouse, spouse.chat_id, f'{button}:{request.pk}')
    assert 'больше не требуется' in result['text']
    event.refresh_from_db()
    assert event.status == 'confirmed'


def test_task_project_and_assignee_without_llm(people):
    person,spouse=people
    project=services.create_project(person,title='Общий проект',visibility='shared')
    reply=bot.dispatch_command(person,person.chat_id,f'/task Заказать отель | - | 30 | shared | {project.pk} | 1002')
    task=Task.objects.get(title='Заказать отель')
    assert task.assignee==spouse and task.project==project
    assert reply['entity_id']==str(task.pk)


def test_share_and_undo_recheck_owner_and_acl(people):
    person,spouse=people
    task=services.create_task(person,title='Покупка')
    with pytest.raises(PermissionDenied):
        bot.dispatch_command(spouse,spouse.chat_id,f'/share task {task.pk} shared')
    bot.dispatch_command(person,person.chat_id,f'/share task {task.pk} shared')
    task.refresh_from_db()
    assert task.visibility=='shared'
    bot.dispatch_command(person,person.chat_id,f'/done {task.pk}')
    bot.dispatch_command(person,person.chat_id,f'/undo_task {task.pk}')
    task.refresh_from_db()
    assert task.status=='todo' and not task.completed_at


@pytest.mark.django_db(transaction=True)
def test_google_command_network_and_token_state_are_outside_update_transaction(people):
    from django.db import connection
    person,_=people
    def disconnected(actor,chat,text,shared):
        assert not connection.in_atomic_block
        actor.profile={'token_disabled':True}
        actor.save(update_fields=['profile'])
        raise ValueError('Подключите Google заново.')
    with patch.object(bot,'dispatch_command',side_effect=disconnected):
        asyncio.run(bot.process_update(update(text='/calendars')))
    person.refresh_from_db()
    assert person.profile['token_disabled']
    assert 'Подключите Google заново' in replies()[0].payload['text']


def test_uploaded_original_is_saved_private_and_served_by_id(people,tmp_path,settings):
    person,spouse=people
    settings.MEDIA_ROOT=tmp_path
    original=b'original ticket bytes'
    payload=update(text='')
    prepared={'upload':{'filename':'ticket.txt','content':original,'mime_type':'text/plain','file_id':'telegram-original','metadata':{'needs_review':True}}}
    process_sync(payload,person,prepared)
    document=Document.objects.get()
    assert document.file.read()==original
    assert document.visibility=='private'
    assert document.metadata['needs_review'] is True
    with pytest.raises(PermissionDenied):
        bot.dispatch_command(spouse,spouse.chat_id,f'/get {document.pk}')
    bot.dispatch_command(person,person.chat_id,f'/share document {document.pk} shared')
    reply=bot.dispatch_command(spouse,spouse.chat_id,f'/get {document.pk}')
    assert reply['document_path']==document.file.path
    assert reply['entity_id']==str(document.pk)


@pytest.mark.django_db(transaction=True)
def test_forwarded_photo_keeps_caption_and_allows_private_followup(people, settings, tmp_path):
    from assistant.integrations.llm import LLMError
    person, spouse = people
    settings.MEDIA_ROOT = tmp_path
    settings.TELEGRAM_BOT_TOKEN = '1234:test'
    settings.LLM_API_KEY = 'test-only'
    settings.LLM_DOCUMENT_MODEL = 'test-only'
    caption = ' Театр LYCEUM 🎭\n\n«Тётка Чарли» — сбор в фойе, вход с площади.  '
    payload = update(user=person.telegram_id)
    message = payload['message']
    del message['text']
    message.update(date=1793880000, caption=caption,
                   photo=[{'file_id': 'photo1', 'file_unique_id': 'unique', 'width': 200, 'height': 300}],
                   forward_origin={'type': 'hidden_user', 'date': 1793800000, 'sender_user_name': 'Театр'})
    message['from']['first_name'] = 'Борис'
    wire_update = Update.model_validate(payload).model_dump(mode='json', by_alias=True, exclude_none=True)
    bot.ingest_update(wire_update)

    async def download(path, *, destination, timeout):
        destination.write(b'poster-image')

    remote_file = File(file_id='photo1', file_unique_id='unique', file_path='photos/poster.jpg')
    with patch.object(bot.Bot, 'get_file', return_value=remote_file), \
         patch.object(bot.Bot, 'download_file', side_effect=download), \
         patch('assistant.integrations.llm._post_response', side_effect=LLMError('OCR unavailable')):
        call_command('runworker', queue='chat', once=True)
    document = Document.objects.get(owner=person)
    assert document.file.read() == b'poster-image'
    assert document.metadata['telegram_caption'] == caption
    assert document.metadata['extracted_text'] == ''
    assert not Event.objects.exists()
    receipt = replies()[0].payload
    assert 'Текст подписи сохранён полностью' in receipt['text']
    assert receipt['entity_id'] == str(document.pk)
    history, refs = bot.read_chat_history(person, person.chat_id, False, current_message_id=2)
    assert refs == [{'kind': 'document', 'id': str(document.pk)}]
    assert caption not in str(history)
    assert str(document.pk) in history[-1]['content']
    assert bot.read_chat_history(spouse, spouse.chat_id, False) == ([], [])


def test_attach_caption_is_a_command_only_for_direct_uploads(people, settings, tmp_path):
    person, _ = people
    settings.MEDIA_ROOT = tmp_path
    parent = event(person)
    command = f'/attach event {parent.pk}'
    description = '  Вход со двора.\nЗапасной вход закрыт.  '
    caption = f'{command}\n{description}'
    assert bot.telegram_attachment_parts({'caption': command}) == (f'event {parent.pk}', '')
    assert bot.telegram_attachment_parts({'caption': description}) == (None, description)
    for index, forwarded in enumerate([True, False], start=1):
        payload = update(identifier=index, text='')
        payload['message']['caption'] = caption
        if forwarded:
            payload['message']['forward_date'] = 1760000000
        data = {'filename': f'{index}.pdf', 'content': bytes([index]), 'mime_type': 'text/plain',
                'file_id': f'file{index}', 'metadata': {}}
        process_sync(payload, person, {'upload': data})
        doc = Document.objects.get(telegram_file_id=data['file_id'])
        assert doc.metadata['telegram_caption'] == (caption if forwarded else description)
        assert parent.documents.filter(pk=doc.pk).exists() == (not forwarded)


def test_stale_daily_plan_button_cannot_accept_replaced_draft(people):
    from datetime import date
    from assistant.models import DailyPlan
    person,_=people
    plan=DailyPlan.objects.create(person=person,day=date(2026,12,4),items=[],version=2)
    process_sync(update(callback=f'plan:{plan.day}:1'),person)
    assert 'План уже обновлён' in replies()[0].payload['text']
    assert not Event.objects.exists()


def test_group_plan_button_belongs_to_requesting_person(people, settings):
    person, spouse = people
    settings.SHARED_CHAT_ID = settings.TELEGRAM_GROUP_ID = -500
    day = (timezone.now() + timedelta(days=1)).date().isoformat()
    reply = bot.dispatch_command(person, -500, '/plan ' + day, True)
    callback = reply['reply_markup']['inline_keyboard'][0][0]['callback_data']
    assert len(callback.encode()) <= 64
    with pytest.raises(PermissionDenied):
        bot.dispatch_callback(spouse, -500, callback, True)
    accepted = bot.dispatch_callback(person, -500, callback, True)
    assert 'План принят' in accepted['text']


def test_llm_reply_preserves_both_context_and_result_permission_refs(people):
    person,_=people
    task=services.create_task(person,title='Дело')
    obj=event(person)
    context_ref={'kind':'task','id':str(task.pk)}
    result_ref={'kind':'event','id':str(obj.pk)}
    prepared={'action':{'action':'list','args':{'kind':'event'},'permission_refs':[context_ref]}}
    with patch.object(services,'execute_action',return_value={'text':'Список','permission_refs':[result_ref]}):
        process_sync(update(text='Покажи встречи'),person,prepared)
    assert replies()[0].payload['permission_refs']==[result_ref,context_ref]
