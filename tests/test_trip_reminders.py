from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch

import pytest

from assistant import runtime, services
from assistant.models import Job, Trip

TRAVEL_DAY = date(2026, 11, 2)
MOSCOW_EIGHT = datetime(2026, 11, 1, 5, tzinfo=timezone.utc)


def reminders():
    return Job.objects.filter(kind='trip_reminder')


def test_dated_trip_notifies_each_traveler_on_previous_local_day(people):
    boris, anna = people
    anna.timezone = 'America/New_York'
    anna.save()
    # This already-saved trip predates the notification feature.
    trip = Trip.objects.create(owner=boris, title='Театральные выходные', starts_on=TRAVEL_DAY,
                               visibility='shared')
    trip.participants.set(people)
    runtime.schedule_tick(MOSCOW_EIGHT - timedelta(seconds=1))
    assert not reminders().exists()
    runtime.schedule_tick(MOSCOW_EIGHT)
    runtime.schedule_tick(MOSCOW_EIGHT)
    job = reminders().get()
    assert job.payload['person_id'] == str(boris.pk)
    assert trip.title not in str(job.payload)
    with patch.object(runtime.timezone, 'now', return_value=MOSCOW_EIGHT), \
         patch.object(runtime, '_tg', return_value={'message_id': 12}) as tg:
        runtime.handle_job(job)
        runtime.handle_job(job)
        tg.assert_called_once()
        method, data = tg.call_args.args
        assert method == 'sendMessage' and data['chat_id'] == boris.chat_id
        assert trip.title in data['text'] and '02.11.2026' in data['text']
    # New York changes to winter time that morning.
    new_york_eight = MOSCOW_EIGHT + timedelta(hours=8)
    runtime.schedule_tick(new_york_eight - timedelta(seconds=1))
    assert reminders().count() == 1
    runtime.schedule_tick(new_york_eight)
    anna_job = reminders().get(payload__person_id=str(anna.pk))
    assert anna_job.run_at == new_york_eight


def test_trip_without_date_cancelled_or_visible_to_nontraveler_skipped(people):
    owner, reader = people
    services.create_trip(owner, title='Без даты')
    cancelled = services.create_trip(owner, title='Отменена', starts_on=TRAVEL_DAY)
    cancelled.status = 'cancelled'
    cancelled.save()
    trip = services.create_trip(owner, title='Виден обоим, едет владелец', starts_on=TRAVEL_DAY,
                                visibility='shared')
    old = Trip.objects.create(owner=owner, title='Старая без участников', starts_on=TRAVEL_DAY)
    other = services.create_trip(owner, title='Едет только Анна', starts_on=TRAVEL_DAY,
                                 visibility='shared', participants=[reader.pk])
    # Old malformed participant lists must not broaden a private trip's audience.
    old.participants.set([owner, reader])
    runtime.schedule_tick(MOSCOW_EIGHT)
    assert set(reminders().values_list('payload__trip_id', 'payload__person_id')) == {
        (str(trip.pk), str(owner.pk)), (str(old.pk), str(owner.pk)), (str(other.pk), str(reader.pk)),
    }


@pytest.mark.parametrize('change', ['revoke', 'remove', 'cancel', 'reschedule', 'deleted',
                                    'disallow', 'offline', 'invalid_chat', 'day_passed'])
def test_queued_trip_requires_current_access_membership_and_date(people, settings, change):
    owner, person = people
    trip = services.create_trip(owner, title='Личные билеты', starts_on=TRAVEL_DAY,
                                visibility='shared', participants=[person.pk])
    now = MOSCOW_EIGHT
    runtime.schedule_tick(now)
    job = reminders().get()
    if change == 'revoke':
        services.share_entity(owner, 'trip', trip.pk, 'private')
    elif change == 'remove':
        trip.participants.set([owner])
    elif change == 'cancel':
        trip.status = 'cancelled'
        trip.save()
    elif change == 'reschedule':
        trip.starts_on += timedelta(days=1)
        trip.save()
        now += timedelta(days=1)  # Old job catches up when new notification becomes due.
    elif change == 'deleted':
        trip.delete()
    elif change == 'disallow':
        settings.ALLOWED_TELEGRAM_IDS = (owner.telegram_id,)
    elif change in ('offline', 'invalid_chat'):
        person.chat_id = None if change == 'offline' else owner.chat_id
        person.save()
    else:
        now += timedelta(days=1)
    with patch.object(runtime.timezone, 'now', return_value=now), patch.object(runtime, '_tg') as tg:
        runtime.handle_job(job)
        tg.assert_not_called()
    if change == 'reschedule':
        runtime.schedule_tick(now)
        assert reminders().exclude(pk=job.pk).get().payload['starts_on'] == trip.starts_on.isoformat()


def test_quiet_hours_and_timezone_change_resume_within_eligible_day(people):
    person, _ = people
    # Legacy row with no participants should reach the owner.
    trip = Trip.objects.create(owner=person, title='Выезд', starts_on=TRAVEL_DAY)
    person.quiet_end = time(9)
    person.save()
    runtime.schedule_tick(MOSCOW_EIGHT)
    assert not reminders().exists()
    moscow_nine = MOSCOW_EIGHT + timedelta(hours=1)
    runtime.schedule_tick(moscow_nine)
    job = reminders().get()
    person.timezone = 'America/New_York'
    person.save()
    with patch.object(runtime.timezone, 'now', return_value=moscow_nine), patch.object(runtime, '_tg') as tg:
        runtime.handle_job(job)
        tg.assert_not_called()
    new_york_nine = moscow_nine + timedelta(hours=8)
    runtime.schedule_tick(new_york_nine)
    new_job = reminders().exclude(pk=job.pk).get()
    with patch.object(runtime.timezone, 'now', return_value=new_york_nine), \
         patch.object(runtime, '_tg', return_value={'message_id': 43}) as tg:
        runtime.handle_job(new_job)
        assert trip.title in tg.call_args.args[1]['text']
