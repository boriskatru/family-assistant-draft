"""Both family members can edit shared events/trips, including existing rows."""
import asyncio
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from assistant import services
from assistant.agent.context import create_context
from assistant.agent.operations import execute_operation
from assistant.agent.retrieval import read_record
from assistant.models import ActionRequest, AuditLog, Event, Job, Person, Trip

pytestmark = pytest.mark.django_db


def parent(owner, kind, visibility='shared', participants=None):
    data = {'title': 'Совместная поездка', 'visibility': visibility}
    if participants is not None:
        data['participants'] = [str(p.pk) for p in participants]
    if kind == 'trip':
        return services.create_trip(owner, **data)
    start = timezone.now() + timedelta(days=10)
    return services.create_event(owner, starts_at=start, ends_at=start+timedelta(hours=1), **data)


@pytest.mark.parametrize('owner_index', [0, 1])
@pytest.mark.parametrize('kind', ['event', 'trip'])
def test_spouse_adds_and_checks_packing_list_on_existing_shared_record(people, owner_index, kind):
    owner, spouse = people[owner_index], people[1-owner_index]
    original = parent(owner, kind, participants=people)
    # Re-load an existing row: no editor assignment or data backfill is needed.
    obj = type(original).objects.get(pk=original.pk)
    assert not obj.editors.exists()
    checklist = services.make_checklist(spouse, kind, obj.pk, 'еда; вино; тёплая одежда; плавки')
    assert checklist.items.count() == 4
    item = checklist.items.get(text='тёплая одежда')
    assert services.can_edit(checklist, spouse) and services.can_edit(item, spouse)
    services.set_checklist_item(spouse, item.pk, True)
    item.refresh_from_db()
    assert item.checked_by == spouse
    services.set_checklist_item(owner, item.pk, False)
    item.refresh_from_db()
    assert item.checked_at is None
    assert read_record(spouse, kind, obj.pk)['record']['can_edit'] is True
    assert read_record(spouse, 'checklist', checklist.pk)['record']['can_edit'] is True
    assert obj.owner_id == owner.pk and not obj.editors.exists()
    assert not ActionRequest.objects.exists()


@pytest.mark.parametrize('owner_index', [0, 1])
@pytest.mark.parametrize('edit_as_owner', [False, True])
def test_shared_event_edit_is_immediate_and_preserves_ownership(people, owner_index, edit_as_owner):
    owner, spouse = people[owner_index], people[1-owner_index]
    actor = owner if edit_as_owner else spouse
    event = parent(owner, 'event', participants=people)
    start = event.starts_at
    version = event.version
    result = services.update_event(actor, event.pk, {
        'starts_at': start + timedelta(hours=2),
        'ends_at': event.ends_at + timedelta(hours=2),
        'description': 'Адрес, схема проезда и данные для заселения',
        'location': 'Домик «Клён»',
    }, expected_version=version)
    assert isinstance(result, Event) and result.pk == event.pk
    event.refresh_from_db()
    assert event.starts_at == start + timedelta(hours=2)
    assert event.description == 'Адрес, схема проезда и данные для заселения'
    assert event.location == 'Домик «Клён»'
    assert event.version == version + 1
    assert event.owner == owner
    assert AuditLog.objects.get(target_id=event.pk, action='update').actor == actor
    assert all(r.object_version == event.version and r.status == 'pending'
               and r.due_at == event.starts_at - timedelta(minutes=r.offset_minutes)
               for r in event.reminders.all())
    assert Job.objects.filter(kind='google_event', dedup_key=f'google:{event.pk}:v{event.version}').exists()
    assert not ActionRequest.objects.exists()
    assert not Job.objects.filter(dedup_key__startswith='action:').exists()


@pytest.mark.parametrize('actor_index', [0, 1])
def test_either_spouse_can_cancel_shared_event_and_cancel_reminders(people, actor_index):
    event = parent(people[0], 'event', participants=people)
    result = services.update_event(people[actor_index], event.pk, {'status': 'cancelled'},
                                   expected_version=event.version)
    assert result.status == 'cancelled'
    assert not result.reminders.exclude(status='cancelled').exists()
    assert Job.objects.filter(kind='google_event', dedup_key=f'google:{event.pk}:v{result.version}').exists()
    assert not ActionRequest.objects.exists()


def test_second_shared_editor_must_reread_a_changed_version(people):
    owner, spouse = people
    event = parent(owner, 'event', participants=people)
    services.update_event(owner, event.pk, {'title': 'Правка Бориса'}, expected_version=event.version)
    with pytest.raises(ValidationError, match='Запись уже изменилась'):
        services.update_event(spouse, event.pk, {'title': 'Правка Ани'}, expected_version=event.version)
    event.refresh_from_db()
    assert event.title == 'Правка Бориса'
    assert AuditLog.objects.filter(target_id=event.pk, action='update').count() == 1


@pytest.mark.parametrize('kind', ['event', 'trip'])
@pytest.mark.parametrize('visibility', ['private', 'busy_only'])
def test_private_or_busy_parent_still_denies_spouse_mutation(people, kind, visibility):
    owner, spouse = people
    obj = parent(owner, kind, visibility)
    checklist = services.make_checklist(owner, kind, obj.pk, 'Паспорт')
    assert not services.can_edit(obj, spouse)
    with pytest.raises(PermissionDenied):
        services.make_checklist(spouse, kind, obj.pk, 'Камера')
    with pytest.raises(PermissionDenied):
        services.set_checklist_item(spouse, checklist.items.get().pk, True)
    if kind == 'event':
        with pytest.raises(PermissionDenied):
            services.update_event(spouse, obj.pk, {'title': 'Чужая личная встреча'})


@pytest.mark.parametrize('kind', ['event', 'trip'])
def test_shared_access_requires_current_allowlist_and_rechecks_visibility(people, settings, kind):
    owner, spouse = people
    obj = parent(owner, kind)
    outsider = Person.objects.create(telegram_id=999, name='Посторонний')
    obj.editors.add(outsider)
    assert not services.can_edit(obj, outsider)
    with pytest.raises(PermissionDenied):
        services.make_checklist(outsider, kind, obj.pk, 'Нельзя')
    if kind == 'event':
        with pytest.raises(PermissionDenied):
            services.update_event(outsider, obj.pk, {'title': 'Нельзя'})
    settings.ALLOWED_TELEGRAM_IDS = (owner.telegram_id,)
    assert not services.can_edit(obj, spouse)
    with pytest.raises(PermissionDenied):
        services.make_checklist(spouse, kind, obj.pk, 'Нельзя')
    if kind == 'event':
        with pytest.raises(PermissionDenied):
            services.update_event(spouse, obj.pk, {'title': 'Нельзя'})
    settings.ALLOWED_TELEGRAM_IDS = tuple(p.telegram_id for p in people)
    services.share_entity(owner, kind, obj.pk, 'private')
    with pytest.raises(PermissionDenied):
        services.make_checklist(spouse, kind, obj.pk, 'Теперь личное')


def test_pending_shared_edit_cannot_apply_after_initiator_is_removed(people, settings):
    owner, spouse = people
    event = parent(owner, 'event', participants=people)
    # A proposal persisted before immediate editing was introduced.
    request = ActionRequest.objects.create(initiator=spouse, approver=owner, kind='update_event',
        target_type='event', target_id=event.pk, target_version=event.version,
        changes={'title': 'Новая версия'}, expires_at=timezone.now() + timedelta(hours=24))
    settings.ALLOWED_TELEGRAM_IDS = (owner.telegram_id,)
    with pytest.raises(PermissionDenied):
        services.approve_action(owner, request.pk)
    event.refresh_from_db()
    request.refresh_from_db()
    assert event.title == 'Совместная поездка' and request.status == 'proposed'


def test_legacy_approval_cannot_overwrite_a_later_direct_edit(people):
    owner, spouse = people
    event = parent(owner, 'event', participants=people)
    request = ActionRequest.objects.create(initiator=spouse, approver=owner, kind='update_event',
        target_type='event', target_id=event.pk, target_version=event.version,
        changes={'title': 'Устаревшее предложение'}, expires_at=timezone.now() + timedelta(hours=24))
    services.update_event(spouse, event.pk, {'title': 'Актуальная правка'}, expected_version=event.version)
    with pytest.raises(ValidationError, match='Событие изменилось'):
        services.approve_action(owner, request.pk)
    event.refresh_from_db()
    assert event.title == 'Актуальная правка'
    assert AuditLog.objects.filter(target_id=event.pk, action='update').count() == 1


def test_invalid_direct_edit_leaves_event_reminders_and_legacy_request_unchanged(people):
    owner, spouse = people
    event = parent(owner, 'event', participants=people)
    request = ActionRequest.objects.create(initiator=spouse, approver=owner, kind='update_event',
        target_type='event', target_id=event.pk, target_version=event.version,
        changes={'title': 'Предложение'}, expires_at=timezone.now() + timedelta(hours=24))
    initial_version = event.version
    initial_reminders = list(event.reminders.values('pk', 'due_at', 'status', 'object_version'))
    initial_jobs = Job.objects.count()
    with pytest.raises(ValidationError, match='Конец должен быть позже начала'):
        services.update_event(spouse, event.pk, {'ends_at': event.starts_at - timedelta(minutes=1)},
                               expected_version=initial_version)
    event.refresh_from_db()
    request.refresh_from_db()
    assert event.version == initial_version and event.title == 'Совместная поездка'
    assert list(event.reminders.values('pk', 'due_at', 'status', 'object_version')) == initial_reminders
    assert request.status == 'proposed' and request.target_version == initial_version
    assert Job.objects.count() == initial_jobs
    assert not AuditLog.objects.filter(target_id=event.pk, action='update').exists()


@pytest.mark.parametrize('kind', ['event', 'trip'])
def test_spouse_links_own_document_without_expanding_document_access(people, tmp_path, settings, kind):
    owner, spouse = people
    settings.MEDIA_ROOT = tmp_path
    obj = parent(owner, kind)
    doc = services.save_document(spouse, 'booking.txt', b'booking', 'text/plain')
    services.link_document(spouse, doc.pk, kind, obj.pk)
    assert getattr(doc, 'events' if kind == 'event' else 'trips').filter(pk=obj.pk).exists()
    assert not services.can_view(doc, owner)
    services.share_entity(spouse, 'document', doc.pk, 'shared')
    doc.refresh_from_db()
    assert services.can_view(doc, owner) and not services.can_edit(doc, owner)
    with pytest.raises(PermissionDenied):
        services.link_document(owner, doc.pk, kind, obj.pk)


def test_shared_tasks_and_projects_keep_explicit_editor_rules(people):
    owner, spouse = people
    for obj in (services.create_project(owner, title='Проект', visibility='shared'),
                services.create_task(owner, title='Дело', visibility='shared')):
        assert services.can_view(obj, spouse) and not services.can_edit(obj, spouse)
        obj.editors.add(spouse)
        assert services.can_edit(obj, spouse)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('actor_index', [0, 1])
@pytest.mark.parametrize('shared', [False, True])
def test_agent_can_add_packing_list_and_segment_to_spouses_trip(people, settings, actor_index, shared):
    settings.SHARED_CHAT_ID = -500
    actor, owner = people[actor_index], people[1-actor_index]
    trip = parent(owner, 'trip', participants=people)
    event = parent(owner, 'event', participants=people)
    ctx = asyncio.run(create_context(actor.pk, -500 if shared else actor.chat_id, 700,
                                    'Добавь вещи и этап в нашу поездку', shared_context=shared))

    def run(name, args):
        return asyncio.run(ctx.dispatch(name, args, lambda: execute_operation(ctx, name, args), mutation=True))

    packing = run('make_checklist', {'parent_type': 'trip', 'parent_id': str(trip.pk), 'text': 'еда; плавки'})
    assert packing['ok'], packing
    segment = run('add_trip_segment', {'trip_id': str(trip.pk), 'event_id': str(event.pk)})
    assert segment['ok'], segment
    event.refresh_from_db()
    assert event.trip == trip and event.owner == owner
    assert Trip.objects.count() == 1 and Event.objects.count() == 1
    assert set(trip.checklists.get().items.values_list('text', flat=True)) == {'еда', 'плавки'}
