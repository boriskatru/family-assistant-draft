"""Real PostgreSQL contention: optimistic versions must be checked after locking."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
import time

import pytest
from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction

from assistant.addressbook import save_place
from assistant.models import AuditLog, Person, Place


@pytest.mark.django_db(transaction=True)
def test_two_spouses_edit_same_place_without_silently_overwriting_each_other(people):
    if connection.vendor != 'postgresql':
        pytest.skip('Requires two PostgreSQL sessions and real row locks')

    owner, spouse = people
    place = save_place(owner, {
        'title': 'Общий офис', 'visibility': 'shared', 'address': 'Первоначальный адрес',
        'description': 'Сохранить это примечание',
    })
    backend_pids = Queue()

    def edit(person_id, address):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout TO '15s'")
                cursor.execute('SELECT pg_backend_pid()')
                backend_pids.put(cursor.fetchone()[0])
            person = Person.objects.get(pk=person_id)
            try:
                saved = save_place(person, {
                    'id': place.pk, 'expected_version': 1, 'address': address,
                })
            except ValidationError as exc:
                assert 'уже изменилась' in ' '.join(exc.messages)
                return {'status': 'stale', 'person_id': person_id}
            return {'status': 'updated', 'person_id': person_id,
                    'address': saved.address, 'version': saved.version}
        finally:
            # Each thread owns a different PostgreSQL connection. Close it in
            # that same thread so pytest can drop its database afterwards.
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as workers:
        with transaction.atomic():
            Place.objects.select_for_update().get(pk=place.pk)
            first = workers.submit(edit, owner.pk, 'Адрес от Бориса')
            second = workers.submit(edit, spouse.pk, 'Адрес от Анны')
            pids = [backend_pids.get(timeout=10), backend_pids.get(timeout=10)]
            assert pids[0] != pids[1]
            # Both transactions must reach the contested lock before it is
            # released. A read-before-lock implementation would carry two
            # copies of version 1 and incorrectly accept both modifications.
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
                    pytest.fail('Both address-book writers must contend before the row is released')
        outcomes = [first.result(timeout=10), second.result(timeout=10)]

    assert sorted(result['status'] for result in outcomes) == ['stale', 'updated']
    accepted = next(result for result in outcomes if result['status'] == 'updated')
    place.refresh_from_db()
    assert place.version == accepted['version'] == 2
    assert place.address == accepted['address']
    assert place.owner_id == owner.pk and place.visibility == 'shared'
    assert place.title == 'Общий офис' and place.description == 'Сохранить это примечание'
    updates = AuditLog.objects.filter(target_id=place.pk, action='update_addressbook')
    assert updates.count() == 1 and updates.get().actor_id == accepted['person_id']
