"""Check the documented OpenAI schema subset at the actual SDK HTTP boundary.

The transport is local: these tests do not claim acceptance by a live API.
Unlike an unconditional HTTP 200 mock, it rejects unsupported schema formats
before returning model output, including for ordinary requests without tools.
"""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from assistant import bot
from assistant.agent import runner
from assistant.integrations.llm import EventArgs
from assistant.models import AgentRun, AgentToolCall, Event, Job


# https://developers.openai.com/api/docs/guides/structured-outputs
# The `format` subset is narrower than general JSON Schema: `uri` is absent.
SUPPORTED_FORMATS = {'date-time', 'time', 'date', 'duration', 'email',
                     'hostname', 'ipv4', 'ipv6', 'uuid'}


def unsupported_formats(value, path=()):
    if isinstance(value, dict):
        if 'format' in value and value['format'] not in SUPPORTED_FORMATS:
            yield path, value['format']
        for key, child in value.items():
            yield from unsupported_formats(child, (*path, key))
    elif isinstance(value, list):
        for number, child in enumerate(value):
            yield from unsupported_formats(child, (*path, number))


def test_complete_tool_catalog_uses_supported_formats():
    tools = runner.build_tools()
    assert len(tools) >= 30
    problems = [(tool.name, path, value) for tool in tools
                for path, value in unsupported_formats(tool.params_json_schema)]
    assert problems == []


def answer(text):
    return {'type': 'message', 'role': 'assistant', 'id': 'msg_schema', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}


@pytest.fixture
def wire(settings, monkeypatch):
    settings.AGENT_ENABLED = True
    settings.LLM_API_KEY = 'unit-test-only'
    settings.LLM_MODEL = 'test-model'
    settings.LLM_BASE_URL = 'https://api.openai.com/v1'
    settings.PERSONAL_CONTEXT_FILE = ''
    state = SimpleNamespace(requests=[], outputs=[], rejected=[])

    def respond(request):
        assert request.method == 'POST' and request.url.path == '/v1/responses'
        payload = json.loads(request.content)
        state.requests.append(payload)
        for index, tool in enumerate(payload.get('tools', [])):
            failures = list(unsupported_formats(tool.get('parameters', {})))
            if failures:
                state.rejected.append((tool['name'], failures))
                return httpx.Response(400, json={'error': {
                    'message': f"Invalid schema for function '{tool['name']}': unsupported format",
                    'code': 'invalid_function_parameters',
                    'param': f'tools[{index}].parameters', 'type': 'invalid_request_error',
                }})
        return httpx.Response(200, json={
            'id': f'resp_{len(state.requests)}', 'object': 'response', 'created_at': 1,
            'status': 'completed', 'model': payload['model'],
            'output': [state.outputs.pop(0)],
            'parallel_tool_calls': False, 'tool_choice': 'auto', 'tools': [],
            'usage': {'input_tokens': 1000, 'output_tokens': 10, 'total_tokens': 1010,
                      'input_tokens_details': {'cached_tokens': 0},
                      'output_tokens_details': {'reasoning_tokens': 0}},
        })

    original_init = httpx.AsyncClient.__init__

    def local_transport(client, *args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(respond)
        original_init(client, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, '__init__', local_transport)
    return state


def send(person, update_id, text):
    asyncio.run(bot.process_update({'update_id': update_id, 'message': {
        'message_id': update_id, 'text': text,
        'from': {'id': person.telegram_id, 'is_bot': False},
        'chat': {'id': person.chat_id, 'type': 'private'},
    }}))
    return AgentRun.objects.get(update_id=update_id)


@pytest.mark.django_db(transaction=True)
def test_old_uri_schema_reproduces_failure_even_for_a_greeting(wire, people, settings, monkeypatch):
    settings.ERROR_ADMIN_TELEGRAM_ID = people[0].telegram_id
    original = runner.build_tools

    def old_catalog():
        tools = original()
        event = next(tool for tool in tools if tool.name == 'create_event')
        event.params_json_schema['$defs']['EventArgs']['properties']['meeting_url']['anyOf'][0]['format'] = 'uri'
        return tools

    monkeypatch.setattr(runner, 'build_tools', old_catalog)
    wire.outputs.append(answer('Привет!'))
    run = send(people[0], 98001, 'Привет')
    assert run.status == 'failed'
    assert len(wire.requests) == 1 and len(wire.outputs) == 1
    assert wire.rejected[0][0] == 'create_event'
    assert not Event.objects.exists()
    assert not AgentToolCall.objects.filter(run=run).exists()
    diagnostic = Job.objects.get(payload__error_alert=True).payload['text']
    assert all(value in diagnostic for value in
               ('BadRequestError', 'HTTP: 400', 'invalid_function_parameters', 'create_event'))


@pytest.mark.django_db(transaction=True)
def test_greeting_succeeds_with_the_complete_corrected_catalog(wire, people):
    wire.outputs.append(answer('Привет!'))
    run = send(people[0], 98002, 'Привет')
    assert run.status == 'completed' and run.final_reply['text'] == 'Привет!'
    assert len(wire.requests) == 1 and not wire.rejected
    assert any(tool['name'] == 'create_event' for tool in wire.requests[0]['tools'])


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('url', ['https://meet.example.test/room?code=abc', 'http://meeting.example.test/room'])
def test_create_event_tool_preserves_valid_call_link(wire, people, url):
    person, _ = people
    data = {
        'title': 'Созвон по проекту', 'description': '', 'visibility': 'private', 'domain': 'work',
        'starts_at': '2027-04-06T10:00:00+03:00', 'ends_at': '2027-04-06T11:00:00+03:00',
        'timezone': 'Europe/Moscow', 'end_timezone': None,
        'project_id': None, 'task_id': None, 'trip_id': None,
        'participants': [str(person.pk)], 'location': '', 'place_id': None,
        'kind': 'call', 'flexible': False, 'buffer_before': 0, 'buffer_after': 0,
        'remind_minutes': None, 'meeting': False, 'meeting_url': url, 'checklist': [],
    }
    wire.outputs.extend([
        {'type': 'function_call', 'name': 'create_event', 'arguments': json.dumps({'data': data}),
         'call_id': 'call_event', 'id': 'fc_event', 'status': 'completed'},
        answer('Созвон сохранён.'),
    ])
    run = send(person, 98003, f'Добавь созвон 6 апреля 2027 в 10:00 на час: {url}')
    assert run.status == 'completed', run.final_reply
    assert len(wire.requests) == 2 and not wire.rejected
    saved = Event.objects.get()
    assert saved.meeting_url == url and saved.owner_id == person.pk
    receipt = AgentToolCall.objects.get(run=run, name='create_event')
    assert receipt.status == 'completed' and receipt.result['ok'] is True


@pytest.mark.parametrize('url', [
    'ftp://meeting.example.test/room',
    'javascript:alert(1)',
    'https://user:password@meeting.example.test/room',
    'https://meeting.example.test/' + 'x' * 2000,
])
def test_schema_adjustment_keeps_runtime_url_validation(url):
    with pytest.raises(ValidationError):
        EventArgs.model_validate({
            'title': 'Созвон', 'starts_at': '2027-04-06T10:00:00+03:00',
            'ends_at': '2027-04-06T11:00:00+03:00', 'meeting_url': url,
        })
