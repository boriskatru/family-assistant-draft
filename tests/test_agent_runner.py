"""Exercise the actual SDK loop with a deterministic model, without API calls."""
import asyncio
import ast
import json
from types import SimpleNamespace

import pytest
from agents import function_tool
from agents.items import ModelResponse
from agents.models.interface import Model, ModelTracing
from agents.tool_context import ToolContext
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from assistant.agent.runner import run_cycle, build_tools, _public_checkpoint, _input_reference


def response(*items, tokens=30):
    return ModelResponse(output=list(items),usage=Usage(requests=1,input_tokens=tokens-10,output_tokens=10,total_tokens=tokens),response_id=None)


def call(name, arguments, ident):
    return ResponseFunctionToolCall(type='function_call',name=name,arguments=json.dumps(arguments),call_id=ident,id=ident)


def answer(text):
    return ResponseOutputMessage(type='message',id='msg-final',role='assistant',status='completed',content=[ResponseOutputText(type='output_text',text=text,annotations=[])])


class ScriptedModel(Model):
    def __init__(self, steps):
        self.steps=list(steps)
        self.inputs=[]
        self.tracing=[]

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing, **kwargs):
        self.inputs.append(input)
        self.tracing.append(tracing)
        assert model_settings.parallel_tool_calls is False
        assert model_settings.store is False
        step=self.steps.pop(0)
        if isinstance(step,Exception):
            raise step
        if callable(step):
            return step(input)
        return step

    async def stream_response(self,*args,**kwargs):
        raise AssertionError('Streaming not used')
        yield None


class FakeContext:
    def __init__(self):
        self.run=SimpleNamespace(final_reply={},usage={})
        self.calls=[]
        self.snapshots=[]
        self.status=None
        self.committed=[]

    async def dispatch(self,name,args,executor,mutation=False,external=False,call_id=''):
        self.calls.append((name,args,mutation,call_id))
        result=executor()
        if mutation:
            self.committed.append(result)
        return result

    async def save_checkpoint(self,items,usage=None):
        self.snapshots.append(items)
        if usage is not None:
            self.run.usage=dict(usage)

    async def finish(self,status,text):
        self.status=status
        self.run.final_reply={'text':text,'permission_refs':[],'committed':self.committed}
        return self.run.final_reply

    async def partial_summary(self):
        return json.dumps(self.committed,ensure_ascii=False) if self.committed else 'Записанных изменений нет.'


def basic_tools():
    @function_tool
    async def available_slot(ctx: ToolContext, day: str) -> dict:
        """Read the next available slot."""
        return await ctx.context.dispatch('available_slot',{'day':day},lambda:{'start':'2026-12-01T14:00:00+03:00'},call_id=ctx.tool_call_id)

    @function_tool
    async def save_event(ctx: ToolContext, start: str) -> dict:
        """Persist the chosen event."""
        return await ctx.context.dispatch('save_event',{'start':start},lambda:{'id':'saved-123','start':start},mutation=True,call_id=ctx.tool_call_id)

    return [available_slot,save_event]


def tool_outputs(inputs):
    return [item for item in inputs if isinstance(item,dict) and item.get('type')=='function_call_output']


def parsed_output(item):
    # SDK accepts arbitrary return values and stringifies dictionaries.
    try:return json.loads(item['output'])
    except json.JSONDecodeError:return ast.literal_eval(item['output'])


def test_real_runner_observes_tool_results_and_chooses_next_action():
    ctx=FakeContext()
    def after_slot(inputs):
        results=tool_outputs(inputs)
        assert len(results)==1
        slot=parsed_output(results[0])['start']
        return response(call('save_event',{'start':slot},'call-save'))
    def after_save(inputs):
        results=tool_outputs(inputs)
        assert len(results)==2
        assert parsed_output(results[-1])['id']=='saved-123'
        return response(answer('Событие создано на 14:00.'))
    model=ScriptedModel([response(call('available_slot',{'day':'2026-12-01'},'call-slot')),after_slot,after_save])
    result=asyncio.run(run_cycle(ctx,'Найди окно и создай встречу.',model=model,tools=basic_tools(),reference={'timezone':'Europe/Moscow'}))
    assert ctx.status=='completed'
    assert [c[0] for c in ctx.calls]==['available_slot','save_event']
    assert ctx.calls[1][1]['start']=='2026-12-01T14:00:00+03:00'
    assert ctx.run.usage['requests']==3
    assert all(t==ModelTracing.DISABLED for t in model.tracing)
    assert 'создано' in result['text']
    assert any(item.get('type')=='function_call_output' for item in ctx.snapshots[-1])


def test_turn_limit_preserves_completed_action(settings):
    settings.AGENT_MAX_TURNS=1
    settings.AGENT_MAX_CONTINUATIONS=0
    ctx=FakeContext()
    model=ScriptedModel([response(call('save_event',{'start':'2026-12-01T14:00:00+03:00'},'call-save'))])
    result=asyncio.run(run_cycle(ctx,'Создай встречу.',model=model,tools=basic_tools(),reference={}))
    assert ctx.status=='limited'
    assert len(result['committed'])==1
    assert 'сохранены' in result['text']
    assert len(model.inputs)==1


def test_model_failure_reports_partial_result_without_claiming_rollback():
    ctx=FakeContext()
    model=ScriptedModel([response(call('save_event',{'start':'2026-12-01T14:00:00+03:00'},'call-save')),RuntimeError('secret-provider-body')])
    result=asyncio.run(run_cycle(ctx,'Создай и проверь встречу.',model=model,tools=basic_tools(),reference={}))
    assert ctx.status=='failed' and result['committed']
    assert 'сохранены' in result['text']
    assert 'secret-provider-body' not in result['text']


def test_actual_token_limit_stops_before_tool_execution(settings):
    settings.AGENT_MAX_TOTAL_TOKENS=12000
    ctx=FakeContext()
    model=ScriptedModel([response(call('save_event',{'start':'2026-12-01T14:00:00+03:00'},'call-save'),tokens=13000)])
    result=asyncio.run(run_cycle(ctx,'Создай встречу.',model=model,tools=basic_tools(),reference={}))
    assert ctx.status=='limited'
    assert not ctx.calls and not ctx.committed
    assert ctx.run.usage['total_tokens']==13000


def test_estimated_input_budget_stops_before_model_call(settings):
    settings.AGENT_MAX_TOTAL_TOKENS=1000
    ctx=FakeContext()
    model=ScriptedModel([])
    asyncio.run(run_cycle(ctx,'Проверь расписание.',model=model,tools=basic_tools(),reference={'history':'x'*5000}))
    assert ctx.status=='limited' and not model.inputs


def test_unknown_tool_cannot_execute_arbitrary_action():
    ctx=FakeContext()
    model=ScriptedModel([response(call('run_shell',{'cmd':'cat /etc/passwd'},'evil'))])
    asyncio.run(run_cycle(ctx,'Проверь расписание.',model=model,tools=basic_tools(),reference={}))
    assert ctx.status=='failed' and not ctx.calls


def test_completed_run_returns_cached_answer_without_model():
    ctx=FakeContext()
    ctx.run.final_reply={'text':'Уже выполнено.','permission_refs':[]}
    model=ScriptedModel([])
    result=asyncio.run(run_cycle(ctx,'Создай встречу.',model=model,reference={}))
    assert result['text']=='Уже выполнено.' and not model.inputs


def test_tool_catalog_is_typed_native_tools_without_execution_escape():
    tools=build_tools()
    names={t.name for t in tools}
    assert {'search_records','read_document','search_web','read_webpage','search_places','route_between','create_event','accept_daily_plan'}<=names
    assert not {'run_shell','sql','execute_python','browser'}&names
    assert len(names)==len(tools)
    for tool in tools:
        schema=tool.params_json_schema
        assert schema['additionalProperties'] is False
        assert 'ctx' not in schema.get('properties',{})
        assert tool.strict_json_schema


def test_checkpoint_discards_reasoning_and_unknown_internal_items():
    items=[{'type':'reasoning','summary':[{'text':'private reasoning'}]},
           {'type':'message','role':'assistant','content':[{'type':'output_text','text':'Visible'}]},
           {'type':'function_call','call_id':'call1','name':'read_record','arguments':'{}'},
           {'type':'computer_call','action':{'type':'click'}}]
    saved=_public_checkpoint(items)
    assert len(saved)==2
    assert 'private reasoning' not in json.dumps(saved)


def test_sender_profile_available_in_group_context_without_internal_fields(people):
    person,_=people
    person.profile={'locations':{'home':'Secret home address'},'transport_mode':'walking','internal_secret':'never'}
    person.save()
    ctx=SimpleNamespace(person=person,chat_id=person.chat_id,shared_context=False)
    personal=_input_reference(ctx)
    assert personal['own_profile']['locations']['home']=='Secret home address'
    assert 'internal_secret' not in personal['own_profile']
    ctx.shared_context=True
    shared=_input_reference(ctx)
    assert shared['own_profile']==personal['own_profile']
    assert 'internal_secret' not in shared['own_profile'] and 'recent_user_messages' not in shared


@pytest.mark.django_db(transaction=True)
def test_real_catalog_read_create_read_uses_database_and_durable_receipts(people):
    from assistant.agent.context import create_context
    from assistant.models import Task,AgentRun
    person,_=people
    def create_after_search(inputs):
        found=parsed_output(tool_outputs(inputs)[-1])
        assert found['ok'] and found['items']==[]
        return response(call('create_task',{'data':{'title':'Купить билеты','estimated_minutes':30}},'create-one'))
    def read_after_create(inputs):
        saved=parsed_output(tool_outputs(inputs)[-1])
        assert saved['ok'] and saved['entity_type']=='task'
        return response(call('read_record',{'kind':'task','record_id':saved['entity_id']},'read-created'))
    def done_after_read(inputs):
        saved=parsed_output(tool_outputs(inputs)[-1])
        assert saved['ok'] and 'Купить билеты' in str(saved)
        return response(answer('Создано дело «Купить билеты», 30 минут.'))
    model=ScriptedModel([response(call('search_records',{'kind':'task','query':'Купить билеты'},'search-first')),create_after_search,read_after_create,done_after_read])
    async def run():
        ctx=await create_context(person.pk,person.chat_id,92101,'Добавь дело купить билеты',False)
        return await run_cycle(ctx,'Добавь дело купить билеты',model=model,reference={})
    result=asyncio.run(run())
    assert Task.objects.filter(owner=person,title='Купить билеты').count()==1
    run=AgentRun.objects.get(update_id=92101)
    assert run.status=='completed'
    assert list(run.tool_calls.order_by('created_at').values_list('name',flat=True))==['search_records','create_task','read_record']
    assert result['permission_refs'] and 'Создано' in result['text']


@pytest.mark.django_db(transaction=True)
def test_context_tool_budget_propagates_out_of_sdk(people,settings):
    from assistant.agent.context import create_context
    from assistant.models import Task,AgentRun
    person,_=people
    settings.AGENT_MAX_TOOL_CALLS=1
    model=ScriptedModel([
        response(call('create_task',{'data':{'title':'Первое дело'}},'one')),
        response(call('create_task',{'data':{'title':'Второе дело'}},'two')),
    ])
    async def run():
        ctx=await create_context(person.pk,person.chat_id,92102,'Создай два дела',False)
        return await run_cycle(ctx,'Создай два дела',model=model,reference={})
    result=asyncio.run(run())
    run=AgentRun.objects.get(update_id=92102)
    assert run.status=='limited'
    assert Task.objects.filter(owner=person).count()==1
    assert 'Первое дело' in result['text']
    assert len(model.inputs)==2


def test_current_generation_receives_reduced_output_budget(settings, monkeypatch):
    # Exercise budget reduction independently of the changing product prompt.
    monkeypatch.setattr('assistant.agent.runner.SYSTEM_INSTRUCTIONS', 'Ответь на вопрос пользователя.')
    settings.AGENT_MAX_TOTAL_TOKENS=1000
    observed=[]
    class BudgetModel(ScriptedModel):
        async def get_response(self,*args,**kwargs):
            settings_=kwargs.get('model_settings') or args[2]
            observed.append(settings_.max_tokens)
            return await super().get_response(*args,**kwargs)
    ctx=FakeContext()
    model=BudgetModel([response(answer('Готово.'))])
    asyncio.run(run_cycle(ctx,'Ответь.',model=model,tools=basic_tools(),reference={}))
    assert ctx.status=='completed'
    assert observed and 0<observed[0]<2000
