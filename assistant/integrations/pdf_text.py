"""Extract untrusted PDF text in a bounded, isolated child process.

The worker receives bytes through stdin, never a model-provided filesystem path.
It has no shell, reduced environment, Linux CPU/address-space limits and a wall
deadline. Parse failure returns a fixed exception without document contents.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


MAX_INPUT_BYTES = 20 * 1024 * 1024
WALL_TIMEOUT_SECONDS = 8


class PDFExtractionError(RuntimeError):
    """Safe error that contains no parser output or document data."""


def extract_pdf(content, max_pages=200, max_chars=1_000_000, max_page_chars=100_000):
    """Return {pages:[{page:int,text:str}],truncated:bool} within hard bounds."""
    if not isinstance(content, bytes) or not content or len(content) > MAX_INPUT_BYTES:
        raise PDFExtractionError('PDF пустой или превышает предел 20 МиБ.')
    for value, upper in ((max_pages, 200), (max_chars, 1_000_000), (max_page_chars, 100_000)):
        if type(value) is not int or not 1 <= value <= upper:
            raise PDFExtractionError('Некорректный предел извлечения PDF.')
    worker = Path(__file__).with_name('pdf_worker.py')
    command = [sys.executable, '-I', str(worker), str(max_pages), str(max_chars), str(max_page_chars)]
    try:
        process = subprocess.run(command, input=content, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=WALL_TIMEOUT_SECONDS,
                                 check=False, env={'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'},
                                 close_fds=True)
    except (subprocess.TimeoutExpired, OSError):
        raise PDFExtractionError('PDF не удалось прочитать в пределах времени и ресурсов.') from None
    if process.returncode != 0 or len(process.stdout) > max_chars * 4 + max_pages * 100 + 4096:
        raise PDFExtractionError('PDF не удалось прочитать в пределах времени и ресурсов.')
    try:
        parsed = json.loads(process.stdout)
        if set(parsed) != {'pages', 'truncated'} or type(parsed['truncated']) is not bool:
            raise ValueError()
        pages = parsed['pages']
        if not isinstance(pages, list) or len(pages) > max_pages:
            raise ValueError()
        total = 0
        for index, page in enumerate(pages, start=1):
            if not isinstance(page, dict) or set(page) != {'page', 'text'}:
                raise ValueError()
            if page['page'] != index or not isinstance(page['text'], str) or len(page['text']) > max_page_chars:
                raise ValueError()
            total += len(page['text'])
        if total > max_chars:
            raise ValueError()
        return parsed
    except (TypeError, ValueError, KeyError):
        raise PDFExtractionError('PDF-процесс вернул некорректный результат.') from None
