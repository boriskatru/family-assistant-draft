"""Regression for mistaken birthday tasks, including the full agent action boundary."""
import asyncio
from datetime import date, timedelta

import pytest

from assistant import bot, services
from assistant.agent.context import create_context
from assistant.agent.operations import execute_operation
from assistant.agent.retrieval import read_record
from assistant.integrations.llm import ConvertTaskToEventArgs
from assistant.models import ActionRequest, Event, Task


pytestmark = pytest.mark.django_db(transaction=True)


def run(ctx, name, args):
    return asyncio.run(ctx.dispatch(name, args, lambda: execute_operation(ctx, name, args), mutation=True))


@pytest.mark.parametrize('actor_index', [0, 1])
@pytest.mark.parametrize('shared', [False, True])
def test_agent_corrects_six_birthday_tasks_and_keeps_gift_preparation(people, settings, actor_index, shared):
    settings.SHARED_CHAT_ID = -500
    owner, spouse = people
    actor = people[actor_index]
    members = [str(p.pk) for p in people]
    tasks = [services.create_task(owner, title=f'День рождения друга {i}',
        description=f'Памятная дата {i}', visibility='shared', participants=members, estimated_minutes=5)
        for i in range(6)]
    gifts = services.create_task(owner, title='Подготовить подарки', visibility='shared', participants=members)
    ctx = asyncio.run(create_context(actor.pk, -500 if shared else actor.chat_id, 1200,
        'Исправь дни рождения на события без времени для нас обоих', shared_context=shared))
    for i, task in enumerate(tasks):
        day = date(2030, 12, 10) + timedelta(days=i)
        args = {'task_id': str(task.pk), 'expected_version': task.version,
                'event': {'all_day': True, 'start_date': day.isoformat(), 'blocks_time': False,
                          'participants': members, 'title': None, 'description': None,
                          'visibility': None, 'domain': None}}
        result = run(ctx, 'convert_task_to_event', args)
        assert result['ok'] and result['status'] == 'confirmed', result
        event = Event.objects.get(pk=result['entity_id'])
        task.refresh_from_db()
        assert task.status == 'cancelled'
        assert event.task_id == task.pk and event.owner_id == owner.pk
        assert event.title == task.title and event.description == task.description
        assert event.start_date == day and event.end_date == day + timedelta(days=1)
        assert event.all_day and not event.blocks_time
        assert set(event.participants.all()) == set(people)
        assert event.reminders.count() == 2
        assert {'kind': 'task', 'id': str(task.pk)} in result['permission_refs']
        assert run(ctx, 'convert_task_to_event', args)['replayed']
        summary = read_record(actor, 'event', event.pk, shared_context=shared)['record']
        assert summary['all_day'] and summary['blocks_time'] is False
        assert summary['start_date'] == day.isoformat()
        source = read_record(actor, 'task', task.pk, shared_context=shared)['record']
        assert source['related']['event']['items'][0]['id'] == str(event.pk)
        card = bot.dispatch_command(actor, -500 if shared else actor.chat_id,
                                    f'/show event {event.pk}', shared=shared)
        assert 'без времени' in card['text'] and '00:00' not in card['text']
    gifts.refresh_from_db()
    assert gifts.status == 'todo' and gifts.version == 1
    assert Task.objects.filter(status='cancelled').count() == 6
    assert Event.objects.count() == 6 and not ActionRequest.objects.exists()


def test_agent_can_set_and_clear_a_shared_task_deadline_at_noon(people):
    owner, spouse = people
    task = services.create_task(owner, title='Подготовить подарок', visibility='shared')
    ctx = asyncio.run(create_context(spouse.pk, spouse.chat_id, 1201, 'Поставь срок на полдень'))
    result = run(ctx, 'update_task', {'task_id': str(task.pk), 'expected_version': task.version,
        'changes': {'due_at': '2030-12-24T12:00:00+03:00'}})
    assert result['ok'] and result['entity_type'] == 'task', result
    task.refresh_from_db()
    assert task.due_at.hour == 9  # stored UTC, user asked 12:00 Europe/Moscow
    assert task.reminders.filter(status='pending').count() == 1
    card = bot.dispatch_command(spouse, spouse.chat_id, f'/show task {task.pk}')
    assert '24.12.2030 12:00' in card['text']
    cleared = run(ctx, 'update_task', {'task_id': str(task.pk), 'expected_version': task.version,
        'changes': {'clear_due_at': True}})
    assert cleared['ok'], cleared
    task.refresh_from_db()
    assert task.due_at is None and not task.reminders.filter(status='pending').exists()
    assert not Event.objects.exists()


def test_conversion_schema_does_not_replace_source_metadata_with_defaults():
    from uuid import uuid4
    data = ConvertTaskToEventArgs.model_validate({'task_id': uuid4(), 'expected_version': 1,
        'event': {'all_day': True, 'start_date': '2030-12-24'}}).model_dump(mode='json', exclude_none=True)
    assert not {'title', 'description', 'visibility', 'domain', 'starts_at', 'ends_at'} & data['event'].keys()


@pytest.mark.parametrize('owner_zone,reader_zone', [
    ('Pacific/Kiritimati', 'Pacific/Honolulu'), ('Pacific/Honolulu', 'Pacific/Kiritimati'),
])
def test_date_search_and_list_match_the_readers_calendar_date(people, owner_zone, reader_zone):
    from assistant.agent.retrieval import search_records
    owner, reader = people
    owner.timezone, reader.timezone = owner_zone, reader_zone
    owner.save()
    reader.save()
    event = services.create_event(owner, title='Памятная дата', all_day=True,
        start_date='2030-12-24', visibility='shared', participants=[str(p.pk) for p in people])
    for day_number in (23, 24, 25):
        day = date(2030, 12, day_number)
        after = day + timedelta(days=1)
        found = search_records(reader, 'event', date_from=day.isoformat(), date_to=after.isoformat())
        assert [row['id'] for row in found['items']] == ([str(event.pk)] if day_number == 24 else [])
        listed = services.execute_action(reader, {'action': 'list', 'args': {'kind': 'event', 'day': day.isoformat()}})
        assert ('Памятная дата' in listed['text']) is (day_number == 24)
