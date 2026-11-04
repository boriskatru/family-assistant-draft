"""Check the actual SDK request body; only the HTTP transport is substituted."""
import asyncio
import json

import httpx
import pytest

from assistant import bot
from assistant.models import AgentRun


pytestmark = pytest.mark.django_db(transaction=True)


def send_update(person, update_id, text, group=None):
    message = {
        'message_id': update_id, 'text': text,
        'from': {'id': person.telegram_id, 'is_bot': False},
        'chat': {'id': group or person.chat_id, 'type': 'supergroup' if group else 'private'},
    }
    asyncio.run(bot.process_update({'update_id': update_id, 'message': message}))
    run = AgentRun.objects.get(update_id=update_id)
    assert run.status == 'completed', run.final_reply
    return run


def answer(text):
    return {'type': 'message', 'role': 'assistant', 'id': 'msg_test', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}


@pytest.fixture
def wire(settings, monkeypatch):
    """Drive the bot and real SDK using queued synthetic Responses HTTP results."""
    settings.LLM_API_KEY = 'unit-test-only'
    settings.LLM_BASE_URL = 'https://api.openai.com/v1'
    settings.AGENT_ENABLED = True
    requests = []
    replies = []

    def respond(request):
        assert request.method == 'POST' and request.url.path == '/v1/responses'
        payload = json.loads(request.content)
        requests.append(payload)
        item, cached, written = replies.pop(0)
        input_details = {'cached_tokens': cached}
        if written is not None:
            input_details['cache_write_tokens'] = written
        return httpx.Response(200, json={
            'id': f'resp_{len(requests)}', 'object': 'response', 'created_at': 1,
            'status': 'completed', 'model': payload['model'], 'output': [item],
            'parallel_tool_calls': False, 'tool_choice': 'auto', 'tools': [],
            'usage': {'input_tokens': 2000, 'output_tokens': 10, 'total_tokens': 2010,
                      'input_tokens_details': input_details,
                      'output_tokens_details': {'reasoning_tokens': 0}},
        })

    original_init = httpx.AsyncClient.__init__

    def use_local_transport(client, *args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(respond)
        original_init(client, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, '__init__', use_local_transport)
    return requests, replies


@pytest.mark.parametrize(('model', 'ttl'), [('gpt-6-luna', '30m'), ('gpt-6.1-terra', '12h')])
def test_responses_cache_scope_history_and_usage(wire, people, settings, model, ttl):
    requests, replies = wire
    person, spouse = people
    settings.LLM_MODEL = model
    settings.LLM_PROMPT_CACHE_TTL = ttl
    settings.SHARED_CHAT_ID = settings.TELEGRAM_GROUP_ID = -500
    settings.LLM_REASONING_EFFORT = 'low'
    first_question = 'Подбери время для нашего ужина.'
    clarification = 'В субботу или в воскресенье?'
    replies.extend([
        ({'type': 'function_call', 'name': 'search_records', 'arguments': '{"kind":"task"}',
          'call_id': 'call_1', 'id': 'fc_1', 'status': 'completed'}, 0, 1024),
        (answer(clarification), 1024, 128),
        (answer('Выбрана суббота.'), 0, 0),
        (answer('Здравствуйте.'), 0, 0),
        (answer('Добрый день.'), 0, 0),
    ])
    run = send_update(person, 6201, first_question)
    assert run.usage['requests'] == 2
    assert run.usage['cached_input_tokens'] == 1024
    assert run.usage['cache_write_tokens'] == 1152
    assert run.usage['input_tokens'] == 4000 and run.usage['total_tokens'] == 4020
    history_reply = bot.dispatch_command(person, person.chat_id, '/agent_history')['text']
    assert 'Входных токенов: 4000, из кэша: 1024' in history_reply
    send_update(person, 6202, 'В субботу.')
    send_update(spouse, 6203, 'Привет!')
    send_update(person, 6204, 'Привет в группе!', group=-500)

    first, after_tool, followup, other_person, other_chat = requests
    assert first['prompt_cache_key'] == after_tool['prompt_cache_key'] == followup['prompt_cache_key']
    assert len({first['prompt_cache_key'], other_person['prompt_cache_key'], other_chat['prompt_cache_key']}) == 3
    assert first['tools'] == followup['tools'] and first['tools']
    for payload in requests:
        assert payload['model'] == model and payload['store'] is False
        assert payload['reasoning']['effort'] == 'low'
        assert payload['prompt_cache_options']['ttl'] == ttl
        assert payload['prompt_cache_options'].get('mode', 'implicit') == 'implicit'
        assert payload['input'][0] == first['input'][0]
        prefix = payload['input'][0]
        assert prefix['role'] == 'developer'
        assert any(part.get('prompt_cache_breakpoint', {}).get('mode') == 'explicit' for part in prefix['content'])

    history = followup['input']
    encoded = [json.dumps(item, ensure_ascii=False) for item in history]
    previous_question = next(i for i, x in enumerate(encoded) if first_question in x)
    previous_answer = next(i for i, x in enumerate(encoded) if clarification in x)
    assert previous_question < previous_answer and history[previous_answer]['role'] == 'assistant'
    assert all('current_time' not in x for x in encoded[1:previous_answer + 1])
    assert not any(first_question in json.dumps(p['input'], ensure_ascii=False) for p in (other_person, other_chat))


@pytest.mark.parametrize(('base', 'model'), [
    ('https://compatible.example/v1', 'gpt-6-luna'),
    ('https://api.openai.com/v1', 'gpt-5.4-mini'),
])
def test_older_model_and_compatible_endpoint_omit_new_cache_options(wire, people, settings, base, model):
    requests, replies = wire
    settings.LLM_BASE_URL = base
    settings.LLM_MODEL = model
    settings.LLM_PROMPT_CACHE_TTL = '12h'
    replies.append((answer('Ответ получен.'), 0, None))
    send_update(people[0], 6205, 'Мой вопрос')
    payload, = requests
    assert 'prompt_cache_options' not in payload
    if base == 'https://compatible.example/v1':
        assert 'prompt_cache_key' not in payload
    assert 'prompt_cache_breakpoint' not in json.dumps(payload['input'])
