"""Private subprocess entry point for pdf_text.py. Not an application endpoint."""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import warnings


def _resource_limits():
    # Production is Linux. Windows keeps the parent's wall timeout/byte limits;
    # resource is unavailable there. Do not use preexec_fn in a threaded server.
    try:
        import resource
    except ImportError:
        return
    resource.setrlimit(resource.RLIMIT_CPU, (5, 6))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main():
    _resource_limits()
    logging.disable(logging.CRITICAL)
    warnings.simplefilter('ignore')
    max_pages, max_chars, max_page_chars = map(int, sys.argv[1:])
    if not (1 <= max_pages <= 200 and 1 <= max_chars <= 1_000_000 and 1 <= max_page_chars <= 100_000):
        return 2
    content = sys.stdin.buffer.read(20 * 1024 * 1024 + 1)
    if not content or len(content) > 20 * 1024 * 1024:
        return 2
    # Import the parser only after resource limits are active.
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(content), strict=False)
    count = len(reader.pages)
    result = {'pages': [], 'truncated': count > max_pages}
    total = 0
    for index in range(min(count, max_pages)):
        raw = reader.pages[index].extract_text() or ''
        remaining = min(max_page_chars, max_chars - total)
        text = raw[:remaining]
        result['pages'].append({'page': index + 1, 'text': text})
        total += len(text)
        if len(raw) > remaining:
            result['truncated'] = True
        if total >= max_chars:
            result['truncated'] = result['truncated'] or index + 1 < count
            break
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    sys.stdout.buffer.flush()
    return 0


if __name__ == '__main__':
    try:
        result = main()
    except BaseException:
        result = 2
    # No traceback or parser messages; the parent receives only an exit code.
    os._exit(result)
