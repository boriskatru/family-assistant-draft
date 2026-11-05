"""Address book services: explicit records, scoped aliases, stable event snapshots."""
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from pydantic import ValidationError as SchemaError

from .addressbook_schemas import ContactArgs, PlaceArgs
from .models import Contact, Place, PlaceAlias, Project


MODELS = {'place': Place, 'contact': Contact}


def normalize(value):
    return ' '.join(str(value).split()).casefold().replace('ё', 'е')


def _authorise(user):
    if user.telegram_id not in settings.ALLOWED_TELEGRAM_IDS:
        raise PermissionDenied('Пользователь не допущен к адресной книге.')


def _ref(kind, obj):
    return {'kind': kind, 'id': str(obj.pk)}


def _model(kind):
    model = MODELS.get(kind)
    if model is None:
        raise ValidationError('Раздел адресной книги: place или contact.')
    return model


def _read(user, kind, identifier, shared_context=False, lock=False):
    from .services import can_view
    _authorise(user)
    qs = _model(kind).objects.all()
    if lock:
        qs = qs.select_for_update()
    try:
        obj = qs.get(pk=identifier)
    except (_model(kind).DoesNotExist, ValidationError, TypeError, ValueError):
        raise PermissionDenied('Запись адресной книги не найдена или недоступна.') from None
    if not can_view(obj, user, shared_context):
        raise PermissionDenied('Запись адресной книги не найдена или недоступна.')
    return obj


def _validate(schema, data):
    try:
        return schema.model_validate(data).model_dump(exclude_none=True)
    except SchemaError as exc:
        # Do not echo input values (addresses/phones) in validation failures.
        messages = [str(error['msg']).removeprefix('Value error, ') for error in exc.errors()]
        raise ValidationError(' '.join(messages)) from None


def _linked(user, kind, ids, visibility, shared_context):
    from .services import get_entity
    result = []
    for pk in dict.fromkeys(ids):
        obj = get_entity(kind, pk, user, shared_context)
        if visibility == 'shared' and obj.visibility != 'shared':
            raise PermissionDenied('Для общей карточки выберите общие связанные записи.')
        result.append(obj)
    return result


def _save(user, kind, data, shared_context):
    from .services import _audit, can_edit
    _authorise(user)
    schema = PlaceArgs if kind == 'place' else ContactArgs
    values = _validate(schema, data)
    identifier = values.pop('id', None)
    expected_version = values.pop('expected_version', None)
    clear_coordinates = values.pop('clear_coordinates', False)
    relation_values = {key: values.pop(key) for key in ('contact_ids', 'project_ids') if key in values}
    if identifier:
        obj = _read(user, kind, identifier, shared_context, lock=True)
        if not can_edit(obj, user):
            raise PermissionDenied('Нет прав на изменение записи адресной книги.')
        if obj.version != expected_version:
            raise ValidationError('Запись уже изменилась. Откройте актуальную карточку.')
        if 'visibility' in values and values['visibility'] != obj.visibility and obj.owner_id != user.pk:
            raise PermissionDenied('Видимость карточки меняет её владелец.')
    else:
        obj = _model(kind)(owner=user)
        values.setdefault('visibility', 'shared' if shared_context else 'private')
    target_visibility = values.get('visibility', obj.visibility)
    if shared_context and target_visibility != 'shared':
        raise PermissionDenied('В общем чате создаются и изменяются только общие карточки.')
    if kind == 'place' and 'address' in values and values['address'] != obj.address:
        values.setdefault('address_confirmed', False)
        # An old coordinate must not silently survive an address change.
        if 'latitude' not in values:
            clear_coordinates = True
    if kind == 'place' and clear_coordinates:
        obj.latitude = obj.longitude = None
    for field, value in values.items():
        setattr(obj, field, value)
    if kind == 'place' and obj.address_confirmed and not obj.address.strip():
        raise ValidationError('Нельзя подтвердить пустой адрес.')

    linked = {}
    for key, related_kind, attr in (('contact_ids', 'contact', 'contacts'), ('project_ids', 'project', 'projects')):
        if key in relation_values:
            linked[attr] = _linked(user, related_kind, relation_values[key], target_visibility, shared_context)
        elif identifier and hasattr(obj, attr) and 'visibility' in values:
            # Visibility changes cannot publish private linked records.
            _linked(user, related_kind, list(getattr(obj, attr).values_list('pk', flat=True)), target_visibility, shared_context)
    searchable = [obj.title, obj.description, *obj.aliases]
    if kind == 'place':
        searchable += [obj.address, obj.category]
    else:
        searchable += [obj.relationship, obj.phone, obj.email]
    obj.search_text = normalize('\n'.join(searchable))
    if identifier:
        obj.version += 1
    obj.full_clean()
    obj.save()
    for attr, objects in linked.items():
        getattr(obj, attr).set(objects)
    _audit(user, obj, 'update_addressbook' if identifier else 'create_addressbook')
    return obj


@transaction.atomic
def save_place(user, data, shared_context=False):
    return _save(user, 'place', data, shared_context)


@transaction.atomic
def save_contact(user, data, shared_context=False):
    return _save(user, 'contact', data, shared_context)


def snapshot_place(place):
    """Snapshot only route/display fields; access notes and contact data stay separate."""
    return {'id': str(place.pk), 'version': place.version, 'title': place.title,
            'address': place.address, 'address_confirmed': place.address_confirmed,
            'latitude': place.latitude, 'longitude': place.longitude, 'map_url': place.map_url}


@transaction.atomic
def place_for_link(user, place_id, target_visibility, shared_context=False):
    # Event/segment mutations hold this row until their outer transaction ends,
    # so an address or access change cannot race copying the agreed snapshot.
    place = _read(user, 'place', place_id, shared_context, lock=True)
    if target_visibility == 'shared' and place.visibility != 'shared':
        raise PermissionDenied('Личный адрес нельзя привязать к общему событию. Сначала откройте доступ к месту.')
    return place


def _summary(kind, obj):
    result = {'id': str(obj.pk), 'kind': kind, 'title': obj.title,
              'visibility': obj.visibility, 'version': obj.version}
    if kind == 'place':
        result.update(address=obj.address, address_confirmed=obj.address_confirmed,
                      latitude=obj.latitude, longitude=obj.longitude, category=obj.category)
    else:
        result.update(contact_kind=obj.kind, relationship=obj.relationship)
    return result


def search_addressbook(user, kind, query='', offset=0, limit=20, shared_context=False):
    from .services import visible
    _authorise(user)
    model = _model(kind)
    if not isinstance(query, str) or len(query) > 300:
        raise ValidationError('Поисковый запрос: до 300 символов.')
    if type(offset) is not int or not 0 <= offset <= 100000 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValidationError('Некорректная страница адресной книги.')
    qs = visible(model.objects.all(), user, shared_context)
    term = normalize(query)
    personal_alias_match = False
    matched_contacts = None
    if term:
        if kind == 'place':
            specific = qs.filter(personal_aliases__owner=user, personal_aliases__normalized=term)
            if specific.exists():
                qs = specific
                personal_alias_match = True
            else:
                contacts = visible(Contact.objects.all(), user, shared_context).filter(search_text__contains=term)
                qs = qs.filter(Q(search_text__contains=term) | Q(contacts__in=contacts))
                matched_contacts = contacts
        else:
            qs = qs.filter(search_text__contains=term)
    qs = qs.order_by('title', 'pk').distinct()
    total = qs.count()
    objects = list(qs[offset:offset+limit])
    refs = [_ref(kind, obj) for obj in objects]
    if matched_contacts is not None:
        # The place/contact relationship is information too: delayed delivery
        # and chat history must recheck the contact that supplied the match.
        refs.extend(_ref('contact', contact) for contact in matched_contacts.filter(places__in=objects).distinct())
    return {'items': [_summary(kind, obj) for obj in objects], 'total': total,
            'next_offset': offset+len(objects) if offset+len(objects) < total else None,
            'ambiguous': bool(term and total > 1), 'personal_alias_match': personal_alias_match,
            'permission_refs': refs, 'untrusted_content': True}


def read_addressbook(user, kind, record_id, shared_context=False):
    from .services import can_edit, visible
    obj = _read(user, kind, record_id, shared_context)
    data = _summary(kind, obj)
    data.update(owner_id=str(obj.owner_id), description=obj.description, domain=obj.domain,
                aliases=obj.aliases, can_edit=can_edit(obj, user))
    refs = [_ref(kind, obj)]
    projects = list(visible(obj.projects.all(), user, shared_context).order_by('title', 'pk')[:30])
    data['projects'] = [{'id': str(p.pk), 'title': p.title} for p in projects]
    refs.extend(_ref('project', p) for p in projects)
    if kind == 'place':
        data.update(map_url=obj.map_url, access_notes=obj.access_notes, parking_notes=obj.parking_notes,
                    personal_aliases=list(obj.personal_aliases.filter(owner=user).order_by('alias').values_list('alias', flat=True)))
        contacts = list(visible(obj.contacts.all(), user, shared_context).order_by('title', 'pk')[:30])
        data['contacts'] = [_summary('contact', c) for c in contacts]
        refs.extend(_ref('contact', c) for c in contacts)
    else:
        data.update(phone=obj.phone, email=obj.email)
        places = list(visible(obj.places.all(), user, shared_context).order_by('title', 'pk')[:50])
        data['places'] = [_summary('place', p) for p in places]
        data['places_truncated'] = visible(obj.places.all(), user, shared_context).count() > 50
        refs.extend(_ref('place', p) for p in places)
    return {'record': data, 'permission_refs': refs, 'untrusted_content': True}


@transaction.atomic
def set_place_alias(user, place_id, alias, remove=False, shared_context=False):
    from .services import _audit
    place = _read(user, 'place', place_id, shared_context)
    if not isinstance(alias, str) or not 1 <= len(alias.strip()) <= 100 or type(remove) is not bool:
        raise ValidationError('Псевдоним места: от 1 до 100 символов.')
    display = ' '.join(alias.split())
    key = normalize(display)
    if remove:
        PlaceAlias.objects.filter(owner=user, place=place, normalized=key).delete()
    else:
        PlaceAlias.objects.update_or_create(owner=user, place=place, normalized=key, defaults={'alias': display})
    _audit(user, place, 'remove_place_alias' if remove else 'set_place_alias')
    return {'text': 'Личное название удалено.' if remove else 'Личное название места сохранено.',
            'place_id': str(place.pk), 'alias': display, 'permission_refs': [_ref('place', place)]}
