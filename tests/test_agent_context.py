import asyncio
from unittest.mock import Mock

import pytest
from django.core.exceptions import PermissionDenied,ValidationError

from assistant import services
from assistant.agent.context import create_context,AgentLimitExceeded
from assistant.agent.egress import EgressGuard
from assistant.agent.operations import execute_operation
from assistant.models import AgentRun,AgentToolCall,Task,Job,ActionRequest,Event

pytestmark=pytest.mark.django_db(transaction=True)


def context(person,number=500):
    return asyncio.run(create_context(person.pk,person.chat_id,number,'Помоги спланировать день'))


def operation(ctx,name,args,call_id=''):
    return asyncio.run(ctx.dispatch(name,args,lambda:execute_operation(ctx,name,args),mutation=True,call_id=call_id))


def test_mutation_and_receipt_deduplicate_and_recovery_has_guards(people):
    a,_=people;ctx=context(a)
    args={'title':'Подготовить документы'}
    first=operation(ctx,'create_task',args)
    second=operation(ctx,'create_task',args)
    assert first['entity_id']==second['entity_id'] and second['replayed']
    assert Task.objects.count()==1 and AgentToolCall.objects.filter(mutation=True).count()==1
    # Simulate a crash before the async post-tool source collection.
    run=AgentRun.objects.get(pk=ctx.run.pk)
    assert {'kind':'task','id':first['entity_id']} in run.permission_refs
    resumed=context(a)
    assert resumed.run.status=='interrupted'
    assert 'Подготовить документы' in resumed.run.final_reply['text']
    assert resumed.run.final_reply['permission_refs']


def test_side_effect_rolls_back_with_failed_receipt(people):
    a,_=people;ctx=context(a)
    def broken():
        services.create_task(a,title='Не должно сохраниться')
        raise ValidationError('Не хватает данных')
    result=asyncio.run(ctx.dispatch('broken',{},broken,mutation=True))
    assert not result['ok'] and not Task.objects.exists()
    assert AgentToolCall.objects.get().status=='error'


def test_autonomy_is_fresh_at_each_mutation(people):
    a,_=people;ctx=context(a)
    a.profile={'agent_autonomy':'read_only'};a.save()
    result=operation(ctx,'create_task',{'title':'Нельзя'})
    assert not result['ok'] and not Task.objects.exists()


def test_read_only_original_delivery_is_scoped_and_deduplicated(people,tmp_path,settings):
    a,b=people;settings.MEDIA_ROOT=tmp_path
    doc=services.save_document(a,'ticket.txt',b'original','text/plain')
    a.profile={'agent_autonomy':'read_only'};a.save()
    ctx=context(a)
    one=operation(ctx,'send_document',{'document_id':str(doc.pk)})
    assert one['ok'] and one['status']=='queued'
    operation(ctx,'send_document',{'document_id':str(doc.pk)})
    jobs=Job.objects.filter(dedup_key__startswith='agent-file:')
    assert jobs.count()==1 and jobs.get().payload['chat_id']==a.chat_id
    denied=operation(context(b,501),'send_document',{'document_id':str(doc.pk)})
    assert not denied['ok']


def test_boolean_setter_can_change_back_within_same_run(people):
    a,_=people;ctx=context(a)
    trip=services.create_trip(a,title='Отпуск')
    item=services.make_checklist(a,'trip',trip.pk,'Паспорт').items.get()
    for call,value in [('c1',True),('c2',False),('c3',True)]:
        assert operation(ctx,'set_checklist_item',{'item_id':str(item.pk),'checked':value},call)['ok']
    item.refresh_from_db();assert item.checked_at is not None
    assert AgentToolCall.objects.filter(name='set_checklist_item').count()==3


def test_tool_limit_stops_before_another_side_effect(people,settings):
    a,_=people;settings.AGENT_MAX_TOOL_CALLS=1;ctx=context(a)
    operation(ctx,'create_task',{'title':'Первая'})
    with pytest.raises(AgentLimitExceeded):operation(ctx,'create_task',{'title':'Вторая'})
    assert Task.objects.count()==1


@pytest.mark.parametrize('actor_index', [0, 1])
def test_agent_creates_joint_event_and_links_trip_without_spouse_approval(people, actor_index):
    a,b=people
    actor = people[actor_index]
    ctx=context(actor)
    trip=services.create_trip(actor,title='Поездка',visibility='shared')
    result=operation(ctx,'create_event',{'title':'Общий рейс','visibility':'shared','participants':[str(a.pk),str(b.pk)],'starts_at':'2030-01-01T10:00:00+03:00','ends_at':'2030-01-01T12:00:00+03:00'})
    assert result['ok'] and result['status']=='confirmed'
    added=operation(ctx,'add_trip_segment',{'trip_id':str(trip.pk),'event_id':result['entity_id'],'origin':'Москва','destination':'Казань'})
    assert added['ok']
    event=Event.objects.get(pk=result['entity_id'])
    assert event.status=='confirmed' and event.trip == trip and event.owner == actor
    assert set(event.participants.all()) == set(people)
    assert not ActionRequest.objects.filter(target_id=event.pk).exists()


def test_private_chat_identity_cannot_be_forged(people):
    a,b=people
    with pytest.raises(PermissionDenied):asyncio.run(create_context(a.pk,b.chat_id,800,'test'))


def test_known_secret_and_ticket_identifier_never_reach_external_tool(people,settings):
    a,_=people;settings.LLM_API_KEY='unit-secret-key-for-egress';ctx=context(a)
    remote=Mock(return_value={'ok':True})
    denied=asyncio.run(ctx.dispatch('search_web',{'query':'unit-secret-key-for-egress'},remote,external=True))
    assert not denied['ok'];remote.assert_not_called()
    asyncio.run(ctx.dispatch('read_record',{},lambda:{'metadata':{'booking_reference':'ZXCV97'}}))
    denied=asyncio.run(ctx.dispatch('read_webpage',{'url':'https://example.org/?ref=%5A%58%43%56%39%37'},remote,external=True))
    assert not denied['ok'];remote.assert_not_called()


def test_egress_blocks_document_copy_but_permits_map_address(settings):
    guard=EgressGuard()
    text='Confidential medical document unique contents should never be copied to a public website'
    guard.observe({'document_id':'doc','text':text})
    with pytest.raises(PermissionDenied):guard.check({'url':'https://example.org/?copy='+text})
    guard.check({'origin':'Москва, Тверская 1','destination':'Шереметьево'})


def test_egress_blocks_caption_copies_from_record_or_document_search(settings):
    caption = 'После спектакля участники собираются около служебного входа для личной встречи.'
    for result in (
        {'record': {'kind': 'document', 'telegram_caption': caption}},
        {'results': [{'document_id': 'doc', 'excerpt': caption}]},
    ):
        guard = EgressGuard()
        guard.observe(result)
        with pytest.raises(PermissionDenied):
            guard.check({'query': caption})
        guard.check({'destination': 'Театр LYCEUM'})


def test_recalled_document_guards_follow_a_new_answer_and_block_external_search(people, settings, tmp_path):
    from assistant.chat_memory import remember_turn, history_for_agent

    person, spouse = people
    settings.MEDIA_ROOT = tmp_path
    caption = 'Важная подробность для гостей содержится только в подписи пересланного вложения.'
    doc = services.save_document(spouse, 'ticket.txt', b'original', 'text/plain',
                                 telegram_caption=caption, metadata={'booking_reference': 'ZXCV97'})
    services.share_entity(spouse, 'document', doc.pk, 'shared')
    guard = {'kind': 'document', 'id': str(doc.pk)}
    remember_turn(person, person.chat_id, 100, 'Какой код?', {'text': 'ZXCV97', 'permission_refs': [guard]})
    ctx = context(person)
    ctx.inherit_history(*history_for_agent(ctx))
    remote = Mock(return_value={'ok': True})
    denied = asyncio.run(ctx.dispatch('search_web', {'query':'ZXCV97'}, remote, external=True))
    assert not denied['ok']
    denied = asyncio.run(ctx.dispatch('search_web', {'query': caption}, remote, external=True))
    assert not denied['ok']
    remote.assert_not_called()
    reply = asyncio.run(ctx.finish('completed', 'Из билета: ZXCV97'))
    assert reply['permission_refs'] == [guard]
    remember_turn(person, person.chat_id, 101, 'Повтори', reply)
    services.share_entity(spouse, 'document', doc.pk, 'private')
    assert history_for_agent(ctx) == ([], [])
