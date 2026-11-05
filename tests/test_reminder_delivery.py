from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from assistant import runtime, services
from assistant.models import Job


NOW = datetime(2030, 11, 19, 10, tzinfo=timezone.utc)


def appointment(person):
    return services.create_event(person, title='Встреча', starts_at=NOW + timedelta(minutes=15),
                                 ends_at=NOW + timedelta(hours=1), remind_minutes=[15])


def postpone(person, event):
    services.update_event(person, event.pk, {'starts_at': event.starts_at + timedelta(days=1),
                                             'ends_at': event.ends_at + timedelta(days=1)})


def test_tick_does_not_claim_a_rescheduled_event(people):
    person, _ = people
    event = appointment(person)
    reminder = event.reminders.get()

    def edit_then_check(*args):
        if quiet.call_count == 1:
            postpone(person, event)
        return None

    with patch.object(runtime, '_quiet_end', side_effect=edit_then_check) as quiet:
        runtime.schedule_tick(NOW)
    reminder.refresh_from_db()
    assert reminder.status == 'pending' and reminder.object_version == event.version + 1
    assert not Job.objects.filter(kind='reminder').exists()
    runtime.schedule_tick(NOW + timedelta(days=1))
    assert Job.objects.get(kind='reminder').payload['object_version'] == reminder.object_version


def test_reminder_and_job_roll_back_together_after_enqueue_fails(people):
    event = appointment(people[0])

    def fail(*args, **kwargs):
        assert event.reminders.get().status == 'queued'
        raise RuntimeError('outbox failed')

    with patch.object(runtime, 'enqueue', side_effect=fail), pytest.raises(RuntimeError, match='outbox failed'):
        runtime.schedule_tick(NOW)
    assert event.reminders.get().status == 'pending'
    runtime.schedule_tick(NOW)
    assert event.reminders.get().status == 'queued'
    assert Job.objects.filter(kind='reminder').count() == 1


@pytest.mark.parametrize('parent_kind', ['event', 'task'])
def test_finishing_an_old_delivery_preserves_new_version_and_status(people, parent_kind):
    person, _ = people
    if parent_kind == 'event':
        parent = appointment(person)
    else:
        parent = services.create_task(person, title='Срок', due_at=NOW + timedelta(hours=1))
    runtime.schedule_tick(NOW)
    job = Job.objects.get(kind='reminder')

    def edit_during_send(*args):
        if parent_kind == 'event':
            postpone(person, parent)
        else:
            services.share_entity(person, 'task', parent.pk, 'shared')

    with patch.object(runtime, 'send_payload', side_effect=edit_during_send) as send:
        runtime.handle_job(job)
        runtime.handle_job(job)
        send.assert_called_once()
    reminder = parent.reminders.get()
    assert reminder.status == 'pending' and reminder.object_version == parent.version + 1
    runtime.schedule_tick(reminder.due_at)
    assert Job.objects.filter(kind='reminder', payload__object_version=reminder.object_version).exists()


def test_call_scheduled_before_registration_reaches_only_registered_private_chat(people):
    person, _ = people
    person.chat_id = None
    person.save()
    start = datetime(2030, 11, 5, 12, tzinfo=timezone.utc)
    due = start - timedelta(hours=1)
    services.create_event(person, title='Созвон', starts_at=start, ends_at=start+timedelta(hours=1))
    runtime.schedule_tick(due)
    assert not Job.objects.filter(kind='reminder').exists()
    person.chat_id = person.telegram_id
    person.save()
    now = due + timedelta(minutes=1)
    runtime.schedule_tick(now)
    with patch('assistant.runtime.timezone.now', return_value=now), patch('assistant.runtime._tg', return_value={}) as tg:
        runtime.handle_job(Job.objects.get(kind='reminder'))
    assert tg.call_args.args[1]['chat_id'] == person.telegram_id
