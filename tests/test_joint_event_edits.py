"""Shared edits through the agent and Telegram commands need no spouse approval."""
import asyncio
from datetime import timedelta

import pytest

from assistant import bot, services
from assistant.agent.context import create_context
from assistant.agent.operations import execute_operation
from assistant.models import ActionRequest, AgentToolCall, Job


pytestmark = pytest.mark.django_db(transaction=True)


def joint_event(owner, people):
    return services.create_event(owner, title='Домик Клён', visibility='shared',
        participants=[str(p.pk) for p in people], starts_at='2030-01-01T15:00:00+03:00',
        ends_at='2030-01-02T12:00:00+03:00')


@pytest.mark.parametrize('actor_index', [0, 1])
@pytest.mark.parametrize('shared', [False, True])
def test_agent_saves_cabin_instructions_immediately_and_retries_once(people, settings, actor_index, shared):
    settings.SHARED_CHAT_ID = -500
    actor = people[actor_index]
    event = joint_event(people[0], people)
    description = 'Заезд после 15:00. На первом перекрёстке налево, до чёрных ворот. Парковка во дворе.'
    args = {'event_id': str(event.pk), 'expected_version': event.version,
            'changes': {'description': description, 'location': 'Домик Клён, Сычёвки'}}
    ctx = asyncio.run(create_context(actor.pk, -500 if shared else actor.chat_id, 950,
        'Добавь всю информацию в совместное событие', shared_context=shared))

    def apply():
        return asyncio.run(ctx.dispatch('update_event', args,
            lambda: execute_operation(ctx, 'update_event', args), mutation=True, call_id='cabin-edit'))

    result = apply()
    assert result['ok'] and result['status'] == 'confirmed'
    assert result['entity_id'] == str(event.pk) and 'request_id' not in result
    assert 'обновлено' in result['text'] and 'согласован' not in result['text']
    assert {'kind': 'event', 'id': str(event.pk)} in result['permission_refs']
    event.refresh_from_db()
    assert event.description == description and event.location == 'Домик Клён, Сычёвки'
    assert not ActionRequest.objects.exists()
    assert not Job.objects.filter(dedup_key__startswith='action:').exists()
    version = event.version
    assert apply()['replayed']
    event.refresh_from_db()
    assert event.version == version
    assert AgentToolCall.objects.filter(name='update_event', status='completed').count() == 1


@pytest.mark.parametrize('actor_index', [0, 1])
@pytest.mark.parametrize('shared', [False, True])
def test_commands_move_and_cancel_shared_event_immediately(people, settings, actor_index, shared):
    settings.SHARED_CHAT_ID = -500
    actor = people[actor_index]
    event = joint_event(people[0], people)
    chat_id = -500 if shared else actor.chat_id
    reply = bot.dispatch_command(actor, chat_id,
        f'/move {event.pk} | 2030-01-03 16:00 | 60', shared=shared)
    event.refresh_from_db()
    assert event.starts_at.day == 3 and event.ends_at - event.starts_at == timedelta(hours=1)
    assert event.status == 'confirmed'
    assert 'ожидает согласования' not in reply['text']
    reply = bot.dispatch_command(actor, chat_id, f'/cancel {event.pk}', shared=shared)
    event.refresh_from_db()
    assert event.status == 'cancelled' and 'cancelled' in reply['text']
    assert not event.reminders.exclude(status='cancelled').exists()
    assert not ActionRequest.objects.exists()
    assert not Job.objects.filter(dedup_key__startswith='action:').exists()


def test_edit_to_proposed_event_is_not_reported_as_a_new_approval_request(people):
    owner, spouse = people
    event = services.create_event(owner, title='Встреча для Ани', visibility='shared',
        participants=[str(spouse.pk)], starts_at='2030-01-01T15:00:00+03:00',
        ends_at='2030-01-01T16:00:00+03:00')
    ctx = asyncio.run(create_context(spouse.pk, spouse.chat_id, 951, 'Добавь описание'))
    result = execute_operation(ctx, 'update_event', {'event_id': str(event.pk),
        'expected_version': event.version, 'changes': {'description': 'Новая информация'}})
    event.refresh_from_db()
    assert event.description == 'Новая информация' and result['status'] == 'proposed'
    assert 'request_id' not in result
    assert ActionRequest.objects.filter(target_id=event.pk).count() == 1
    assert not ActionRequest.objects.filter(target_id=event.pk, kind='update_event').exists()
