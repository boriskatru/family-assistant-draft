"""Lead times from Telegram, both model paths and Google import."""
import json
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from assistant import bot, runtime, services
from assistant.integrations import google, llm
from assistant.models import CalendarConnection, CalendarSource, Event, Job
from assistant.recurrence import create_rule_from_event, materialize_recurrences

pytestmark = pytest.mark.django_db
START = datetime(2030, 1, 5, 11, tzinfo=UTC)


@pytest.mark.parametrize(('title', 'extra', 'expected'), [
    ('Интервью', {'kind': 'call'}, 60),
    ('Созвон с редактором', {}, 60),
    ('Обсуждение', {'meeting_url': 'https://video.example.org/team/123'}, 60),
    ('Обсуждение', {'location': 'https://us02web.zoom.us/j/12345'}, 60),
    ('Обед', {'location': 'https://meet.google.com.example.org/'}, 15),
    ('Подготовка к созвону', {}, 15),
    ('Театр', {}, 15),
    ('Рейс SU123', {'kind': 'travel'}, 1440),
    ('Поездка в Истру', {}, 1440),
    ('Обсудить поездку в Истру', {}, 15),
])
def test_default_lead_times(people, title, extra, expected):
    event = services.create_event(people[0], title=title, starts_at=START, ends_at=START+timedelta(hours=1), **extra)
    reminder = event.reminders.get()
    assert reminder.offset_minutes == expected and reminder.due_at == START-timedelta(minutes=expected)
    assert Job.objects.get(kind='google_event').payload['create_meet'] is False


def test_command_call_notifies_each_attendee_at_one_hour_without_google(people):
    a, b = people
    lead = START - timedelta(hours=1)
    result = bot.dispatch_command(a, a.chat_id, '/call 2030-01-05 14:00 | 30 | Планёрка | Онлайн | shared | 1001,1002')
    event = Event.objects.get(pk=result['entity_id'])
    assert event.kind == 'call'
    assert set(event.reminders.values_list('recipient_id', 'due_at')) == {(a.id, lead), (b.id, lead)}
    runtime.schedule_tick(lead-timedelta(seconds=1))
    assert not Job.objects.filter(kind='reminder').exists()
    runtime.schedule_tick(lead)
    runtime.schedule_tick(lead)
    jobs = Job.objects.filter(kind='reminder')
    assert jobs.count() == 2
    with patch('assistant.runtime.timezone.now', return_value=lead), \
         patch('assistant.runtime._tg', return_value={'message_id': 123}) as sender:
        for job in jobs:
            runtime.handle_job(job)
            runtime.handle_job(job)
    assert [call.args[0] for call in sender.call_args_list] == ['sendMessage', 'sendMessage']
    assert {call.args[1]['chat_id'] for call in sender.call_args_list} == {a.chat_id, b.chat_id}
    assert all('Планёрка' in call.args[1]['text'] for call in sender.call_args_list)


@pytest.mark.parametrize(('custom', 'expected'), [(None, [60]), ([], []), ([5, 30], [5, 30])])
def test_free_text_preserves_default_custom_and_silent_call_series(people, custom, expected):
    a, _ = people
    args = {'title': 'Созвон', 'kind': 'call', 'starts_at': START.isoformat(),
            'ends_at': (START+timedelta(hours=1)).isoformat(), 'remind_minutes': custom}
    # The agent sends its EventArgs on to the same validator as the legacy interpreter.
    args = llm.EventArgs.model_validate(args).model_dump(mode='json', exclude_none=True)
    proposal = llm.validate_action({'action': 'create_event', 'args_json': json.dumps(args), 'reply': 'Созвон'})
    event = Event.objects.get(pk=services.execute_action(a, proposal)['entity_id'])
    rule = create_rule_from_event(a, event, rrule='FREQ=WEEKLY;COUNT=2')
    assert materialize_recurrences(START) == 1
    for item in Event.objects.filter(recurrence=rule):
        assert list(item.reminders.order_by('offset_minutes').values_list('offset_minutes', flat=True)) == expected


def test_travel_event_inside_dated_trip_keeps_its_short_departure_reminder(people):
    a, _ = people
    trip = services.create_trip(a, title='Поездка', starts_on=date(2030, 1, 5))
    segment = services.create_event(a, title='Поезд', kind='travel', starts_at=START, ends_at=START+timedelta(hours=2),
                                    trip_id=trip.pk)
    assert segment.reminders.get().offset_minutes == 15


def test_imported_online_meeting_gets_one_hour_once(people):
    a, _ = people
    source = CalendarSource.objects.create(connection=CalendarConnection.objects.create(person=a),
                                           calendar_id='local-test')
    item = {'id': 'import-test', 'etag': 'first', 'summary': 'Интервью',
            'start': {'dateTime': START.isoformat()}, 'end': {'dateTime': (START+timedelta(hours=1)).isoformat()},
            'hangoutLink': 'https://meet.google.com/abc-defg-hij'}
    with patch('assistant.integrations.google.timezone.now', return_value=START-timedelta(days=1)):
        google._import_item(source, item)
        google._import_item(source, item)
    reminder = Event.objects.get(title='Интервью').reminders.get()
    assert reminder.offset_minutes == 60 and reminder.due_at == START-timedelta(hours=1)


def test_agent_accepts_existing_meeting_url_without_requesting_google_meet(people):
    args = {'title': 'Разбор', 'starts_at': START.isoformat(), 'ends_at': (START+timedelta(hours=1)).isoformat(),
            'meeting_url': 'https://video.example.org/team/123'}
    proposal = llm.validate_action({'action': 'create_event', 'args_json': json.dumps(args), 'reply': 'Разбор'})
    event = Event.objects.get(pk=services.execute_action(people[0], proposal)['entity_id'])
    assert event.meeting_url == args['meeting_url']
    assert event.reminders.get().offset_minutes == 60
    assert Job.objects.get(kind='google_event').payload['create_meet'] is False


def test_hour_before_call_across_spring_clock_change_is_an_elapsed_hour(people):
    start = datetime(2030, 3, 31, 3, 30, tzinfo=ZoneInfo('Europe/Berlin'))
    event = services.create_event(people[0], title='Созвон', starts_at=start, ends_at=start+timedelta(hours=1))
    assert event.reminders.get().due_at == start.astimezone(UTC)-timedelta(hours=1)
