"""Versioned edits with the same ACLs as creation and record retrieval."""
from datetime import date, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from pydantic import ValidationError as SchemaError

from . import services
from .models import Checklist, ChecklistItem, Document, Event, Project, Trip, TripSegment
from .editing_schemas import (ProjectChanges, TripChanges, ChecklistChanges,
    ChecklistItemChanges, NewChecklistItem, TripSegmentChanges)


def _values(schema, changes):
    try:
        parsed = schema.model_validate(changes)
    except SchemaError as exc:
        raise ValidationError('Некорректные поля изменения записи.') from exc
    values = parsed.model_dump(exclude_none=True)
    # Clearing is explicit; null from strict function schemas means unchanged.
    for flag, target in [('clear_starts_on','starts_on'),('clear_ends_on','ends_on'),
                         ('clear_project','project_id'),('clear_assignee','assignee_id'),
                         ('clear_origin_place','origin_place_id'),('clear_destination_place','destination_place_id')]:
        if values.pop(flag, False):
            if target in values:
                raise ValidationError('Нельзя одновременно установить и очистить одно поле.')
            values[target] = None
    if not values:
        raise ValidationError('Не указаны изменения.')
    return values


def _json(value):
    if isinstance(value, (date, UUID)):
        return value.isoformat() if isinstance(value, date) else str(value)
    if isinstance(value, dict):
        return {key:_json(item) for key,item in value.items()}
    if isinstance(value, (list,tuple)):
        return [_json(item) for item in value]
    return value


def _locked(user, kind, identifier, version, shared_context=False):
    obj = services.get_entity(kind, identifier, user, shared_context)
    obj = type(obj).objects.select_for_update().get(pk=obj.pk)
    if not services.can_edit(obj, user):
        raise PermissionDenied('Нет прав на изменение записи.')
    if version is None or obj.version != version:
        raise ValidationError('Запись уже изменилась, откройте её заново.')
    return obj


def _people(ids, user):
    # Empty explicitly means no participants; services._people has a create default.
    return services._people(ids, user) if ids else []


def _span(obj):
    if obj.starts_on and obj.ends_on and obj.ends_on < obj.starts_on:
        raise ValidationError('Дата окончания не может быть раньше начала.')


def _participants(obj, user, ids, target_visibility):
    people = _people(ids,user)
    if target_visibility != 'shared' and any(person.pk != obj.owner_id for person in people):
        raise ValidationError('Для других участников нужна общая видимость.')
    obj.participants.set(people)
    return [str(person.pk) for person in people]


def update_event_participants(user, event, participants, target_visibility=None, shared_context=False):
    """Set participants inside update_event's transaction, before its visibility check."""
    if not services.can_view(event,user,shared_context) or not services.can_edit(event,user):
        raise PermissionDenied('Нет прав на изменение участников события.')
    return _participants(event,user,participants,target_visibility or event.visibility)


def _apply_owned(user,obj,values):
    before = {}
    people = values.pop('participants',None)
    for key,value in values.items():
        before[key] = _json(getattr(obj,key))
        setattr(obj,key,value)
    if people is not None:
        before['participants'] = list(map(str,obj.participants.values_list('pk',flat=True)))
        _participants(obj,user,people,obj.visibility)
    elif obj.visibility != 'shared' and obj.participants.exclude(pk=obj.owner_id).exists():
        raise ValidationError('Для других участников нужна общая видимость; измените участников вместе с доступом.')
    if obj.visibility != 'shared':
        obj.editors.clear()
    _span(obj)
    obj.version += 1
    obj.full_clean()
    obj.save()
    services._audit(user,obj,'update',before)
    return obj


@transaction.atomic
def update_project(user,project_id,changes,expected_version,shared_context=False):
    obj = _locked(user,'project',project_id,expected_version,shared_context)
    return _apply_owned(user,obj,_values(ProjectChanges,changes))


@transaction.atomic
def update_trip(user,trip_id,changes,expected_version,shared_context=False,shift_events=False):
    obj = _locked(user,'trip',trip_id,expected_version,shared_context)
    values = _values(TripChanges,changes)
    if values.get('project_id'):
        values['project_id'] = services.get_entity('project',values['project_id'],user,shared_context).pk
    moved=[]
    if shift_events:
        if not obj.starts_on or not values.get('starts_on'):
            raise ValidationError('Для переноса всей поездки нужны прежняя и новая даты начала.')
        delta = values['starts_on']-obj.starts_on
        if 'ends_on' not in values and obj.ends_on:
            values['ends_on'] = obj.ends_on + delta
        # Lock and check every event first: the whole move rolls back if any is inaccessible.
        events=list(Event.objects.select_for_update().filter(trip=obj).exclude(status='cancelled').order_by('pk'))
        for event in events:
            if not services.can_view(event,user,shared_context) or not services.can_edit(event,user):
                raise PermissionDenied('В поездке есть событие, которое вы не можете переносить.')
        if delta:
            for event in events:
                if event.all_day:
                    change={'start_date':event.start_date+delta,'end_date':event.end_date+delta}
                else:
                    change={'starts_at':event.starts_at.astimezone(ZoneInfo(event.timezone))+delta,
                            'ends_at':event.ends_at.astimezone(ZoneInfo(event.end_timezone))+delta}
                services.update_event(user,event.pk,change,event.version,shared_context)
                moved.append(str(event.pk))
    obj = _apply_owned(user,obj,values)
    obj._shifted_events=moved
    return obj


@transaction.atomic
def update_checklist(user,checklist_id,changes,expected_version,shared_context=False):
    obj=_locked(user,'checklist',checklist_id,expected_version,shared_context)
    values=_values(ChecklistChanges,changes)
    before={key:getattr(obj,key) for key in values}
    for key,value in values.items():setattr(obj,key,value)
    obj.version+=1;obj.full_clean();obj.save()
    services._audit(user,obj,'update',before)
    return obj


def _item(user,item_id,expected_version,shared_context):
    try:
        item=ChecklistItem.objects.get(pk=item_id)
    except (ChecklistItem.DoesNotExist,ValueError,ValidationError):
        raise PermissionDenied('Пункт не найден или недоступен.') from None
    # Consistent parent-before-item locking for all checklist edits.
    checklist=services.get_entity('checklist',item.checklist_id,user,shared_context)
    checklist=Checklist.objects.select_for_update().get(pk=checklist.pk)
    if not services.can_edit(checklist,user):raise PermissionDenied('Нет прав на изменение списка.')
    item=ChecklistItem.objects.select_for_update().get(pk=item.pk)
    if expected_version is None or item.version != expected_version:
        raise ValidationError('Пункт уже изменился, откройте список заново.')
    item.checklist=checklist
    return item


def _assignee(user,checklist,values):
    if values.get('assignee_id'):
        person=services._people([values['assignee_id']],user)[0]
        parent=checklist.parent
        if parent.visibility != 'shared' and person.pk != parent.owner_id:
            raise ValidationError('Для другого исполнителя список должен относиться к общему событию или поездке.')
        values['assignee_id']=person.pk


@transaction.atomic
def add_checklist_item(user,checklist_id,data,expected_version,shared_context=False):
    checklist=_locked(user,'checklist',checklist_id,expected_version,shared_context)
    values=_values(NewChecklistItem,data)
    _assignee(user,checklist,values)
    if 'position' not in values:
        last=checklist.items.aggregate(last=Max('position'))['last']
        values['position']=0 if last is None else last+1
    item=ChecklistItem(checklist=checklist,**values)
    item.full_clean();item.save()
    checklist.version+=1;checklist.save(update_fields=['version','updated_at'])
    services._audit(user,item,'create')
    return item


@transaction.atomic
def update_checklist_item(user,item_id,changes,expected_version,shared_context=False):
    item=_item(user,item_id,expected_version,shared_context)
    values=_values(ChecklistItemChanges,changes)
    _assignee(user,item.checklist,values)
    if 'checked' in values:
        checked=values.pop('checked')
        values['checked_at']=timezone.now() if checked and not item.checked_at else item.checked_at if checked else None
        values['checked_by_id']=user.pk if checked and not item.checked_at else item.checked_by_id if checked else None
    before={key:_json(getattr(item,key)) for key in values}
    for key,value in values.items():setattr(item,key,value)
    item.version+=1;item.full_clean();item.save()
    item.checklist.version+=1;item.checklist.save(update_fields=['version','updated_at'])
    services._audit(user,item,'update',before)
    return item


@transaction.atomic
def remove_checklist_item(user,item_id,expected_version,shared_context=False):
    item=_item(user,item_id,expected_version,shared_context)
    checklist=item.checklist
    services._audit(user,item,'delete',{'text':item.text,'checklist_id':str(checklist.pk)})
    item.delete()
    checklist.version+=1;checklist.save(update_fields=['version','updated_at'])
    return checklist


@transaction.atomic
def unlink_document(user,document_id,parent_type,parent_id,expected_version,shared_context=False):
    doc=_locked(user,'document',document_id,expected_version,shared_context)
    relation={'event':'events','trip':'trips','task':'tasks'}.get(parent_type)
    if not relation:raise ValidationError('Документ связан с событием, поездкой или задачей.')
    parent=services.get_entity(parent_type,parent_id,user,shared_context)
    if not services.can_edit(parent,user):raise PermissionDenied('Нет прав на изменение связи документа.')
    getattr(doc,relation).remove(parent)
    doc.version+=1;doc.save(update_fields=['version','updated_at'])
    services._audit(user,doc,'unlink',{'parent_type':parent_type,'parent_id':str(parent.pk)})
    return doc


@transaction.atomic
def update_trip_segment(user,segment_id,changes,expected_version,shared_context=False):
    try:segment=TripSegment.objects.get(pk=segment_id)
    except (TripSegment.DoesNotExist,ValueError,ValidationError):
        raise PermissionDenied('Этап поездки не найден или недоступен.') from None
    trip=services.get_entity('trip',segment.trip_id,user,shared_context)
    event=services.get_entity('event',segment.event_id,user,shared_context)
    if not services.can_edit(trip,user) or not services.can_edit(event,user):
        raise PermissionDenied('Нет прав на изменение этапа поездки.')
    segment=TripSegment.objects.select_for_update().get(pk=segment.pk)
    if expected_version is None or segment.version!=expected_version:
        raise ValidationError('Этап поездки изменился, откройте его заново.')
    values=_values(TripSegmentChanges,changes)
    visibility='shared' if 'shared' in (trip.visibility,event.visibility) else event.visibility
    # Free-text replacement detaches the saved place so the stale snapshot cannot win later.
    for side in ('origin','destination'):
        key=side+'_place_id'
        if key in values and values[key] is None:
            values[side+'_snapshot']={}
        elif side in values and key not in values:
            values[key]=None;values[side+'_snapshot']={}
    values.update(services.prepare_segment_places(user,values,visibility,shared_context))
    before={key:_json(getattr(segment,key)) for key in values}
    for key,value in values.items():setattr(segment,key,value)
    segment.version+=1;segment.full_clean();segment.save()
    services._audit(user,segment,'update',before)
    return segment
