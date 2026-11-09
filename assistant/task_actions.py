"""Task edits and atomic correction of a task mistakenly entered as an event."""
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from pydantic import ValidationError as SchemaError

from .models import AuditLog, Event, Person, Reminder, Task
from .task_schemas import TaskChanges


def _locked_task(user, task_id, expected_version, shared_context=False):
    from . import services
    # Use the same unavailable-record response as the other application services.
    services.get_entity('task', task_id, user, shared_context)
    task = Task.objects.select_for_update().get(pk=task_id)
    if not services.can_edit(task, user):
        raise PermissionDenied('Нет прав на изменение дела.')
    if expected_version is None or task.version != expected_version:
        raise ValidationError('Задача уже изменилась, откройте её заново.')
    return task


def _reschedule_task(task, deadline_changed=False, reopened=False, old_assignee_id=None):
    """Keep delivery history and explicit disabling, fence queued deliveries by version."""
    from .reminder_defaults import task_reminder_due
    active = task.status in ('todo', 'in_progress') and task.due_at is not None
    pending = task.reminders.filter(status__in=['pending', 'queued', 'paused'])
    if not active:
        pending.update(status='cancelled')
        return
    assigned_changed = old_assignee_id is not None and old_assignee_id != task.assignee_id
    if not (deadline_changed or reopened or assigned_changed):
        pending.update(object_version=task.version, status='pending')
        return
    configs = {}
    for reminder in task.reminders.order_by('-object_version', '-created_at'):
        recipient_id = task.assignee_id if assigned_changed and reminder.recipient_id == old_assignee_id else reminder.recipient_id
        configs.setdefault((recipient_id, reminder.offset_minutes, reminder.kind), reminder)
    pending.update(status='cancelled')
    if not configs:
        configs[(task.assignee_id, 60, 'reminder')] = None
    for (recipient_id, offset, kind), latest in configs.items():
        if latest is not None and latest.status == 'disabled':
            continue
        # A private task cannot keep sending details to a removed co-assignee.
        if task.visibility != 'shared' and recipient_id != task.owner_id:
            continue
        Reminder.objects.create(task=task, recipient_id=recipient_id,
            due_at=task_reminder_due(task, offset, kind), offset_minutes=offset,
            kind=kind, object_version=task.version, status='pending')


def _cancel_generated_blocks(user, task):
    from . import services
    # Only planner-generated task blocks are cancelled. A meeting linked to a
    # task has its own meaning and must not silently disappear with that task.
    blocks = Event.objects.select_for_update().filter(task=task, kind='task', flexible=True,
        status__in=['confirmed', 'proposed'], starts_at__gte=timezone.now()).order_by('pk')
    for event in blocks:
        if not services.can_edit(event, user):
            raise PermissionDenied('Нет прав на отмену блока этой задачи; сначала разрешите доступ к нему.')
        services.update_event(user, event.pk, {'status': 'cancelled'}, expected_version=event.version)


def _cyclic(task, targets, relation):
    """Follow parent/dependency edges without exposing foreign record contents."""
    frontier = {obj.pk for obj in targets}
    seen = set()
    while frontier:
        if task.pk in frontier:
            return True
        current = frontier - seen
        if not current:
            return False
        seen |= current
        field = 'parent_id' if relation == 'parent' else 'dependencies__pk'
        frontier = set(Task.objects.filter(pk__in=current).values_list(field,flat=True)) - {None}
    return False


def _successor_has_only_system_changes(successor):
    """A version bump from undo/redo is not a manual edit of the next occurrence.

    Require a complete audit chain, so historical/unknown edits remain protected.
    Any real edit breaks the chain, even if the user subsequently restores a value.
    """
    if successor.version == 1:
        return True
    transitions=set()
    rows=AuditLog.objects.filter(target_type='task',target_id=successor.pk,
        action__in=['undo_successor','restore_successor']).values_list('before','after')
    for before,after in rows:
        previous,current=before.get('version'),after.get('version')
        if isinstance(previous,int) and isinstance(current,int) and current==previous+1:
            transitions.add(current)
    return all(version in transitions for version in range(2,successor.version+1))


@transaction.atomic
def update_task(user, task_id, changes, expected_version, shared_context=False):
    from . import services
    from .planning_actions import cancel_task_reservations
    # Serialize family graph changes before locking tasks, avoiding concurrent cycles.
    list(Person.objects.select_for_update().filter(
        telegram_id__in=settings.ALLOWED_TELEGRAM_IDS).order_by('pk'))
    task = _locked_task(user, task_id, expected_version, shared_context)
    try:
        validated = TaskChanges.model_validate(changes)
    except SchemaError:
        raise ValidationError('Некорректные поля изменения задачи; проверьте срок, статус и длительность.') from None
    values = validated.model_dump(exclude_none=True)
    for flag,field in [('clear_due_at','due_at'),('clear_not_before','not_before'),
                       ('clear_project','project_id'),('clear_trip','trip_id'),('clear_parent','parent_id')]:
        if values.pop(flag,False):values[field]=None
    participants = values.pop('participants',None)
    dependencies = values.pop('dependencies',None)
    for field,kind in [('project_id','project'),('trip_id','trip'),('parent_id','task')]:
        if values.get(field):
            linked = services.get_entity(kind,values[field],user,shared_context)
            if field == 'parent_id' and _cyclic(task,[linked],'parent'):
                raise ValidationError('Задача не может зависеть от себя по цепочке вложенности.')
            values[field] = linked.pk
    if values.get('assignee_id'):
        values['assignee_id'] = services._people([values['assignee_id']],user)[0].pk
    people = (services._people(participants,user) if participants else []) if participants is not None else list(task.participants.all())
    assignee_id = values.get('assignee_id',task.assignee_id)
    # Reassignment replaces the old assignee's participation unless the caller
    # explicitly provided a participant set retaining that person.
    if 'assignee_id' in values and participants is None:
        people = [person for person in people if person.pk != task.assignee_id]
    assigned = Person.objects.get(pk=assignee_id)
    people = list({person.pk:person for person in [*people,assigned]}.values())
    visibility = values.get('visibility',task.visibility)
    if visibility != 'shared' and any(person.pk != task.owner_id for person in people):
        raise ValidationError('Для другого исполнителя или участников нужна общая видимость.')
    deps = None
    if dependencies is not None:
        deps = [services.get_entity('task',identifier,user,shared_context) for identifier in dependencies]
        if _cyclic(task,deps,'dependencies'):
            raise ValidationError('Зависимости задач не должны образовывать цикл.')
    before = {key:getattr(task,key) for key in values}
    before['participants'] = list(map(str,task.participants.values_list('pk',flat=True)))
    if deps is not None:
        before['dependencies'] = list(map(str,task.dependencies.values_list('pk',flat=True)))
    old_status, old_due, old_assignee = task.status, task.due_at, task.assignee_id
    reopened = old_status in ('done','cancelled') and values.get('status',old_status) in ('todo','in_progress')
    if old_status == 'done' and reopened:
        successor=Task.objects.select_for_update().filter(previous_task=task).first()
        if successor and (successor.status not in ('todo','cancelled') or not _successor_has_only_system_changes(successor)):
            raise ValidationError('Следующая задача уже изменена. Сначала разрешите конфликт следующего повторения.')
        if successor:
            before_successor_version=successor.version
            successor.status='cancelled';successor.version+=1;successor.save()
            services._audit(user,successor,'undo_successor',{'version':before_successor_version})
            successor.reminders.filter(status__in=['pending','queued','paused']).update(status='cancelled')
            cancel_task_reservations(successor,user,'reopened_predecessor')
        values['completed_at']=None
        values.setdefault('remaining_minutes',values.get('estimated_minutes',task.estimated_minutes))
    if 'estimated_minutes' in values and 'remaining_minutes' not in values and task.remaining_minutes == task.estimated_minutes:
        before['remaining_minutes'] = task.remaining_minutes
        values['remaining_minutes'] = values['estimated_minutes']
    schedule_fields={'due_at','not_before','assignee_id','estimated_minutes','remaining_minutes','allow_split',
                     'domain','project_id','trip_id','parent_id','visibility'}
    schedule_changed = any(key in schedule_fields and getattr(task,key) != value for key,value in values.items())
    schedule_changed |= set(task.participants.values_list('pk',flat=True)) != {person.pk for person in people}
    if deps is not None:
        schedule_changed |= set(task.dependencies.values_list('pk',flat=True)) != {dep.pk for dep in deps}
    for key, value in values.items():setattr(task,key,value)
    if task.remaining_minutes > task.estimated_minutes:
        raise ValidationError('Остаток времени не может быть больше оценки; уточните оба значения.')
    if task.not_before and task.due_at and task.not_before > task.due_at:
        raise ValidationError('Дата начала работы не может быть позже срока задачи.')
    task.version += 1
    task.full_clean();task.save()
    task.participants.set(people)
    if deps is not None:task.dependencies.set(deps)
    if task.visibility != 'shared':task.editors.clear()
    _reschedule_task(task,deadline_changed=old_due!=task.due_at,reopened=reopened,old_assignee_id=old_assignee)
    # Removed participants must not continue receiving private reminders.
    if task.visibility != 'shared':
        task.reminders.filter(status__in=['pending','queued','paused']).exclude(recipient_id=task.owner_id).update(status='cancelled')
    if (task.status == 'cancelled' and old_status != 'cancelled') or schedule_changed:
        task._cancelled_reservations=len(cancel_task_reservations(task,user,'task_changed'))
    else:task._cancelled_reservations=0
    def serial(value):
        if hasattr(value,'isoformat'):return value.isoformat()
        if isinstance(value,UUID):return str(value)
        return value
    services._audit(user, task, 'update', {key:serial(value) for key,value in before.items()})
    return task


@transaction.atomic
def convert_task_to_event(user, task_id, expected_version, event_data, shared_context=False):
    """Create an event and cancel its mistaken source task in one transaction.

    The task relation retains provenance. A stale retry cannot create another
    event; the caller can read the linked event before deciding what to do next.
    """
    from . import services
    # Event creation and plan acceptance lock people before task rows. Keep
    # that order here too, so concurrent acceptance cannot deadlock conversion.
    services.get_entity('task', task_id, user, shared_context)
    list(Person.objects.select_for_update().filter(
        telegram_id__in=settings.ALLOWED_TELEGRAM_IDS).order_by('pk'))
    task = _locked_task(user, task_id, expected_version, shared_context)
    if task.status not in ('todo', 'in_progress'):
        raise ValidationError('Задача уже закрыта; проверьте связанные события перед повторным созданием.')
    if task.recurrence_id and task.recurrence.active:
        raise ValidationError('Задача входит в действующую серию. Сначала остановите серию задач; преобразование всей серии в события пока не поддерживается.')
    if task.dependencies.exclude(status='done').exists() or task.dependents.exclude(status__in=['done', 'cancelled']).exists():
        raise ValidationError('У задачи есть незавершённые зависимости. Сначала уточните связи, затем преобразуйте её.')
    data = dict(event_data)
    if data.get('task_id') and str(data['task_id']) != str(task.pk):
        raise ValidationError('Событие должно быть связано с преобразуемой задачей.')
    if data.get('visibility', task.visibility) != task.visibility:
        raise ValidationError('При преобразовании сохраняется видимость задачи. Измените доступ отдельно.')
    data.setdefault('title', task.title)
    data.setdefault('description', task.description)
    data.setdefault('domain', task.domain)
    data['visibility'] = task.visibility
    data['task_id'] = str(task.pk)
    data.setdefault('participants', [str(p.pk) for p in task.participants.all()])
    if task.project_id:
        data.setdefault('project_id', str(task.project_id))
    if task.trip_id:
        data.setdefault('trip_id', str(task.trip_id))
    event = services.create_event(user, shared_context=shared_context, **data)
    event.owner = task.owner
    event.save(update_fields=['owner'])
    # Existing links do not grant the actor access to somebody else's personal
    # document; attach only files currently visible to the actor, keeping their ACL.
    for document in task.documents.all():
        if services.can_view(document, user, shared_context):
            document.events.add(event)
    update_task(user, task.pk, {'status': 'cancelled'}, task.version, shared_context)
    services._audit(user, event, 'convert_task', {'task_id': str(task.pk)})
    return event
