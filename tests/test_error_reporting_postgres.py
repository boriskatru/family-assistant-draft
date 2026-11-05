"""PostgreSQL workers must share the rolling alert cooldown atomically."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
import time

import pytest
from django.db import connection, connections, transaction

from assistant.error_reporting import report_error
from assistant.models import Job, Person


@pytest.mark.django_db(transaction=True)
def test_two_workers_emit_one_private_alert_with_the_same_error_id(people, settings):
    if connection.vendor != 'postgresql':
        pytest.skip('Requires two PostgreSQL sessions and real row locks')
    admin, _ = people
    settings.ERROR_ADMIN_TELEGRAM_ID = admin.telegram_id
    settings.ERROR_ALERT_COOLDOWN_SECONDS = 600
    backend_pids = Queue()

    def report():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout TO '15s'")
                cursor.execute('SELECT pg_backend_pid()')
                backend_pids.put(cursor.fetchone()[0])
            return report_error(RuntimeError('not included in diagnostics'), 'agent.run')
        finally:
            # Connections belong to individual threads; close them there so
            # pytest can drop the test database after joining both workers.
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as workers:
        with transaction.atomic():
            Person.objects.select_for_update().get(pk=admin.pk)
            first = workers.submit(report)
            second = workers.submit(report)
            pids = [backend_pids.get(timeout=10), backend_pids.get(timeout=10)]
            assert pids[0] != pids[1]
            deadline = time.monotonic()+10
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
                    pytest.fail('Both reporters must acquire the administrator lock before checking the cooldown')
        error_ids = [first.result(timeout=10), second.result(timeout=10)]

    alerts = Job.objects.filter(kind='telegram_send', payload__error_alert=True)
    assert alerts.count() == 1
    alert = alerts.get()
    assert error_ids == [alert.payload['error_id'], alert.payload['error_id']]
    assert alert.payload['chat_id'] == admin.telegram_id
    assert alert.payload['actor_id'] == str(admin.pk)
