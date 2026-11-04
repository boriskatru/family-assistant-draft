import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.client.session.aiohttp import AiohttpSession
from django.core.management import call_command
from django.db import OperationalError

from assistant import bot
from assistant.models import IncomingUpdate, Job
from assistant.runtime import handle_job


pytestmark = pytest.mark.django_db(transaction=True)


def message(number, text='/id'):
    return {'update_id': number, 'message': {
        'date': 1700000000, 'message_id': number, 'text': text,
        'chat': {'id': 1001, 'type': 'private'},
        'from': {'id': 1001, 'is_bot': False, 'first_name': 'Борис'},
    }}


def poll_wire(settings, responses):
    """Feed Bot API JSON through aiogram's actual response parser with a bound bot."""
    settings.TELEGRAM_BOT_TOKEN = '1234:test-only-poll-token'
    responses = iter(responses)
    offsets = []

    async def request(session, telegram_bot, method, timeout=None):
        if method.__api_method__ == 'getWebhookInfo':
            status = 200
            body = {'ok': True, 'result': {'url': '', 'has_custom_certificate': False, 'pending_update_count': 0}}
        else:
            assert method.__api_method__ == 'getUpdates'
            offsets.append(method.offset)
            try:
                status, body = next(responses)
            except StopIteration:
                raise asyncio.CancelledError from None
        return session.check_response(telegram_bot, method, status, json.dumps(body)).result

    with patch.object(AiohttpSession, 'make_request', request), patch.object(asyncio, 'sleep', new_callable=AsyncMock):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(bot.poll_forever())
    return offsets


def test_link_preview_and_nested_defaults_do_not_block_next_command(people, settings):
    person, _ = people
    link = message(10, 'https://example.org')
    link['message'].update(
        entities=[{'type': 'url', 'offset': 0, 'length': 19}],
        link_preview_options={'url': 'https://example.org', 'prefer_small_media': False},
    )
    photo = message(11)
    del photo['message']['text']
    photo['message'].update(
        caption='Театр LYCEUM\nНачало в 16:00.',
        photo=[{'file_id': 'photo', 'file_unique_id': 'unique', 'width': 90, 'height': 90}],
        forward_origin={'type': 'hidden_user', 'date': 1690000000, 'sender_user_name': 'Театр'},
        reply_to_message=link['message'],
    )
    command = message(12)
    batch = [link, photo, command]
    assert poll_wire(settings, [(200, {'ok': True, 'result': batch})]) == [None, 13]
    assert list(IncomingUpdate.objects.order_by('update_id').values_list('payload', flat=True)) == batch
    assert Job.objects.filter(queue='chat', kind='telegram_update').count() == 3
    handle_job(Job.objects.get(dedup_key='tg-update:12'))
    with patch('assistant.runtime._tg', return_value={'message_id': 20}) as send:
        call_command('runworker', queue='notifications', once=True)
    method, body = send.call_args.args
    assert method == 'sendMessage' and body['chat_id'] == person.telegram_id
    assert f'Ваш Telegram ID: {person.telegram_id}' in body['text']


def test_conflict_and_failed_enqueue_do_not_ack_or_log_private_data(settings, caplog):
    batch = [message(20), message(21), message(22)]
    ingest = bot.ingest_update
    failed = False

    def fail_once(payload):
        nonlocal failed
        if payload['update_id'] == 21 and not failed:
            failed = True
            raise OperationalError('private database detail')
        return ingest(payload)

    conflict = {'ok': False, 'error_code': 409, 'description': 'private conflict details'}
    with patch.object(bot, 'ingest_update', fail_once):
        offsets = poll_wire(settings, [(409, conflict), (200, {'ok': True, 'result': batch}),
                                       (200, {'ok': True, 'result': batch[1:]})])
    assert offsets == [None, None, 21, 23]
    assert list(IncomingUpdate.objects.order_by('update_id').values_list('update_id', flat=True)) == [20, 21, 22]
    assert Job.objects.filter(queue='chat', kind='telegram_update').count() == 3
    assert 'TelegramConflictError' in caplog.text and 'OperationalError' in caplog.text
    assert 'private conflict details' not in caplog.text and 'private database detail' not in caplog.text
    assert settings.TELEGRAM_BOT_TOKEN not in caplog.text
