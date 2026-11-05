"""Operational diagnostics are private, bounded and contain no user payloads."""
import json
import logging
import uuid
from datetime import timedelta
from unittest.mock import patch

import httpx
import pytest
from django.db import OperationalError
from django.utils import timezone
from openai import BadRequestError

from assistant.error_reporting import may_deliver_alert, report_error, safe_error_details
from assistant.models import Job
from assistant.runtime import TelegramRequestError, _tg, send_payload


@pytest.fixture
def error_admin(people, settings):
    settings.ERROR_ADMIN_TELEGRAM_ID = people[0].telegram_id
    settings.ERROR_ALERT_COOLDOWN_SECONDS = 600
    return people[0]


def provider_error():
    secret = 'sk-example-do-not-echo'
    body = {'code': 'invalid_function_parameters', 'param': 'tools[42].parameters',
            'message': "Invalid schema for function 'save_place': private address: Test Street 9; " + secret}
    response = httpx.Response(400, request=httpx.Request('POST', 'https://example.test/responses?key='+secret))
    return BadRequestError(body['message'], response=response, body=body)


def test_real_sdk_error_reports_only_safe_metadata_to_admin(error_admin, people, settings, caplog):
    settings.SHARED_CHAT_ID = -100123456
    run_id = uuid.uuid4()
    job_id = uuid.uuid4()
    with caplog.at_level(logging.ERROR):
        error_id = report_error(provider_error(), 'agent.run', run_id=run_id, job_id=job_id)
    job = Job.objects.get(kind='telegram_send')
    assert len(error_id) == 12 and int(error_id, 16) >= 0
    assert job.queue == 'notifications'
    assert job.payload['error_alert'] is True
    assert job.payload['chat_id'] == error_admin.telegram_id
    assert job.payload['actor_id'] == str(error_admin.pk)
    text = job.payload['text']
    assert all(value in text for value in ['BadRequestError', 'HTTP: 400', 'invalid_function_parameters',
                                          'tools[42].parameters', 'save_place', str(run_id), str(job_id), error_id])
    output = text + caplog.text + json.dumps(job.payload)
    assert all(secret not in output for secret in ['sk-example-do-not-echo', 'Test Street 9', 'example.test', 'private address'])
    with patch('assistant.runtime._tg', return_value={'message_id': 10}) as send:
        send_payload(job, job.payload)
    assert send.call_args.args[1]['chat_id'] == error_admin.telegram_id
    assert send.call_args.args[1]['chat_id'] != people[1].telegram_id
    assert send.call_args.args[1]['chat_id'] != settings.SHARED_CHAT_ID


@pytest.mark.parametrize('admin_id', [0, -100123456, 999999])
def test_disabled_group_or_unknown_admin_never_queues(people, settings, admin_id):
    settings.ERROR_ADMIN_TELEGRAM_ID = admin_id
    report_error(RuntimeError('private contents'), 'agent.run')
    assert not Job.objects.exists()


def test_admin_must_be_registered_in_private_chat(error_admin):
    error_admin.chat_id = -100123456
    error_admin.save(update_fields=['chat_id'])
    report_error(RuntimeError('private contents'), 'agent.run')
    assert not Job.objects.exists()


def test_rolling_cooldown_deduplicates_across_old_time_buckets(error_admin):
    start = timezone.now().replace(second=59, microsecond=0)
    with patch('assistant.error_reporting.timezone.now', return_value=start):
        first = report_error(RuntimeError('first private text'), 'agent.run')
    with patch('assistant.error_reporting.timezone.now', return_value=start+timedelta(seconds=1)):
        second = report_error(RuntimeError('different private text'), 'agent.run')
    with patch('assistant.error_reporting.timezone.now', return_value=start+timedelta(seconds=599)):
        third = report_error(RuntimeError('different again'), 'agent.run')
    assert first == second == third
    assert Job.objects.count() == 1
    with patch('assistant.error_reporting.timezone.now', return_value=start+timedelta(seconds=601)):
        fourth = report_error(RuntimeError('retry'), 'agent.run')
    assert fourth != first
    assert Job.objects.count() == 2


def test_different_error_locations_are_not_suppressed(error_admin):
    report_error(RuntimeError('hidden'), 'agent.run')
    report_error(RuntimeError('hidden'), 'scheduler')
    assert Job.objects.count() == 2


@pytest.mark.parametrize('change', ['disabled', 'admin_changed', 'revoked', 'private_chat_missing', 'group_destination'])
def test_alert_rechecks_recipient_at_delivery(error_admin, people, settings, change):
    report_error(RuntimeError('hidden'), 'agent.run')
    job = Job.objects.get()
    payload = dict(job.payload)
    if change == 'disabled':
        settings.ERROR_ADMIN_TELEGRAM_ID = 0
    elif change == 'admin_changed':
        settings.ERROR_ADMIN_TELEGRAM_ID = people[1].telegram_id
    elif change == 'revoked':
        settings.ALLOWED_TELEGRAM_IDS = (people[1].telegram_id,)
    elif change == 'private_chat_missing':
        error_admin.chat_id = None
        error_admin.save(update_fields=['chat_id'])
    else:
        settings.SHARED_CHAT_ID = -100123456
        payload['chat_id'] = settings.SHARED_CHAT_ID
    assert not may_deliver_alert(payload)
    with patch('assistant.runtime._tg') as send:
        send_payload(job, payload)
    send.assert_not_called()


def test_reporting_failure_never_replaces_original_error(error_admin, caplog):
    with patch('assistant.queue.enqueue', side_effect=OperationalError('postgres://secret:password@example.test/db')):
        error_id = report_error(provider_error(), 'agent.run')
    assert len(error_id) == 12
    assert not Job.objects.exists()
    assert 'diagnostic delivery unavailable' in caplog.text
    assert 'invalid_function_parameters' in caplog.text
    assert all(secret not in caplog.text for secret in ['password', 'sk-example', 'Test Street', 'postgres://'])


def test_arbitrary_messages_codes_params_and_references_are_never_echoed(error_admin, caplog):
    error = RuntimeError('Bearer hidden-token private-prompt')
    error.code = 'hidden-code-from-payload'
    error.param = 'tools[2].private_address'
    error.message = 'private-prompt'
    report_error(error, 'agent.run', run_id='private-name', job_id='hidden-token')
    text = Job.objects.get().payload['text'] + caplog.text
    assert all(value not in text for value in ['hidden-token', 'private-prompt', 'hidden-code', 'private_address', 'private-name'])
    assert 'RuntimeError' in text


def test_provider_cause_survives_wrapper_without_echoing_wrapper_text():
    try:
        try:
            raise provider_error()
        except BadRequestError as error:
            raise RuntimeError('wrapper contains private body') from error
    except RuntimeError as wrapper:
        details = safe_error_details(wrapper, 'agent.run')
    assert details['code'] == 'invalid_function_parameters'
    assert details['type'] == 'RuntimeError'
    assert 'wrapper contains' not in json.dumps(details)


def test_telegram_failure_has_safe_http_code_and_project_location(settings):
    settings.TELEGRAM_BOT_TOKEN = '12345:PRIVATE_TOKEN'
    response = httpx.Response(403, json={'description': 'message mentions PRIVATE_DOCUMENT'})
    with patch('assistant.runtime.httpx.Client') as client:
        client.return_value.__enter__.return_value.post.return_value = response
        try:
            _tg('sendMessage', {'chat_id': 1001, 'text': 'PRIVATE_DOCUMENT'})
        except TelegramRequestError as error:
            details = safe_error_details(error, 'worker.notifications')
    assert details['status'] == 403
    assert any(frame.startswith('assistant/runtime.py:') for frame in details['frames'])
    assert '/workspace' not in json.dumps(details)
    assert all(value not in json.dumps(details) for value in ['PRIVATE_TOKEN', 'PRIVATE_DOCUMENT'])


def test_disabled_reporting_logs_without_database_access(settings, caplog):
    settings.ERROR_ADMIN_TELEGRAM_ID = 0
    error_id = report_error(RuntimeError('private input'), 'agent.run')
    assert error_id in caplog.text
    assert 'delivery unavailable' not in caplog.text
    assert 'private input' not in caplog.text
