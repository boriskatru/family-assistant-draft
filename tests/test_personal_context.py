"""Server profile is opt-in, bounded, reloaded and excluded from group by default."""
import importlib.util
from pathlib import Path

import pytest

from assistant.personal_context import PersonalContextError, build_instructions


@pytest.fixture
def profile(tmp_path, settings):
    path = tmp_path / 'personal_context.md'
    settings.PERSONAL_CONTEXT_FILE = str(path)
    settings.PERSONAL_CONTEXT_MAX_CHARS = 20000
    settings.PERSONAL_CONTEXT_IN_GROUPS = False
    return path


def test_missing_empty_and_disabled_profiles_preserve_base(profile, settings):
    assert build_instructions('Основные правила') == 'Основные правила'
    profile.write_text('', encoding='utf-8')
    assert build_instructions('Основные правила') == 'Основные правила'
    profile.write_text(' \n\t', encoding='utf-8')
    assert build_instructions('Основные правила') == 'Основные правила'
    settings.PERSONAL_CONTEXT_FILE = ''
    assert build_instructions('Основные правила') == 'Основные правила'


def test_profile_is_reloaded_with_unicode_and_base_preserved(profile):
    profile.write_text('Семья: имя и проект.', encoding='utf-8-sig')
    first = build_instructions('Основные правила')
    assert first.startswith('Основные правила\n')
    assert 'Семья: имя и проект.' in first
    profile.write_text('Новая редакция профиля.', encoding='utf-8')
    second = build_instructions('Основные правила')
    assert 'Новая редакция профиля.' in second
    assert 'Семья: имя и проект.' not in second


def test_group_needs_explicit_opt_in(profile, settings):
    profile.write_text('Сведения семьи', encoding='utf-8')
    assert build_instructions('Правила', shared_context=True) == 'Правила'
    settings.PERSONAL_CONTEXT_IN_GROUPS = True
    assert 'Сведения семьи' in build_instructions('Правила', shared_context=True)


def test_character_limit_uses_decoded_characters_and_does_not_echo(profile, settings, caplog):
    settings.PERSONAL_CONTEXT_MAX_CHARS = 5
    profile.write_text('Я'*5, encoding='utf-8')
    assert 'Я'*5 in build_instructions('Правила')
    profile.write_text('СЕКРЕТ', encoding='utf-8')
    with pytest.raises(PersonalContextError) as caught:
        build_instructions('Правила')
    assert 'PERSONAL_CONTEXT_MAX_CHARS' in str(caught.value)
    assert 'СЕКРЕТ' not in str(caught.value) + caplog.text
    assert str(profile) not in str(caught.value)


def test_invalid_utf8_is_sanitized_and_group_does_not_read_it(profile, caplog):
    profile.write_bytes(b'PRIVATE\xff\xfe')
    assert build_instructions('Правила', shared_context=True) == 'Правила'
    with pytest.raises(PersonalContextError) as caught:
        build_instructions('Правила')
    assert 'PRIVATE' not in str(caught.value) + caplog.text
    assert str(profile) not in str(caught.value)


def test_unreadable_profile_has_safe_error(profile, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError('PRIVATE PATH AND CONTENT')
    monkeypatch.setattr(Path, 'open', denied)
    with pytest.raises(PersonalContextError) as caught:
        build_instructions('Правила')
    assert 'PRIVATE' not in str(caught.value)


def test_initializer_does_not_overwrite_content(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        'init_personal_context', Path(__file__).resolve().parents[1] / 'scripts' / 'init_personal_context.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Permissions are asserted by requested arguments without depending on
    # whether the test runner is root or group 10001 exists on this machine.
    chown = []
    monkeypatch.setattr(module.os, 'chown', lambda *args: chown.append(args))
    path, created = module.initialise(tmp_path)
    assert created and path.read_bytes() == b''
    path.write_text('Заполнено на сервере', encoding='utf-8')
    again, created = module.initialise(tmp_path)
    assert not created and again == path
    assert path.read_text(encoding='utf-8') == 'Заполнено на сервере'
    assert path.stat().st_mode & 0o777 == 0o640
    assert path.parent.stat().st_mode & 0o777 == 0o750
    assert all(args[1:] == (0, 10001) for args in chown)


def test_distributed_template_is_empty_and_private_files_are_excluded():
    root = Path(__file__).resolve().parents[1]
    assert (root / 'config' / 'personal_context.example.md').read_bytes() == b''
    assert 'private/' in (root / '.gitignore').read_text().splitlines()
    assert 'private' in (root / '.dockerignore').read_text().splitlines()


def test_actual_agent_receives_profile_and_reloads_it_next_run(profile):
    import asyncio
    from assistant.agent.runner import run_cycle
    from tests.test_agent_runner import FakeContext, ScriptedModel, answer, response

    class ProfileModel(ScriptedModel):
        async def get_response(self, system_instructions, *args, **kwargs):
            self.instructions = system_instructions
            return await super().get_response(system_instructions, *args, **kwargs)

    for content in ('TEST_PROFILE_FIRST', 'TEST_PROFILE_SECOND'):
        profile.write_text(content, encoding='utf-8')
        model = ProfileModel([response(answer('Готово'))])
        ctx = FakeContext()
        asyncio.run(run_cycle(ctx, 'Привет', model=model, tools=[], reference={}))
        assert ctx.status == 'completed' and content in model.instructions
    assert 'TEST_PROFILE_FIRST' not in model.instructions


def test_profile_error_stops_agent_before_model_and_shows_safe_hint(profile, settings):
    import asyncio
    from assistant.agent.runner import run_cycle
    from tests.test_agent_runner import FakeContext, ScriptedModel
    settings.PERSONAL_CONTEXT_MAX_CHARS = 4
    profile.write_text('PRIVATE_PROFILE', encoding='utf-8')
    model = ScriptedModel([])
    ctx = FakeContext()
    result = asyncio.run(run_cycle(ctx, 'Привет', model=model, tools=[], reference={}))
    assert ctx.status == 'failed' and not model.inputs
    assert 'PERSONAL_CONTEXT_MAX_CHARS' in result['text']
    assert 'PRIVATE_PROFILE' not in result['text']


@pytest.mark.django_db
def test_legacy_interpreter_receives_profile(people, profile, settings):
    from unittest.mock import patch
    from assistant.integrations import llm
    profile.write_text('TEST_LEGACY_PROFILE', encoding='utf-8')
    settings.LLM_API_KEY = 'test-key'
    settings.LLM_MODEL = 'test-model'
    envelope = {'action': 'answer', 'args_json': '{"text":"Привет"}', 'reply': 'Привет'}
    with patch.object(llm, '_post_response', return_value=envelope) as request:
        llm.interpret(people[0], 'Привет')
    assert 'TEST_LEGACY_PROFILE' in request.call_args.args[2]
