"""Conversation limits slide across 50 turns without exhausting the agent."""
import asyncio
import json

import httpx
import pytest

from assistant import bot, services
from assistant.chat_memory import read_chat_history, remember_turn
from assistant.models import AgentRun, Conversation, Task


pytestmark = pytest.mark.django_db(transaction=True)


def remember(person, number, content=None, answer=None):
    remember_turn(person, person.chat_id, number, content or f'Вопрос {number}',
                  {'text': answer or f'Ответ {number}', 'permission_refs': []})


@pytest.fixture
def transport(settings, monkeypatch):
    """Exercise the real Responses adapter; no provider or Telegram network."""
    settings.LLM_API_KEY = 'unit-test-only'
    settings.LLM_MODEL = 'gpt-6-luna'
    settings.LLM_BASE_URL = 'https://api.openai.com/v1'
    settings.AGENT_ENABLED = True
    settings.AGENT_HISTORY_USER_LIMIT = 50
    settings.PERSONAL_CONTEXT_FILE = ''
    requests = []

    def respond(request):
        assert request.method == 'POST' and request.url.path == '/v1/responses'
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(200, json={
            'id': f'resp_{len(requests)}', 'object': 'response', 'created_at': 1,
            'status': 'completed', 'model': payload['model'],
            'output': [{'type': 'message', 'role': 'assistant', 'id': 'msg_boundary',
                        'status': 'completed', 'content': [{'type': 'output_text',
                        'text': 'Новый ответ', 'annotations': []}]}],
            'parallel_tool_calls': False, 'tool_choice': 'auto', 'tools': [],
            'usage': {'input_tokens': 1000, 'output_tokens': 10, 'total_tokens': 1010,
                      'input_tokens_details': {'cached_tokens': 0},
                      'output_tokens_details': {'reasoning_tokens': 0}},
        })

    original_init = httpx.AsyncClient.__init__

    def local_client(client, *args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(respond)
        original_init(client, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, '__init__', local_client)
    return requests


@pytest.mark.parametrize('past_turns', [49, 50, 51, 100])
def test_real_sdk_continues_after_fifty_user_turns(transport, people, past_turns):
    person, _ = people
    task = services.create_task(person, title='Сохранённое дело')
    for number in range(1, past_turns + 1):
        remember(person, number)
    current = past_turns + 1
    update_id = 87000 + current
    asyncio.run(bot.process_update({
        'update_id': update_id,
        'message': {'message_id': current, 'text': 'Продолжим разговор',
                    'from': {'id': person.telegram_id, 'is_bot': False},
                    'chat': {'id': person.chat_id, 'type': 'private'}},
    }))
    run = AgentRun.objects.get(update_id=update_id)
    assert run.status == 'completed', run.final_reply
    payload, = transport
    history = [item for item in payload['input'] if item.get('role') == 'user']
    assert len(history) == 50  # 49 prior user turns plus this request.
    assert history[0]['content'] == f'Вопрос {past_turns - 48}'
    assert history[-2]['content'] == f'Вопрос {past_turns}'
    assert history[-1]['content'][0]['text'] == 'Продолжим разговор'
    saved = Conversation.objects.get(person=person, chat_id=person.chat_id).messages
    assert len(saved) == 50
    assert saved[-1]['message_id'] == current
    assert saved[-1]['assistant'] == 'Новый ответ'
    assert Task.objects.get(pk=task.pk).title == 'Сохранённое дело'


def test_full_window_respects_char_budget_without_partial_exchanges(people, settings):
    person, _ = people
    settings.AGENT_HISTORY_USER_LIMIT = 50
    for number in range(1, 52):
        remember(person, number, f'Вопрос {number}: "строка"\n' + 'длинный текст ' * number,
                 f'Ответ {number}: ' + 'пример ' * number)
    all_messages, refs = read_chat_history(person, person.chat_id, False)
    assert len(all_messages) == 98 and refs == []
    newest_three = all_messages[-6:]
    exact_budget = sum(len(json.dumps(item, ensure_ascii=False)) + 2 for item in newest_three)
    assert read_chat_history(person, person.chat_id, False, max_history_chars=exact_budget) == (
        newest_three, [])
    assert read_chat_history(person, person.chat_id, False, max_history_chars=exact_budget - 1) == (
        newest_three[2:], [])
    assert read_chat_history(person, person.chat_id, False, max_history_chars=0) == ([], [])
    # Fitting the request context never rewrites the stored transcript.
    assert len(Conversation.objects.get(person=person, chat_id=person.chat_id).messages) == 50


def test_legacy_and_invalid_rows_do_not_break_reading_full_window(people, settings):
    person, _ = people
    settings.AGENT_HISTORY_USER_LIMIT = 50
    entries = [None, 'bad row', 4, {'role': 'assistant', 'content': 'orphan'},
               {'role': 'user', 'content': None}, {'role': 'user', 'content': '/help'}]
    entries.extend({'role': 'user', 'content': f'Старый вопрос {i}'} for i in range(1, 50))
    entries += [
        {'role': 'user', 'content': 'Чужой ответ', 'assistant': 'Нельзя показывать',
         'permission_refs': [{'kind': 'missing', 'id': 'not-a-uuid'}]},
        {'role': 'user', 'content': 'Последний вопрос', 'assistant': 'Последний ответ',
         'permission_refs': [], 'message_id': 51},
    ]
    Conversation.objects.create(person=person, chat_id=person.chat_id, messages=entries)
    messages, refs = read_chat_history(person, person.chat_id, False, current_message_id=52)
    assert refs == []
    assert messages[0]['content'] == 'Старый вопрос 3'
    assert messages[-2:] == [{'role': 'user', 'content': 'Последний вопрос'},
                            {'role': 'assistant', 'content': 'Последний ответ'}]
    assert not any('Нельзя показывать' in message['content'] for message in messages)

