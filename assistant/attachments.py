"""Durable upload checkpoints. Never hold a database transaction across an LLM call."""
from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from . import services
from .models import Document, IncomingUpdate


def is_forwarded(message):
    return any(message.get(key) for key in (
        'forward_origin', 'forward_date', 'forward_from', 'forward_from_chat',
        'forward_sender_name', 'forward_from_message_id', 'forward_signature', 'is_automatic_forward',
    ))


def saved_upload(update_id, person, shared=False):
    payload = IncomingUpdate.objects.filter(update_id=update_id).values_list('payload', flat=True).first() or {}
    state = payload.get('_attachment') or {}
    if not state.get('document_id'):
        return None, {}
    doc = services.get_entity('document', state['document_id'], person, shared)
    if doc.owner_id != person.pk:
        raise PermissionDenied('Вложение относится к другому отправителю.')
    return doc, state


@transaction.atomic
def persist_upload(payload, person, data, caption, parent=None, shared=False):
    incoming, _ = IncomingUpdate.objects.get_or_create(update_id=payload['update_id'], defaults={'payload': payload})
    incoming = IncomingUpdate.objects.select_for_update().get(pk=incoming.pk)
    doc, state = saved_upload(payload['update_id'], person, shared)
    if doc is not None:
        return doc, state
    if parent:
        obj = services.get_entity(parent[0], parent[1], person, shared)
        if not services.can_edit(obj, person):
            raise PermissionDenied('Для привязки файла нужны права редактора записи.')
    doc = services.save_document(person, data['filename'], data['content'], data['mime_type'],
                                 telegram_caption=caption, telegram_file_id=data['file_id'])
    if parent:
        relation = {'event': 'events', 'trip': 'trips', 'task': 'tasks'}[parent[0]]
        if not getattr(doc, relation).filter(pk=parent[1]).exists():
            doc = services.link_document(person, doc.pk, *parent)
    message = payload.get('message') or {}
    state = {'document_id': str(doc.pk), 'extraction_done': bool(doc.metadata.get('_extraction_done')),
             'forwarded': is_forwarded(message), 'album': bool(message.get('media_group_id')),
             'attach_only': bool(parent)}
    incoming.payload = {**incoming.payload, '_attachment': state}
    incoming.save(update_fields=['payload', 'updated_at'])
    return doc, state


def original_bytes(doc):
    path = Path(doc.file.path).resolve()
    if Path(settings.MEDIA_ROOT).resolve() not in path.parents:
        raise PermissionDenied('Недопустимый путь документа.')
    with path.open('rb') as source:
        content = source.read(settings.MAX_UPLOAD_MB * 1024 * 1024 + 1)
    if len(content) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise ValidationError('Файл превышает допустимый размер.')
    return content


@transaction.atomic
def finish_extraction(update_id, person, metadata, shared=False):
    incoming = IncomingUpdate.objects.select_for_update().get(update_id=update_id)
    doc, state = saved_upload(update_id, person, shared)
    doc = Document.objects.select_for_update().get(pk=doc.pk)
    if doc.metadata.get('_extraction_done'):
        # Another update for the same original may have completed while we read it.
        state = {**state, 'extraction_done': True}
        incoming.payload = {**incoming.payload, '_attachment': state}
        incoming.save(update_fields=['payload', 'updated_at'])
        return doc, state
    if not state.get('extraction_done'):
        # Caption is the exact Telegram original, never text rewritten by a model.
        caption = doc.telegram_caption
        successful = metadata.get('recognition_status') == 'complete'
        previous = doc.metadata if isinstance(doc.metadata, dict) else {}
        if successful:
            doc.metadata = dict(metadata)
        else:
            # A failed re-read of an older upload must not erase useful ticket data.
            doc.metadata = {**previous, **{key: value for key, value in metadata.items()
                            if key in ('warnings', 'needs_review', 'recognition_status') or not previous.get(key)}}
        doc.metadata['_extraction_done'] = successful or bool(previous.get('_extraction_done'))
        if caption:
            doc.metadata['telegram_caption'] = caption
        doc.document_type = str(doc.metadata.get('document_type') or doc.document_type or 'other')[:30]
        extracted = metadata.get('extracted_text')
        if isinstance(extracted, str) and len(extracted) > len(doc.extracted_text):
            doc.extracted_text = extracted
        doc.version += 1
        doc.save(update_fields=['metadata', 'document_type', 'extracted_text', 'version', 'updated_at'])
        state = {**state, 'extraction_done': True}
        incoming.payload = {**incoming.payload, '_attachment': state}
        incoming.save(update_fields=['payload', 'updated_at'])
    return doc, state


def request_text(doc, message):
    text = f'Обработай полученное вложение, документ {doc.pk}, по правилам работы с вложениями.'
    # Only a caption written by the actual sender can be a new instruction.
    # Forwarded captions remain accessible solely as untrusted document content.
    if not is_forwarded(message) and (message.get('caption') or '').strip():
        text += '\nПоручение отправителя: ' + message['caption'].strip()[:8000]
    return text


def input_reference(ctx):
    doc, state = saved_upload(ctx.run.update_id, ctx.person, ctx.shared_context)
    if doc is None:
        return None
    refs = [{'kind': 'document', 'id': str(doc.pk)}]
    linked = []
    for kind, relation in [('event', 'events'), ('trip', 'trips'), ('task', 'tasks')]:
        for obj in services.visible(getattr(doc, relation).all(), ctx.person, ctx.shared_context)[:20]:
            linked.append({'kind': kind, 'id': str(obj.pk)})
            refs.append({'kind': kind, 'id': str(obj.pk)})
    ctx.inherit_history([], refs)
    ctx.attachment_read_only = bool(state.get('album'))
    return {'document_id': str(doc.pk), 'forwarded': state.get('forwarded', False),
            'album_part': state.get('album', False), 'linked_records': linked}


def receipt(doc, *, text='', markdown=False):
    text = text.replace('Записанных изменений нет.', 'Изменений через инструменты агента нет.')
    prefix = f'Файл сохранён. ID: {doc.pk}' if markdown else f'Файл сохранён: {doc.original_name}\nID: {doc.pk}'
    if doc.telegram_caption:
        prefix += '\nТекст подписи сохранён полностью.'
    return {'text': prefix + ('\n\n' + text if text else f'\nОткрыть: /show document {doc.pk}'),
            'entity_type': 'document', 'entity_id': str(doc.pk),
            'permission_refs': [{'kind': 'document', 'id': str(doc.pk)}], 'format_markdown': markdown}
