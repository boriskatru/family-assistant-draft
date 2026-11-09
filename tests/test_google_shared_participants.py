from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.utils import timezone

from assistant.integrations import google
from assistant.models import CalendarBinding, CalendarConnection, CalendarSource, Event, Person, Reminder
from assistant.planning import attending_events


pytestmark = pytest.mark.django_db


@pytest.fixture
def source(people, monkeypatch):
    monkeypatch.setattr(google, '_notify', Mock())
    connection = CalendarConnection.objects.create(person=people[0], encrypted_tokens='unused')
    return CalendarSource.objects.create(connection=connection, calendar_id='family@example.test', role='shared')


def item(**changes):
    start = timezone.now().replace(microsecond=0) + timedelta(days=3)
    result = {'id': 'provider-origin', 'etag': 'v1', 'summary': 'Семейная встреча',
              'visibility': 'default', 'status': 'confirmed',
              'start': {'dateTime': start.isoformat()},
              'end': {'dateTime': (start + timedelta(hours=1)).isoformat()}}
    result.update(changes)
    return result


def legacy_binding(source, people):
    remote = item()
    event = Event.objects.create(owner=people[0], sync_status='synced', **google._remote_fields(remote, source))
    event.participants.add(people[0])
    snapshot = google._snapshot(event)
    snapshot.pop('participants')
    snapshot.pop('counts_toward_load')
    binding = CalendarBinding.objects.create(source=source, event=event,
        external_id=remote['id'], etag=remote['etag'], last_version=1, snapshot=snapshot)
    return binding, remote


def test_shared_import_is_in_both_plans_with_one_reminder_each(source, people):
    # A registered but unapproved account must not gain attendance or reminders.
    outsider = Person.objects.create(name='Друг', telegram_id=9000)
    remote = item()
    google._import_item(source, remote)
    google._import_item(source, remote)
    event = CalendarBinding.objects.get(source=source).event
    assert set(event.participants.all()) == set(people)
    assert event.reminders.count() == 2
    assert not event.reminders.filter(recipient=outsider).exists()
    for person in people:
        assert attending_events(person, event.starts_at, event.ends_at).filter(pk=event.pk).exists()
    assert event.version == 1


def test_private_import_does_not_gain_family_participants(source, people):
    google._import_item(source, item(visibility='private'))
    event = CalendarBinding.objects.get(source=source).event
    assert event.visibility == 'private'
    assert set(event.participants.all()) == {people[0]}
    assert set(event.reminders.values_list('recipient_id', flat=True)) == {people[0].pk}
    assert not attending_events(people[1], event.starts_at, event.ends_at).exists()


def test_personal_calendar_import_keeps_owner_only(source, people):
    source.role = 'personal'
    source.save()
    google._import_item(source, item())
    event = CalendarBinding.objects.get(source=source).event
    assert event.visibility == 'private'
    assert set(event.participants.all()) == {people[0]}


def test_unchanged_legacy_import_is_repaired_without_conflict(source, people):
    binding, remote = legacy_binding(source, people)
    google._import_item(source, remote)
    google._import_item(source, remote)
    binding.refresh_from_db()
    event = binding.event
    assert set(event.participants.all()) == set(people)
    assert event.version == binding.last_version == 2
    assert event.sync_status == 'synced'
    assert not binding.conflict
    assert event.reminders.filter(recipient=people[1]).count() == 1


def test_legacy_repair_is_offline_dry_run_then_idempotent(source, people):
    binding, _ = legacy_binding(source, people)
    assert google.repair_shared_calendar_participants() == {'events': 1, 'participants_added': 1}
    assert binding.event.participants.count() == 1
    assert google.repair_shared_calendar_participants(apply=True) == {'events': 1, 'participants_added': 1}
    assert google.repair_shared_calendar_participants(apply=True) == {'events': 0, 'participants_added': 0}


def test_app_created_shared_single_attendee_event_not_expanded(source, people):
    binding, remote = legacy_binding(source, people)
    binding.external_id = 'fa' + binding.event_id.hex
    binding.save()
    remote['id'] = binding.external_id
    google._import_item(source, remote)
    assert set(binding.event.participants.all()) == {people[0]}
    assert google.repair_shared_calendar_participants(apply=True)['events'] == 0


def test_explicit_participant_removal_survives_later_import(source, people):
    remote = item()
    google._import_item(source, remote)
    binding = CalendarBinding.objects.get(source=source)
    event = binding.event
    event.participants.set([people[0]])
    event.version += 1
    event.save()
    google._import_item(source, remote)
    assert set(event.participants.all()) == {people[0]}
    assert google.repair_shared_calendar_participants(apply=True)['events'] == 0


def test_provider_privacy_reduction_removes_spouse_busy_time_and_reminders(source, people):
    remote = item()
    google._import_item(source, remote)
    event = CalendarBinding.objects.get(source=source).event
    google._import_item(source, {**remote, 'etag': 'v2', 'visibility': 'private'})
    event.refresh_from_db()
    assert event.visibility == 'private'
    assert set(event.participants.all()) == {people[0]}
    assert not event.reminders.filter(recipient=people[1], status='pending').exists()
    assert not attending_events(people[1], event.starts_at, event.ends_at).exists()


def test_provider_sharing_formerly_private_event_adds_spouse(source, people):
    remote = item(visibility='private')
    google._import_item(source, remote)
    google._import_item(source, {**remote, 'etag': 'v2', 'visibility': 'default'})
    event = CalendarBinding.objects.get(source=source).event
    assert event.visibility == 'shared'
    assert set(event.participants.all()) == set(people)
    assert set(event.reminders.filter(status='pending').values_list('recipient_id', flat=True)) == {p.pk for p in people}


def test_metadata_edit_does_not_repeat_delivered_reminders(source, people):
    remote = item()
    google._import_item(source, remote)
    event = CalendarBinding.objects.get(source=source).event
    event.reminders.update(status='sent')
    google._import_item(source, {**remote, 'etag': 'v2', 'description': 'Новый адрес входа'})
    assert event.reminders.count() == 2
    assert event.reminders.filter(status='sent').count() == 2


@pytest.mark.parametrize('value', [True, False, None])
def test_explicit_load_override_roundtrips_google_metadata(source, people, value):
    google._import_item(source, item())
    event = CalendarBinding.objects.get(source=source).event
    event.counts_toward_load = value
    event.save()
    payload = google._export_payload(event)
    assert google._remote_fields(payload, source)['counts_toward_load'] is value
