from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from django.utils import timezone

from assistant import bot, services
from assistant.chat_memory import history_for_agent, read_chat_history, remember_turn
from assistant.models import Conversation, IncomingUpdate


pytestmark = pytest.mark.django_db


def say(person, chat, number, request, answer, refs=()):
    remember_turn(person, chat, number, request, {'text': answer, 'permission_refs': list(refs)})


def test_window_counts_user_turns_and_follows_telegram_order(people, settings):
    person, _ = people
    chat = person.chat_id
    settings.AGENT_HISTORY_USER_LIMIT = 50
    for i in [*range(1, 53), 54, 53]:
        say(person, chat, i, f'Q{i}', f'A{i}')
    saved = Conversation.objects.get(person=person, chat_id=chat).messages
    assert len(saved) == 50 and [turn['message_id'] for turn in saved] == list(range(5, 55))
    history, _ = read_chat_history(person, chat, False)
    assert len(history) == 98  # Current request will be the 50th user message.
    assert history[:2] == [{'role': 'user', 'content': 'Q6'}, {'role': 'assistant', 'content': 'A6'}]
    assert history[-2:] == [{'role': 'user', 'content': 'Q54'}, {'role': 'assistant', 'content': 'A54'}]

    # A worker answering an earlier message never treats later completions as
    # prior conversation, including a duplicate of its own update on recovery.
    IncomingUpdate.objects.create(update_id=5252, payload={'message': {'message_id': 52}})
    ctx = SimpleNamespace(person=person, chat_id=chat, shared_context=False, run=SimpleNamespace(update_id=5252))
    assert history_for_agent(ctx)[0][-1]['content'] == 'A51'
    settings.AGENT_HISTORY_USER_LIMIT = 1
    assert history_for_agent(ctx) == ([], [])


def test_revoked_turn_hides_user_quote_and_answer_and_preserves_guards(people, django_assert_num_queries):
    person, spouse = people
    task = services.create_task(spouse, title='Результат обследования', visibility='shared')
    guard = {'kind': 'task', 'id': str(task.pk)}
    for number in range(1, 6):
        say(person, person.chat_id, number, 'Повтори результат обследования', 'Контроль в пятницу', [guard])
    say(person, person.chat_id, 6, 'Как меня зовут?', 'Борис')
    with django_assert_num_queries(2):  # One transcript and one bulk Task read.
        history, guards = read_chat_history(person, person.chat_id, False)
    assert len(history) == 12 and guards == [guard]

    services.share_entity(spouse, 'task', task.pk, 'private')
    assert read_chat_history(person, person.chat_id, False) == (
        [{'role': 'user', 'content': 'Как меня зовут?'}, {'role': 'assistant', 'content': 'Борис'}], [])


def test_budget_keeps_newest_whole_exchange_and_only_its_guards(people):
    person, _ = people
    guards = [{'kind': 'task', 'id': str(services.create_task(person, title=f'Задача {i}').pk)} for i in (1, 2)]
    say(person, person.chat_id, 1, 'Большой вопрос', 'Длинный ответ' * 500, guards[:1])
    say(person, person.chat_id, 2, 'Что дальше?', 'Второе дело', guards[1:])
    recent = [{'role': 'user', 'content': 'Что дальше?'}, {'role': 'assistant', 'content': 'Второе дело'}]
    cap = len(json.dumps(recent, ensure_ascii=False))
    ctx = SimpleNamespace(person=person, chat_id=person.chat_id, shared_context=False)
    assert history_for_agent(ctx, max_history_chars=cap) == (recent, guards[1:])
    # No split exchange and no references left over from rejected turns.
    assert history_for_agent(ctx, max_history_chars=cap-1) == ([], [])


def test_busy_guard_never_exposes_a_full_detail_turn(people):
    person, spouse = people
    now = timezone.now()
    event = services.create_event(spouse, title='Название при полном доступе', visibility='shared',
                                  starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=1, hours=1))
    guard = {'kind': 'event', 'id': str(event.pk)}
    say(person, person.chat_id, 1, 'Покажи окно', 'Слот занят', [{**guard, 'mode': 'busy'}])
    say(person, person.chat_id, 2, 'Назови событие', event.title, [guard])
    messages, refs = read_chat_history(person, person.chat_id, False)
    assert len(messages) == 4 and refs == [guard]
    services.share_entity(spouse, 'event', event.pk, 'busy_only')
    messages, refs = read_chat_history(person, person.chat_id, False)
    assert [message['content'] for message in messages] == ['Покажи окно', 'Слот занят']
    assert refs == [{**guard, 'mode': 'busy'}]


def test_private_and_group_dialogs_are_separate_and_read_legacy_rows(people, settings):
    person, spouse = people
    group = -101
    settings.SHARED_CHAT_ID = group
    past = [{'role': 'user', 'content': 'старое сообщение'}, {'role': 'user', 'content': '/google'},
            {'role': 'assistant', 'content': 'старый ответ без проверок'}]
    for chat_id in [person.chat_id, group]:
        Conversation.objects.create(person=person, chat_id=chat_id, messages=past)
    say(person, group, 2, 'Наш вопрос', 'Наш ответ')
    say(spouse, group, 3, 'Вопрос другого отправителя', 'Его ответ')
    say(spouse, spouse.chat_id, 4, 'Приватный чат супруги', 'Её ответ')
    say(person, person.chat_id, 3, 'Личный диалог', 'ответ')
    say(person, person.chat_id, 4, '/google', 'https://accounts.example/private-state')
    assert [m['content'] for m in read_chat_history(person, group, True)[0]] == ['Наш вопрос', 'Наш ответ']
    assert [m['content'] for m in read_chat_history(person, person.chat_id, False)[0]] == [
        'старое сообщение', 'Личный диалог', 'ответ']
    assert read_chat_history(person, spouse.chat_id, False) == ([], [])
    assert read_chat_history(person, person.chat_id, True) == ([], [])


def test_outbox_completion_remembers_whole_turn_once_without_action_payload(people):
    person, _ = people
    request = 'А' * 2500 + ' Хвост сообщения'
    payload = {'update_id': 999, 'message': {'message_id': 234, 'from': {'id': person.telegram_id},
               'chat': {'id': person.chat_id, 'type': 'private'}, 'text': request}}
    prepared = {'text': request, 'reply': {'text': 'На какое время?', 'permission_refs': [],
                'reply_markup': {'inline_keyboard': []}}}
    for _ in range(2):
        bot._finish_sync(payload, bot._identity(payload), person.pk, prepared)
    assert read_chat_history(person, person.chat_id, False) == (
        [{'role': 'user', 'content': request}, {'role': 'assistant', 'content': 'На какое время?'}], [])
