from assistant.telegram_format import render_markdown


def entity_texts(part):
    """Read entity slices the way a Telegram client does, including surrogate pairs."""
    text_bytes = part['text'].encode('utf-16-le')
    entities = part.get('entities', [])
    result = []
    for i, entity in enumerate(entities):
        start = entity['offset']
        end = start + entity['length']
        assert 0 <= start < end <= len(text_bytes) // 2
        for other in entities[:i]:
            left = other['offset']
            right = left + other['length']
            if start < right and left < end:
                assert (left <= start and end <= right) or (start <= left and right <= end)
                assert {entity['type'], other['type']}.isdisjoint({'code', 'pre'})
        value = text_bytes[2 * start:2 * end].decode('utf-16-le')
        result.append((entity['type'], value))
    return result


def test_schedule_headings_and_times_after_emoji():
    source = (
        '🗓 Борис, вот расписание **с 5 по 18 ноября**.\n\n'
        '### Четверг, 5 ноября\n'
        '- **10:00–13:00** — созвон с участниками.\n'
        '- **14:00–15:00** — обсуждение проекта.'
    )
    part, = render_markdown(source)
    text = part['text']
    assert '**' not in text and '###' not in text
    assert '10:00–13:00' in text and '14:00–15:00' in text
    assert '\n' in text[text.index('созвон'):text.index('14:00')]
    assert {('bold', s) for s in (
        'с 5 по 18 ноября', 'Четверг, 5 ноября', '10:00–13:00', '14:00–15:00',
    )} <= set(entity_texts(part))


def test_long_emphasized_answer_splits_without_losing_text_or_emoji():
    content = 'я🎭' * 2400
    parts = render_markdown(f'До **{content}** конец')
    assert len(parts) >= 2
    assert ''.join(p['text'] for p in parts) == f'До {content} конец'
    styles = []
    for part in parts:
        assert 0 < len(part['text'].encode('utf-16-le')) // 2 <= 3900
        styles.extend(value for kind, value in entity_texts(part) if kind == 'bold')
    assert ''.join(styles) == content


def test_dense_schedule_preserves_each_time_in_order():
    times = [f'{hour:02d}:{minute:02d}' for hour in range(24) for minute in range(0, 60, 10)]
    parts = render_markdown('\n'.join(f'- **{time}** — дело' for time in times))
    assert [value for part in parts for kind, value in entity_texts(part) if kind == 'bold'] == times
    assert all(len(part.get('entities', [])) <= 100 for part in parts)


def test_literal_code_inside_emphasis_and_fences():
    source = '**Открой `/show file_id` сегодня**.\n\n```text\n### Это **текст**, а не заголовок.\n```'
    part, = render_markdown(source)
    entities = entity_texts(part)
    assert ('code', '/show file_id') in entities
    assert any(kind == 'pre' and value.strip() == '### Это **текст**, а не заголовок.'
               for kind, value in entities)
    assert '```' not in part['text']


def test_links_allow_web_urls_only_and_raw_html_has_no_authority():
    source = ('🏛 [Репертуар](https://example.org/shows?q=1&lang=ru) и '
              '[контакт](tg://user?id=123). <b>Текст афиши & билеты</b>')
    part, = render_markdown(source)
    assert 'контакт' in part['text'] and 'Текст афиши & билеты' in part['text']
    assert ('text_link', 'Репертуар') in entity_texts(part)
    links = [entity['url'] for entity in part['entities'] if entity['type'] == 'text_link']
    assert links == ['https://example.org/shows?q=1&lang=ru']
    assert all(kind != 'bold' for kind, _ in entity_texts(part))
    assert 'parse_mode' not in part
