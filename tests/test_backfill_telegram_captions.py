from io import StringIO

import pytest
from django.core.management import call_command

from assistant.models import Document, IncomingUpdate, Job

pytestmark = pytest.mark.django_db


def document(owner, visibility='private'):
    return Document.objects.create(
        owner=owner, title='Афиша', telegram_file_id='file-77',
        metadata={'ocr_text': 'Тётка Чарли'}, visibility=visibility,
    )


def old_upload(doc, identifier, caption, *, author=None, media='photo', file_id='file-77'):
    owner = author or doc.owner
    message = {
        'message_id': identifier + 20,
        'from_user': {'id': owner.telegram_id, 'is_bot': False},
        'chat': {'type': 'private', 'id': owner.telegram_id},
        'caption': caption,
        media: [{'file_id': 'thumbnail'}, {'file_id': file_id}] if media == 'photo' else {'file_id': file_id},
    }
    source = IncomingUpdate.objects.create(
        update_id=identifier, status='completed',
        payload={'update_id': identifier, 'message': message, '_bot_processed': True},
    )
    receipt = Job.objects.create(
        dedup_key=f'tg-reply:{identifier}:0', kind='telegram_send', queue='notifications',
        payload={'entity_type': 'document', 'entity_id': str(doc.pk), 'actor_id': str(owner.pk),
                 'chat_id': owner.telegram_id, 'shared_context': False},
    )
    return source, receipt


def run_backfill(apply=False):
    output = StringIO()
    call_command('backfill_telegram_captions', apply=apply, stdout=output)
    return output.getvalue()


@pytest.mark.parametrize('media', ['photo', 'document'])
def test_restores_exact_confirmed_caption_once_without_replaying(people, media):
    doc = document(people[0])
    caption = '  Английский театр «LYCEUM»\n13 ноября в 16:00\n'
    old_upload(doc, 1, caption, media=media)
    old_upload(doc, 2, caption, media=media)
    assert 'eligible=1 applied=0' in run_backfill()
    doc.refresh_from_db()
    assert 'telegram_caption' not in doc.metadata
    assert 'applied=1' in run_backfill(apply=True)
    doc.refresh_from_db()
    assert doc.metadata == {'ocr_text': 'Тётка Чарли', 'telegram_caption': caption}
    assert doc.version == 2
    assert 'applied=0' in run_backfill(apply=True)
    doc.refresh_from_db()
    assert doc.version == 2 and Job.objects.count() == 2


@pytest.mark.parametrize('case', [
    'conflicting', 'wrong_file', 'shared', 'foreign_receipt', 'group', 'no_receipt', 'pending', 'typed_command',
])
def test_skips_unproven_or_inappropriate_source(people, case):
    person, spouse = people
    doc = document(person, 'shared' if case == 'shared' else 'private')
    source, receipt = old_upload(
        doc, 4, '/attach event f78' if case == 'typed_command' else 'Секрет',
        author=spouse if case == 'foreign_receipt' else person,
        file_id='different' if case == 'wrong_file' else 'file-77',
    )
    if case == 'conflicting':
        old_upload(doc, 5, 'Другая подпись')
    if case == 'group':
        source.payload['message']['chat']['type'] = 'supergroup'
        source.save(update_fields=['payload'])
    if case == 'no_receipt':
        receipt.delete()
    if case == 'pending':
        source.status = 'pending'
        source.save(update_fields=['status'])
    assert 'applied=0' in run_backfill(apply=True)
    doc.refresh_from_db()
    assert doc.metadata == {'ocr_text': 'Тётка Чарли'}


def test_does_not_duplicate_an_already_reforwarded_document(people):
    person, _ = people
    old, newer = document(person), document(person)
    caption = 'Зал № 3'
    old_upload(old, 7, caption)
    newer.metadata['telegram_caption'] = caption
    newer.save(update_fields=['metadata'])
    assert 'duplicate=1' in run_backfill(apply=True)
    old.refresh_from_db()
    assert 'telegram_caption' not in old.metadata
