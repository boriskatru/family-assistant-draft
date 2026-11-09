from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from pydantic import ValidationError as SchemaError

from assistant import services
from assistant.models import AuditLog, Event, Person, Reminder, Task
from assistant.task_actions import convert_task_to_event, update_task
from assistant.task_schemas import TaskChanges


pytestmark = pytest.mark.django_db


def task(owner, people=None, **values):
    defaults = dict(title='День рождения Коли', description='Празднуем 24 декабря',
        due_at=timezone.now() + timedelta(days=30), estimated_minutes=5)
    if people:
        defaults.update(visibility='shared', participants=[str(p.pk) for p in people])
    defaults.update(values)
    return services.create_task(owner, **defaults)


@pytest.mark.parametrize('actor_index', [0, 1])
@pytest.mark.parametrize('shared', [False, True])
def test_either_spouse_updates_deadline_and_reminders_without_changing_owner(people, actor_index, shared):
    original = task(people[0], people)
    old_due, old_version = original.due_at, original.version
    moved = update_task(people[actor_index], original.pk,
        {'due_at': '2030-12-24T12:00:00+03:00'}, old_version, shared)
    assert moved.owner == people[0] and moved.assignee == people[0]
    assert moved.due_at.isoformat() == '2030-12-24T12:00:00+03:00'
    assert moved.version == old_version + 1
    assert moved.reminders.get(status='pending').due_at == moved.due_at - timedelta(hours=1)
    assert moved.reminders.get(status='pending').object_version == moved.version
    assert moved.reminders.get(status='cancelled').due_at == old_due - timedelta(hours=1)
    assert AuditLog.objects.get(target_id=moved.pk, action='update').actor == people[actor_index]
    with pytest.raises(ValidationError, match='изменилась'):
        update_task(people[1-actor_index], moved.pk, {'title': 'Устаревшая правка'}, old_version)


def test_private_and_removed_people_cannot_edit_tasks(people, settings):
    owner, spouse = people
    private = task(owner)
    with pytest.raises(PermissionDenied):
        update_task(spouse, private.pk, {'status': 'cancelled'}, private.version)
    shared = task(owner, people)
    outsider = Person.objects.create(telegram_id=9999, name='Посторонний')
    shared.editors.add(outsider)
    with pytest.raises(PermissionDenied):
        update_task(outsider, shared.pk, {'title': 'Чужая правка'}, shared.version)
    settings.ALLOWED_TELEGRAM_IDS = (owner.telegram_id,)
    with pytest.raises(PermissionDenied):
        update_task(spouse, shared.pk, {'title': 'Отозванный доступ'}, shared.version)


def test_description_edit_preserves_sent_reminder_and_clear_deadline_stops_pending(people):
    owner, _ = people
    original = task(owner)
    reminder = original.reminders.get()
    reminder.status = 'sent'
    reminder.save()
    original = update_task(owner, original.pk, {'description': 'Подробности'}, original.version)
    assert original.reminders.count() == 1 and original.reminders.get().status == 'sent'
    original = update_task(owner, original.pk, {'due_at': original.due_at + timedelta(days=1)}, original.version)
    assert original.reminders.filter(status='sent').count() == 1
    assert original.reminders.filter(status='pending').count() == 1
    original = update_task(owner, original.pk, {'clear_due_at': True}, original.version)
    assert original.due_at is None
    assert not original.reminders.filter(status__in=['pending', 'queued']).exists()
    assert original.reminders.filter(status='sent').count() == 1


def test_first_deadline_creates_reminder_and_invalid_changes_rollback(people):
    owner, _ = people
    original = task(owner, due_at=None)
    assert not original.reminders.exists()
    original = update_task(owner, original.pk, {'due_at': '2030-12-24T12:00:00+03:00'}, original.version)
    assert original.reminders.get().object_version == original.version
    previous = original.version
    with pytest.raises(ValidationError):
        update_task(owner, original.pk, {'estimated_minutes': 1, 'remaining_minutes': 10}, original.version)
    original.refresh_from_db()
    assert original.version == previous and original.estimated_minutes == 5
    with pytest.raises(ValidationError):
        update_task(owner, original.pk, {'owner_id': str(owner.pk)}, original.version)


def test_cancel_obsolete_task_cancels_only_generated_future_blocks(people):
    owner, spouse = people
    original = task(owner, people)
    start = timezone.now() + timedelta(days=10)
    args = dict(title=original.title, task_id=original.pk, starts_at=start,
        ends_at=start + timedelta(minutes=5), visibility='shared', participants=[p.pk for p in people])
    block = services.create_event(owner, kind='task', flexible=True, **args)
    separate = services.create_event(owner, kind='meeting', **args)
    cancelled = update_task(spouse, original.pk, {'status': 'cancelled'}, original.version)
    block.refresh_from_db()
    separate.refresh_from_db()
    assert cancelled.status == 'cancelled' and cancelled.completed_at is None
    assert cancelled.remaining_minutes == 5
    assert block.status == 'cancelled' and separate.status == 'confirmed'
    assert not cancelled.reminders.filter(status='pending').exists()
    assert not block.reminders.filter(status='pending').exists()


@pytest.mark.parametrize('actor_index', [0, 1])
def test_convert_task_atomically_keeps_description_owner_and_file_access(people, settings, tmp_path, actor_index):
    owner, spouse = people
    actor = people[actor_index]
    settings.MEDIA_ROOT = tmp_path
    original = task(owner, people)
    public = services.save_document(owner, 'birthday.txt', b'party', 'text/plain')
    services.share_entity(owner, 'document', public.pk, 'shared')
    services.link_document(owner, public.pk, 'task', original.pk)
    private = services.save_document(owner, 'secret.txt', b'private', 'text/plain')
    services.link_document(owner, private.pk, 'task', original.pk)
    old_version = original.version
    created = convert_task_to_event(actor, original.pk, old_version,
        {'starts_at': '2030-12-24T12:00:00+03:00', 'ends_at': '2030-12-24T13:00:00+03:00'})
    original.refresh_from_db()
    assert original.status == 'cancelled' and original.completed_at is None
    assert created.task_id == original.pk and created.owner == owner
    assert created.description == original.description and created.title == original.title
    assert created.visibility == 'shared' and set(created.participants.all()) == set(people)
    assert created.documents.filter(pk=public.pk).exists()
    assert created.documents.filter(pk=private.pk).exists() is (actor == owner)
    assert not services.can_view(private, spouse)
    assert not original.reminders.filter(status='pending').exists()
    with pytest.raises(ValidationError):
        convert_task_to_event(actor, original.pk, old_version,
            {'starts_at': '2030-12-24T12:00:00+03:00', 'ends_at': '2030-12-24T13:00:00+03:00'})
    assert Event.objects.filter(task=original).count() == 1


def test_conversion_failure_leaves_original_task_and_reminders(people):
    owner, _ = people
    original = task(owner)
    with pytest.raises(ValidationError):
        convert_task_to_event(owner, original.pk, original.version,
            {'starts_at': '2030-12-24T13:00:00+03:00', 'ends_at': '2030-12-24T12:00:00+03:00'})
    original.refresh_from_db()
    assert original.status == 'todo' and original.version == 1
    assert original.reminders.get().status == 'pending'
    assert not Event.objects.exists()


def test_birthday_conversion_accepts_only_a_date_and_does_not_block_day(people):
    owner, spouse = people
    original = task(owner, people)
    gifts = services.create_task(owner, title='Подготовить подарки ко дням рождения',
        visibility='shared', estimated_minutes=120)
    created = convert_task_to_event(spouse, original.pk, original.version,
        {'all_day': True, 'start_date': '2030-12-24', 'participants': [str(p.pk) for p in people]})
    original.refresh_from_db()
    assert created.all_day and not created.blocks_time
    assert created.start_date.isoformat() == '2030-12-24'
    assert created.end_date.isoformat() == '2030-12-25'
    assert original.status == 'cancelled' and created.status == 'confirmed'
    gifts.refresh_from_db()
    assert gifts.status == 'todo' and gifts.version == 1 and gifts.remaining_minutes == 120
    assert Task.objects.filter(title=gifts.title).count() == 1


def test_recurring_task_cancellation_keeps_occurrence_closed_and_conversion_requires_stopped_series(people):
    from assistant.recurrence import create_rule_from_task, materialize_recurrences
    owner, _ = people
    original = task(owner)
    rule = create_rule_from_task(owner, original, mode='calendar', rrule='FREQ=DAILY;COUNT=2')
    original.refresh_from_db()
    with pytest.raises(ValidationError, match='действующую серию'):
        convert_task_to_event(owner, original.pk, original.version,
            {'all_day': True, 'start_date': '2030-12-24'})
    assert not Event.objects.exists()
    update_task(owner, original.pk, {'status': 'cancelled'}, original.version)
    materialize_recurrences(original.due_at)
    materialize_recurrences(original.due_at)
    original.refresh_from_db()
    assert original.status == 'cancelled'
    assert Task.objects.filter(recurrence=rule, occurrence_key=original.occurrence_key).count() == 1


def test_failure_after_event_creation_rolls_back_event_documents_and_task(people, settings, tmp_path):
    owner, spouse = people
    settings.MEDIA_ROOT = tmp_path
    original = task(owner, people)
    doc = services.save_document(owner, 'birthday.txt', b'party', 'text/plain')
    services.link_document(owner, doc.pk, 'task', original.pk)
    start = timezone.now() + timedelta(days=10)
    protected = services.create_event(spouse, title='Личный блок', task_id=original.pk,
        starts_at=start, ends_at=start + timedelta(minutes=5), kind='task', flexible=True)
    with pytest.raises(PermissionDenied, match='блока'):
        convert_task_to_event(owner, original.pk, original.version,
            {'starts_at': '2030-12-24T12:00:00+03:00', 'ends_at': '2030-12-24T13:00:00+03:00'})
    original.refresh_from_db()
    assert original.status == 'todo' and original.version == 1
    assert original.reminders.get().status == 'pending'
    assert list(Event.objects.values_list('pk', flat=True)) == [protected.pk]
    assert not doc.events.exists()
    assert not AuditLog.objects.filter(action='convert_task').exists()


def test_reopening_cancelled_task_rearms_deadline_but_not_work_blocks(people):
    owner, _ = people
    original = task(owner)
    original = update_task(owner, original.pk, {'status': 'cancelled'}, original.version)
    original = update_task(owner, original.pk, {'status': 'todo'}, original.version)
    assert original.status == 'todo' and original.reminders.filter(status='pending').count() == 1
    assert original.reminders.get(status='pending').object_version == original.version


def test_task_schema_rejects_empty_ambiguous_or_permission_changes():
    for invalid in ({}, {'clear_due_at': False}, {'due_at': '2030-12-24T12:00:00'},
                    {'due_at': '2030-12-24T12:00:00+03:00', 'clear_due_at': True},
                    {'status': 'done'}, {'owner_id': 'somebody'}):
        with pytest.raises(SchemaError):
            TaskChanges.model_validate(invalid)
    assert TaskChanges.model_validate({'allow_split': False}).allow_split is False
