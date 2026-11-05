"""Exercise the production upgrade path with pre-addressbook application data.

The same test runs on PostgreSQL 16 in GitHub Actions. Historical app models
ensure inserts match the old schema rather than the currently imported models.
"""
from datetime import datetime, timedelta, timezone

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_upgrade_preserves_legacy_events_segments_reminders_and_profile():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    previous = [('assistant', '0002_agent_execution')]
    try:
        executor.migrate(previous)
        old = executor.loader.project_state(previous).apps
        Person = old.get_model('assistant', 'Person')
        Trip = old.get_model('assistant', 'Trip')
        Event = old.get_model('assistant', 'Event')
        Segment = old.get_model('assistant', 'TripSegment')
        Reminder = old.get_model('assistant', 'Reminder')
        Checklist = old.get_model('assistant', 'Checklist')
        Item = old.get_model('assistant', 'ChecklistItem')
        start = datetime(2030, 1, 1, 9, tzinfo=timezone.utc)
        profile = {'locations': {'home': 'Старый домашний адрес', 'work': 'Старый рабочий адрес'}}
        person = Person.objects.create(telegram_id=1001, chat_id=1001, name='Борис', profile=profile)
        spouse = Person.objects.create(telegram_id=1002, chat_id=1002, name='Анна')
        trip = Trip.objects.create(owner=person, title='Существующая поездка', visibility='shared')
        trip.participants.set([person, spouse])
        event = Event.objects.create(
            owner=person, trip=trip, title='Существующий рейс', visibility='shared',
            starts_at=start, ends_at=start+timedelta(hours=2), location='Исходный адрес события',
        )
        event.participants.set([person, spouse])
        segment = Segment.objects.create(
            trip=trip, event=event, origin='Исходное отправление', destination='Исходное прибытие',
            booking_reference='TEST-BOOKING', booking_status='booked',
        )
        reminder = Reminder.objects.create(
            event=event, recipient=spouse, due_at=start-timedelta(hours=1),
            offset_minutes=60, status='pending', object_version=event.version,
        )
        checklist = Checklist.objects.create(trip=trip, source_text='Паспорт')
        item = Item.objects.create(checklist=checklist, text='Паспорт', checked_at=start, checked_by=spouse)

        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        current = executor.loader.project_state(latest).apps
        upgraded = current.get_model('assistant', 'Event').objects.get(pk=event.pk)
        upgraded_segment = current.get_model('assistant', 'TripSegment').objects.get(pk=segment.pk)
        upgraded_reminder = current.get_model('assistant', 'Reminder').objects.get(pk=reminder.pk)
        upgraded_item = current.get_model('assistant', 'ChecklistItem').objects.get(pk=item.pk)

        assert upgraded.owner_id == person.pk and upgraded.trip_id == trip.pk
        assert upgraded.starts_at == start and upgraded.ends_at == start+timedelta(hours=2)
        assert upgraded.location == 'Исходный адрес события'
        assert set(upgraded.participants.values_list('pk', flat=True)) == {person.pk, spouse.pk}
        assert upgraded.place_id is None and upgraded.place_snapshot == {}
        assert upgraded_segment.origin == 'Исходное отправление'
        assert upgraded_segment.destination == 'Исходное прибытие'
        assert upgraded_segment.origin_place_id is None and upgraded_segment.destination_place_id is None
        assert upgraded_segment.origin_snapshot == {} and upgraded_segment.destination_snapshot == {}
        assert upgraded_segment.booking_reference == 'TEST-BOOKING' and upgraded_segment.booking_status == 'booked'
        assert upgraded_reminder.recipient_id == spouse.pk
        assert upgraded_reminder.due_at == start-timedelta(hours=1) and upgraded_reminder.status == 'pending'
        assert upgraded_reminder.offset_minutes == 60 and upgraded_reminder.object_version == event.version
        assert upgraded_item.checked_at == start and upgraded_item.checked_by_id == spouse.pk
        assert current.get_model('assistant', 'Person').objects.get(pk=person.pk).profile == profile
        # New directories start empty; migration must not guess addresses or
        # accidentally turn private profile information into shared entries.
        assert current.get_model('assistant', 'Place').objects.count() == 0
        assert current.get_model('assistant', 'Contact').objects.count() == 0
    finally:
        # A failing assertion must not leave the schema downgraded for the
        # rest of the suite or pytest-django's teardown.
        MigrationExecutor(connection).migrate(latest)
