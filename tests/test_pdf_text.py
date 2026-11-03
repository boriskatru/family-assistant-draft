import io
import subprocess
from unittest.mock import patch

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from assistant.integrations.pdf_text import PDFExtractionError, extract_pdf


@pytest.fixture
def two_page_pdf():
    writer = PdfWriter()
    for label in ('HELLO WORLD', 'SECOND PAGE'):
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                 NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
            NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(('BT /F1 12 Tf 10 200 Td (' + label + ') Tj ET').encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_actual_pdf_text_extracted_in_subprocess(two_page_pdf):
    assert extract_pdf(two_page_pdf) == {
        'pages': [{'page': 1, 'text': 'HELLO WORLD'}, {'page': 2, 'text': 'SECOND PAGE'}],
        'truncated': False,
    }


def test_pdf_page_limit_reports_incomplete_document(two_page_pdf):
    result = extract_pdf(two_page_pdf, max_pages=1)
    assert result['pages'] == [{'page': 1, 'text': 'HELLO WORLD'}]
    assert result['truncated'] is True


def test_pdf_character_limit_preserves_page_reference(two_page_pdf):
    result = extract_pdf(two_page_pdf, max_chars=5)
    assert result['pages'] == [{'page': 1, 'text': 'HELLO'}]
    assert result['truncated'] is True


@pytest.mark.parametrize('content', [b'', b'not a PDF: sensitive booking data'])
def test_invalid_pdf_does_not_return_parser_contents(content):
    with pytest.raises(PDFExtractionError) as error:
        extract_pdf(content)
    assert 'sensitive' not in str(error.value)


def test_pdf_subprocess_timeout_is_bounded_and_sanitized(two_page_pdf):
    with patch('assistant.integrations.pdf_text.subprocess.run',
               side_effect=subprocess.TimeoutExpired('private input', 8)) as run:
        with pytest.raises(PDFExtractionError) as error:
            extract_pdf(two_page_pdf)
    assert 'private input' not in str(error.value)
    assert run.call_args.kwargs['timeout'] == 8
    assert run.call_args.kwargs['stderr'] == subprocess.DEVNULL
    assert '-I' in run.call_args.args[0]
