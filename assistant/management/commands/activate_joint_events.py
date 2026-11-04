from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from assistant import services
from assistant.models import ActionRequest, Event, Job


class Command(BaseCommand):
    help = 'Activate unexpired proposals for future events attended by both family members. Dry run by default.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Activate matching events and their reminders.')

    def handle(self, *args, **options):
        apply = options['apply']
        eligible = 0
        applied = 0
        requests = ActionRequest.objects.filter(
            kind='confirm_event', status='proposed', target_type='event', changes={'status': 'confirmed'},
            initiator__telegram_id__in=settings.ALLOWED_TELEGRAM_IDS,
            approver__telegram_id__in=settings.ALLOWED_TELEGRAM_IDS, expires_at__gt=timezone.now(),
        )
        for request_id in requests.order_by('pk').values_list('pk', flat=True).iterator():
            with transaction.atomic():
                # Match approve_action's lock order so a concurrent button press wins or observes superseded.
                req = requests.select_for_update(of=('self',)).filter(pk=request_id).first()
                if req is None or req.initiator_id == req.approver_id:
                    continue
                event = Event.objects.select_for_update().filter(
                    pk=req.target_id, owner_id=req.initiator_id, status='proposed', visibility='shared',
                    version=req.target_version,
                ).first()
                if event is None or set(event.participants.values_list('pk', flat=True)) != {req.initiator_id, req.approver_id}:
                    continue
                now = timezone.now()
                if req.expires_at <= now or event.starts_at <= now:
                    continue
                if ActionRequest.objects.filter(target_type='event', target_id=event.pk, status='proposed',
                                                expires_at__gt=now).exclude(pk=req.pk).exists():
                    continue  # Leave open edits and cancellations actionable on their original version.
                prompt = Job.objects.select_for_update().filter(
                    kind='telegram_send', dedup_key=f'action:{req.pk}',
                ).first()
                if prompt is not None and prompt.status == 'running':
                    continue  # It can be inspected again after delivery finishes.
                eligible += 1
                if apply:
                    services._apply_event(req.initiator, event, req.changes)
                    req.status = 'superseded'
                    req.save(update_fields=['status', 'updated_at'])
                    if prompt is not None and prompt.status == 'pending':
                        prompt.status = 'cancelled'
                        prompt.save(update_fields=['status', 'updated_at'])
                    applied += 1
        mode = 'apply' if apply else 'dry-run'
        self.stdout.write(f'{mode} eligible={eligible} applied={applied}')
