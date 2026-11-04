from datetime import datetime, timedelta, timezone as utc
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile

from assistant import bot, services
from assistant.models import Document, Event, Task, TripSegment
from assistant.agent import retrieval

pytestmark = pytest.mark.django_db
DAY = '2030-01-07'


def event(person, title='Встреча', hour=10, **kwargs):
    start = datetime(2030, 1, 7, hour, tzinfo=utc.utc)
    obj = services.create_event(person, title=title, starts_at=start, ends_at=start + timedelta(hours=1), **kwargs)
    return obj


def document(person, text, tmp_path, settings, filename='notes.txt', visibility='private', **kwargs):
    settings.MEDIA_ROOT = tmp_path
    obj = services.save_document(person, filename, text.encode(), 'text/plain', **kwargs)
    obj.visibility = visibility
    obj.save()
    return obj


def test_busy_content_is_not_a_search_oracle(people):
    a, b = people
    hidden = event(a, title='Диагноз тайна', visibility='busy_only', location='Секретный адрес')
    full = retrieval.search_records(b, 'event')
    assert full['items'][0]['title'] == 'Занят'
    assert 'тайна' not in str(full) and 'Секретный адрес' not in str(full)
    assert full['permission_refs'] == [{'kind': 'event', 'id': str(hidden.pk), 'mode': 'busy'}]
    assert retrieval.search_records(b, 'event', query='Диагноз')['items'] == []
    assert retrieval.search_records(b, 'event', shared_context=True)['items'] == []
    assert retrieval.read_record(b, 'event', str(hidden.pk))['record']['title'] == 'Занят'
    with pytest.raises(PermissionDenied):
        retrieval.read_record(b, 'event', str(hidden.pk), shared_context=True)


def test_table_allowlist_and_record_pagination(people):
    a, _ = people
    for i in range(3):
        services.create_task(a, title=f'Номер {i}')
    one = retrieval.search_records(a, 'task', limit=2)
    two = retrieval.search_records(a, 'task', offset=one['next_offset'], limit=2)
    assert len(one['items']) == 2 and len(two['items']) == 1 and two['next_offset'] is None
    assert set(i['id'] for i in one['items']).isdisjoint(i['id'] for i in two['items'])
    for kind in ('calendarconnection', 'person', 'SELECT * FROM assistant_task'):
        with pytest.raises(ValidationError):
            retrieval.search_records(a, kind)
    with pytest.raises(ValidationError):
        retrieval.search_records(a, 'task', date_from='2030-02-01', date_to='2030-01-01')


def test_date_filters_use_user_zone_and_exclusive_end(people):
    a, _ = people
    a.timezone = 'Asia/Tokyo'; a.save()
    early = event(a, hour=0)
    late = event(a, hour=20)
    rows = retrieval.search_records(a, 'event', date_from=DAY, date_to='2030-01-08')['items']
    assert [r['id'] for r in rows] == [str(early.pk)]
    assert str(late.pk) not in str(rows)


def test_shared_trip_does_not_reveal_private_children(people, tmp_path, settings):
    a, b = people
    trip = services.create_trip(a, title='Общая', visibility='shared')
    private = event(a, title='Личный этап')
    public = event(a, title='Открытый этап', visibility='shared')
    TripSegment.objects.create(trip=trip, event=private, origin='Тайна', destination='Ещё тайна')
    TripSegment.objects.create(trip=trip, event=public, origin='A', destination='B')
    secret = document(a, 'Секрет', tmp_path, settings)
    secret.trips.add(trip)
    result = retrieval.read_record(b, 'trip', str(trip.pk))
    assert 'Личный этап' not in str(result) and 'Тайна' not in str(result)
    assert str(private.pk) not in str(result) and str(secret.pk) not in str(result)
    assert result['record']['segments'][0]['event']['title'] == 'Открытый этап'
    assert result['record']['documents'] == []


def test_project_reader_exposes_only_accessible_linked_tasks(people):
    a, b = people
    project = services.create_project(a, title='Общий проект', visibility='shared')
    services.create_task(a, title='Скрытая задача', project_id=str(project.pk))
    public = services.create_task(a, title='Общая задача', project_id=str(project.pk), visibility='shared')
    result = retrieval.read_record(b, 'project', str(project.pk))
    assert 'Скрытая' not in str(result)
    assert result['record']['related']['task']['total'] == 1
    assert result['record']['related']['task']['items'][0]['id'] == str(public.pk)


def test_original_text_is_read_beyond_legacy_excerpt_and_paginates(people, tmp_path, settings):
    a, b = people
    body = 'А' * 35000 + '\nУникальный билет Япония\n' + 'Б' * 100
    doc = document(a, body, tmp_path, settings)
    doc.extracted_text = body[:30000]; doc.save()
    found = retrieval.search_documents(a, 'Уникальный Япония')
    assert found['results'][0]['document_id'] == str(doc.pk)
    assert 'Япония' in found['results'][0]['excerpt']
    page = retrieval.read_document(a, str(doc.pk), offset=35000, limit=10)
    assert page['text'] == body[35000:35010] and page['next_offset'] == 35010
    assert page['sources'][0]['start'] == 35000
    with pytest.raises(PermissionDenied):
        retrieval.read_document(b, str(doc.pk))
    assert retrieval.search_documents(b, 'Япония')['results'] == []
    with pytest.raises(PermissionDenied):
        retrieval.read_document(a, str(doc.pk), shared_context=True)


def test_pdf_reads_original_later_pages_with_page_citations(people, tmp_path, settings):
    a, _ = people
    settings.MEDIA_ROOT = tmp_path
    doc = services.save_document(a, 'ticket.pdf', b'%PDF fake original', 'application/pdf')
    pages = [{'page': n + 1, 'text': 'x' * 2000} for n in range(31)]
    pages.append({'page': 32, 'text': 'Рейс Москва Токио номер XYZ9'})
    with patch('assistant.integrations.pdf_text.extract_pdf', return_value={'pages': pages, 'truncated': False}) as extract:
        result = retrieval.search_documents(a, 'XYZ9')
        assert result['results'][0]['page'] == 32
        offset = result['results'][0]['read_offset']
        read = retrieval.read_document(a, str(doc.pk), offset=offset)
        assert 'XYZ9' in read['text']
        assert 32 in [s['page'] for s in read['sources']]
        assert extract.call_args.kwargs == {'max_pages': 200, 'max_chars': 1000000, 'max_page_chars': 100000}


def test_pdf_budget_failure_preserves_saved_text_without_parser_details(people, tmp_path, settings):
    from assistant.integrations.pdf_text import PDFExtractionError
    a, _ = people
    settings.MEDIA_ROOT = tmp_path
    doc = services.save_document(a, 'broken.pdf', b'%PDF fake original', 'application/pdf')
    doc.extracted_text = 'Ранее распознанный билет'; doc.save()
    with patch('assistant.integrations.pdf_text.extract_pdf', side_effect=PDFExtractionError('SECRET parser bytes')):
        result = retrieval.read_document(a, str(doc.pk))
    assert result['text'] == doc.extracted_text
    assert result['warnings'] and 'SECRET' not in str(result)


def test_document_ocr_fallback_and_path_guard(people, tmp_path, settings):
    a, _ = people
    settings.MEDIA_ROOT = tmp_path
    doc = services.save_document(a, 'scan.jpg', b'JPEG fake', 'image/jpeg', metadata={'extracted_text': 'Бронь OCR987'})
    result = retrieval.search_documents(a, 'OCR987')
    assert result['results'][0]['document_id'] == str(doc.pk)
    doc.file.name = '../outside.txt'; doc.original_name='outside.txt'; doc.mime_type='text/plain'; doc.save()
    with pytest.raises(PermissionDenied):
        retrieval.read_document(a, str(doc.pk))


def test_document_caption_preserved_alongside_ocr_and_acl(people, tmp_path, settings):
    owner, spouse = people
    settings.MEDIA_ROOT = tmp_path
    caption = 'Английский театр LYCEUM\n' + '🎭 ' * 300 + 'Юбилей (35+)'
    ocr = 'Тётка Чарли · 13.11.2026 16:00'
    doc = services.save_document(owner, 'афиша.jpg', b'photo', 'image/jpeg',
                                 metadata={'extracted_text': ocr}, telegram_caption=caption)
    query = 'юбилей (35+)'
    results = retrieval.search_documents(owner, 'Юбилей Чарли')['results']
    assert [r['document_id'] for r in results] == [str(doc.pk)]
    assert retrieval.search_records(owner, 'document', query)['items'][0]['id'] == str(doc.pk)
    assert retrieval.search_records(owner, 'document', 'юбилей .*')['items'] == []
    read = retrieval.read_document(owner, doc.pk)
    assert caption in read['text'] and ocr in read['text']
    assert read['text'].count(caption) == 1 and read['untrusted_content']
    card = bot.dispatch_command(owner, owner.chat_id, f'/show document {doc.pk}', False)
    assert caption in card['text'] and 'Распознанные сведения' not in card['text']
    assert retrieval.search_documents(spouse, query)['results'] == []
    assert retrieval.search_records(owner, 'document', query, shared_context=True)['total'] == 0
    with pytest.raises(PermissionDenied):
        bot.dispatch_command(spouse, spouse.chat_id, f'/show document {doc.pk}', False)
    with pytest.raises(PermissionDenied):
        retrieval.read_record(spouse, 'document', doc.pk)


def test_caption_survives_full_document_and_metadata_budgets(people, tmp_path, settings, monkeypatch):
    owner, _ = people
    caption = 'Уникальная подпись'
    monkeypatch.setattr(retrieval, 'MAX_DOCUMENT_CHARS', 20000)
    doc = document(owner, 'x' * 22000, tmp_path, settings,
                   metadata={'extracted_text': 'x' * 22000}, telegram_caption=caption)
    result = retrieval.read_document(owner, doc.pk, limit=100)
    assert caption in result['text'] and result['content_truncated']
    record = retrieval.read_record(owner, 'document', doc.pk)['record']
    assert record['telegram_caption'] == caption and record['metadata_truncated']


def test_caption_can_be_read_without_ocr(people, tmp_path, settings):
    owner, _ = people
    settings.MEDIA_ROOT = tmp_path
    doc = services.save_document(owner, 'photo.jpg', b'photo', 'image/jpeg', telegram_caption='Места в партере')
    result = retrieval.read_document(owner, doc.pk)
    assert result['text'] == 'Подпись в Telegram:\nМеста в партере'
    assert result['warnings'] == []
    assert retrieval.search_documents(owner, 'партере')['results'][0]['document_id'] == str(doc.pk)


def test_document_structured_metadata_search_and_long_offsets(people, tmp_path, settings):
    a, _ = people
    doc = document(a, 'x' * 120000 + 'Последняя страница', tmp_path, settings,
                   metadata={'booking_reference': 'RESV2345'})
    result = retrieval.search_documents(a, 'RESV2345')
    assert 'RESV2345' in result['results'][0]['excerpt']
    read = retrieval.read_document(a, str(doc.pk), offset=120000)
    assert 'Последняя страница' in read['text']


def test_document_search_uses_scan_cursor_without_losing_matches(people, tmp_path, settings, monkeypatch):
    a, _ = people
    monkeypatch.setattr(retrieval, 'SEARCH_SCAN_SIZE', 2)
    docs = [document(a, f'Метка {i}', tmp_path, settings, filename=f'{i}.txt') for i in range(4)]
    first = retrieval.search_documents(a, 'Метка', limit=1)
    assert first['searched_documents'] == 1 and first['next_offset'] == 1
    second = retrieval.search_documents(a, 'Метка', offset=first['next_offset'], limit=10)
    assert second['searched_documents'] == 2 and second['next_offset'] == 3
    third = retrieval.search_documents(a, 'Метка', offset=second['next_offset'])
    assert third['next_offset'] is None
    ids = [r['document_id'] for result in (first, second, third) for r in result['results']]
    assert len(set(ids)) == 4


def test_common_availability_uses_private_times_without_details(people, monkeypatch):
    a, b = people
    for p in people:
        p.timezone='UTC'; p.save()
    private = event(b, title='Скрытый приём', hour=7, buffer_after=30)
    monkeypatch.setattr(retrieval.timezone, 'now', lambda: datetime(2030, 1, 6, tzinfo=utc.utc))
    result = retrieval.available_slots(a, DAY, participants=[str(a.pk), str(b.pk)])
    assert result['windows'][0]['starts_at'] == '2030-01-07T08:30:00+00:00'
    assert 'Скрытый' not in str(result) and str(private.pk) not in str(result)
    assert result['availability_only'] is True
    assert Event.objects.count() == 1
    with pytest.raises(PermissionDenied):
        retrieval.available_slots(a, DAY, shared_context=True)


def test_unknown_or_unauthorised_participant_fails(people):
    a, b = people
    with pytest.raises(PermissionDenied):
        retrieval.available_slots(a, DAY, participants=['00000000-0000-0000-0000-000000000001'])
    with pytest.raises(ValidationError):
        retrieval.available_slots(a, DAY, participants=['bad-id'])
    with pytest.raises(ValidationError):
        retrieval.read_record(a, 'secret_table', str(b.pk))
