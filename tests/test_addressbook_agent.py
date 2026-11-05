"""Addressbook tools execute through the real SDK adapter and durable agent ACLs."""
import ast
import asyncio
import json
from unittest.mock import patch

import pytest
from agents.tool_context import ToolContext

from assistant import addressbook, services
from assistant.agent import external
from assistant.agent.context import create_context
from assistant.agent.runner import build_tools
from assistant.models import AgentToolCall, Contact, Person, Place, TripSegment


pytestmark = pytest.mark.django_db(transaction=True)


def context(person, update_id=900, shared=False):
    return asyncio.run(create_context(person.pk, -500 if shared else person.chat_id,
                                     update_id, 'Помоги с адресами и маршрутом', shared_context=shared))


def invoke(ctx, name, arguments, call_id='test-call'):
    tool = next(tool for tool in build_tools() if tool.name == name)
    payload = json.dumps(arguments, ensure_ascii=False)
    wrapper = ToolContext(context=ctx, tool_name=name, tool_call_id=call_id, tool_arguments=payload)
    result = asyncio.run(tool.on_invoke_tool(wrapper, payload))
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            try:
                return ast.literal_eval(result)
            except (ValueError, SyntaxError):
                return {'ok': False, 'error': result}
    return result


def place(person, title, **kwargs):
    return addressbook.save_place(person, {'title': title, **kwargs})


@pytest.mark.parametrize('actor_index', [0, 1])
def test_native_save_place_has_durable_receipt_and_shared_edit(people, actor_index):
    owner, actor = people[1-actor_index], people[actor_index]
    original = place(owner, 'Общий офис', visibility='shared', address='Москва, Тверская 1',
                     address_confirmed=True, aliases=['офис компании'])
    ctx = context(actor)
    args = {'data': {'id': str(original.pk), 'expected_version': original.version,
                     'title': 'Офис команды', 'address': None, 'aliases': None}}
    first = invoke(ctx, 'save_place', args)
    repeated = invoke(ctx, 'save_place', args)
    assert first['ok'], first
    assert repeated['ok'] and repeated['replayed']
    original.refresh_from_db()
    assert original.owner == owner and original.title == 'Офис команды'
    assert original.address == 'Москва, Тверская 1' and original.aliases == ['офис компании']
    assert first['permission_refs'] == [{'kind': 'place', 'id': str(original.pk)}]
    assert AgentToolCall.objects.filter(run=ctx.run, name='save_place', status='completed').count() == 1
    stale = invoke(ctx, 'save_place', {'data': {'id': str(original.pk), 'expected_version': 1, 'title': 'Устаревшее'}})
    assert not stale['ok']


def test_native_contact_is_not_a_bot_user(people):
    actor, _ = people
    ctx = context(actor)
    result = invoke(ctx, 'save_contact', {'data': {'title': 'Друг', 'kind': 'person', 'phone': '+79990000000'}})
    assert result['ok'], result
    assert Contact.objects.count() == 1 and Person.objects.count() == 2
    assert result['permission_refs'] == [{'kind': 'contact', 'id': str(Contact.objects.get().pk)}]


def test_native_personal_alias_and_search_do_not_use_spouses_nickname(people):
    actor, spouse = people
    own = place(actor, 'Дом родственников', visibility='shared')
    other = place(spouse, 'Другой дом', visibility='shared')
    addressbook.set_place_alias(spouse, other.pk, 'мама')
    ctx = context(actor)
    saved = invoke(ctx, 'set_place_alias', {'place_id': str(own.pk), 'alias': 'мама'})
    assert saved['ok'], saved
    result = invoke(ctx, 'search_addressbook', {'kind': 'place', 'query': 'мама'})
    assert result['ok'], result
    assert [item['id'] for item in result['items']] == [str(own.pk)]
    assert result['permission_refs'] == [{'kind': 'place', 'id': str(own.pk)}]


def test_native_alias_can_be_added_removed_and_readded_within_one_run(people):
    actor, _ = people
    obj = place(actor, 'Дом')
    ctx = context(actor)
    for call_id, remove in [('add-1',False),('remove-2',True),('add-3',False)]:
        result = invoke(ctx, 'set_place_alias', {'place_id': str(obj.pk), 'alias': 'домой', 'remove': remove}, call_id)
        assert result['ok'], result
    found = invoke(ctx, 'search_addressbook', {'kind': 'place', 'query': 'домой'})
    assert found['total'] == 1


def test_native_segment_saves_place_snapshots_and_preserves_them_after_move(people):
    actor, spouse = people
    origin = place(spouse, 'Старый офис', visibility='shared', address='Москва, Тверская 1', address_confirmed=True)
    destination = place(actor, 'Встреча', visibility='shared', address='Москва, Тверская 2', address_confirmed=True)
    trip = services.create_trip(actor, title='Поездка', visibility='shared')
    event = services.create_event(actor, title='Дорога', visibility='shared',
                                  starts_at='2030-01-01T10:00+03:00', ends_at='2030-01-01T11:00+03:00')
    ctx = context(actor)
    result = invoke(ctx, 'add_trip_segment', {'trip_id': str(trip.pk), 'event_id': str(event.pk),
                    'origin_place_id': str(origin.pk), 'destination_place_id': str(destination.pk)})
    assert result['ok'], result
    segment = TripSegment.objects.get(pk=result['segment_id'])
    assert segment.origin_place_id == origin.pk and segment.destination_place_id == destination.pk
    assert segment.origin_snapshot['address'] == 'Москва, Тверская 1'
    addressbook.save_place(spouse, {'id': str(origin.pk), 'expected_version': origin.version,
                                   'address': 'Москва, Тверская 3', 'address_confirmed': True})
    segment.refresh_from_db()
    assert segment.origin_snapshot['address'] == 'Москва, Тверская 1'


def test_read_only_blocks_place_mutation_and_allows_read(people):
    actor, _ = people
    obj = place(actor, 'Место')
    actor.profile['agent_autonomy'] = 'read_only'
    actor.save()
    ctx = context(actor)
    result = invoke(ctx, 'save_place', {'data': {'title': 'Нельзя'}})
    assert not result['ok'] and Place.objects.count() == 1
    result = invoke(ctx, 'read_addressbook', {'kind': 'place', 'record_id': str(obj.pk)})
    assert result['ok'] and result['record']['title'] == obj.title


def test_route_places_sends_only_coords_or_confirmed_address_and_retains_acl(people):
    actor, _ = people
    start = place(actor, 'Дом', latitude='55.75', longitude='37.61', access_notes='Код 123456')
    end = place(actor, 'Офис', address='Москва, Тверская 1', address_confirmed=True,
                parking_notes='Личный телефон охранника')
    ctx = context(actor)
    with patch.object(external, 'route_between', return_value={'ok': True, 'routes': [], 'estimate': True}) as route:
        result = invoke(ctx, 'route_places', {'origin_place_id': str(start.pk), 'destination_place_id': str(end.pk)})
    assert result['ok'], result
    sent = route.call_args.kwargs
    assert [float(x) for x in sent['origin'].split(',')] == [55.75, 37.61]
    assert sent['destination'] == 'Москва, Тверская 1'
    assert '123456' not in str(sent) and 'охранника' not in str(sent)
    assert {ref['id'] for ref in result['permission_refs']} == {str(start.pk), str(end.pk)}
    assert {ref['id'] for ref in ctx.run.permission_refs} == {str(start.pk), str(end.pk)}
    assert ctx.external_count == 1


@pytest.mark.parametrize('shared', [False, True])
def test_route_places_refuses_private_foreign_or_incomplete_location(people, settings, shared):
    actor, spouse = people
    settings.SHARED_CHAT_ID = -500
    hidden = place(spouse, 'Закрытый адрес', address='Москва, Тверская 1', address_confirmed=True)
    known = place(actor, 'Адрес для всех', visibility='shared', latitude=55, longitude=37)
    vague = place(actor, 'Москва', visibility='shared', address='Москва')
    ctx = context(actor, shared=shared)
    with patch.object(external, 'route_between') as route:
        denied = invoke(ctx, 'route_places', {'origin_place_id': str(hidden.pk), 'destination_place_id': str(known.pk)})
        incomplete = invoke(ctx, 'route_places', {'origin_place_id': str(vague.pk), 'destination_place_id': str(known.pk)})
    assert not denied['ok'] and not incomplete['ok']
    assert 'полный адрес' in incomplete['error']
    route.assert_not_called()


def test_google_route_waypoints_keep_coordinates_numeric(settings):
    settings.GOOGLE_MAPS_API_KEY = 'test-maps-key'
    with patch.object(external, '_json_request', return_value={'routes': [{'duration': '120s'}]}) as request:
        result = external.route_between('55.75,37.61', 'Москва, Тверская 1')
    assert result['ok']
    sent = request.call_args.kwargs['json']
    assert sent['origin'] == {'location': {'latLng': {'latitude': 55.75, 'longitude': 37.61}}}
    assert sent['destination'] == {'address': 'Москва, Тверская 1'}
    with patch.object(external, '_json_request') as request:
        assert not external.route_between('91,37', '55,37')['ok']
    request.assert_not_called()
