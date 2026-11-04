"""Render model Markdown as ordinary Telegram messages with explicit entities."""
from bisect import bisect_right
from collections import deque
from urllib.parse import urlsplit

from markdown_it import MarkdownIt


_MARKDOWN = MarkdownIt('commonmark', {'html': False}).enable('strikethrough')
_MESSAGE_LIMIT = 3900  # Leave headroom below sendMessage's 4096-character limit.
_ENTITY_LIMIT = 100
_INLINE_STYLES = {'strong_open': 'bold', 'em_open': 'italic', 's_open': 'strikethrough'}


def _web_link(href):
    try:
        url = urlsplit(href)
        if (url.scheme in ('http', 'https') and url.hostname and url.username is None
                and url.port != 0 and len(href) <= 2048 and '\\' not in href):
            return ('text_link', href)
    except ValueError:
        pass
    return None


def _inline(tokens, heading):
    styles = [('bold', '')] if heading else []
    for token in tokens:
        kind = token.type
        if kind in _INLINE_STYLES:
            styles.append((_INLINE_STYLES[kind], ''))
        elif kind == 'link_open':
            styles.append(_web_link(token.attrGet('href') or ''))
        elif kind in ('strong_close', 'em_close', 's_close', 'link_close'):
            styles.pop()
        elif kind == 'code_inline':
            # Telegram forbids nesting code/pre inside any other entity.
            link = next((style for style in styles if style and style[0] == 'text_link'), None)
            yield token.content, (link or ('code', ''),)
        elif kind in ('text', 'image', 'softbreak', 'hardbreak'):
            text = '\n' if kind in ('softbreak', 'hardbreak') else token.content
            yield text, tuple(dict.fromkeys(style for style in styles if style))


def _runs(source):
    lists = []
    heading = False
    quote_depth = 0
    item_prefix = ''
    previous_end = None
    for token in _MARKDOWN.parse(source):
        kind = token.type
        if kind in ('heading_open', 'heading_close'):
            heading = kind == 'heading_open'
        elif kind in ('ordered_list_open', 'bullet_list_open'):
            lists.append(int(token.attrs.get('start', 1)) if kind == 'ordered_list_open' else None)
        elif kind in ('ordered_list_close', 'bullet_list_close'):
            lists.pop()
        elif kind == 'list_item_open':
            number = lists[-1]
            item_prefix = '  ' * (len(lists) - 1) + ('• ' if number is None else f'{number}. ')
            if number is not None:
                lists[-1] += 1
        elif kind == 'list_item_close':
            item_prefix = ''
        elif kind in ('blockquote_open', 'blockquote_close'):
            quote_depth += token.nesting
        elif kind in ('inline', 'fence', 'code_block', 'hr'):
            start, end = token.map
            if previous_end is not None:
                yield ('\n\n' if start > previous_end else '\n'), ()
            previous_end = end
            if item_prefix or quote_depth:
                yield '› ' * quote_depth + item_prefix, ()
                item_prefix = ''
            if kind == 'inline':
                yield from _inline(token.children, heading)
            elif kind == 'hr':
                yield '—', ()
            else:
                yield token.content.removesuffix('\n'), (('pre', ''),)


def _text_and_spans(runs):
    """Coalesce adjacent runs into properly nested half-open character ranges."""
    fragments = []
    spans = []
    active = []
    position = 0
    for text, styles in runs:
        if not text:
            continue
        shared = 0
        while shared < min(len(active), len(styles)) and active[shared][1] == styles[shared]:
            shared += 1
        spans.extend((start, position, style) for start, style in active[shared:])
        del active[shared:]
        active.extend((position, style) for style in styles[shared:])
        fragments.append(text)
        position += len(text)
    spans.extend((start, position, style) for start, style in active)
    spans.sort(key=lambda span: (span[0], -span[1]))
    return ''.join(fragments), spans


def _parts(text, spans=()):
    if not text.strip():
        return [{'text': 'Готово.'}]
    # Index Python characters once. Astral characters occupy two Telegram units.
    units = [0]
    for char in text:
        units.append(units[-1] + (2 if ord(char) > 0xffff else 1))
    pending = deque(spans)
    active = []
    parts = []
    start = 0
    while start < len(text):
        end = bisect_right(units, units[start] + _MESSAGE_LIMIT) - 1
        if end < len(text):
            # Prefer a nearby line boundary, then a word boundary.
            middle = (start + end) // 2
            boundary = text.rfind('\n', middle, end)
            if boundary < 0:
                boundary = text.rfind(' ', middle, end)
            if boundary >= 0:
                end = boundary + 1
        while pending and pending[0][0] < end:
            if len(active) == _ENTITY_LIMIT:
                end = pending[0][0]
                break
            active.append(pending.popleft())
        part = {'text': text[start:end]}
        entities = []
        for left, right, (kind, url) in active:
            left = max(left, start)
            right = min(right, end)
            # Telegram excludes trailing whitespace from entity lengths.
            while right > left and text[right - 1].isspace():
                right -= 1
            if left < right:
                entity = {'type': kind, 'offset': units[left] - units[start],
                          'length': units[right] - units[left]}
                if url:
                    entity['url'] = url
                entities.append(entity)
        if entities:
            part['entities'] = entities
        if part['text'].strip():
            parts.append(part)
        active = [span for span in active if span[1] > end]
        start = end
    return parts


def render_plain(text):
    return _parts(text)


def render_markdown(text):
    try:
        rendered, spans = _text_and_spans(_runs(text))
    except (ValueError, RecursionError):
        # An unusual URL or deeply nested markup must not block the outbox.
        return render_plain(text)
    return _parts(rendered, spans) if rendered.strip() else render_plain(text)
