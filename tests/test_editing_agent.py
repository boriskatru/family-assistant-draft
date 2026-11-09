"""Exercise new editing capabilities through the real SDK and durable tool boundary."""
import asyncio
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from assistant import editing, services
from assistant.agent.context import create_context
from assistant.agent.operations import execute_operation
from assistant.agent.runner import build_tools, run_cycle
from assistant.models import AgentRun, ChecklistItem, Event, Reminder, TripSegment
from tests.test_agent_runner import ScriptedModel, answer, call, parsed_output, response, tool_outputs

pytestmark = pytest.mark.django_db(transaction=True)


def last(inputs):
    return parsed_output(tool_outputs(inputs)[-1])


def cycle(actor, steps, identifier=151001, shared=False):
    model=ScriptedModel(steps)
    async def run():
        ctx=await create_context(actor.pk,actor.chat_id,identifier,'Измени записи по моему поручению',shared)
        return await run_cycle(ctx,'Измени записи по моему поручению',model=model,reference={})
    reply=asyncio.run(run())
    run=AgentRun.objects.get(update_id=identifier)
    assert run.status=='completed',reply
    assert not model.steps
    return run,reply


def make_event(owner,**kwargs):
    start=timezone.now()+timedelta(days=10)
    return services.create_event(owner,title='Встреча',starts_at=start,ends_at=start+timedelta(hours=1),**kwargs)


def test_native_trip_shift_returns_valid_refs_and_can_read_result(people):
    owner,spouse=people
    trip=services.create_trip(owner,title='Поездка',visibility='shared',starts_on='2030-12-01',ends_on='2030-12-03')
    event=services.create_event(owner,title='Поезд',visibility='shared',trip_id=trip.pk,
        starts_at='2030-12-01T12:00:00+03:00',ends_at='2030-12-01T14:00:00+03:00')
    def shift(inputs):
        data=last(inputs)['record']
        return response(call('update_trip',{'data':{'trip_id':data['id'],'expected_version':data['version'],
            'changes':{'starts_on':'2030-12-08'},'shift_events':True}},'move'))
    def read_moved(inputs):
        data=last(inputs)
        assert data['ok'] and data['shifted_events']==1
        assert {'kind':'event','id':str(event.pk)} in data['permission_refs']
        return response(call('read_record',{'kind':'trip','record_id':data['entity_id']},'read-moved'))
    def finish(inputs):
        assert last(inputs)['record']['starts_on']=='2030-12-08'
        return response(answer('Поездка перенесена.'))
    run,reply=cycle(spouse,[response(call('read_record',{'kind':'trip','record_id':str(trip.pk)},'read')),
        shift,read_moved,finish])
    trip.refresh_from_db();event.refresh_from_db()
    assert trip.ends_on==date(2030,12,10) and event.starts_at.day==8
    assert run.tool_calls.filter(name='update_trip',status='completed').count()==1
    assert {'kind':'event','id':str(event.pk)} in reply['permission_refs']


def test_native_event_update_can_add_spouse_and_then_read_participants(people):
    owner,spouse=people
    event=make_event(owner)
    def read_changed(inputs):
        data=last(inputs);assert data['ok']
        return response(call('read_record',{'kind':'event','record_id':data['entity_id']},'read'))
    def finish(inputs):
        data=last(inputs)['record']
        assert {p['id'] for p in data['participants']}=={str(p.pk) for p in people}
        return response(answer('Аня добавлена.'))
    cycle(owner,[response(call('update_event',{'data':{'event_id':str(event.pk),'expected_version':event.version,
        'changes':{'visibility':'shared','participants':[str(p.pk) for p in people]}}},'update')),read_changed,finish])
    event.refresh_from_db()
    assert event.visibility=='shared' and set(event.participants.all())==set(people)
    assert event.reminders.filter(recipient=spouse,status='pending').exists()


def test_native_settings_read_update_stale_and_validation(people):
    owner,spouse=people
    old_spouse_version=spouse.version
    def update(inputs):
        value=last(inputs);assert value['ok']
        return response(call('update_preferences',{'expected_version':value['version'],
            'changes':{'morning':'09:00','work_days':[0,1,2,3],'max_daily_minutes':420}},'update'))
    def stale(inputs):
        value=last(inputs);assert value['ok'] and value['version']==owner.version+1
        return response(call('update_preferences',{'expected_version':owner.version,'changes':{'morning':'10:00'}},'stale'))
    def malformed(inputs):
        value=last(inputs);assert not value['ok'] and value['error_type']=='ValidationError'
        return response(call('update_preferences',{'expected_version':owner.version+1,'changes':{'morning':'27:88'}},'invalid'))
    def finish(inputs):
        output=tool_outputs(inputs)[-1]['output']
        assert 'Некорректные аргументы' in output
        return response(answer('Время плана изменено на девять.'))
    cycle(owner,[response(call('read_preferences',{},'read')),update,stale,malformed,finish])
    owner.refresh_from_db();spouse.refresh_from_db()
    assert owner.morning_time.strftime('%H:%M')=='09:00'
    assert owner.profile['work_days']==[0,1,2,3] and owner.profile['max_daily_minutes']==420
    assert spouse.version==old_spouse_version


def test_native_checklist_add_edit_remove_uses_readable_versions(people):
    owner,spouse=people
    event=make_event(owner,visibility='shared',participants=[p.pk for p in people])
    checklist=services.make_checklist(owner,'event',event.pk,'Паспорт')
    def edit(inputs):
        data=last(inputs);assert data['ok']
        assert data['version']==data['checklist_version']==2 and data['item_version']==1
        return response(call('update_checklist_item',{'data':{'item_id':data['item_id'],
            'expected_version':data['item_version'],'changes':{'quantity':'2','assignee_id':str(spouse.pk)}}},'edit'))
    def remove(inputs):
        data=last(inputs);assert data['ok'] and data['version']==3 and data['item_version']==2
        return response(call('remove_checklist_item',{'data':{'item_id':data['item_id'],
            'expected_version':data['item_version']}},'remove'))
    def read(inputs):
        data=last(inputs);assert data['ok'] and data['version']==4
        return response(call('read_record',{'kind':'checklist','record_id':data['entity_id']},'read'))
    def finish(inputs):
        data=last(inputs)['record']
        assert data['version']==4 and [i['text'] for i in data['items']]==['Паспорт']
        return response(answer('Список обновлён.'))
    cycle(spouse,[response(call('add_checklist_item',{'data':{'checklist_id':str(checklist.pk),
        'expected_version':checklist.version,'data':{'text':'Камера'}}},'add')),edit,remove,read,finish])
    assert ChecklistItem.objects.filter(checklist=checklist).count()==1


def test_native_segment_edit_version_can_be_reused(people):
    owner,spouse=people
    trip=services.create_trip(owner,title='Поездка',visibility='shared')
    event=make_event(owner,trip_id=trip.pk,visibility='shared')
    segment=TripSegment.objects.create(trip=trip,event=event)
    def read(inputs):
        data=last(inputs)
        assert data['ok'] and data['version']==trip.version and data['segment_version']==2
        return response(call('read_record',{'kind':'trip','record_id':data['entity_id']},'read'))
    def finish(inputs):
        data=last(inputs)['record']['segments'][0]
        assert data['id']==str(segment.pk) and data['version']==2 and data['booking_status']=='booked'
        return response(answer('Бронирование отмечено.'))
    cycle(spouse,[response(call('update_trip_segment',{'data':{'segment_id':str(segment.pk),
        'expected_version':segment.version,'changes':{'booking_status':'booked','checkin_minutes':120}}},'update')),read,finish])


def test_native_custom_all_day_reminder_then_disable_preserves_other_spouse(people):
    owner,spouse=people
    event=services.create_event(owner,title='День рождения',all_day=True,start_date='2030-12-24',
        visibility='shared',participants=[p.pk for p in people])
    def disable(inputs):
        data=last(inputs);assert data['ok'] and data['version']==2
        return response(call('update_reminders',{'data':{'kind':'event','record_id':str(event.pk),
            'expected_version':data['version'],'mode':'disable'}},'disable'))
    def finish(inputs):
        assert last(inputs)['ok'] and last(inputs)['version']==3
        return response(answer('Напоминания отключены для вас.'))
    cycle(owner,[response(call('update_reminders',{'data':{'kind':'event','record_id':str(event.pk),
        'expected_version':event.version,'mode':'replace','reminders':[{'local_time':'11:00','offset_minutes':0}]}},'clock')),disable,finish])
    custom=event.reminders.get(recipient=owner,kind='at:11:00')
    assert custom.due_at.astimezone(ZoneInfo('Europe/Moscow')).hour==11 and custom.status=='disabled'
    assert event.reminders.filter(recipient=spouse,status='pending').exists()


def test_native_stop_series_returns_accessible_record_and_version(people):
    from assistant.recurrence import create_rule_from_event
    owner,spouse=people
    event=make_event(owner,visibility='shared',participants=[p.pk for p in people])
    rule=create_rule_from_event(owner,event,rrule='FREQ=WEEKLY;COUNT=3')
    event.refresh_from_db()
    def read(inputs):
        data=last(inputs);assert data['ok']
        assert {'kind':'event','id':str(event.pk)} in data['permission_refs']
        return response(call('read_record',{'kind':'event','record_id':data['record_id']},'read'))
    def finish(inputs):
        assert last(inputs)['ok']
        return response(answer('Повторение остановлено.'))
    cycle(spouse,[response(call('stop_recurrence',{'data':{'kind':'event','record_id':str(event.pk),
        'expected_version':event.version,'cancel_future':False}},'stop')),read,finish])
    rule.refresh_from_db();assert not rule.active


@pytest.mark.parametrize('action',['update_project','update_trip','add_checklist_item','update_checklist_item',
    'remove_checklist_item','unlink_document','update_trip_segment'])
def test_dispatch_blocks_private_edits_without_leaking_record_contents(people,settings,tmp_path,action):
    owner,spouse=people;settings.MEDIA_ROOT=tmp_path
    project=services.create_project(owner,title='SECRET_PROJECT')
    trip=services.create_trip(owner,title='SECRET_TRIP')
    event=make_event(owner,trip_id=trip.pk)
    checklist=services.make_checklist(owner,'event',event.pk,'SECRET_ITEM')
    item=checklist.items.get()
    doc=services.save_document(owner,'secret.txt',b'SECRET_DOCUMENT','text/plain')
    doc=services.link_document(owner,doc.pk,'event',event.pk)
    segment=TripSegment.objects.create(trip=trip,event=event)
    args={
        'update_project':{'project_id':str(project.pk),'expected_version':1,'changes':{'title':'new'}},
        'update_trip':{'trip_id':str(trip.pk),'expected_version':1,'changes':{'title':'new'}},
        'add_checklist_item':{'checklist_id':str(checklist.pk),'expected_version':1,'data':{'text':'new'}},
        'update_checklist_item':{'item_id':str(item.pk),'expected_version':1,'changes':{'text':'new'}},
        'remove_checklist_item':{'item_id':str(item.pk),'expected_version':1},
        'unlink_document':{'document_id':str(doc.pk),'expected_version':doc.version,'parent_type':'event','parent_id':str(event.pk)},
        'update_trip_segment':{'segment_id':str(segment.pk),'expected_version':1,'changes':{'booking_status':'booked'}},
    }[action]
    async def run():
        ctx=await create_context(spouse.pk,spouse.chat_id,151010,'Измени запись',False)
        return await ctx.dispatch(action,args,lambda:execute_operation(ctx,action,args),mutation=True)
    result=asyncio.run(run())
    assert not result['ok'] and result['error_type']=='PermissionDenied'
    assert 'SECRET' not in str(result)
    assert ChecklistItem.objects.filter(pk=item.pk).exists() and doc.events.filter(pk=event.pk).exists()
    for obj in [project,trip,event,checklist,item,segment]:obj.refresh_from_db();assert obj.version==1


def test_dispatch_stale_version_rejected_and_exact_success_replayed(people):
    owner,_=people
    trip=services.create_trip(owner,title='Trip')
    args={'trip_id':str(trip.pk),'expected_version':1,'changes':{'title':'Updated'}}
    async def run():
        ctx=await create_context(owner.pk,owner.chat_id,151020,'Обнови поездку',False)
        good=await ctx.dispatch('update_trip',args,lambda:execute_operation(ctx,'update_trip',args),mutation=True)
        replay=await ctx.dispatch('update_trip',args,lambda:execute_operation(ctx,'update_trip',args),mutation=True)
        stale={**args,'changes':{'title':'Stale'}}
        rejected=await ctx.dispatch('update_trip',stale,lambda:execute_operation(ctx,'update_trip',stale),mutation=True)
        return good,replay,rejected
    good,replay,rejected=asyncio.run(run())
    assert good['ok'] and replay['replayed'] and not rejected['ok']
    trip.refresh_from_db();assert trip.title=='Updated' and trip.version==2


def test_all_added_tools_are_native_strict_schemas():
    expected={'update_project','update_trip','update_trip_segment','update_checklist','add_checklist_item',
        'update_checklist_item','remove_checklist_item','unlink_document','update_reminders',
        'update_recurrence','stop_recurrence','read_preferences','update_preferences'}
    tools={tool.name:tool for tool in build_tools()}
    assert expected<=tools.keys()
    def check(schema):
        if isinstance(schema,dict):
            if schema.get('type')=='object':
                assert schema.get('additionalProperties') is False
                assert set(schema.get('required',[]))==set(schema.get('properties',{}))
            for value in schema.values():check(value)
        elif isinstance(schema,list):
            for value in schema:check(value)
    for name in expected:check(tools[name].params_json_schema)
