"""Copy selected place details into plans without following future address changes."""
from django.core.exceptions import ValidationError


def place_label(snapshot):
    if snapshot.get('address'):
        return snapshot['address'][:500]
    if snapshot.get('latitude') is not None and snapshot.get('longitude') is not None:
        return f"{snapshot['latitude']},{snapshot['longitude']}"
    return str(snapshot.get('title', ''))[:500]


def event_place_fields(user, data, visibility, shared_context=False):
    from .addressbook import place_for_link, snapshot_place
    if 'place_id' not in data:
        # A free-text replacement explicitly detaches the old directory entry.
        return {'place_id': None, 'place_snapshot': {}} if 'location' in data else {}
    if data['place_id'] in (None, ''):
        return {'place_id': None, 'place_snapshot': {}}
    place = place_for_link(user, data['place_id'], visibility, shared_context=shared_context)
    snapshot = snapshot_place(place)
    return {'place_id': str(place.pk), 'place_snapshot': snapshot, 'location': place_label(snapshot)}


def validate_event_place(user, event, changes):
    """Recheck selection when an approved update finally applies."""
    from .addressbook import place_for_link
    identifier = changes.get('place_id', event.place_id)
    if not identifier:
        return
    if 'place_id' not in changes and 'visibility' not in changes:
        return
    place = place_for_link(user, identifier, changes.get('visibility', event.visibility))
    if 'place_id' in changes:
        snapshot = changes.get('place_snapshot') or {}
        if snapshot.get('version') != place.version or snapshot.get('id') != str(place.pk):
            raise ValidationError('Адресная карточка изменилась. Выберите актуальное место и повторите изменение.')


def prepare_segment_places(user, args, visibility, shared_context=False):
    from .addressbook import place_for_link, snapshot_place
    values = {}
    for side in ('origin', 'destination'):
        identifier = args.get(side + '_place_id')
        if not identifier:
            continue
        place = place_for_link(user, identifier, visibility, shared_context=shared_context)
        snapshot = snapshot_place(place)
        values.update({side + '_place_id': place.pk, side + '_snapshot': snapshot,
                       side: place_label(snapshot)})
    return values
