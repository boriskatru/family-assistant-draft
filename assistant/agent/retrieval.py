"""Bounded, permission-checked database and original-document reads for agents.

Returned descriptions, documents and metadata are untrusted data, never tool
instructions. No function accepts a database query, SQL expression or file path.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import PermissionDenied, SuspiciousFileOperation, ValidationError
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from assistant.models import Checklist, Document, Event, Person, Project, Task, Trip
from assistant.services import can_edit, can_view, get_entity, safe_event, visible

MODELS = {'event': Event, 'task': Task, 'project': Project, 'trip': Trip, 'document': Document}
MAX_DOCUMENT_PAGES = 200
MAX_DOCUMENT_CHARS = 1_000_000
MAX_PAGE_CHARS = 100_000
SEARCH_SCAN_SIZE = 20


def _authorise(user):
    if user.telegram_id not in settings.ALLOWED_TELEGRAM_IDS:
        raise PermissionDenied('Пользователь не допущен к данным.')


def _page(offset, limit, maximum=50, max_offset=100000):
    if type(offset) is not int or not 0 <= offset <= max_offset:
        raise ValidationError('Некорректное смещение.')
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise ValidationError(f'Лимит должен быть от 1 до {maximum}.')
    return offset, limit


def _ref(kind, obj, user, shared=False):
    result = {'kind': kind, 'id': str(obj.pk)}
    if isinstance(obj, Event) and not can_view(obj, user, shared):
        result['mode'] = 'busy'
    return result


def _source(kind, obj):
    return {'type': 'database', 'kind': kind, 'id': str(obj.pk)}


def _iso(value):
    return value.isoformat() if value is not None else None


def _summary(kind, obj, user, shared=False):
    if kind == 'event' and not can_view(obj, user, shared):
        return safe_event(obj, user, shared)
    result = {'id': str(obj.pk), 'kind': kind, 'title': obj.title,
              'visibility': obj.visibility, 'version': obj.version}
    if hasattr(obj, 'status'):
        result['status'] = obj.status
    if kind == 'event':
        result.update(starts_at=_iso(obj.starts_at), ends_at=_iso(obj.ends_at), timezone=obj.timezone,
                      location=obj.location, meeting_url=obj.meeting_url)
    elif kind == 'task':
        result.update(due_at=_iso(obj.due_at), priority=obj.priority,
                      estimated_minutes=obj.estimated_minutes, remaining_minutes=obj.remaining_minutes)
    elif kind in ('project', 'trip'):
        result.update(starts_on=_iso(obj.starts_on), ends_on=_iso(obj.ends_on))
    elif kind == 'document':
        result.update(filename=obj.original_name, mime_type=obj.mime_type, size=obj.size)
    return result


def _bound(value, user):
    if not value:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = parse_datetime(str(value))
        except ValueError as exc:
            raise ValidationError('Ожидается дата или время ISO 8601.') from exc
    if result is None:
        raise ValidationError('Ожидается дата или время ISO 8601.')
    if timezone.is_naive(result):
        result = result.replace(tzinfo=ZoneInfo(user.timezone))
    return result


def search_records(user, kind, query='', date_from=None, date_to=None, status='', offset=0, limit=20, shared_context=False):
    """Search named domain tables. date_to is exclusive; dates use user's zone."""
    _authorise(user)
    offset, limit = _page(offset, limit)
    model = MODELS.get(kind)
    if model is None:
        raise ValidationError('Раздел: event, task, project, trip или document.')
    if not isinstance(query, str) or len(query) > 300:
        raise ValidationError('Поисковая строка: до 300 символов.')
    start, end = _bound(date_from, user), _bound(date_to, user)
    if start and end and end <= start:
        raise ValidationError('Конец диапазона должен быть позже начала.')
    qs = visible(model.objects.all(), user, shared_context, include_busy=kind == 'event')
    if query.strip():
        # Never use hidden titles as a search predicate: that would be an oracle.
        term = query.strip()
        matches = Q(title__icontains=term) | Q(description__icontains=term)
        if kind == 'document':
            matches |= Q(metadata__telegram_caption__iregex=re.escape(term))
        qs = visible(qs, user, shared_context).filter(matches)
    if status:
        if kind == 'document' or len(status) > 30:
            raise ValidationError('Этот статус не поддерживается.')
        qs = qs.filter(status=status)
    if kind == 'event':
        if start:
            qs = qs.filter(ends_at__gt=start)
        if end:
            qs = qs.filter(starts_at__lt=end)
        qs = qs.order_by('starts_at', 'id')
    elif kind == 'task':
        if start:
            qs = qs.filter(due_at__gte=start)
        if end:
            qs = qs.filter(due_at__lt=end)
        qs = qs.order_by('due_at', 'id')
    elif kind in ('trip', 'project'):
        if start:
            qs = qs.filter(Q(ends_on__gte=start.astimezone(ZoneInfo(user.timezone)).date()) | Q(ends_on__isnull=True))
        if end:
            qs = qs.filter(starts_on__lt=end.astimezone(ZoneInfo(user.timezone)).date())
        qs = qs.order_by('starts_on', 'id')
    else:
        if start or end:
            raise ValidationError('Для документов используйте поиск по тексту, без диапазона дат событий.')
        qs = qs.order_by('-created_at', 'id')
    total = qs.count()
    objects = list(qs[offset:offset + limit])
    return {'items': [_summary(kind, obj, user, shared_context) for obj in objects],
            'total': total, 'next_offset': offset + len(objects) if offset + len(objects) < total else None,
            'permission_refs': [_ref(kind, obj, user, shared_context) for obj in objects],
            'sources': [_source(kind, obj) for obj in objects], 'untrusted_content': True}


def read_record(user, kind, record_id, shared_context=False):
    """Read an authorised object and separately authorised related objects."""
    _authorise(user)
    if kind not in {*MODELS, 'checklist'}:
        raise ValidationError('Неизвестный раздел.')
    if kind == 'event' and not shared_context:
        try:
            obj = Event.objects.get(pk=record_id)
        except (Event.DoesNotExist, ValueError, ValidationError):
            raise PermissionDenied('Объект не найден или недоступен.') from None
        if not can_view(obj, user) and obj.visibility == 'busy_only':
            return {'record': safe_event(obj, user), 'permission_refs': [_ref(kind, obj, user)],
                    'sources': [_source(kind, obj)], 'untrusted_content': True}
    obj = get_entity(kind, record_id, user, shared_context)
    refs = [_ref(kind, obj, user, shared_context)]
    if kind == 'checklist':
        data = {'id': str(obj.pk), 'kind': kind, 'title': obj.title, 'version': obj.version,
                'items': [{'id': str(i.pk), 'text': i.text, 'quantity': i.quantity,
                           'assignee_id': str(i.assignee_id) if i.assignee_id else None,
                           'checked': bool(i.checked_at), 'version': i.version} for i in obj.items.all()[:100]],
                'items_truncated': obj.items.count() > 100}
    else:
        data = _summary(kind, obj, user, shared_context)
        data.update(description=obj.description[:10000], domain=obj.domain, owner_id=str(obj.owner_id))
        if kind == 'event':
            data['place_id'] = str(obj.place_id) if obj.place_id else None
            data['place_snapshot'] = obj.place_snapshot
        if hasattr(obj, 'participants'):
            data['participants'] = [{'id': str(p.pk), 'name': p.name} for p in obj.participants.filter(telegram_id__in=settings.ALLOWED_TELEGRAM_IDS)]
        for parent_kind in ('project', 'trip', 'task'):
            if getattr(obj, parent_kind + '_id', None):
                parent = getattr(obj, parent_kind)
                if can_view(parent, user, shared_context):
                    data[parent_kind] = {'id': str(parent.pk), 'title': parent.title}
                    refs.append(_ref(parent_kind, parent, user, shared_context))
        if kind == 'project':
            data['goal'] = obj.goal
        related = {}
        if kind == 'project':
            related = {'task': Task.objects.filter(project=obj), 'event': Event.objects.filter(project=obj),
                       'trip': Trip.objects.filter(project=obj)}
        elif kind == 'trip':
            related = {'task': Task.objects.filter(trip=obj), 'event': Event.objects.filter(trip=obj)}
        elif kind == 'document':
            related = {'task': obj.tasks.all(), 'event': obj.events.all(), 'trip': obj.trips.all()}
        elif kind == 'task':
            related = {'task': obj.subtasks.all()}
        if related:
            data['related'] = {}
            for related_kind, candidates in related.items():
                candidates = visible(candidates, user, shared_context).order_by('created_at', 'id')
                members = list(candidates[:20])
                data['related'][related_kind] = {
                    'items': [_summary(related_kind, member, user, shared_context) for member in members],
                    'total': candidates.count(), 'truncated': candidates.count() > 20}
                refs += [_ref(related_kind, member, user, shared_context) for member in members]
        if kind == 'task':
            data.update(assignee_id=str(obj.assignee_id), allow_split=obj.allow_split, category=obj.category)
            deps = list(visible(obj.dependencies.all(), user, shared_context)[:50])
            data['dependencies'] = [_summary('task', d, user, shared_context) for d in deps]
            refs += [_ref('task', d, user, shared_context) for d in deps]
        if kind == 'event':
            data.update(buffer_before=obj.buffer_before, buffer_after=obj.buffer_after,
                        kind_detail=obj.kind, end_timezone=obj.end_timezone, flexible=obj.flexible,
                        sync_status=obj.sync_status, packing_notes=obj.packing_notes)
        if kind == 'trip':
            data['packing_notes'] = obj.packing_notes
            data['segments'] = []
            for segment in obj.segments.select_related('event').order_by('position')[:100]:
                if can_view(segment.event, user, shared_context):
                    data['segments'].append({'id': str(segment.pk), 'event': _summary('event', segment.event, user, shared_context),
                                             'origin': segment.origin, 'destination': segment.destination,
                                             'origin_place_id': str(segment.origin_place_id) if segment.origin_place_id else None,
                                             'destination_place_id': str(segment.destination_place_id) if segment.destination_place_id else None,
                                             'origin_snapshot': segment.origin_snapshot, 'destination_snapshot': segment.destination_snapshot,
                                             'booking_status': segment.booking_status, 'carrier': segment.carrier,
                                             'flight_number': segment.flight_number, 'booking_reference': segment.booking_reference,
                                             'checkin_minutes': segment.checkin_minutes, 'baggage_minutes': segment.baggage_minutes,
                                             'transfer_minutes': segment.transfer_minutes})
                    refs.append(_ref('event', segment.event, user, shared_context))
        if kind in ('event', 'trip'):
            data['checklists'] = [{'id': str(c.pk), 'title': c.title} for c in obj.checklists.all()[:20]]
            refs += [{'kind': 'checklist', 'id': c['id']} for c in data['checklists']]
        if kind in ('event', 'trip', 'task'):
            docs = list(visible(obj.documents.all(), user, shared_context)[:20])
            data['documents'] = [_summary('document', d, user, shared_context) for d in docs]
            refs += [_ref('document', d, user, shared_context) for d in docs]
        if kind == 'document':
            # Original paths and storage configuration are never exposed.
            metadata = obj.metadata
            if isinstance(metadata, dict) and 'telegram_caption' in metadata:
                data['telegram_caption'] = obj.telegram_caption
                metadata = {key: value for key, value in metadata.items() if key != 'telegram_caption'}
            metadata_text = json.dumps(metadata, ensure_ascii=False, default=str)
            data.update(document_type=obj.document_type)
            if len(metadata_text) <= 16000:
                data['metadata'] = metadata
            else:
                data.update(metadata_excerpt=metadata_text[:16000], metadata_truncated=True)
    data['can_edit'] = can_edit(obj, user)
    return {'record': data, 'permission_refs': refs, 'sources': [_source(kind, obj)], 'untrusted_content': True}


def _document_text(doc):
    """Return bounded page-aware original text; OCR metadata is a fallback."""
    warnings = []
    chunks = []
    mime = doc.mime_type.lower().split(';', 1)[0]
    is_pdf = mime == 'application/pdf' or doc.original_name.lower().endswith('.pdf')
    is_text = mime in ('text/plain', 'text/markdown', 'text/csv', 'application/json') or Path(doc.original_name).suffix.lower() in ('.txt', '.md', '.csv', '.json')
    truncated = False
    if doc.file and (is_pdf or is_text):
        root = Path(settings.MEDIA_ROOT).resolve()
        try:
            path = Path(doc.file.path).resolve()
        except (SuspiciousFileOperation, ValueError):
            raise PermissionDenied('Недопустимый путь документа.') from None
        if root not in path.parents:
            raise PermissionDenied('Недопустимый путь документа.')
        limit = min(settings.MAX_UPLOAD_MB * 1024 * 1024, 20 * 1024 * 1024)
        try:
            with path.open('rb') as source:
                raw = source.read(limit + 1)
            if len(raw) > limit:
                raise ValidationError('Оригинал документа превышает предел чтения.')
            if is_text:
                try:
                    text = raw.decode('utf-8-sig')
                except UnicodeDecodeError:
                    text = raw.decode('cp1251', errors='replace')
                chunks = [(None, text[:MAX_DOCUMENT_CHARS])]
                truncated = len(text) > MAX_DOCUMENT_CHARS
            else:
                from assistant.integrations.pdf_text import extract_pdf
                parsed = extract_pdf(raw, max_pages=MAX_DOCUMENT_PAGES,
                                     max_chars=MAX_DOCUMENT_CHARS, max_page_chars=MAX_PAGE_CHARS)
                chunks = [(page['page'], page['text']) for page in parsed['pages']]
                truncated = parsed['truncated']
        except (PermissionDenied, ValidationError):
            raise
        except Exception:
            # Parser errors may contain document contents: expose only a fixed notice.
            warnings.append('Оригинал не удалось прочитать; использован сохранённый текст, если он есть.')
            chunks = []
    if not chunks or not any(t.strip() for _, t in chunks):
        stored = doc.extracted_text or ''
        ocr = doc.metadata.get('extracted_text', '') if isinstance(doc.metadata, dict) else ''
        if isinstance(ocr, str) and len(ocr) > len(stored):
            stored = ocr
        chunks = [(None, stored[:MAX_DOCUMENT_CHARS])] if stored or not doc.telegram_caption else []
        truncated = truncated or len(stored) > MAX_DOCUMENT_CHARS
        if not stored.strip() and not doc.telegram_caption:
            warnings.append('Текст отсутствует. Для сканов и фотографий требуется распознавание документов.')
    if doc.telegram_caption:
        chunks.insert(0, (None, 'Подпись в Telegram:\n' + doc.telegram_caption))
    if isinstance(doc.metadata, dict):
        metadata = {key: value for key, value in doc.metadata.items()
                    if key not in {'extracted_text', 'telegram_caption'} and value not in (None, '', [], {})}
        if metadata:
            remainder = MAX_DOCUMENT_CHARS - sum(len(value) + 2 for _, value in chunks)
            if remainder > 0:
                formatted = 'Распознанные реквизиты (требуют проверки):\n' + json.dumps(metadata, ensure_ascii=False, default=str)[:16000]
                chunks.append((None, formatted[:remainder]))
    text_parts = []
    pages = []
    cursor = 0
    for page_number, content in chunks:
        if cursor + (2 if text_parts else 0) >= MAX_DOCUMENT_CHARS:
            truncated = True
            break
        if text_parts:
            text_parts.append('\n\n')
            cursor += 2
        remaining = MAX_DOCUMENT_CHARS - cursor
        if len(content) > remaining:
            content = content[:remaining]
            truncated = True
        pages.append({'page': page_number, 'start': cursor, 'end': cursor + len(content)})
        text_parts.append(content)
        cursor += len(content)
    return ''.join(text_parts), pages, warnings, truncated


def read_document(user, document_id, offset=0, limit=8000, shared_context=False):
    """Read a text chunk. Offsets are Python Unicode character indices, not bytes."""
    _authorise(user)
    offset, limit = _page(offset, limit, maximum=16000, max_offset=MAX_DOCUMENT_CHARS)
    doc = get_entity('document', document_id, user, shared_context)
    text, pages, warnings, truncated = _document_text(doc)
    end = min(len(text), offset + limit)
    content = text[offset:end]
    sources = [{'type': 'document', 'id': str(doc.pk), 'filename': doc.original_name,
                'page': p['page'], 'start': max(offset, p['start']), 'end': min(end, p['end'])}
               for p in pages if p['start'] < end and p['end'] > offset]
    return {'document_id': str(doc.pk), 'filename': doc.original_name, 'text': content,
            'offset': offset, 'next_offset': end if end < len(text) else None,
            'total_characters': len(text), 'content_truncated': truncated,
            'warnings': warnings, 'sources': sources,
            'permission_refs': [_ref('document', doc, user, shared_context)], 'untrusted_content': True}


def search_documents(user, query, offset=0, limit=10, shared_context=False):
    """Lexical original-text search. offset/next_offset paginate documents scanned."""
    _authorise(user)
    offset, limit = _page(offset, limit, maximum=20)
    if not isinstance(query, str) or not query.strip() or len(query) > 300:
        raise ValidationError('Укажите поисковый запрос длиной до 300 символов.')
    terms = re.findall(r'\w+', query.casefold())[:12]
    if not terms:
        raise ValidationError('Запрос должен содержать слова или числа.')
    qs = visible(Document.objects.all(), user, shared_context).order_by('-created_at', 'id')
    total = qs.count()
    results, refs, sources = [], [], []
    scanned = 0
    for doc in qs[offset:offset + SEARCH_SCAN_SIZE]:
        scanned += 1
        text, pages, warnings, truncated = _document_text(doc)
        haystack = (doc.original_name + '\n' + text).casefold()
        if not all(term in haystack for term in terms):
            continue
        lower_text = text.casefold()
        positions = [lower_text.find(term) for term in terms if term in lower_text]
        hit = min(positions) if positions else 0
        begin, end = max(0, hit - 160), min(len(text), hit + 640)
        matching_page = next((p['page'] for p in pages if p['start'] <= hit < p['end']), None)
        source = {'type': 'document', 'id': str(doc.pk), 'filename': doc.original_name,
                  'page': matching_page, 'start': begin, 'end': end}
        results.append({'document_id': str(doc.pk), 'filename': doc.original_name, 'excerpt': text[begin:end],
                        'read_offset': begin, 'page': matching_page, 'content_truncated': truncated,
                        'warnings': warnings})
        refs.append(_ref('document', doc, user, shared_context))
        sources.append(source)
        if len(results) >= limit:
            break
    return {'results': results, 'searched_documents': scanned, 'total_documents': total,
            'next_offset': offset + scanned if offset + scanned < total else None,
            'offset_semantics': 'documents_scanned', 'sources': sources, 'permission_refs': refs,
            'untrusted_content': True}


def available_slots(user, day, duration_minutes=60, participants=None, shared_context=False):
    """Compute common free windows without disclosing private blocking events."""
    _authorise(user)
    if type(duration_minutes) is not int or not 5 <= duration_minutes <= 1440:
        raise ValidationError('Длительность: от 5 до 1440 минут.')
    try:
        target_day = date.fromisoformat(str(day))
        identifiers = [UUID(str(x)) for x in (participants or [user.pk])]
    except (ValueError, TypeError):
        raise ValidationError('Укажите дату YYYY-MM-DD и UUID участников.') from None
    if not 1 <= len(set(identifiers)) <= 2:
        raise ValidationError('Выберите одного или двух участников.')
    actors = list(Person.objects.filter(pk__in=identifiers, telegram_id__in=settings.ALLOWED_TELEGRAM_IDS).order_by('pk'))
    if len(actors) != len(set(identifiers)):
        raise PermissionDenied('Участник не найден или недоступен.')
    from assistant.planning import _at, _base_windows, _busy, _intersection, _subtract
    zone = ZoneInfo(user.timezone)
    begin, end = _at(target_day, time.min, zone), _at(target_day + timedelta(days=1), time.min, zone)
    windows = [(max(begin, timezone.now()), end)]
    refs = {}
    for actor in actors:
        actor_zone = ZoneInfo(actor.timezone)
        cursor = begin.astimezone(actor_zone).date()
        last = end.astimezone(actor_zone).date()
        actor_windows = []
        while cursor <= last:
            actor_windows += _base_windows(actor, cursor, 'personal')
            cursor += timedelta(days=1)
        busy, events = _busy(actor, begin, end)
        actor_windows = _subtract(actor_windows, busy)
        windows = _intersection(windows, actor_windows)
        for event in events:
            if can_view(event, user) or event.visibility == 'busy_only':
                refs[str(event.pk)] = _ref('event', event, user)
    usable = [(start, finish) for start, finish in windows if (finish - start).total_seconds() >= duration_minutes * 60]
    return {'day': target_day.isoformat(), 'timezone': user.timezone,
            'participants': [{'id': str(p.pk), 'name': p.name} for p in actors],
            'windows': [{'starts_at': start.astimezone(zone).isoformat(), 'ends_at': finish.astimezone(zone).isoformat(),
                         'available_minutes': int((finish - start).total_seconds() // 60)} for start, finish in usable[:20]],
            'duration_minutes': duration_minutes, 'truncated': len(usable) > 20,
            'permission_refs': list(refs.values()), 'sources': [], 'availability_only': True,
            'note': 'Свободные интервалы по текущему календарю и окнам сна; подробности личных событий других людей скрыты. Время не забронировано.'}
