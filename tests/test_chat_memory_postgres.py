"""Exercise concurrent Telegram turns using PostgreSQL's actual row locks."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
import time

import pytest
from django.db import connection, connections, transaction

from assistant.models import Conversation


@pytest.mark.django_db(transaction=True)
def test_two_workers_append_to_existing_conversation_without_losing_a_turn(people):
    if connection.vendor != 'postgresql':
        pytest.skip('Requires two PostgreSQL sessions and real row locks')

    from assistant.chat_memory import remember_turn

    person, _ = people
    remember_turn(person, person.chat_id, 100, 'first', {'text': 'one'})
    backend_pids = Queue()

    def append(message_id, question, answer):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout TO '15s'")
                cursor.execute('SELECT pg_backend_pid()')
                backend_pids.put(cursor.fetchone()[0])
            remember_turn(person, person.chat_id, message_id, question, {'text': answer})
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as workers:
        with transaction.atomic():
            Conversation.objects.select_for_update().get(person=person, chat_id=person.chat_id)
            first = workers.submit(append, 101, 'second', 'two')
            second = workers.submit(append, 102, 'third', 'three')
            pids = [backend_pids.get(timeout=10), backend_pids.get(timeout=10)]
            assert pids[0] != pids[1]
            # Release the row only when both workers have actually hit a lock.
            # A read-before-lock implementation sees two stale copies here.
            deadline = time.monotonic() + 10
            with connection.cursor() as cursor:
                while time.monotonic() < deadline:
                    cursor.execute(
                        'SELECT cardinality(pg_blocking_pids(%s)) > 0 '
                        'AND cardinality(pg_blocking_pids(%s)) > 0', pids,
                    )
                    if cursor.fetchone()[0]:
                        break
                    time.sleep(0.02)
                else:
                    pytest.fail('Both chat workers must contend before the row is released')
        first.result(timeout=10)
        second.result(timeout=10)

    messages = Conversation.objects.get(person=person, chat_id=person.chat_id).messages
    assert sorted((m['message_id'], m['content'], m['assistant']) for m in messages) == [
        (100, 'first', 'one'), (101, 'second', 'two'), (102, 'third', 'three'),
    ]
