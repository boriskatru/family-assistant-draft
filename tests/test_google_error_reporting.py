"""Google failures remain actionable without leaking provider or user content."""
import io
import json
import logging
import uuid
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone

from assistant.error_reporting import report_error, safe_error_details, stored_google_error_details
from assistant.integrations.google import GoogleIntegrationError, GoogleRetryableError
from assistant.models import Job
from assistant.queue import MAX_ATTEMPTS, claim, enqueue, finish

pytestmark = pytest.mark.django_db(transaction=True)


def google_error(error_type=GoogleIntegrationError, **metadata):
    error = error_type('PRIVATE_PROVIDER_BODY secret-token https://private.example/calendar')
    error.provider = 'google_calendar'
    for key, value in metadata.items():
        setattr(error, key, value)
    return error


def alerts():
    return Job.objects.filter(payload__error_alert=True)


def test_google_error_includes_safe_operation_status_and_reason(people, settings, caplog):
    settings.ERROR_ADMIN_TELEGRAM_ID = people[0].telegram_id
    error = google_error(status_code=403, reason='forbiddenForNonOrganizer', operation='event_update',
                         failure_count=3, retryable_count=1)
    report_error(error, 'worker.service')
    alert = alerts().get().payload['text']
    assert 'Google Calendar' in alert
    assert 'изменение события' in alert
    assert 'HTTP: 403' in alert
    assert 'forbiddenForNonOrganizer' in alert
    assert 'только организатору' in alert
    assert 'Неудачных операций: 3' in alert
    assert 'Из них с временной ошибкой: 1' in alert
    assert 'PRIVATE_PROVIDER_BODY' not in alert + caplog.text
    assert 'secret-token' not in alert + caplog.text
    assert 'private.example' not in alert + caplog.text


def test_google_metadata_is_allowlisted_and_bounded():
    error = google_error(status_code=404, reason='PRIVATE_PROVIDER_BODY', operation='private-calendar-id',
                         failure_count='secret-token', retryable_count=1000001)
    details = safe_error_details(error, 'worker.service')
    assert details['status'] == 404
    assert 'Календарь или событие' in details['reason']
    assert 'модель' not in details['reason']
    assert all(key not in details for key in ('code', 'operation', 'failure_count', 'retryable_count'))
    assert 'PRIVATE_PROVIDER_BODY' not in json.dumps(details)
    assert 'private-calendar-id' not in json.dumps(details)
    assert 'secret-token' not in json.dumps(details)


def test_transient_google_failure_is_logged_and_retried_before_one_terminal_alert(people, settings, caplog):
    settings.ERROR_ADMIN_TELEGRAM_ID = people[0].telegram_id
    job = enqueue('google_sync', {}, 'transient-google-test')
    error = google_error(GoogleRetryableError, status_code=503, reason='backendError', operation='calendar_sync')
    with patch('assistant.management.commands.runworker.handle_job', side_effect=error), caplog.at_level(logging.WARNING):
        for attempt in range(1, MAX_ATTEMPTS + 1):
            Job.objects.filter(pk=job.pk).update(run_at=timezone.now())
            call_command('runworker', queue='service', once=True)
            job.refresh_from_db()
            assert job.attempts == attempt
            assert job.status == ('failed' if attempt == MAX_ATTEMPTS else 'pending')
            assert alerts().count() == (1 if attempt == MAX_ATTEMPTS else 0)
            assert len(job.error) <= 500
            assert json.loads(job.error)['status'] == 503
    assert len([record for record in caplog.records if record.name == 'assistant.error_reporting']) == MAX_ATTEMPTS
    assert 'Автоматические повторные попытки этого задания исчерпаны' in alerts().get().payload['text']
    assert 'PRIVATE_PROVIDER_BODY' not in job.error + caplog.text


def test_permanent_google_failure_stops_retrying_and_alerts_immediately(people, settings):
    settings.ERROR_ADMIN_TELEGRAM_ID = people[0].telegram_id
    job = enqueue('google_event', {'event_id': str(uuid.uuid4())}, 'permanent-google-test')
    error = google_error(status_code=400, reason='badRequest', operation='event_update')
    with patch('assistant.management.commands.runworker.handle_job', side_effect=error) as handler:
        call_command('runworker', queue='service', once=True)
        Job.objects.filter(pk=job.pk).update(run_at=timezone.now())
        call_command('runworker', queue='service', once=True)
    job.refresh_from_db()
    assert job.status == 'failed' and job.attempts == 1
    assert handler.call_count == 1
    assert alerts().count() == 1
    assert 'badRequest' in alerts().get().payload['text']
    assert 'исчерпаны' not in alerts().get().payload['text']


def test_worker_does_not_report_failure_after_losing_job_lease(people, settings):
    settings.ERROR_ADMIN_TELEGRAM_ID = people[0].telegram_id
    enqueue('google_sync', {}, 'lost-lease-google-test')
    with patch('assistant.management.commands.runworker.handle_job', side_effect=google_error()), \
         patch('assistant.management.commands.runworker.finish', return_value=0):
        call_command('runworker', queue='service', once=True)
    assert not alerts().exists()


def test_terminal_finish_remains_fenced():
    job = enqueue('google_event', {}, 'fenced-google-test')
    stale = claim('service')
    Job.objects.filter(pk=job.pk).update(lock_token=uuid.uuid4())
    assert finish(stale, error=google_error(), retryable=False) == 0
    job.refresh_from_db()
    assert job.status == 'running' and not job.error


def test_job_diagnostics_reports_safe_metadata_and_historical_limitations(people, settings):
    job = enqueue('google_event', {'private_message': 'PRIVATE_PAYLOAD'}, 'safe-job-diagnostics')
    claimed = claim('service')
    finish(claimed, error=google_error(status_code=403, reason='forbidden', operation='event_update'), retryable=False)
    historical = enqueue('google_sync', {}, 'historical-job-diagnostics')
    Job.objects.filter(pk=historical.pk).update(error='GoogleRetryableError')
    missing = uuid.uuid4()
    output = io.StringIO()
    call_command('job_diagnostics', str(job.pk), str(historical.pk), str(missing), stdout=output)
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert records[0]['error']['status'] == 403
    assert records[0]['error']['code'] == 'forbidden'
    assert records[0]['error']['operation'] == 'event_update'
    assert 'note' not in records[0]
    assert records[1]['error']['type'] == 'GoogleRetryableError'
    assert 'нельзя установить точную причину' in records[1]['note']
    assert records[2] == {'job_id': str(missing), 'found': False}
    assert 'PRIVATE_PAYLOAD' not in output.getvalue()
    assert 'PRIVATE_PROVIDER_BODY' not in output.getvalue()
    assert 'secret-token' not in output.getvalue()


@pytest.mark.parametrize('stored', [
    'PRIVATE_PROVIDER_BODY',
    'GoogleIntegrationError PRIVATE_PROVIDER_BODY',
    json.dumps({'type': 'GoogleIntegrationError', 'provider': 'google_calendar',
                'code': 'PRIVATE_PROVIDER_BODY', 'operation': 'secret-token',
                'message': 'PRIVATE_PROVIDER_BODY', 'frames': ['private.example']}),
    json.dumps({'type': 'PRIVATE_PROVIDER_BODY', 'provider': 'google_calendar'}),
    json.dumps({'type': 'GoogleIntegrationError', 'provider': 'PRIVATE_PROVIDER_BODY'}),
    json.dumps({'type': ['PRIVATE_PROVIDER_BODY'], 'provider': 'google_calendar'}),
    json.dumps({'type': {'message': 'PRIVATE_PROVIDER_BODY'}, 'provider': 'google_calendar'}),
    '["PRIVATE_PROVIDER_BODY"]',
])
def test_historical_error_content_is_never_reprinted(stored):
    clean = json.dumps(stored_google_error_details(stored))
    assert 'PRIVATE_PROVIDER_BODY' not in clean
    assert 'secret-token' not in clean
    assert 'private.example' not in clean
