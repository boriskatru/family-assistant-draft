"""Inspect job failures without exposing messages, tokens, or provider bodies."""
import json
import uuid

from django.core.management.base import BaseCommand, CommandError

from assistant.error_reporting import stored_google_error_details
from assistant.models import Job


class Command(BaseCommand):
    help = 'Print safe Google failure diagnostics for one or more job UUIDs (read-only)'

    def add_arguments(self, parser):
        parser.add_argument('job_ids', nargs='+', type=uuid.UUID)

    def handle(self, *args, **options):
        identifiers = list(dict.fromkeys(options['job_ids']))
        if len(identifiers) > 20:
            raise CommandError('Можно проверить не более 20 заданий за один вызов.')
        jobs = Job.objects.only('id', 'kind', 'queue', 'status', 'attempts', 'run_at', 'error').in_bulk(identifiers)
        for identifier in identifiers:
            result = {'job_id': str(identifier)}
            job = jobs.get(identifier)
            if job is None:
                result['found'] = False
            else:
                details = stored_google_error_details(job.error)
                result.update(found=True,
                    kind=job.kind if job.kind in ('google_event', 'google_sync') else 'other',
                    queue=job.queue if job.queue in ('service', 'chat', 'notifications') else 'unknown',
                    status=job.status if job.status in ('pending', 'running', 'done', 'failed') else 'unknown',
                    attempts=job.attempts, run_at=job.run_at.isoformat(), error=details)
                if job.error and not any(key in details for key in ('status', 'code', 'operation')):
                    result['note'] = 'HTTP-статус и причина Google не сохранены. По этому заданию нельзя установить точную причину.'
            self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
