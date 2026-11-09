import io
import json
import zipfile
from datetime import datetime
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied

from assistant import services
from assistant.agent import retrieval
from assistant.integrations import llm
from assistant.integrations.office_text import extract_office, office_kind, OfficeExtractionError


def archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as result:
        for name, value in files.items():
            result.writestr(name, value)
    return buffer.getvalue()


def docx(text='Встреча с Анной 13 декабря', suffix=''):
    return archive({'word/document.xml': '<w:document xmlns:w="urn:word"><w:body>'
        '<w:p><w:r><w:t>' + text + '</w:t></w:r></w:p>'
        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Адрес домика</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        + suffix + '</w:body></w:document>',
        'word/header1.xml': '<w:hdr xmlns:w="urn:word"><w:p><w:r><w:t>Договор</w:t></w:r></w:p></w:hdr>'})


def xlsx():
    serial = (datetime(2026, 12, 24) - datetime(1899, 12, 30)).days
    return archive({
        'xl/workbook.xml': '<workbook xmlns:r="urn:relationships"><sheets><sheet name="Дни рождения" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/sharedStrings.xml': '<sst><si><t>Коля</t></si><si><r><t>По</t></r><r><t>дарок</t></r></si></sst>',
        'xl/styles.xml': '<styleSheet><cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>',
        'xl/worksheets/sheet1.xml': f'<worksheet><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" s="1"><v>{serial}</v></c>'
           '<c r="C1" t="inlineStr"><is><t>Полдень</t></is></c><c r="D1"><f>1+2</f><v>3</v></c></row>'
           '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2" t="b"><v>1</v></c></row></sheetData></worksheet>',
    })


def metadata(text):
    return dict(document_type='other', title='Документ', passengers=[], booking_reference=None,
        origin=None, destination=None, departure=None, arrival=None, warnings=[], extracted_text=text, events=[])


def test_docx_extracts_paragraph_tables_headers_with_boundaries():
    result = extract_office(docx(), 'docx')
    assert 'Встреча с Анной 13 декабря' in result['text']
    assert 'Адрес домика' in result['text'] and 'Договор' in result['text']
    assert result['truncated'] is False
    limited = extract_office(docx('А' * 200), 'docx', max_chars=30)
    assert len(limited['text']) == 30 and limited['truncated'] is True


def test_xlsx_extracts_sheets_shared_inline_strings_dates_and_cached_formula():
    result = extract_office(xlsx(), 'xlsx')['text']
    assert '[Лист: Дни рождения]' in result and 'A1: Коля' in result
    assert 'B1: 2026-12-24' in result and 'C1: Полдень' in result
    assert 'D1: =1+2 [сохранённое значение: 3]' in result
    assert 'A2: Подарок' in result and 'B2: TRUE' in result


@pytest.mark.parametrize('content', [b'not a zip',
    archive({'../escape': 'secret'}),
    archive({'word/document.xml': '<!DOCTYPE x [<!ENTITY leak "private">]><x>&leak;</x>'}),
    archive({'word/document.xml': ('<!DOCTYPE x [<!ENTITY leak "private">]><x>&leak;</x>').encode('utf-16')}),
    archive({'word/document.xml': 'x' * (16 * 1024 * 1024 + 1)})])
def test_office_invalid_and_unsafe_inputs_fail_without_contents(content):
    with pytest.raises(OfficeExtractionError) as exc:
        extract_office(content, 'docx')
    assert 'secret' not in str(exc.value) and 'private' not in str(exc.value)


def test_office_type_detection_works_for_generic_telegram_mime():
    assert office_kind('test.DOCX', 'application/octet-stream') == 'docx'
    assert office_kind('file', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet') == 'xlsx'


@pytest.mark.parametrize('kind,content', [('docx', docx()), ('xlsx', xlsx())])
def test_office_readable_with_and_without_document_model(kind, content, settings):
    settings.LLM_API_KEY = ''
    result = llm.extract_document(content, 'application/octet-stream', 'file.' + kind)
    assert result['extracted_text'] and result['recognition_status'] == 'unavailable'
    settings.LLM_API_KEY = 'test-only'
    settings.LLM_DOCUMENT_MODEL = 'test-document-model'
    with patch.object(llm, '_post_response', return_value=metadata('model rewrite')) as post:
        result = llm.extract_document(content, 'application/octet-stream', 'file.' + kind)
    assert result['recognition_status'] == 'complete'
    assert result['extracted_text'] != 'model rewrite'
    inputs = post.call_args.args[1][0]['content']
    assert inputs[0]['type'] == 'input_text'
    assert json.loads(inputs[0]['text'])['untrusted_document_text'] == result['extracted_text']


def test_mixed_pdf_model_receives_original_and_keeps_ocr(settings):
    settings.LLM_API_KEY = 'test-only'
    settings.LLM_DOCUMENT_MODEL = 'test-document-model'
    with patch('assistant.integrations.pdf_text.extract_pdf', return_value={
        'pages': [{'page': 1, 'text': 'Текстовая страница'}, {'page': 2, 'text': ''}], 'truncated': False,
    }), patch.object(llm, '_post_response', return_value=metadata('Текстовая страница. Скан: рейс АВ123')) as post:
        result = llm.extract_document(b'%PDF-fake', 'application/pdf', 'mixed.pdf')
    assert any(item['type'] == 'input_file' for item in post.call_args.args[1][0]['content'])
    assert 'Текстовая страница' in result['extracted_text']
    assert 'рейс АВ123' in result['recognized_text']


@pytest.mark.django_db
@pytest.mark.parametrize('kind,content,query', [('docx', docx(), 'домика'), ('xlsx', xlsx(), 'Коля')])
def test_office_original_read_and_search_obey_permissions(people, settings, tmp_path, kind, content, query):
    a, b = people
    settings.MEDIA_ROOT = tmp_path
    doc = services.save_document(a, 'file.' + kind, content, 'application/octet-stream')
    read = retrieval.read_document(a, doc.pk)
    assert query in read['text'] and read['untrusted_content']
    assert retrieval.search_documents(a, query)['results'][0]['document_id'] == str(doc.pk)
    with pytest.raises(PermissionDenied):
        retrieval.read_document(b, doc.pk)


@pytest.mark.django_db
def test_pdf_read_search_merges_native_and_ocr_without_repeating_metadata(people, settings, tmp_path):
    a, _ = people
    settings.MEDIA_ROOT = tmp_path
    doc = services.save_document(a, 'mixed.pdf', b'%PDF-fake', 'application/pdf',
        metadata={'extracted_text': 'Текстовая страница', 'recognized_text': 'Скан: рейс АВ123'})
    with patch('assistant.integrations.pdf_text.extract_pdf', return_value={
        'pages': [{'page': 1, 'text': 'Текстовая страница'}, {'page': 2, 'text': ''}], 'truncated': False,
    }):
        result = retrieval.read_document(a, doc.pk)
        assert 'Текстовая страница' in result['text']
        assert result['text'].count('Скан: рейс АВ123') == 1
        assert retrieval.search_documents(a, 'АВ123')['results'][0]['document_id'] == str(doc.pk)
