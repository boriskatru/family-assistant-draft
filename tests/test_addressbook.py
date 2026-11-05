from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from assistant import services
from assistant.addressbook import (place_for_link, read_addressbook, save_contact, save_place,
                                   search_addressbook, set_place_alias, snapshot_place)
from assistant.models import Contact, Event, Person, Place, PlaceAlias


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('creator_index', [0, 1])
@pytest.mark.parametrize('kind,save', [('place', save_place), ('contact', save_contact)])
def test_both_spouses_edit_existing_shared_card_without_owner_transfer(people, creator_index, kind, save):
    owner, spouse = people[creator_index], people[1-creator_index]
    original = save(owner, {'title': 'Общая карточка', 'visibility': 'shared'})
    edited = save(spouse, {'id': original.pk, 'expected_version': original.version, 'description': 'Уточнение'})
    assert edited.owner == owner and edited.version == 2
    assert edited.title == 'Общая карточка' and edited.description == 'Уточнение'
    assert not edited.editors.exists()
    card = read_addressbook(spouse, kind, original.pk)
    assert card['record']['can_edit'] is True
    assert card['permission_refs'][0] == {'kind': kind, 'id': str(original.pk)}
    with pytest.raises(ValidationError, match='изменилась'):
        save(owner, {'id': original.pk, 'expected_version': 1, 'title': 'Старая версия'})


@pytest.mark.parametrize('kind,save', [('place', save_place), ('contact', save_contact)])
def test_private_cards_hidden_from_spouse_group_and_stranger(people, settings, kind, save):
    owner, spouse = people
    card = save(owner, {'title': 'Секретное место'})
    assert search_addressbook(spouse, kind, 'Секретное')['total'] == 0
    for viewer, group in [(spouse, False), (owner, True)]:
        with pytest.raises(PermissionDenied):
            read_addressbook(viewer, kind, card.pk, shared_context=group)
        with pytest.raises(PermissionDenied):
            save(viewer, {'id': card.pk, 'expected_version': card.version, 'title': 'Перезаписать'}, shared_context=group)
    settings.ALLOWED_TELEGRAM_IDS = (spouse.telegram_id,)
    with pytest.raises(PermissionDenied):
        search_addressbook(owner, kind)


def test_group_creation_shared_default_and_private_visibility_is_rejected(people):
    owner, spouse = people
    place = save_place(owner, {'title': 'Офис'}, shared_context=True)
    assert place.visibility == 'shared'
    with pytest.raises(PermissionDenied):
        save_place(owner, {'title': 'Личный дом', 'visibility': 'private'}, shared_context=True)
    with pytest.raises(PermissionDenied, match='владелец'):
        save_place(spouse, {'id': place.pk, 'expected_version': place.version, 'visibility': 'private'})
    assert Place.objects.count() == 1


def test_contact_is_not_a_bot_user_and_place_can_link_company_and_project(people):
    owner, spouse = people
    project = services.create_project(owner, title='Внедрение', visibility='shared')
    company = save_contact(owner, {'title': 'Ахмад Чай', 'kind': 'organization',
                                  'aliases': ['Ahmad Tea'], 'visibility': 'shared', 'project_ids': [project.pk]})
    office = save_place(owner, {'title': 'Завод', 'visibility': 'shared', 'contact_ids': [company.pk],
                               'project_ids': [project.pk], 'address': 'Неполный адрес'})
    assert Person.objects.count() == 2 and Contact.objects.count() == 1
    result = search_addressbook(spouse, 'place', 'ахмад')
    assert [item['id'] for item in result['items']] == [str(office.pk)]
    assert {'kind': 'contact', 'id': str(company.pk)} in result['permission_refs']
    assert search_addressbook(spouse, 'place', 'AHMAD')['total'] == 1
    contact = read_addressbook(spouse, 'contact', company.pk)
    assert contact['record']['contact_kind'] == 'organization'
    assert contact['record']['places'][0]['id'] == str(office.pk)
    assert {'kind': 'project', 'id': str(project.pk)} in contact['permission_refs']


def test_linked_private_contact_cannot_be_published_or_inferred(people):
    owner, spouse = people
    contact = save_contact(owner, {'title': 'Личный друг'})
    with pytest.raises(PermissionDenied):
        save_place(owner, {'title': 'Общий офис', 'visibility': 'shared', 'contact_ids': [contact.pk]})
    place = save_place(owner, {'title': 'Личное место', 'contact_ids': [contact.pk]})
    with pytest.raises(PermissionDenied):
        save_place(owner, {'id': place.pk, 'expected_version': place.version, 'visibility': 'shared'})
    # Model an existing common place whose linked contact has become private.
    place.visibility = 'shared'
    place.save(update_fields=['visibility'])
    assert read_addressbook(spouse, 'place', place.pk)['record']['contacts'] == []
    assert search_addressbook(spouse, 'place', 'Личный друг')['total'] == 0


def test_personal_aliases_resolve_relative_to_sender_and_report_ambiguity(people):
    boris, anna = people
    first = save_place(boris, {'title': 'Дом мамы Бориса', 'visibility': 'shared'})
    second = save_place(anna, {'title': 'Дом мамы Анны', 'visibility': 'shared'})
    for actor, place in [(boris, first), (anna, second)]:
        set_place_alias(actor, place.pk, 'Мама')
        set_place_alias(actor, place.pk, 'мама')
    assert PlaceAlias.objects.count() == 2
    assert search_addressbook(boris, 'place', 'МАМА')['items'][0]['id'] == str(first.pk)
    assert search_addressbook(anna, 'place', 'мама', shared_context=True)['items'][0]['id'] == str(second.pk)
    assert read_addressbook(anna, 'place', first.pk)['record']['personal_aliases'] == []
    set_place_alias(boris, second.pk, 'мама')
    result = search_addressbook(boris, 'place', 'мама', limit=1)
    assert result['ambiguous'] and result['total'] == 2 and result['next_offset'] == 1
    set_place_alias(boris, second.pk, 'мама', remove=True)
    assert not search_addressbook(boris, 'place', 'мама')['ambiguous']


def test_private_alias_target_stays_private_and_common_aliases_unicode_normalized(people):
    owner, spouse = people
    place = save_place(owner, {'title': 'Дом в Королёве', 'aliases': ['ДАЧА', ' дача ', 'Королёв']})
    assert place.aliases == ['ДАЧА', 'Королёв']
    set_place_alias(owner, place.pk, 'Дом')
    assert search_addressbook(owner, 'place', 'КОРОЛЕВ')['total'] == 1
    assert search_addressbook(spouse, 'place', 'Дом')['total'] == 0
    assert search_addressbook(owner, 'place', 'Дом', shared_context=True)['total'] == 0
    with pytest.raises(PermissionDenied):
        set_place_alias(spouse, place.pk, 'Дом')


@pytest.mark.parametrize('data', [
    {'latitude': 91, 'longitude': 10}, {'latitude': 10},
    {'latitude': float('nan'), 'longitude': 10}, {'latitude': 1, 'longitude': 181},
    {'map_url': 'javascript:alert(1)'}, {'map_url': 'https://user:password@example.com'},
    {'visibility': 'busy_only'}, {'address_confirmed': True},
])
def test_bad_or_incomplete_values_rejected_without_partial_write(people, data):
    owner, _ = people
    with pytest.raises(ValidationError):
        save_place(owner, {'title': 'Точка', **data})
    assert not Place.objects.exists()


def test_address_change_clears_old_coordinates_confirmation_and_preserves_event_snapshot(people):
    owner, _ = people
    place = save_place(owner, {'title': 'Офис', 'address': 'Москва, улица Тестовая, 1',
                              'address_confirmed': True, 'latitude': 55.7, 'longitude': 37.6,
                              'access_notes': 'Внутренний код', 'parking_notes': 'За воротами'})
    snapshot = snapshot_place(place)
    start = timezone.now()+timedelta(days=1)
    event = Event.objects.create(owner=owner, title='Встреча', starts_at=start,
                                 ends_at=start+timedelta(hours=1), place=place,
                                 place_snapshot=snapshot, location=place.address)
    updated = save_place(owner, {'id': place.pk, 'expected_version': place.version, 'address': 'Новый город'})
    assert updated.latitude is None and updated.longitude is None and not updated.address_confirmed
    event.refresh_from_db()
    assert event.location == 'Москва, улица Тестовая, 1' and event.place_snapshot == snapshot
    assert 'access_notes' not in snapshot and 'parking_notes' not in snapshot
    assert 'Внутренний код' not in str(snapshot)


def test_explicit_new_coordinates_and_clearing_are_supported(people):
    owner, _ = people
    place = save_place(owner, {'title': 'Место', 'latitude': 1, 'longitude': 2})
    changed = save_place(owner, {'id': place.pk, 'expected_version': place.version,
                                 'address': 'Адрес', 'latitude': 3, 'longitude': 4})
    assert (changed.latitude, changed.longitude) == (3, 4)
    cleared = save_place(owner, {'id': changed.pk, 'expected_version': changed.version, 'clear_coordinates': True})
    assert cleared.latitude is None and cleared.longitude is None


def test_place_link_acl_and_db_coordinate_constraint(people):
    owner, spouse = people
    place = save_place(owner, {'title': 'Личный адрес'})
    assert place_for_link(owner, place.pk, 'private') == place
    with pytest.raises(PermissionDenied):
        place_for_link(owner, place.pk, 'shared')
    with pytest.raises(PermissionDenied):
        place_for_link(spouse, place.pk, 'private')
    with pytest.raises(IntegrityError), transaction.atomic():
        Place.objects.filter(pk=place.pk).update(latitude=10)
    place.refresh_from_db()
    assert place.latitude is None
