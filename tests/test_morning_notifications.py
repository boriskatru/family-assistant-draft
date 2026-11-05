from datetime import datetime, time, timedelta, timezone
from unittest.mock import patch

import pytest

from assistant import planning, runtime
from assistant.models import DailyPlan, Event, Job


UTC = timezone.utc
MORNING = datetime(2030, 1, 7, 5, tzinfo=UTC)  # 08:00 MSK / 14:00 JST.


def private_event(owner, label, start):
    event = Event.objects.create(owner=owner, title=label, starts_at=start,
                                 ends_at=start + timedelta(hours=1))
    event.participants.add(owner)
    return event


def tick_and_deliver(now):
    with patch('assistant.runtime.timezone.now', return_value=now):
        runtime.schedule_tick(now)
        for job in Job.objects.filter(kind='daily_plan'):
            runtime.handle_job(job)


def test_default_mornings_follow_personal_time_zones_and_deliver_once(people):
    boris, anna = people
    anna.timezone = 'Asia/Tokyo'
    anna.save(update_fields=['timezone'])
    private_event(boris, 'Совещание Бориса', MORNING + timedelta(hours=1))
    private_event(anna, 'Занятие Анны', MORNING)
    tokyo_morning = MORNING - timedelta(hours=6)
    with patch('assistant.runtime._tg', return_value={'message_id': 10}) as telegram:
        tick_and_deliver(tokyo_morning - timedelta(seconds=1))
        telegram.assert_not_called()
        tick_and_deliver(tokyo_morning)
        tick_and_deliver(tokyo_morning)
        tick_and_deliver(MORNING)
        tick_and_deliver(MORNING + timedelta(minutes=1))
    assert [call.args[1]['chat_id'] for call in telegram.call_args_list] == [anna.chat_id, boris.chat_id]
    anna_plan, boris_plan = [call.args[1]['text'] for call in telegram.call_args_list]
    assert '07.01.2030' in anna_plan and '14:00' in anna_plan and 'Занятие Анны' in anna_plan
    assert 'Совещание Бориса' not in anna_plan
    assert '09:00' in boris_plan and 'Совещание Бориса' in boris_plan and 'Занятие Анны' not in boris_plan
    assert Job.objects.filter(kind='daily_plan').count() == 2
    assert list(DailyPlan.objects.values_list('version', flat=True)) == [1, 1]


def test_custom_morning_and_scheduler_catchup_window(people):
    boris, anna = people
    boris.morning_time = time(10)
    boris.save(update_fields=['morning_time'])
    anna.chat_id = None
    anna.save(update_fields=['chat_id'])
    with patch('assistant.runtime._tg', return_value={'message_id': 12}) as telegram:
        tick_and_deliver(MORNING)
        telegram.assert_not_called()
        # Scheduler resumes at 10:12 local, within 3 hours of the configured morning.
        tick_and_deliver(MORNING + timedelta(hours=2, minutes=12))
        # The following day's scheduler resumes after that window has passed.
        tick_and_deliver(MORNING + timedelta(days=1, hours=5))
    telegram.assert_called_once()
    assert telegram.call_args.args[1]['chat_id'] == boris.chat_id


@pytest.mark.parametrize('change', ['next_day', 'access_removed', 'chat_mismatch'])
def test_delayed_morning_job_rechecks_day_and_recipient(people, settings, change):
    boris, _ = people
    runtime.schedule_tick(MORNING)
    job = Job.objects.get(kind='daily_plan', payload__person_id=str(boris.pk))
    now = MORNING
    if change == 'access_removed':
        settings.ALLOWED_TELEGRAM_IDS = ()
    elif change == 'chat_mismatch':
        boris.chat_id = 999
        boris.save(update_fields=['chat_id'])
    else:
        now += timedelta(days=1)
    with patch('assistant.runtime.timezone.now', return_value=now), patch('assistant.runtime._tg') as telegram:
        runtime.handle_job(job)
    telegram.assert_not_called()
    assert not DailyPlan.objects.filter(person=boris).exists()


def test_morning_keeps_guards_of_the_actual_message_across_concurrent_renders(people):
    owner, other = people
    event = private_event(owner, 'Сведения о сделке', MORNING)
    event.visibility = 'shared'
    event.save(update_fields=['visibility'])
    event.participants.add(other)
    runtime.schedule_tick(MORNING)
    job = Job.objects.get(kind='daily_plan', payload__person_id=str(other.pk))
    render = planning.daily_plan_message

    def concurrent_render(person, day):
        original = render(person, day)
        assert 'Сведения о сделке' in original['text']
        event.participants.remove(other)
        event.visibility = 'private'
        event.save(update_fields=['visibility'])
        assert render(person, day)['permission_refs'] == []
        return original

    with patch('assistant.runtime.timezone.now', return_value=MORNING), \
         patch.object(planning, 'daily_plan_message', side_effect=concurrent_render), \
         patch('assistant.runtime._tg', return_value={'message_id': 31}) as telegram:
        runtime.handle_job(job)
    assert 'Доступ к данным изменился' in telegram.call_args.args[1]['text']
    assert 'Сведения о сделке' not in telegram.call_args.args[1]['text']
