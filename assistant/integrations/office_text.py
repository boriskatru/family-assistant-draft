"""Bounded DOCX/XLSX text extraction; never executes macros, formulas or links.

Parsing takes place in an isolated child with resource limits. ZIP paths remain
in memory and are never extracted to disk. All output is untrusted source data.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_DOCUMENT_CHARS = 1_000_000
OFFICE_MIMES = {
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
}


class OfficeExtractionError(RuntimeError):
    pass


def office_kind(filename, mime_type=''):
    suffix = Path(str(filename).replace('\\', '/')).suffix.lower().lstrip('.')
    return suffix if suffix in ('docx', 'xlsx') else OFFICE_MIMES.get(mime_type.lower().split(';', 1)[0].strip())


def extract_office(content, kind, max_chars=MAX_DOCUMENT_CHARS):
    if kind not in ('docx', 'xlsx') or not isinstance(content, bytes) or not content or len(content) > MAX_INPUT_BYTES:
        raise OfficeExtractionError('Office-файл пустой, слишком большой или имеет неподдерживаемый формат.')
    if type(max_chars) is not int or not 1 <= max_chars <= MAX_DOCUMENT_CHARS:
        raise OfficeExtractionError('Некорректный предел чтения Office-файла.')
    try:
        result = subprocess.run(
            [sys.executable, '-I', str(Path(__file__).resolve()), kind, str(max_chars)],
            input=content, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=8, check=False, env={'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}, close_fds=True,
        )
        if result.returncode or len(result.stdout) > max_chars * 6 + 4096:
            raise ValueError()
        parsed = json.loads(result.stdout)
        if set(parsed) != {'text', 'truncated'} or not isinstance(parsed['text'], str) or len(parsed['text']) > max_chars or type(parsed['truncated']) is not bool:
            raise ValueError()
        return parsed
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise OfficeExtractionError('Не удалось прочитать Office-файл в пределах времени и ресурсов.') from None


def _worker(kind, max_chars):
    import io
    import posixpath
    import re
    import zipfile
    from datetime import datetime, timedelta
    from xml.etree import ElementTree as ET

    content = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if not content or len(content) > MAX_INPUT_BYTES:
        raise ValueError()
    z = zipfile.ZipFile(io.BytesIO(content))
    entries = z.infolist()
    if len(entries) > 4096 or sum(i.file_size for i in entries) > 64 * 1024 * 1024:
        raise ValueError()
    names = set()
    for info in entries:
        name = info.filename
        if name in names or name.startswith('/') or '\\' in name or '..' in name.split('/') or info.flag_bits & 1:
            raise ValueError()
        names.add(name)
    def xml(name):
        info = z.getinfo(name)
        if info.file_size > 16 * 1024 * 1024:
            raise ValueError()
        raw = z.read(info)
        if b'\x00' in raw or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise ValueError()
        return ET.fromstring(raw)
    def local(tag):
        return tag.rsplit('}', 1)[-1]
    result, length, truncated = [], 0, False
    def emit(value):
        nonlocal length, truncated
        if not value:
            return
        value = ('\n' if result else '') + value
        remaining = max_chars - length
        result.append(value[:remaining])
        length += min(remaining, len(value))
        truncated = truncated or len(value) > remaining
    def text_nodes(node):
        return ''.join(n.text or '' for n in node.iter() if local(n.tag) == 't')
    if kind == 'docx':
        main = 'word/document.xml'
        extras = sorted(n for n in names if re.fullmatch(r'word/(header\d+|footer\d+|footnotes|endnotes)\.xml', n))
        for name in [main, *extras]:
            if length >= max_chars:
                truncated = True
                break
            root = xml(name)
            if name != main:
                emit('[' + name.split('/')[-1] + ']')
            for paragraph in root.iter():
                if local(paragraph.tag) != 'p':
                    continue
                pieces = []
                for node in paragraph.iter():
                    tag = local(node.tag)
                    if tag == 't':
                        pieces.append(node.text or '')
                    elif tag == 'tab':
                        pieces.append('\t')
                    elif tag in ('br', 'cr'):
                        pieces.append('\n')
                emit(''.join(pieces))
                if length >= max_chars:
                    truncated = True
                    break
    else:
        workbook = xml('xl/workbook.xml')
        relationships = xml('xl/_rels/workbook.xml.rels')
        targets = {}
        for relation in relationships:
            if relation.get('TargetMode') == 'External':
                continue
            target = relation.get('Target', '')
            resolved = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join('xl', target))
            if not resolved.startswith('xl/') or '..' in resolved.split('/'):
                raise ValueError()
            targets[relation.get('Id')] = resolved
        date1904 = any(local(n.tag) == 'workbookPr' and n.get('date1904') in ('1', 'true') for n in workbook)
        shared = []
        if 'xl/sharedStrings.xml' in names:
            shared = [text_nodes(n) for n in xml('xl/sharedStrings.xml') if local(n.tag) == 'si']
        formats, styles = {}, []
        if 'xl/styles.xml' in names:
            for group in xml('xl/styles.xml'):
                if local(group.tag) == 'numFmts':
                    formats.update({int(n.get('numFmtId')): n.get('formatCode', '') for n in group})
                elif local(group.tag) == 'cellXfs':
                    styles = [int(n.get('numFmtId', 0)) for n in group]
        def numeric(value, style):
            try:
                fmt = styles[int(style)] if style is not None else 0
                code = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]', '', formats.get(fmt, '')).lower()
                is_date = fmt in range(14, 23) or fmt in range(45, 48) or bool(re.search('[dmyhs]', code))
                if is_date:
                    serial = float(value)
                    if serial < 0 or serial > 2958465:
                        return value
                    # Excel deliberately preserves a fictional 1900-02-29.
                    if not date1904 and int(serial) == 60:
                        return '1900-02-29 (Excel)'
                    origin = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 31)
                    dt = origin + timedelta(days=serial - (1 if not date1904 and serial > 60 else 0))
                    if fmt in (18, 19, 20, 21, 45, 46, 47) or (code and not re.search('[dmy]', code)):
                        return dt.time().isoformat()
                    return dt.isoformat(sep=' ') if dt.time().isoformat() != '00:00:00' else dt.date().isoformat()
            except (ValueError, IndexError, OverflowError):
                pass
            return value
        sheets = [n for n in workbook.iter() if local(n.tag) == 'sheet']
        for sheet in sheets:
            if length >= max_chars:
                truncated = True
                break
            rel_id = next((value for key, value in sheet.attrib.items() if local(key) == 'id'), None)
            target = targets.get(rel_id)
            if not target or not target.startswith('xl/worksheets/'):
                raise ValueError()
            emit('[Лист: ' + sheet.get('name', '') + ']')
            for row in xml(target).iter():
                if local(row.tag) != 'row':
                    continue
                cells = []
                for cell in row:
                    if local(cell.tag) != 'c':
                        continue
                    typ, value, formula = cell.get('t'), '', ''
                    for child in cell:
                        tag = local(child.tag)
                        if tag == 'v':
                            value = child.text or ''
                        elif tag == 'is':
                            value = text_nodes(child)
                        elif tag == 'f':
                            formula = child.text or ''
                    if typ == 's':
                        index = int(value)
                        if not 0 <= index < len(shared):
                            raise ValueError()
                        value = shared[index]
                    elif typ == 'b':
                        value = 'TRUE' if value == '1' else 'FALSE'
                    elif typ not in ('inlineStr', 'str', 'e', 'd'):
                        value = numeric(value, cell.get('s'))
                    if formula:
                        value = '=' + formula + (' [сохранённое значение: ' + value + ']' if value else ' [значение не сохранено]')
                    if value:
                        cells.append(cell.get('r', '?') + ': ' + value)
                emit('\t'.join(cells))
                if length >= max_chars:
                    truncated = True
                    break
    return {'text': ''.join(result), 'truncated': truncated}


if __name__ == '__main__':
    import os
    try:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (5, 6))
            resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except ImportError:
            pass
        kind, budget = sys.argv[1:]
        budget = int(budget)
        if kind not in ('docx', 'xlsx') or not 1 <= budget <= MAX_DOCUMENT_CHARS:
            raise ValueError()
        sys.stdout.buffer.write(json.dumps(_worker(kind, budget), ensure_ascii=False).encode('utf-8'))
        sys.stdout.buffer.flush()
        code = 0
    except BaseException:
        code = 2
    os._exit(code)
