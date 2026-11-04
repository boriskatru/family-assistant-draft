"""Restore captions from confirmed private Telegram uploads, without replaying updates."""
import re
from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from assistant.bot import telegram_attachment_parts
from assistant.models import Document, IncomingUpdate, Job, Person


def confirmed_caption(document):
    telegram_id = document.owner.telegram_id
    receipts = Job.objects.filter(
        kind='telegram_send', dedup_key__startswith='tg-reply:',
        payload__entity_type='document', payload__entity_id=str(document.pk),
        payload__actor_id=str(document.owner_id), payload__chat_id=telegram_id,
        payload__shared_context=False,
    ).values_list('dedup_key', flat=True)
    # The reply and the document were committed in the same transaction.
    # A hash or Telegram file_id alone does not identify which message was saved.
    update_ids = [
        int(match[1]) for key in receipts
        if (match := re.fullmatch(r'tg-reply:(\d+):0(?::0)?', key))
    ]
    updates = IncomingUpdate.objects.filter(
        status='completed', update_id__in=update_ids, payload__message__caption__isnull=False,
    ).values_list('payload', flat=True)
    candidate = ''
    for payload in updates.iterator():
        message = payload['message']
        sender = message.get('from', message.get('from_user')) or {}
        chat = message.get('chat') or {}
        if (chat.get('type') != 'private' or chat.get('id') != telegram_id
                or sender.get('id') != telegram_id or sender.get('is_bot')):
            continue
        attachment = message.get('document') or (message.get('photo') or [{}])[-1]
        if not attachment or not isinstance(message.get('caption'), str):
            continue
        _, caption = telegram_attachment_parts(message)
        if not caption:
            continue
        if attachment.get('file_id') != document.telegram_file_id or (candidate and candidate != caption):
            return 'ambiguous', ''
        candidate = caption
    return ('eligible', candidate) if candidate else ('unmatched', '')


class Command(BaseCommand):
    help = 'Inspect saved private Telegram uploads for recoverable captions. Pass --apply to save them.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write only unambiguous, missing captions.')

    def handle(self, *args, **options):
        apply = options['apply']
        counts = Counter()
        documents = Document.objects.filter(
            owner__telegram_id__in=settings.ALLOWED_TELEGRAM_IDS,
        ).exclude(telegram_file_id='')
        for doc_id, owner_id in documents.order_by('pk').values_list('pk', 'owner_id').iterator():
            with transaction.atomic():
                qs = documents.select_related('owner')
                if apply:
                    # Serialize with save_document so a concurrent re-forward cannot create
                    # two records with the same owner, file hash and caption.
                    Person.objects.select_for_update().only('pk').get(pk=owner_id)
                    qs = qs.select_for_update(of=('self',))
                document = qs.filter(pk=doc_id).first()
                if document is None:
                    continue
                if document.visibility != 'private':
                    counts['nonprivate'] += 1
                    continue
                if not isinstance(document.metadata, dict) or 'telegram_caption' in document.metadata:
                    counts['existing_metadata'] += 1
                    continue
                state, caption = confirmed_caption(document)
                if state == 'eligible' and Document.objects.filter(
                    owner_id=owner_id, sha256=document.sha256, metadata__telegram_caption=caption,
                ).exclude(pk=doc_id).exists():
                    state = 'duplicate'
                counts[state] += 1
                if apply and state == 'eligible':
                    document.metadata = {**document.metadata, 'telegram_caption': caption}
                    document.version += 1
                    document.save(update_fields=['metadata', 'version', 'updated_at'])
                    counts['applied'] += 1
        mode = 'apply' if apply else 'dry-run'
        fields = ('eligible', 'applied', 'ambiguous', 'duplicate', 'nonprivate', 'existing_metadata', 'unmatched')
        self.stdout.write(mode + ' ' + ' '.join(f'{key}={counts[key]}' for key in fields))
