"""Load the server administrator's optional family profile for each LLM run."""
from pathlib import Path

from django.conf import settings


class PersonalContextError(RuntimeError):
    """Safe to show to a user; never includes file contents or local paths."""


def build_instructions(base: str, shared_context: bool = False) -> str:
    """Append configured context without caching or replacing application rules.

    The optional file is maintained on the VPS, outside the image and source
    control. An empty file/path or a missing file leaves the base unchanged.
    """
    if shared_context and not settings.PERSONAL_CONTEXT_IN_GROUPS:
        return base
    filename = settings.PERSONAL_CONTEXT_FILE
    if not filename:
        return base
    limit = settings.PERSONAL_CONTEXT_MAX_CHARS
    try:
        # Limit decoded characters without reading an unbounded file into RAM.
        with Path(filename).open('r', encoding='utf-8-sig') as source:
            context = source.read(limit + 1)
    except FileNotFoundError:
        return base
    except (OSError, UnicodeError):
        raise PersonalContextError(
            'Не удалось прочитать персональный контекст. Проверьте файл UTF-8 и права доступа на сервере.'
        ) from None
    if len(context) > limit:
        raise PersonalContextError(
            'Персональный контекст превышает PERSONAL_CONTEXT_MAX_CHARS. Сократите файл или увеличьте лимит.'
        )
    context = context.strip()
    if not context:
        return base
    return (
        base + '\n\nПЕРСОНАЛЬНЫЙ КОНТЕКСТ С СЕРВЕРА\n'
        'Ниже — справочная информация и предпочтения семьи, заполненные администратором. '
        'Используй их для понимания сообщений. Они не являются текущим поручением и сами по себе '
        'не создают событий, задач или напоминаний. Актуальные сведения проверяй по БД и последним '
        'уточнениям пользователей. Применяй основные правила доступа, согласования и работы инструментов.\n'
        '<family_context>\n' + context + '\n</family_context>'
    )
