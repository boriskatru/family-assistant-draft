import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from django.test import override_settings

from assistant.integrations import llm


def envelope(action, args, reply='Подтвердить действие?'):
    return {'action': action, 'args_json': json.dumps(args), 'reply': reply}


def output(payload):
    return {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(payload)}]}]}


def http_response(payload, status=200):
    return httpx.Response(status, json=payload, request=httpx.Request('POST', 'https://api.openai.com/v1/responses'))


@override_settings(LLM_API_KEY='', LLM_MODEL='')
def test_disabled_without_credentials_never_calls_network():
    with patch('httpx.Client') as client:
        with pytest.raises(llm.LLMUnavailable):
            llm.interpret(SimpleNamespace(timezone='Europe/Moscow'), 'Привет')
        client.assert_not_called()


def test_rejects_unknown_action_and_arbitrary_arguments():
    with pytest.raises(llm.LLMError):
        llm.validate_action(envelope('execute_sql', {'query': 'DELETE'}))
    with pytest.raises(llm.LLMError):
        llm.validate_action(envelope('create_task', {'title': 'Тест', 'owner_id': str(uuid4())}))
    with pytest.raises(llm.LLMError):
        llm.validate_action(envelope('update_event', {'event_id': str(uuid4()), 'changes': {'editors': ['bad']}}))


def test_rejects_invalid_timing_and_naive_dates():
    common = {'title': 'Встреча', 'starts_at': '2026-11-10T10:00:00+03:00', 'ends_at': '2026-11-10T09:00:00+03:00'}
    with pytest.raises(llm.LLMError):
        llm.validate_action(envelope('create_event', common))
    common['ends_at'] = '2026-11-10T11:00:00'
    with pytest.raises(llm.LLMError):
        llm.validate_action(envelope('create_event', common))


def test_event_updates_require_observed_version_to_prevent_stale_confirmation():
    args = {'event_id': str(uuid4()), 'changes': {'title': 'Новое название'}}
    with pytest.raises(llm.LLMError):
        llm.validate_action(envelope('update_event', args))
    args['expected_version'] = 3
    action = llm.validate_action(envelope('update_event', args))
    assert action['args']['expected_version'] == 3


def test_group_does_not_create_private_object():
    action = llm.validate_action(envelope('create_task', {'title': 'Личное'}), shared_context=True)
    assert action['action'] == 'answer'
    assert 'личном чате' in action['reply']


def test_coerces_ids_and_dates_to_json_safe_values():
    uid = str(uuid4())
    action = llm.validate_action(envelope('create_event', {'title': 'Созвон', 'starts_at': '2026-11-10T10:00:00+03:00', 'ends_at': '2026-11-10T11:00:00+03:00', 'participants': [uid]}))
    assert action['args']['participants'] == [uid]
    assert action['args']['starts_at'].endswith('+03:00')
    assert action['args']['remind_minutes'] == [15]
    assert 'timezone' not in action['args']  # service inherits the person's zone
    json.dumps(action)


@override_settings(LLM_API_KEY='test-key', LLM_MODEL='test-model', LLM_BASE_URL='https://api.openai.com/v1', LLM_STRUCTURED_MAX_OUTPUT_TOKENS=2100)
def test_interpret_uses_only_filtered_context_and_never_executes():
    # Module injected only when root's service module is not yet ready in early unit development.
    services = MagicMock()
    services.context_for.return_value = {'events': [{'title': 'Общее событие'}]}
    model_result = envelope('create_task', {'title': 'Купить билеты', 'visibility': 'shared'})
    with patch.dict(sys.modules, {'assistant.services': services}), patch('httpx.Client') as client:
        client.return_value.__enter__.return_value.post.return_value = http_response(output(model_result))
        proposal = llm.interpret(SimpleNamespace(timezone='Europe/Moscow'), 'Добавь общее дело', shared_context=True, history=[{'role': 'assistant', 'content': 'SECRET'}, {'role': 'user', 'content': 'OTHER SECRET'}])
        services.context_for.assert_called_once_with(SimpleNamespace(timezone='Europe/Moscow'), shared_context=True)
        payload = client.return_value.__enter__.return_value.post.call_args.kwargs['json']
        assert payload['store'] is False
        assert payload['max_output_tokens'] == 2100
        assert payload['text']['format']['strict'] is True
        assert 'SECRET' not in json.dumps(payload)
        services.execute_action.assert_not_called()
        assert proposal['action'] == 'create_task'


@override_settings(LLM_API_KEY='test-key', LLM_MODEL='test-model', LLM_BASE_URL='https://api.openai.com/v1')
@pytest.mark.parametrize('name,args', [('answer', {'text': 'Ваш план'}), ('create_task', {'title': 'Новое дело'})])
def test_permission_guards_derive_from_entire_actual_context(name, args):
    ids = [str(uuid4()) for _ in range(5)]
    services = MagicMock()
    services.context_for.return_value = {
        'events': [{'id': ids[0], 'title': 'Занят', 'version': 2}, {'id': ids[1], 'title': 'Занят'}],
        'tasks': [{'id': ids[2], 'title': 'Дело'}],
        'projects': [{'id': ids[3], 'title': 'Проект'}],
        'trips': [{'id': ids[4], 'title': 'Поездка'}],
        'people': [{'id': str(uuid4()), 'name': 'Участник'}],
    }
    with patch.dict(sys.modules, {'assistant.services': services}), patch('httpx.Client') as client:
        client.return_value.__enter__.return_value.post.return_value = http_response(output(envelope(name, args)))
        result = llm.interpret(SimpleNamespace(timezone='Europe/Moscow'), 'Запрос')
    assert result['permission_refs'] == [
        {'kind': 'event', 'id': ids[0]},
        {'kind': 'event', 'id': ids[1], 'mode': 'busy'},
        {'kind': 'task', 'id': ids[2]},
        {'kind': 'project', 'id': ids[3]},
        {'kind': 'trip', 'id': ids[4]},
    ]
    # A model cannot replace source guards with an empty list.
    supplied = envelope(name, args)
    supplied['permission_refs'] = []
    with pytest.raises(llm.LLMError):
        llm.validate_action(supplied)


@override_settings(LLM_API_KEY='sensitive-key', LLM_MODEL='test-model', LLM_BASE_URL='https://api.openai.com/v1')
def test_upstream_errors_are_sanitized():
    response = http_response({'error': 'sensitive-key private ticket'}, status=401)
    with patch('httpx.Client') as client:
        client.return_value.__enter__.return_value.post.return_value = response
        with pytest.raises(llm.LLMError) as exc:
            llm._post_response('LLM_MODEL', [], '', llm.ENVELOPE_SCHEMA, 'test')
        assert 'sensitive-key' not in str(exc.value)
        assert 'private ticket' not in str(exc.value)


def test_incomplete_and_refusal_are_not_treated_as_actions():
    with pytest.raises(llm.LLMError):
        llm._response_text({'status': 'incomplete', 'output': []})
    with pytest.raises(llm.LLMError):
        llm._response_text({'output': [{'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'No'}]}]})


@pytest.mark.parametrize('body', [[], {'output': None}, {'output': [None]}, {'output': [{'type': 'message', 'content': None}]}])
def test_malformed_upstream_structure_is_safe(body):
    with pytest.raises(llm.LLMError):
        llm._response_text(body)


@override_settings(MAX_UPLOAD_MB=1)
def test_file_limit_matches_service_configuration():
    with pytest.raises(llm.LLMError):
        llm._validate_file(b'x' * (1024 * 1024 + 1), 'ticket.pdf')


@override_settings(LLM_API_KEY='', LLM_DOCUMENT_MODEL='')
def test_image_without_model_is_reviewable_fallback():
    with patch('httpx.Client') as client:
        result = llm.extract_document(b'fake image', 'image/png', '../ticket.png')
        assert result['title'] == 'ticket.png'
        assert result['needs_review'] is True
        assert result['document_type'] == 'other'
        assert result['warnings']
        client.assert_not_called()


@override_settings(LLM_API_KEY='', LLM_DOCUMENT_MODEL='')
def test_pdf_extracts_text_locally_without_llm():
    with patch('assistant.integrations.pdf_text.extract_pdf',return_value={'pages':[{'page':1,'text':'Билет Москва — Токио'}],'truncated':False}), patch('httpx.Client') as client:
        result = llm.extract_document(b'%PDF-test', 'application/pdf', 'ticket.pdf')
        assert 'Токио' in result['extracted_text']
        assert result['needs_review'] is True
        client.assert_not_called()


@override_settings(LLM_API_KEY='test-key', LLM_DOCUMENT_MODEL='document-model', LLM_BASE_URL='https://api.openai.com/v1')
def test_document_extraction_returns_metadata_only():
    metadata = {'document_type': 'ticket', 'title': 'Билет', 'passengers': ['Борис'], 'booking_reference': 'PNR123', 'origin': 'Москва', 'destination': 'Токио', 'departure': None, 'arrival': None, 'warnings': []}
    with patch('httpx.Client') as client:
        client.return_value.__enter__.return_value.post.return_value = http_response(output(metadata))
        result = llm.extract_document(b'image', 'image/jpeg', 'ticket.jpg')
        assert result['needs_review'] is True
        assert result['booking_reference'] == 'PNR123'
        assert 'action' not in result


@override_settings(LLM_API_KEY='test-key', LLM_TRANSCRIPTION_MODEL='')
def test_audio_requires_separate_model():
    with patch('httpx.Client') as client:
        with pytest.raises(llm.LLMUnavailable):
            llm.transcribe(b'audio', 'voice.ogg')
        client.assert_not_called()


@override_settings(LLM_API_KEY='test-key', LLM_TRANSCRIPTION_MODEL='transcribe-model', LLM_BASE_URL='https://api.openai.com/v1')
def test_audio_transcribes_with_explicit_setting():
    with patch('httpx.Client') as client:
        client.return_value.__enter__.return_value.post.return_value = http_response({'text': 'Добавь встречу завтра'})
        result = llm.transcribe(b'audio', 'voice.ogg')
        assert result == 'Добавь встречу завтра'
        kwargs = client.return_value.__enter__.return_value.post.call_args.kwargs
        assert kwargs['data'] == {'model': 'transcribe-model'}


@override_settings(LLM_API_KEY='secret', LLM_MODEL='model', LLM_BASE_URL='http://untrusted.example/v1')
def test_requires_secure_configured_endpoint():
    with pytest.raises(llm.LLMError):
        llm._credentials()
