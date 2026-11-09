"""The group changes the reply audience, never the authenticated record owner."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied

from assistant import bot, runtime, services
from assistant.agent import retrieval
from assistant.agent.context import create_context
from assistant.agent.operations import execute_operation
from assistant.chat_memory import read_chat_history, remember_turn
from assistant.models import Event, Job, Task
from assistant.queue import enqueue


pytestmark = pytest.mark.django_db(transaction=True)
GROUP = -500


@pytest.fixture(autouse=True)
def group(settings):
    settings.TELEGRAM_GROUP_ID = settings.SHARED_CHAT_ID = GROUP


def context(person, number=701):
    return asyncio.run(create_context(person.pk, GROUP, number, 'Мои записи в общем чате', shared_context=True))


def operation(ctx, name, args):
    return asyncio.run(ctx.dispatch(name, args, lambda: execute_operation(ctx, name, args), mutation=True))


@pytest.mark.parametrize('actor_index', [0, 1])
def test_group_search_read_edit_and_checklist_keep_each_senders_private_records(people, actor_index):
    actor, spouse = people[actor_index], people[1-actor_index]
    start = datetime(2030, 1, 7, 10, tzinfo=timezone.utc)
    own = services.create_event(actor, title='Моя встреча', starts_at=start, ends_at=start+timedelta(hours=1))
    foreign = services.create_event(spouse, title='Личная встреча другого', starts_at=start, ends_at=start+timedelta(hours=1))
    common = services.create_event(spouse, title='Общая встреча', visibility='shared', starts_at=start, ends_at=start+timedelta(hours=1))
    found = retrieval.search_records(actor, 'event', shared_context=True)
    assert {item['id'] for item in found['items']} == {str(own.pk), str(common.pk)}
    assert foreign.title not in str(found)
    card = retrieval.read_record(actor, 'event', own.pk, shared_context=True)['record']
    assert card['can_edit'] and card['title'] == own.title
    ctx = context(actor)
    changed = operation(ctx, 'update_event', {'event_id':str(own.pk), 'expected_version':own.version,
                                             'changes':{'title':'Переназвано в группе'}})
    assert changed['ok'], changed
    own.refresh_from_db()
    assert own.title == 'Переназвано в группе' and own.visibility == 'private'
    checked = operation(ctx, 'make_checklist', {'parent_type':'event', 'parent_id':str(own.pk), 'text':'Паспорт'})
    assert checked['ok'], checked
    denied = operation(ctx, 'update_event', {'event_id':str(foreign.pk), 'expected_version':foreign.version,
                                            'changes':{'title':'Нельзя'}})
    assert not denied['ok']
    foreign.refresh_from_db()
    assert foreign.title == 'Личная встреча другого'
    with pytest.raises(PermissionDenied):
        retrieval.read_record(spouse, 'event', own.pk, shared_context=True)


def test_group_can_create_explicit_private_task_without_sharing(people):
    actor, spouse = people
    created = operation(context(actor), 'create_task', {'title':'Моё личное дело', 'visibility':'private'})
    assert created['ok'], created
    task = Task.objects.get(pk=created['entity_id'])
    assert task.owner == actor and task.visibility == 'private'
    assert not retrieval.search_records(spouse, 'task', shared_context=True)['items']


def test_private_file_read_queue_and_delivery_to_owners_group_is_allowed_once(people, settings, tmp_path):
    actor, spouse = people
    settings.MEDIA_ROOT = tmp_path
    doc = services.save_document(actor, 'ticket.txt', 'Билет на поезд'.encode(), 'text/plain')
    assert 'Билет на поезд' in retrieval.read_document(actor, doc.pk, shared_context=True)['text']
    ctx = context(actor)
    sent = operation(ctx, 'send_document', {'document_id':str(doc.pk)})
    assert sent['ok'], sent
    job = Job.objects.get(dedup_key=f'agent-file:{ctx.run.pk}:{doc.pk}')
    assert job.payload['actor_id'] == str(actor.pk) and job.payload['chat_id'] == GROUP
    with patch.object(runtime, '_tg', return_value={'message_id':77}) as telegram:
        runtime.handle_job(job)
        runtime.handle_job(job)
    telegram.assert_called_once()
    assert telegram.call_args.args[0] == 'sendDocument'
    assert telegram.call_args.args[1]['chat_id'] == GROUP
    doc.refresh_from_db()
    assert doc.visibility == 'private'
    denied = operation(context(spouse, 702), 'send_document', {'document_id':str(doc.pk)})
    assert not denied['ok']
    with pytest.raises(PermissionDenied):
        bot.dispatch_command(spouse, GROUP, f'/get {doc.pk}', shared=True)


def test_group_delivery_rechecks_source_access_and_rejects_unconfigured_destination(people, settings):
    owner, reader = people
    task = services.create_task(owner, title='Отозванный текст', visibility='shared')
    payload = {'chat_id':GROUP, 'actor_id':str(reader.pk), 'text':task.title,
               'permission_refs':[{'kind':'task','id':str(task.pk)}]}
    job = enqueue('telegram_send', payload, 'group-revoked')
    services.share_entity(owner, 'task', task.pk, 'private')
    with patch.object(runtime, '_tg', return_value={'message_id':78}) as telegram:
        runtime.send_payload(job, payload)
    assert task.title not in telegram.call_args.args[1]['text']
    assert 'Доступ к данным изменился' in telegram.call_args.args[1]['text']
    with patch.object(runtime, '_tg') as telegram, pytest.raises(PermissionError):
        runtime.send_payload(enqueue('telegram_send', {}, 'wrong-group'), {**payload,'chat_id':GROUP-1})
    telegram.assert_not_called()
    settings.ALLOWED_TELEGRAM_IDS = (owner.telegram_id,)
    with patch.object(runtime, '_tg') as telegram:
        runtime.send_payload(enqueue('telegram_send', {}, 'revoked-user'), payload)
    telegram.assert_not_called()


def test_shared_group_history_keeps_private_source_acl_and_separate_private_chats(people):
    owner, spouse = people
    task = services.create_task(owner, title='Моя закрытая задача')
    guard = {'kind':'task', 'id':str(task.pk)}
    remember_turn(owner, GROUP, 1, 'Что у меня?', {'text':task.title,'permission_refs':[guard]})
    history, refs = read_chat_history(owner, GROUP, True)
    assert [entry['content'] for entry in history] == [
        f'{owner.name} (user_id={owner.pk}):\nЧто у меня?', task.title]
    assert refs == [guard]
    assert read_chat_history(spouse, GROUP, True) == ([], [])
    assert read_chat_history(owner, owner.chat_id, False) == ([], [])
    # Even a stale/forged transcript source cannot be replayed for the spouse.
    remember_turn(spouse, GROUP, 2, 'Повтори чужое', {'text':task.title,'permission_refs':[guard]})
    assert read_chat_history(spouse, GROUP, True) == ([], [])


def test_group_plan_and_availability_allow_own_details_and_redacted_busy_guards(people):
    actor, spouse = people
    actor.timezone = 'UTC'; actor.save()
    start = datetime(2030, 1, 7, 10, tzinfo=timezone.utc)
    own = services.create_event(actor, title='Мой личный созвон', starts_at=start, ends_at=start+timedelta(hours=1))
    hidden = services.create_event(spouse, title='Скрытое название', starts_at=start+timedelta(hours=2),
                                    ends_at=start+timedelta(hours=3), visibility='busy_only')
    # A legacy participant row must disclose at most busy time.
    hidden.participants.add(actor)
    ctx = context(actor)
    planned = operation(ctx, 'plan_day', {'day':'2030-01-07'})
    assert planned['ok'] and own.title in planned['text'] and hidden.title not in planned['text']
    guard = {'kind':'event','id':str(hidden.pk),'mode':'busy'}
    assert guard in planned['permission_refs']
    payload = {'actor_id':str(actor.pk), 'chat_id':GROUP, 'text':planned['text'],
               'permission_refs':planned['permission_refs']}
    with patch.object(runtime, '_tg', return_value={'message_id':79}) as telegram:
        runtime.send_payload(enqueue('telegram_send',payload,'group-plan'),payload)
    assert own.title in telegram.call_args.args[1]['text'] and hidden.title not in telegram.call_args.args[1]['text']
    accepted = operation(ctx, 'accept_daily_plan', {'day':'2030-01-07','expected_version':planned['version']})
    assert accepted['ok'], accepted


def test_unapproved_sender_and_group_cannot_start_context_or_read_records(people, settings):
    actor, spouse = people
    task = services.create_task(actor, title='Запрет')
    with pytest.raises(PermissionDenied):
        asyncio.run(create_context(actor.pk, GROUP-1, 800, 'Запрос', shared_context=True))
    settings.ALLOWED_TELEGRAM_IDS = (spouse.telegram_id,)
    with pytest.raises(PermissionDenied):
        context(actor)
    assert not services.visible(Task.objects.all(), actor, shared_context=True).exists()
    assert not services.can_view(task, actor, shared_context=True)
    with pytest.raises(PermissionDenied):
        retrieval.read_record(actor, 'task', task.pk, shared_context=True)
