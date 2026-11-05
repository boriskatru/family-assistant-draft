"""Exercise error paths through real SDK/worker entrypoints; no live APIs."""
import asyncio
import json
from unittest.mock import patch

import httpx
import pytest
from django.core.management import call_command

from assistant import bot
from assistant.models import AgentRun, Job
from assistant.queue import enqueue

pytestmark = pytest.mark.django_db(transaction=True)


def alerts():
    return Job.objects.filter(payload__error_alert=True)


def test_provider_rejection_in_group_is_private_and_correlated(people, settings, monkeypatch, caplog):
    admin, spouse = people
    settings.ERROR_ADMIN_TELEGRAM_ID = admin.telegram_id
    settings.SHARED_CHAT_ID = settings.TELEGRAM_GROUP_ID = -500
    settings.AGENT_ENABLED = True
    settings.LLM_MODEL = 'test-model'
    settings.LLM_API_KEY = 'secret-api-key'
    settings.LLM_BASE_URL = 'https://provider.example/v1'
    request_count = []

    def reject(request):
        request_count.append(1)
        return httpx.Response(400, json={'error': {
            'message': 'Invalid schema with secret-provider-body and private-street',
            'code': 'invalid_function_parameters', 'param': 'tools[0].parameters',
            'type': 'invalid_request_error',
        }})

    original_init = httpx.AsyncClient.__init__

    def local_transport(client, *args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(reject)
        original_init(client, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, '__init__', local_transport)
    asyncio.run(bot.process_update({'update_id': 99001, 'message': {
        'message_id': 99001, 'text': 'Добавь место private-street',
        'from': {'id': spouse.telegram_id, 'is_bot': False},
        'chat': {'id': -500, 'type': 'supergroup'},
    }}))
    assert len(request_count) == 1
    run = AgentRun.objects.get(update_id=99001)
    assert run.status == 'failed'
    diagnostic = alerts().get()
    assert diagnostic.payload['chat_id'] == admin.telegram_id
    assert diagnostic.payload['actor_id'] == str(admin.pk)
    assert 'BadRequestError' in diagnostic.payload['text']
    assert '400' in diagnostic.payload['text']
    assert 'agent.run' in diagnostic.payload['text']
    group_reply = Job.objects.get(kind='telegram_send', payload__chat_id=-500)
    assert 'Код ошибки:' in group_reply.payload['text']
    assert diagnostic.payload['error_id'] in group_reply.payload['text']
    assert diagnostic.payload['error_id'] in run.final_reply['text']
    assert 'Проверьте настройки API' not in group_reply.payload['text']
    assert 'Записанных изменений нет' in group_reply.payload['text']
    combined = diagnostic.payload['text'] + group_reply.payload['text'] + caplog.text
    for secret in ('secret-api-key', 'secret-provider-body', 'private-street'):
        assert secret not in combined
    with patch('assistant.runtime._tg', return_value={'message_id': 123}) as send:
        call_command('runworker', queue='notifications', once=True)
    assert send.call_args.args[1]['chat_id'] == admin.telegram_id


def test_worker_failure_reports_once_and_alert_failure_does_not_recurse(people, settings):
    admin, _ = people
    settings.ERROR_ADMIN_TELEGRAM_ID = admin.telegram_id
    job = enqueue('recurrence', {}, 'broken-service-test')
    with patch('assistant.management.commands.runworker.handle_job', side_effect=RuntimeError('secret')):
        call_command('runworker', queue='service', once=True)
        job.refresh_from_db()
        assert job.status == 'pending' and job.error == 'RuntimeError'
        assert alerts().count() == 1
        call_command('runworker', queue='notifications', once=True)
    assert alerts().count() == 1
    diagnostic = alerts().get()
    assert diagnostic.status == 'pending' and diagnostic.attempts == 1
    assert 'secret' not in json.dumps(diagnostic.payload)


def test_scheduler_failure_is_reported_before_once_reraises(people, settings):
    settings.ERROR_ADMIN_TELEGRAM_ID = people[0].telegram_id
    with patch('assistant.management.commands.runscheduler.schedule_tick', side_effect=RuntimeError('secret')):
        with pytest.raises(RuntimeError):
            call_command('runscheduler', once=True)
    assert 'scheduler' in alerts().get().payload['text']


def test_polled_serialization_error_keeps_offset_and_queues_diagnostic(people, settings):
    from types import SimpleNamespace
    from aiogram.types import WebhookInfo

    settings.ERROR_ADMIN_TELEGRAM_ID = people[0].telegram_id
    settings.TELEGRAM_BOT_TOKEN = '1234:test'

    def bad_dump(**kwargs):
        raise TypeError('secret-update-body')

    bad = SimpleNamespace(update_id=99004, model_dump=bad_dump)
    no_webhook = WebhookInfo(url='', has_custom_certificate=False, pending_update_count=0)
    with patch.object(bot.Bot, 'get_webhook_info', return_value=no_webhook), \
         patch.object(bot.Bot, 'get_updates', side_effect=[[bad], asyncio.CancelledError()]) as poll, \
         patch.object(bot.asyncio, 'sleep', return_value=None):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(bot.poll_forever())
    assert poll.await_args.kwargs['offset'] is None
    diagnostic = alerts().get()
    assert 'bot.poll.serialize' in diagnostic.payload['text']
    assert 'secret-update-body' not in diagnostic.payload['text']
