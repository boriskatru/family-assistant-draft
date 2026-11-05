"""Verify upload checkpoint races with actual PostgreSQL row locks."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
import time

import pytest
from django.db import connection, connections, transaction

from assistant.attachments import persist_upload
from assistant.models import Document, IncomingUpdate


@pytest.mark.django_db(transaction=True)
def test_two_workers_persist_same_update_as_one_original_and_checkpoint(people, settings, tmp_path):
    if connection.vendor != 'postgresql':
        pytest.skip('Requires independent PostgreSQL sessions and real row locks')

    person, _ = people
    settings.MEDIA_ROOT = tmp_path
    payload = {'update_id': 17801, 'message': {'message_id': 17801, 'caption': 'Билет'}}
    IncomingUpdate.objects.create(update_id=payload['update_id'], payload=payload)
    data = {'filename': 'ticket.txt', 'content': b'one durable original',
            'mime_type': 'text/plain', 'file_id': 'one-telegram-file'}
    pids = Queue()

    def upload():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout TO '15s'")
                cursor.execute('SELECT pg_backend_pid()')
                pids.put(cursor.fetchone()[0])
            doc, state = persist_upload(payload, person, data, 'Билет')
            return str(doc.pk), state['document_id']
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as workers:
        with transaction.atomic():
            IncomingUpdate.objects.select_for_update().get(update_id=payload['update_id'])
            first = workers.submit(upload)
            second = workers.submit(upload)
            backend_pids = [pids.get(timeout=10), pids.get(timeout=10)]
            assert backend_pids[0] != backend_pids[1]
            deadline = time.monotonic() + 10
            with connection.cursor() as cursor:
                while time.monotonic() < deadline:
                    cursor.execute('SELECT cardinality(pg_blocking_pids(%s)) > 0 '
                                   'AND cardinality(pg_blocking_pids(%s)) > 0', backend_pids)
                    if cursor.fetchone()[0]:
                        break
                    time.sleep(0.02)
                else:
                    pytest.fail('Both workers must contend for the persisted update before release')
        saved = first.result(timeout=10)
        assert second.result(timeout=10) == saved

    doc = Document.objects.get(owner=person)
    assert saved == (str(doc.pk), str(doc.pk))
    assert IncomingUpdate.objects.get(update_id=payload['update_id']).payload['_attachment']['document_id'] == str(doc.pk)
    assert doc.file.read() == data['content']
    assert len([path for path in tmp_path.rglob('*') if path.is_file()]) == 1
