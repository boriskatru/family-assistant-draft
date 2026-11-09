"""Lifecycle of flexible calendar blocks generated for tasks."""
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from .models import Event


@transaction.atomic
def cancel_task_reservations(task, actor, reason='task_changed'):
    """Free future planner blocks after completion or changed task constraints.

    Past/in-progress work and ordinary meetings linked to the task remain intact.
    Calendar synchronization, reminder cancellation and audit use the same event
    service as an explicit user cancellation. The caller's transaction can roll
    back the task update and all block cancellations together.
    """
    from . import services
    if not services.can_edit(task, actor):
        raise PermissionDenied('Нет прав на изменение дела.')
    blocks = Event.objects.select_for_update().filter(
        task=task, kind='task', flexible=True,
        status__in=['confirmed', 'proposed'], starts_at__gte=timezone.now(),
    ).order_by('pk')
    cancelled = []
    for event in blocks:
        if not services.can_edit(event, actor):
            raise PermissionDenied('Нет прав на освобождение блока задачи; сначала разрешите доступ к нему.')
        changed = services.update_event(actor, event.pk, {'status': 'cancelled'}, expected_version=event.version)
        services._audit(actor, changed, 'release_task_block', {'task_id': str(task.pk), 'reason': reason})
        cancelled.append(changed)
    return cancelled


def obsolete_task_reservations(task):
    """Read-only preview of future generated blocks inconsistent with a task."""
    attendees = set(task.participants.values_list('pk', flat=True)) | {task.assignee_id}
    blocks = Event.objects.filter(
        task=task, kind='task', flexible=True, status__in=['confirmed', 'proposed'],
        starts_at__gte=timezone.now()).order_by('pk')
    result = []
    for event in blocks:
        invalid = (task.status in {'done', 'cancelled'}
                   or bool(task.not_before and event.starts_at < task.not_before)
                   or bool(task.due_at and event.ends_at > task.due_at)
                   or set(event.participants.values_list('pk', flat=True)) != attendees
                   or event.domain != task.domain or event.visibility != task.visibility
                   or event.project_id != task.project_id or event.trip_id != task.trip_id)
        if invalid:
            result.append(event)
    return result


@transaction.atomic
def reconcile_task_reservations(task, actor):
    """Remove generated future reservations that no longer fit the task."""
    from . import services
    if not services.can_edit(task, actor):
        raise PermissionDenied('Нет прав на изменение дела.')
    # Lock before previewing so concurrent calendar edits are rechecked.
    list(Event.objects.select_for_update().filter(task=task).order_by('pk'))
    cancelled = []
    for event in obsolete_task_reservations(task):
        if not services.can_edit(event, actor):
            raise PermissionDenied('Нет прав на освобождение блока задачи; сначала разрешите доступ к нему.')
        cancelled.append(services.update_event(actor, event.pk, {'status': 'cancelled'}, expected_version=event.version))
    return cancelled
