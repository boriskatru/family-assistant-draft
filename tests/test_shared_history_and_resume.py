"""Family-chat context and bounded continuations exercise real SDK semantics."""
import asyncio
import runpy

import httpx
import pytest
from openai import APIConnectionError

from assistant import services
from assistant.chat_memory import read_chat_history, remember_turn
from assistant.agent.runner import run_cycle
from tests.test_agent_runner import (
    FakeContext, ScriptedModel, answer, basic_tools, call, response, tool_outputs,
)


@pytest.mark.django_db
def test_group_history_crosses_speakers_but_never_private_chats_or_revoked_acl(people, settings):
    boris, anna = people
    settings.SHARED_CHAT_ID = -42
    shared = services.create_task(boris, title='Подарки', visibility='shared')
    refs = [{'kind': 'task', 'id': str(shared.pk)}]
    remember_turn(boris, -42, 1, 'Добавить подарки?', {'text': 'Готов обсудить.', 'permission_refs': refs})
    remember_turn(boris, boris.chat_id, 2, 'Личный секрет', {'text': 'Личный ответ', 'permission_refs': []})
    remember_turn(anna, -42, 3, 'Да, давай', {'text': 'Добавлено', 'permission_refs': refs})
    history, guards = read_chat_history(anna, -42, True, 4)
    assert len(history) == 4 and guards == refs
    assert history[0]['content'].startswith('Борис (user_id=')
    assert history[2]['content'].startswith('Анна (user_id=')
    assert 'Личный' not in str(history)
    shared.visibility = 'private'
    shared.save()
    assert read_chat_history(anna, -42, True, 4) == ([], [])


@pytest.mark.django_db
def test_group_history_uses_one_global_window_and_excludes_later_messages(people, settings):
    settings.SHARED_CHAT_ID = -42
    settings.AGENT_HISTORY_USER_LIMIT = 3
    boris, anna = people
    for n, who in [(4, anna), (2, anna), (1, boris), (3, boris)]:
        remember_turn(who, -42, n, f'Вопрос {n}', {'text': f'Ответ {n}', 'permission_refs': []})
    history, _ = read_chat_history(anna, -42, True, 4)
    assert [m['content'] for m in history if m['role'] == 'assistant'] == ['Ответ 2', 'Ответ 3']


def test_history_and_model_budgets_are_configurable_without_hidden_ceiling(monkeypatch, settings):
    for name, value in {'AGENT_HISTORY_USER_LIMIT': '250', 'AGENT_MODEL_TIMEOUT_SECONDS': '150',
                        'AGENT_MODEL_RETRIES': '0', 'AGENT_MAX_CONTINUATIONS': '4'}.items():
        monkeypatch.setenv(name, value)
    loaded = runpy.run_path(str(settings.BASE_DIR / 'config/settings.py'))
    assert loaded['AGENT_HISTORY_USER_LIMIT'] == 250
    assert loaded['AGENT_MODEL_TIMEOUT_SECONDS'] == 150
    assert loaded['AGENT_MODEL_RETRIES'] == 0
    assert loaded['AGENT_MAX_CONTINUATIONS'] == 4


def test_max_turn_continuation_has_completed_tool_outputs_and_does_not_repeat_write(settings):
    settings.AGENT_MAX_TURNS = 1
    settings.AGENT_MAX_CONTINUATIONS = 1
    ctx = FakeContext()
    def finish(inputs):
        assert len(tool_outputs(inputs)) == 1
        assert any(item.get('type') == 'function_call' for item in inputs)
        return response(answer('Готово'))
    model = ScriptedModel([
        response(call('save_event', {'start': '2026-12-01T14:00:00+03:00'}, 'save-one')),
        finish,
    ])
    asyncio.run(run_cycle(ctx, 'Создай событие.', model=model, tools=basic_tools(), reference={}))
    assert ctx.status == 'completed' and len(ctx.committed) == 1
    assert len(model.inputs) == 2 and ctx.run.usage['continuations'] == 1


def test_transient_model_failure_continues_exact_request_after_completed_write(settings):
    settings.AGENT_MAX_CONTINUATIONS = 1
    ctx = FakeContext()
    error = APIConnectionError(request=httpx.Request('POST', 'https://example.test/v1/responses'))
    model = ScriptedModel([
        response(call('save_event', {'start': '2026-12-01T14:00:00+03:00'}, 'save-one')),
        error,
        response(answer('Создано')),
    ])
    asyncio.run(run_cycle(ctx, 'Создай и проверь событие.', model=model, tools=basic_tools(), reference={}))
    assert ctx.status == 'completed' and len(ctx.committed) == 1
    assert model.inputs[1] == model.inputs[2]


def test_continuations_cannot_reset_token_budget(settings, monkeypatch):
    monkeypatch.setattr('assistant.agent.runner.SYSTEM_INSTRUCTIONS', 'Ответь.')
    settings.AGENT_MAX_TURNS = 1
    settings.AGENT_MAX_CONTINUATIONS = 10
    settings.AGENT_MAX_TOTAL_TOKENS = 3000
    ctx = FakeContext()
    model = ScriptedModel([
        response(call('available_slot', {'day': '2026-12-01'}, 'read-one'), tokens=2000),
        response(call('save_event', {'start': '2026-12-01T14:00:00+03:00'}, 'save-one'), tokens=2000),
    ])
    asyncio.run(run_cycle(ctx, 'Найди окно и создай событие.', model=model, tools=basic_tools(), reference={}))
    assert ctx.status == 'limited' and not ctx.committed
    assert len(model.inputs) <= 2


def test_transient_retries_are_bounded_and_cannot_reset_overall_timeout(settings):
    settings.AGENT_MAX_CONTINUATIONS = 10
    settings.AGENT_TIMEOUT_SECONDS = 0.05
    class SlowUnavailableModel(ScriptedModel):
        async def get_response(self, *args, **kwargs):
            await asyncio.sleep(0.03)
            return await super().get_response(*args, **kwargs)
    failure = APIConnectionError(request=httpx.Request('POST', 'https://example.test/v1/responses'))
    model = SlowUnavailableModel([failure] * 20)
    ctx = FakeContext()
    asyncio.run(run_cycle(ctx, 'Проверь расписание.', model=model, tools=basic_tools(), reference={}))
    assert ctx.status == 'limited'
    assert len(model.inputs) <= 1


def test_transient_continuation_count_is_a_hard_bound(settings):
    settings.AGENT_MAX_CONTINUATIONS = 1
    failure = APIConnectionError(request=httpx.Request('POST', 'https://example.test/v1/responses'))
    model = ScriptedModel([failure] * 3)
    ctx = FakeContext()
    asyncio.run(run_cycle(ctx, 'Проверь расписание.', model=model, tools=basic_tools(), reference={}))
    assert ctx.status == 'failed' and len(model.inputs) == 2


@pytest.mark.django_db(transaction=True)
def test_repeated_mutation_on_continuation_uses_durable_receipt(people, settings):
    from assistant.agent.context import create_context
    from assistant.models import Task
    settings.AGENT_MAX_TURNS = 1
    settings.AGENT_MAX_CONTINUATIONS = 2
    person, _ = people
    model = ScriptedModel([
        response(call('create_task', {'data': {'title': 'Без дубля'}}, 'first-id')),
        response(call('create_task', {'data': {'title': 'Без дубля'}}, 'second-id')),
        response(answer('Создано одно дело.')),
    ])
    async def run():
        ctx = await create_context(person.pk, person.chat_id, 98012, 'Создай дело', False)
        return await run_cycle(ctx, 'Создай дело', model=model, reference={})
    result = asyncio.run(run())
    assert 'Создано' in result['text']
    assert Task.objects.filter(title='Без дубля').count() == 1
