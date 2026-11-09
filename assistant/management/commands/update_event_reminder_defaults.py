"""Opt-in replacement of a single 15-minute reminder on existing calls/travel."""
from uuid import UUID

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from assistant.models import Event, RecurrenceRule, Trip
from assistant.reminder_defaults import default_event_reminder_minutes, event_reminder_minutes, event_reminder_due


def _only_fifteens(rows, recipients):
    """Skip empty, mixed, duplicate and custom configurations as a unit."""
    return (bool(recipients) and isinstance(rows, list) and len(rows) == len(recipients)
            and all(isinstance(row, dict) and row.get('kind') == 'reminder'
                    and row.get('offset_minutes') == 15 for row in rows)
            and {str(row.get('recipient_id')) for row in rows} == recipients)


class Command(BaseCommand):
    help = ('Preview replacement of existing 15-minute reminders on future calls and travel. '
            'An explicitly chosen 15 minutes cannot be distinguished from the former default.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Apply the listed replacements.')
        parser.add_argument('--id', type=UUID, help='Limit to a single event or a series and its future events.')

    def handle(self, *args, **options):
        apply, selected = options['apply'], options['id']
        owners = settings.ALLOWED_TELEGRAM_IDS
        eligible = 0
        # Change templates before visiting materialized dates, in the same lock order as the expander.
        rules = RecurrenceRule.objects.filter(kind='event', active=True, owner__telegram_id__in=owners)
        if selected:
            rules = rules.filter(pk=selected)
        for pk in rules.values_list('pk', flat=True).iterator():
            with transaction.atomic():
                rule = rules.select_for_update(of=('self',)).filter(pk=pk).first()
                if rule is None or (rule.until and rule.until <= timezone.now()):
                    continue
                template = rule.template
                rows = template.get('reminders', [])
                recipients = set(map(str, template.get('participants', [])))
                if not _only_fifteens(rows, recipients):
                    continue
                trip = template.get('trip_id')
                offset = 0 if template.get('all_day') else default_event_reminder_minutes(
                    kind=template.get('kind', 'meeting'), title=template.get('title', ''),
                    location=template.get('location', ''), meeting_url=template.get('meeting_url', ''),
                    dated_trip=bool(trip and Trip.objects.filter(pk=trip, starts_on__isnull=False).exists()),
                )
                if offset == 15:
                    continue
                self.stdout.write(f'series {rule.pk} {rule.title!r}: 15 -> {offset} min')
                eligible += 1
                if apply:
                    rule.template = {**template, 'reminders': [{**r, 'offset_minutes': offset} for r in rows]}
                    rule.save(update_fields=['template', 'updated_at'])

        events = Event.objects.filter(starts_at__gt=timezone.now(), status='confirmed', owner__telegram_id__in=owners)
        if selected:
            events = events.filter(Q(pk=selected) | Q(recurrence_id=selected))
        for pk in events.order_by('starts_at').values_list('pk', flat=True).iterator():
            with transaction.atomic():
                event = events.select_related('trip').select_for_update(of=('self',)).filter(pk=pk).first()
                if event is None:
                    continue
                offset = event_reminder_minutes(event)
                due = event_reminder_due(event, offset)
                if offset == 15 or due <= timezone.now():
                    continue
                # Preserve history, queued deliveries, manual multi-offset schedules and exceptions.
                rows = list(event.reminders.select_for_update().values('id', 'recipient_id', 'kind',
                                                                        'offset_minutes', 'status', 'object_version'))
                recipients = {str(p) for p in event.participants.values_list('pk', flat=True)}
                if not _only_fifteens(rows, recipients) or any(
                    row['status'] != 'pending' or row['object_version'] != event.version for row in rows
                ):
                    continue
                self.stdout.write(f'event {event.pk} {event.starts_at:%Y-%m-%d %H:%M %z} {event.title!r}: 15 -> {offset} min')
                eligible += 1
                if apply:
                    event.reminders.filter(pk__in=[r['id'] for r in rows]).update(
                        offset_minutes=offset, due_at=due, updated_at=timezone.now(),
                    )
        mode = 'apply' if apply else 'dry-run'
        self.stdout.write(f'{mode} eligible={eligible} applied={eligible if apply else 0}')
