"""Address book commands use the durable Telegram path without external calls."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from django.core.exceptions import PermissionDenied

from assistant import addressbook, bot, runtime
from assistant.models import Contact, IncomingUpdate, Job, Person, Place


pytestmark = pytest.mark.django_db


def payload(person, text='', number=1, shared=False, **fields):
    return {'update_id': number, 'message': {
        'message_id': number,
        'from': {'id': person.telegram_id, 'is_bot': False},
        'chat': {'id': -500 if shared else person.chat_id, 'type': 'supergroup' if shared else 'private'},
        'text': text, **fields,
    }}


def finish(person, incoming):
    bot._finish_sync(incoming, bot._identity(incoming), person.pk)


@pytest.mark.parametrize('actor_index', [0, 1])
def test_place_commands_work_for_both_and_do_not_duplicate_updates(people, actor_index):
    actor, spouse = people[actor_index], people[1-actor_index]
    incoming = payload(actor, '/place Мастерская | Город, улица, 1 | shared')
    finish(actor, incoming)
    finish(actor, incoming)
    place = Place.objects.get()
    assert place.owner == actor and place.visibility == 'shared'
    assert place.address == 'Город, улица, 1' and place.address_confirmed is False
    assert Job.objects.filter(kind='telegram_send').count() == 1
    assert IncomingUpdate.objects.get(update_id=1).status == 'completed'
    result = bot.dispatch_command(spouse, spouse.chat_id, f'/place_edit {place.pk} | Другой город, дом 2')
    place.refresh_from_db()
    assert place.address == 'Другой город, дом 2' and place.owner == actor
    assert result['permission_refs'] == [{'kind': 'place', 'id': str(place.pk)}]
    assert 'Мастерская' in bot.dispatch_command(spouse, spouse.chat_id, f'/show place {place.pk}')['text']


def test_contacts_are_addressbook_records_and_do_not_authorise_users(people):
    actor, _ = people
    count = Person.objects.count()
    finish(actor, payload(actor, '/contact Компания | organization | shared | +7 000 0000000 | team@example.test'))
    contact = Contact.objects.get()
    assert contact.kind == 'organization' and contact.phone == '+7 000 0000000'
    assert Person.objects.count() == count
    card = bot.dispatch_command(actor, actor.chat_id, f'/show contact {contact.pk}')
    assert 'team@example.test' in card['text']
    assert card['entity_type'] == 'contact'


def test_shared_group_only_lists_shared_places_and_cannot_publish_private_entry(people, settings):
    actor, spouse = people
    settings.TELEGRAM_GROUP_ID = settings.SHARED_CHAT_ID = -500
    private = addressbook.save_place(actor, {'title': 'Личный адрес'})
    public = addressbook.save_place(spouse, {'title': 'Общий адрес', 'visibility': 'shared'})
    result = bot.dispatch_command(actor, -500, '/places', shared=True)
    assert private.title not in result['text'] and public.title in result['text']
    with pytest.raises(PermissionDenied):
        bot.dispatch_command(actor, -500, f'/show place {private.pk}', shared=True)
    finish(actor, payload(actor, '/place Не сохранять | адрес | private', shared=True))
    assert Place.objects.count() == 2
    reply = Job.objects.get(kind='telegram_send')
    assert 'Личные места' in reply.payload['text']


def test_actor_aliases_search_and_ambiguity_are_preserved_in_ui(people):
    actor, spouse = people
    one = addressbook.save_place(actor, {'title': 'Место 1', 'visibility': 'shared'})
    two = addressbook.save_place(actor, {'title': 'Место 2', 'visibility': 'shared'})
    bot.dispatch_command(actor, actor.chat_id, f'/alias {one.pk} | у мамы')
    bot.dispatch_command(spouse, spouse.chat_id, f'/alias {two.pk} | у мамы')
    for user, expected, hidden in [(actor, one, two), (spouse, two, one)]:
        reply = bot.dispatch_command(user, user.chat_id, '/places У МАМЫ')
        assert expected.title in reply['text'] and hidden.title not in reply['text']
    bot.dispatch_command(actor, actor.chat_id, f'/alias {two.pk} | у мамы')
    assert 'несколько записей' in bot.dispatch_command(actor, actor.chat_id, '/places у мамы')['text']
    bot.dispatch_command(actor, actor.chat_id, f'/unalias {two.pk} | у мамы')
    assert 'несколько записей' not in bot.dispatch_command(actor, actor.chat_id, '/places у мамы')['text']


def test_addressbook_listing_is_paginated_and_buttons_reference_cards(people):
    actor, _ = people
    for n in range(21):
        addressbook.save_place(actor, {'title': f'Объект {n:02}'})
    first = bot.dispatch_command(actor, actor.chat_id, '/places')
    assert len(first['permission_refs']) == 20 and '/places  | 20' in first['text']
    last = bot.dispatch_command(actor, actor.chat_id, '/places | 20')
    assert len(last['permission_refs']) == 1
    callback = first['reply_markup']['inline_keyboard'][0][0]['callback_data']
    card = bot.dispatch_callback(actor, actor.chat_id, callback)
    assert card['entity_type'] == 'place' and 'Объект 00' in card['text']


@pytest.mark.parametrize('command', ['/place', '/contact', '/place_edit', '/place_confirm', '/alias',
                                     '/unalias', '/saveplace', '/places x | nope', '/places x | -1'])
def test_bad_addressbook_commands_fail_without_creating_records(people, command):
    actor, _ = people
    finish(actor, payload(actor, command))
    assert not Place.objects.exists() and not Contact.objects.exists()
    assert Job.objects.get(kind='telegram_send').payload['text']
    assert IncomingUpdate.objects.get(update_id=1).status == 'completed'


def test_map_link_is_saved_without_fetching_or_inventing_address(people):
    actor, _ = people
    with patch('assistant.agent.external.read_webpage') as fetch:
        finish(actor, payload(actor, '/place Место | https://maps.example.test/pin | private'))
    fetch.assert_not_called()
    place = Place.objects.get()
    assert place.map_url == 'https://maps.example.test/pin'
    assert place.address == '' and place.latitude is None and place.address_confirmed is False


def test_explicit_address_confirmation_and_change_invalidate_pin(people):
    actor, _ = people
    place = addressbook.save_place(actor, {'title': 'Место', 'address': 'Первый полный адрес',
                                          'latitude': 55, 'longitude': 37})
    bot.dispatch_command(actor, actor.chat_id, f'/place_edit {place.pk} | Второй полный адрес')
    place.refresh_from_db()
    assert place.latitude is None and place.longitude is None and place.address_confirmed is False
    bot.dispatch_command(actor, actor.chat_id, f'/place_confirm {place.pk}')
    place.refresh_from_db()
    assert place.address_confirmed is True


@pytest.mark.parametrize('as_venue', [False, True])
def test_native_pin_requires_explicit_save_and_preserves_coordinates(people, as_venue):
    actor, _ = people
    point = {'latitude': 55.75, 'longitude': 37.61}
    data = {'venue': {'location': point, 'title': 'Вход', 'address': 'Адрес входа'}} if as_venue else {'location': point}
    incoming = payload(actor, **data)
    finish(actor, incoming)
    assert not Place.objects.exists()
    assert '/saveplace' in Job.objects.get(kind='telegram_send').payload['text']
    saving = payload(actor, '/saveplace Наш вход | shared', number=2, reply_to_message=incoming['message'])
    finish(actor, saving)
    finish(actor, saving)
    place = Place.objects.get()
    assert place.title == 'Наш вход' and place.latitude == 55.75 and place.longitude == 37.61
    assert place.visibility == 'shared'
    assert place.address == ('Адрес входа' if as_venue else '')


@pytest.mark.django_db(transaction=True)
def test_natural_reply_to_pin_supplies_selected_point_to_agent(people, settings):
    actor, _ = people
    settings.AGENT_ENABLED = True
    incoming = payload(actor, 'Запомни точку как мастерскую', reply_to_message={
        'venue': {'location': {'latitude': 55.75, 'longitude': 37.61}, 'title': 'Вход', 'address': 'Адрес входа'}})
    with patch('assistant.agent.runner.run_agent', new=AsyncMock(return_value={'text': 'Сохранено', 'permission_refs': []})) as agent:
        asyncio.run(bot.process_update(incoming))
    request = agent.await_args.kwargs
    assert request['person_id'] == actor.pk and request['chat_id'] == actor.chat_id
    assert '"latitude": 55.75' in request['text'] and '"longitude": 37.61' in request['text']
    assert '"address": "Адрес входа"' in request['text']
    assert 'не утверждение о текущем местоположении' in request['text']


def test_queued_place_card_rechecks_visibility_before_delivery(people):
    actor, spouse = people
    place = addressbook.save_place(actor, {'title': 'Общий адрес', 'visibility': 'shared'})
    finish(spouse, payload(spouse, f'/show place {place.pk}'))
    queued = Job.objects.get(kind='telegram_send')
    addressbook.save_place(actor, {'id': str(place.pk), 'expected_version': place.version, 'visibility': 'private'})
    with patch.object(runtime, '_tg', return_value={'message_id': 1}) as send:
        with pytest.raises(PermissionDenied):
            runtime.handle_job(queued)
    send.assert_not_called()
