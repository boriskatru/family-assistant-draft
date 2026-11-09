"""Repair local state left by older planner and shared-calendar behaviour."""
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from assistant.models import Event, Task
from assistant.planning_actions import obsolete_task_reservations, reconcile_task_reservations


class Command(BaseCommand):
    help = ('Preview obsolete future task blocks and missing shared-calendar participants. '
            'Pass --apply to repair local records; no external service calls are made.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Apply local repairs and queue normal calendar synchronization.')

    def handle(self, *args, **options):
        apply = options['apply']
        task_ids = Event.objects.filter(
            task__isnull=False, task__owner__telegram_id__in=settings.ALLOWED_TELEGRAM_IDS,
            kind='task', flexible=True, status__in=['confirmed', 'proposed'],
            starts_at__gte=timezone.now(),
        ).order_by('task_id').values_list('task_id', flat=True).distinct()
        eligible = repaired = blocked = 0
        for task_id in list(task_ids):
            try:
                with transaction.atomic():
                    task = Task.objects.select_for_update().select_related('owner').get(pk=task_id)
                    candidates = obsolete_task_reservations(task)
                    eligible += len(candidates)
                    if apply and candidates:
                        repaired += len(reconcile_task_reservations(task, task.owner))
            except (PermissionDenied, ValidationError):
                # Never weaken ACLs to clean up an unrelated private calendar entry.
                blocked += 1
        mode = 'apply' if apply else 'dry-run'
        self.stdout.write(f'{mode} task_blocks_eligible={eligible} task_blocks_repaired={repaired} blocked_tasks={blocked}')
        from assistant.integrations.google import repair_shared_calendar_participants
        result = repair_shared_calendar_participants(apply=apply)
        self.stdout.write(f'{mode} shared_calendar={result}')
