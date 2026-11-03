"""Agent transport wiring and privacy; real networks are never called."""
import asyncio
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async
from django.core.exceptions import PermissionDenied
from django.db import connection

from assistant import bot, services
from assistant.models import AgentRun, Conversation, IncomingUpdate, Job, Task

pytestmark=pytest.mark.django_db


@pytest.fixture(autouse=True)
def enabled_agent(settings):
    settings.AGENT_ENABLED=True
    settings.AGENT_AUTONOMY='personal'


def incoming(identifier=901,text='Добавь дело',user=1001,chat=None,shared=False):
    return {'update_id':identifier,'message':{'message_id':identifier,
        'from':{'id':user,'is_bot':False},
        'chat':{'id':chat or user,'type':'supergroup' if shared else 'private'},'text':text}}


@pytest.mark.django_db(transaction=True)
def test_agent_runs_outside_outbox_transaction_and_update_is_not_replayed(people):
    person,_=people
    def write_task():
        assert not connection.in_atomic_block
        return services.create_task(person,title='Автономное дело')
    async def run(**kwargs):
        assert kwargs=={'person_id':person.pk,'text':'Добавь дело','chat_id':person.chat_id,'update_id':901,'shared_context':False}
        task=await sync_to_async(write_task)()
        return {'text':'Добавлено дело.','permission_refs':[{'kind':'task','id':str(task.pk)}],
                'chat_id':999,'document_path':'/etc/passwd','actor_id':'unsafe'}
    with patch('assistant.agent.runner.run_agent',new=AsyncMock(side_effect=run)) as runner:
        asyncio.run(bot.process_update(incoming()))
        asyncio.run(bot.process_update(incoming()))
    assert runner.await_count==1
    assert Task.objects.filter(title='Автономное дело').count()==1
    reply=Job.objects.get(kind='telegram_send').payload
    assert reply['text']=='Добавлено дело.'
    assert reply['chat_id']==person.chat_id and reply['actor_id']==str(person.pk)
    assert 'document_path' not in reply and 'reply_markup' not in reply
    assert Conversation.objects.get(person=person).pending=={}
    assert IncomingUpdate.objects.get(update_id=901).status=='completed'


@pytest.mark.django_db(transaction=True)
def test_agent_error_never_claims_earlier_actions_were_rolled_back(people):
    person,_=people
    async def broken(**kwargs):
        await sync_to_async(services.create_task)(person,title='Успело сохраниться')
        raise RuntimeError('must not leak internal exception')
    with patch('assistant.agent.runner.run_agent',new=AsyncMock(side_effect=broken)):
        asyncio.run(bot.process_update(incoming()))
    text=Job.objects.get(kind='telegram_send').payload['text']
    assert Task.objects.filter(title='Успело сохраниться').exists()
    assert 'могли сохраниться' in text and '/agent_history' in text
    assert 'must not leak' not in text


@pytest.mark.django_db(transaction=True)
def test_group_context_is_forwarded_and_strangers_cannot_start_agent(people,settings):
    person,_=people
    settings.TELEGRAM_GROUP_ID=-500
    with patch('assistant.agent.runner.run_agent',new=AsyncMock(return_value={'text':'Общий ответ','permission_refs':[]})) as runner:
        asyncio.run(bot.process_update(incoming(shared=True,chat=-500)))
        asyncio.run(bot.process_update(incoming(902,user=9000)))
    assert runner.await_count==1
    assert runner.call_args.kwargs['shared_context'] is True
    assert runner.call_args.kwargs['chat_id']==-500


@pytest.mark.django_db(transaction=True)
def test_legacy_flag_preserves_confirmation_path(people,settings):
    person,_=people
    settings.AGENT_ENABLED=False
    action={'action':'create_task','args':{'title':'Черновик'},'reply':'Предлагаю дело'}
    with patch('assistant.integrations.llm.interpret',return_value=action) as legacy, patch('assistant.agent.runner.run_agent',new=AsyncMock()) as runner:
        asyncio.run(bot.process_update(incoming()))
    legacy.assert_called_once()
    runner.assert_not_called()
    assert not Task.objects.exists()
    assert Conversation.objects.get(person=person).pending['action']==action
    assert 'Применить?' in Job.objects.get(kind='telegram_send').payload['text']


@pytest.mark.django_db(transaction=True)
def test_voice_transcription_uses_the_same_agent_path(people):
    person,_=people
    payload=incoming()
    payload['message'].pop('text')
    payload['message']['voice']={'file_id':'voice-1','file_size':20,'mime_type':'audio/ogg'}
    class FakeTelegram:
        def __init__(self,*args,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return None
        async def get_file(self,file_id):return SimpleNamespace(file_path='voice/1.ogg')
        async def download_file(self,path,destination,timeout):destination.write(b'voice data')
    with patch.object(bot,'Bot',FakeTelegram), patch('assistant.integrations.llm.transcribe',return_value='Найди билет в поездку'), patch('assistant.agent.runner.run_agent',new=AsyncMock(return_value={'text':'Билет найден','permission_refs':[]})) as runner:
        asyncio.run(bot.process_update(payload))
    assert runner.call_args.kwargs['text']=='Найди билет в поездку'
    assert Conversation.objects.get(person=person).messages[-1]['content']=='Найди билет в поездку'
    assert Job.objects.get(kind='telegram_send').payload['text']=='Билет найден'


def test_agent_autonomy_is_personal_setting(people):
    person,spouse=people
    person.profile={'work_days':[0,1]};person.save()
    reply=bot.dispatch_command(person,person.chat_id,'/agent autonomy read_only')
    person.refresh_from_db();spouse.refresh_from_db()
    assert person.profile=={'work_days':[0,1],'agent_autonomy':'read_only'}
    assert 'agent_autonomy' not in spouse.profile
    assert 'только чтение' in reply['text']
    with pytest.raises(PermissionDenied):
        bot.dispatch_command(person,-500,'/agent autonomy personal',shared=True)
    with pytest.raises(ValueError):
        bot.dispatch_command(person,person.chat_id,'/agent autonomy unrestricted')


def test_agent_history_hides_other_users_and_revoked_source_details(people):
    person,spouse=people
    task=services.create_task(spouse,title='Было общим',visibility='shared')
    ref={'kind':'task','id':str(task.pk)}
    AgentRun.objects.create(person=person,chat_id=person.chat_id,update_id=801,input_text='Перенеси Было общим',
        status='completed',final_reply={'text':'Было общим сохранено','permission_refs':[ref]},permission_refs=[ref],tool_count=2)
    AgentRun.objects.create(person=spouse,chat_id=spouse.chat_id,update_id=802,input_text='Чужой запрос',
        status='completed',final_reply={'text':'Чужой ответ'},tool_count=1)
    visible=bot.dispatch_command(person,person.chat_id,'/agent_history')
    assert 'Было общим' in visible['text'] and 'Чужой' not in visible['text']
    assert ref in visible['permission_refs']
    services.share_entity(spouse,'task',task.pk,'private')
    hidden=bot.dispatch_command(person,person.chat_id,'/agent_history')
    assert 'Было общим' not in hidden['text'] and 'Подробности скрыты' in hidden['text']
    with pytest.raises(PermissionDenied):
        bot.dispatch_command(person,-500,'/agent_history',shared=True)


def test_agent_history_does_not_accept_arbitrary_person_or_run_selector(people):
    person,_=people
    with pytest.raises(ValueError):
        bot.dispatch_command(person,person.chat_id,'/agent_history 1002')
