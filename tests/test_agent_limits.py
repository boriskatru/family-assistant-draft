import asyncio
import runpy

import pytest

from assistant.agent.runner import run_cycle
from tests.test_agent_runner import FakeContext, ScriptedModel, answer, basic_tools, call, response


@pytest.mark.parametrize('name,value', [
    ('AGENT_MAX_OUTPUT_TOKENS', '-1'),
    ('AGENT_MAX_TOTAL_TOKENS', 'invalid'),
    ('AGENT_HISTORY_USER_LIMIT', '0'),
    ('AGENT_MODEL_TIMEOUT_SECONDS', '0'),
    ('AGENT_MODEL_RETRIES', '-1'),
    ('AGENT_MAX_CONTINUATIONS', '-1'),
    ('LLM_REASONING_EFFORT', 'unknown'),
    ('LLM_PROMPT_CACHE_TTL', 'unknown'),
])
def test_startup_rejects_invalid_agent_settings(name, value, monkeypatch, settings):
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=name):
        runpy.run_path(str(settings.BASE_DIR / 'config/settings.py'))


@pytest.mark.parametrize('effort', ['', 'high'])
def test_environment_budgets_reach_the_sdk_without_clamping(effort, monkeypatch, settings):
    overrides = {
        'AGENT_MAX_TURNS': '31',
        'AGENT_MAX_OUTPUT_TOKENS': '12000',
        'AGENT_MAX_TOTAL_TOKENS': '230000',
        'LLM_REASONING_EFFORT': effort,
    }
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    loaded = runpy.run_path(str(settings.BASE_DIR / 'config/settings.py'))
    for name in overrides:
        setattr(settings, name, loaded[name])

    class RecordingModel(ScriptedModel):
        async def get_response(self, *args, **kwargs):
            model_settings = kwargs.get('model_settings') or args[2]
            assert model_settings.max_tokens == 12000
            if effort:
                assert model_settings.reasoning.effort == effort
            else:
                assert model_settings.reasoning is None
            return await super().get_response(*args, **kwargs)

    # 31 model calls and > 200k accumulated tokens both exceed the old clamps.
    # Use real SDK orchestration with synthetic usage and no network request.
    reads = [
        response(call('available_slot', {'day': '2031-01-03'}, f'read-{n}'), tokens=6800)
        for n in range(30)
    ]
    model = RecordingModel([*reads, response(answer('Готово.'))])
    ctx = FakeContext()
    asyncio.run(run_cycle(ctx, 'Найди окно.', model=model, tools=basic_tools(), reference={}))
    assert ctx.status == 'completed'
    assert len(model.inputs) == 31
