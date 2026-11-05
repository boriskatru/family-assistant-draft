from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from assistant import addressbook, services
from assistant.agent.retrieval import read_record
from assistant.models import ActionRequest, Event
from assistant.place_links import prepare_segment_places
from assistant.recurrence import create_rule_from_event, materialize_recurrences

pytestmark = pytest.mark.django_db
START = datetime(2030, 1, 1, 9, tzinfo=timezone.utc)


def place(owner, visibility='private'):
    return addressbook.save_place(owner, {'title': 'Мастерская', 'address': 'Тестовый адрес 10',
        'visibility': visibility, 'latitude': 55.1, 'longitude': 37.2,
        'access_notes': 'Код двери 8642', 'parking_notes': 'Парковка во дворе'})


def event(owner, location=None, **values):
    return services.create_event(owner, title='Работа', starts_at=START,
        ends_at=START+timedelta(hours=1), **({'location': location} if location is not None else {}), **values)


def test_selected_place_copies_address_and_keeps_history_after_move(people):
    owner, _ = people
    location = place(owner)
    first = event(owner, place_id=location.pk)
    old_snapshot = first.place_snapshot.copy()
    assert first.location == 'Тестовый адрес 10'
    assert 'access_notes' not in old_snapshot and 'parking_notes' not in old_snapshot
    addressbook.save_place(owner, {'id': location.pk, 'expected_version': location.version,
                                   'address': 'Новый тестовый адрес 20'})
    first.refresh_from_db()
    assert first.place_snapshot == old_snapshot and first.location == old_snapshot['address']
    record = read_record(owner, 'event', first.pk)['record']
    assert record['place_snapshot'] == old_snapshot
    assert record['place_id'] == str(location.pk)
    # An explicit selection later refreshes the snapshot.
    services.update_event(owner, first.pk, {'place_id': location.pk}, expected_version=first.version)
    first.refresh_from_db()
    assert first.location == 'Новый тестовый адрес 20' and first.place_snapshot['version'] == 2


def test_private_place_does_not_widen_through_shared_event_or_trip(people):
    owner, spouse = people
    location = place(owner)
    with pytest.raises(PermissionDenied):
        event(owner, place_id=location.pk, visibility='shared', participants=[owner.pk, spouse.pk])
    first = event(owner, place_id=location.pk)
    with pytest.raises(PermissionDenied):
        services.update_event(owner, first.pk, {'visibility': 'shared'})
    with pytest.raises(PermissionDenied):
        prepare_segment_places(owner, {'origin_place_id': location.pk}, 'shared')
    assert Event.objects.count() == 1


def test_shared_place_selection_is_immediate_and_keeps_snapshot_until_next_edit(people):
    owner, spouse = people
    location = place(owner, 'shared')
    first = event(owner, visibility='shared', participants=[owner.pk, spouse.pk])
    updated = services.update_event(spouse, first.pk, {'place_id': location.pk}, first.version)
    assert isinstance(updated, Event)
    assert updated.place_id == location.pk and updated.location == location.address
    snapshot = updated.place_snapshot.copy()
    assert not ActionRequest.objects.exists()
    addressbook.save_place(owner, {'id': location.pk, 'expected_version': location.version,
                                   'address': 'Новый адрес'})
    first.refresh_from_db()
    assert first.location == snapshot['address'] and first.place_snapshot == snapshot
    services.update_event(spouse, first.pk, {'place_id': location.pk}, first.version)
    first.refresh_from_db()
    assert first.location == 'Новый адрес'
    assert not ActionRequest.objects.exists()


def test_plain_location_replacement_detaches_previous_place(people):
    owner, _ = people
    first = event(owner, place_id=place(owner).pk)
    services.update_event(owner, first.pk, {'location': 'Другая площадка'})
    first.refresh_from_db()
    assert first.location == 'Другая площадка' and first.place_id is None and first.place_snapshot == {}
    with pytest.raises(ValidationError):
        services.update_event(owner, first.pk, {'place_snapshot': {'address': 'Поддельный адрес'}})


def test_recurrence_uses_agreed_snapshot_after_directory_changes(people):
    owner, spouse = people
    location = place(owner, 'shared')
    first = event(owner, place_id=location.pk, visibility='shared', participants=[owner.pk, spouse.pk])
    rule = create_rule_from_event(spouse, first, rrule='FREQ=DAILY;COUNT=2')
    addressbook.save_place(owner, {'id': location.pk, 'expected_version': location.version, 'address': 'Новый адрес'})
    materialize_recurrences(START)
    following = Event.objects.filter(recurrence=rule).exclude(pk=first.pk).get()
    assert following.place_id == location.pk
    assert following.location == first.location
    assert following.place_snapshot == first.place_snapshot


def test_place_reply_history_rechecks_place_access(people):
    from assistant.chat_memory import remember_turn, read_chat_history
    owner, spouse = people
    location = place(owner, 'shared')
    reply = {'text': location.address, 'permission_refs': [{'kind': 'place', 'id': str(location.pk)}]}
    remember_turn(spouse, spouse.chat_id, 10, 'Где мастерская?', reply)
    assert read_chat_history(spouse, spouse.chat_id, False)[0]
    addressbook.save_place(owner, {'id': location.pk, 'expected_version': location.version, 'visibility': 'private'})
    assert read_chat_history(spouse, spouse.chat_id, False) == ([], [])


def test_contact_notes_and_phone_are_not_public_queries(people):
    from assistant.agent.egress import EgressGuard
    guard = EgressGuard()
    guard.observe({'phone': '+79991234567', 'email': 'example@local.test', 'access_notes': 'Код двери 8642'})
    for sensitive in ('79991234567', 'example@local.test', 'Код двери 8642'):
        with pytest.raises(PermissionDenied):
            guard.check({'query': sensitive})
    guard.check({'origin': '55.1,37.2', 'destination': 'Тестовый адрес 10'})
