"""Keep conversational turns per sender and Telegram chat, with their access guards."""
from collections import defaultdict
import json
from uuid import UUID

from django.conf import settings
from django.db import transaction

from . import services
from .models import Conversation, IncomingUpdate


@transaction.atomic
def remember_turn(person, chat_id, message_id, user_text, reply):
    if not isinstance(user_text, str) or not user_text.strip() or user_text.lstrip().startswith('/'):
        return
    if not isinstance(reply, dict) or not isinstance(reply.get('text'), str):
        return
    refs = reply.get('permission_refs', [])
    if not isinstance(refs, list):
        return
    if reply.get('entity_type') and reply.get('entity_id'):
        refs = [*refs, {'kind': reply['entity_type'], 'id': str(reply['entity_id'])}]
    # select_for_update also applies to the get after a concurrent insert on
    # the unique (person, chat_id) constraint. Existing proposals are untouched.
    dialog, _ = Conversation.objects.select_for_update().get_or_create(person=person, chat_id=chat_id)
    turns = [entry for entry in dialog.messages if isinstance(entry, dict) and entry.get('role') == 'user']
    turn = {'role': 'user', 'content': user_text, 'assistant': reply['text'], 'permission_refs': refs}
    if isinstance(message_id, int):
        turn['message_id'] = message_id
        turns = [entry for entry in turns if entry.get('message_id') != message_id]
    turns.append(turn)
    # Workers can finish in a different order from Telegram messages. Legacy
    # user-only entries have no message_id and predate all new-format entries.
    turns.sort(key=lambda entry: entry.get('message_id', 0))
    dialog.messages = turns[-settings.AGENT_HISTORY_USER_LIMIT:]
    dialog.save(update_fields=['messages', 'updated_at'])


def _ref_keys(refs):
    """A malformed guard invalidates the turn, including the user's quotation."""
    if not isinstance(refs, list):
        return None
    keys = []
    for ref in refs:
        if not isinstance(ref, dict) or not isinstance(ref.get('kind'), str):
            return None
        kind = ref['kind']
        mode = ref.get('mode')
        if kind not in services.ENTITY_MODELS or mode not in (None, 'busy') or (mode and kind != 'event'):
            return None
        try:
            key = (kind, UUID(str(ref['id'])), mode)
        except (ValueError, KeyError):
            return None
        keys.append(key)
    return keys


def read_chat_history(person, chat_id, shared, current_message_id=None, *, max_history_chars=None):
    """Return visible prior turns and the guards needed by a reply derived from them."""
    prior_limit = settings.AGENT_HISTORY_USER_LIMIT - 1  # The new user message takes one slot.
    if prior_limit < 1 or chat_id != (settings.SHARED_CHAT_ID if shared else person.chat_id):
        return [], []
    stored = Conversation.objects.filter(person=person, chat_id=chat_id).values_list('messages', flat=True).first() or []
    candidates = []
    for entry in stored:
        if not isinstance(entry, dict) or entry.get('role') != 'user':
            continue
        user_text = entry.get('content')
        if not isinstance(user_text, str) or not user_text.strip() or user_text.lstrip().startswith('/'):
            continue
        message_id = entry.get('message_id')
        if isinstance(message_id, int) and current_message_id is not None and message_id >= current_message_id:
            continue
        if shared and 'assistant' not in entry:
            # The old version never used history from a shared chat. Don't
            # import its unguarded user-only rows into a group conversation.
            continue
        candidates.append(entry)

    # Count input turns before ACL filtering; a revoked turn cannot bring in
    # older messages outside the configured window.
    pairs = []
    ids_by_kind = defaultdict(set)
    for entry in candidates[-prior_limit:]:
        refs = _ref_keys(entry.get('permission_refs', [] if 'assistant' not in entry else None))
        if refs is None:
            continue
        for kind, pk, _ in refs:
            ids_by_kind[kind].add(pk)
        pairs.append((entry, refs))

    # Use one bulk lookup per entity kind for all turns, including replies
    # that inherited the same references from previous conversational context.
    records = {}
    for kind, pks in ids_by_kind.items():
        objects = services.ENTITY_MODELS[kind].objects.all()
        if kind == 'checklist':
            objects = objects.select_related('event', 'trip')
        records[kind] = objects.in_bulk(pks)

    def readable(key):
        kind, pk, mode = key
        obj = records[kind].get(pk)
        return obj is not None and (
            services.can_view(obj, person, shared) or
            (mode == 'busy' and obj.visibility == 'busy_only')
        )

    selected = []
    for entry, refs in reversed(pairs):
        if not all(readable(key) for key in refs):
            continue
        exchange = [{'role': 'user', 'content': entry['content']}]
        if isinstance(entry.get('assistant'), str) and entry['assistant']:
            exchange.append({'role': 'assistant', 'content': entry['assistant']})
        if max_history_chars is not None:
            size = sum(len(json.dumps(message, ensure_ascii=False)) + 2 for message in exchange)
            if size > max_history_chars:
                break
            max_history_chars -= size
        selected.append((exchange, refs))

    messages = []
    guards = {}
    for exchange, refs in reversed(selected):
        messages.extend(exchange)
        for kind, pk, mode in refs:
            if (kind, pk) not in guards or mode is None:
                guard = {'kind': kind, 'id': str(pk)}
                if mode:
                    guard['mode'] = mode
                guards[kind, pk] = guard
    return messages, list(guards.values())


def history_for_agent(ctx, *, max_history_chars=None):
    update_id = getattr(getattr(ctx, 'run', None), 'update_id', None)
    message_id = None
    if update_id is not None:
        payload = IncomingUpdate.objects.filter(update_id=update_id).values_list('payload', flat=True).first() or {}
        message_id = (payload.get('message') or {}).get('message_id')
    return read_chat_history(ctx.person, ctx.chat_id, ctx.shared_context, message_id,
                             max_history_chars=max_history_chars)
