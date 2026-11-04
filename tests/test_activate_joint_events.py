from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from assistant.models import ActionRequest, Event, Job, Reminder
from assistant.services import approve_action


pytestmark = pytest.mark.django_db


def old_request(owner, other):
    start = timezone.now() + timedelta(days=2)
    event = Event.objects.create(
        owner=owner, title='Спектакль', visibility='shared', status='proposed', kind='call',
        starts_at=start, ends_at=start + timedelta(hours=2),
    )
    event.participants.set([owner, other])
    for person in (owner, other):
        Reminder.objects.create(event=event, recipient=person, offset_minutes=30, status='paused',
                                due_at=start-timedelta(minutes=30))
    request = ActionRequest.objects.create(
        initiator=owner, approver=other, kind='confirm_event', target_type='event', target_id=event.pk,
        target_version=event.version, changes={'status': 'confirmed'}, expires_at=start-timedelta(days=1),
    )
    prompt = Job.objects.create(kind='telegram_send', queue='notifications', dedup_key=f'action:{request.pk}')
    return event, request, prompt


def activate(apply=False):
    output = StringIO()
    call_command('activate_joint_events', apply=apply, stdout=output)
    return output.getvalue()


def test_legacy_joint_request_activates_once_and_old_reject_is_inert(people):
    owner, other = people
    event, request, prompt = old_request(owner, other)
    assert 'eligible=1 applied=0' in activate()
    event.refresh_from_db()
    assert event.status == 'proposed'
    assert 'eligible=1 applied=1' in activate(True)
    assert 'eligible=0 applied=0' in activate(True)
    event.refresh_from_db()
    prompt.refresh_from_db()
    request.refresh_from_db()
    assert event.status == 'confirmed' and event.version == 2
    assert request.status == 'superseded' and prompt.status == 'cancelled'
    assert set(event.reminders.filter(status='pending', object_version=2).values_list('recipient_id', flat=True)) == {owner.pk, other.pk}
    assert Job.objects.get(kind='google_event').payload == {'event_id': str(event.pk), 'create_meet': True}
    assert approve_action(other, request.pk, approve=False).status == 'confirmed'
    assert not Job.objects.filter(dedup_key=f'action-result:{request.pk}').exists()


@pytest.mark.parametrize('case', [
    'expired', 'rejected', 'update', 'already_started', 'stale', 'other_only', 'outstanding_edit', 'in_flight',
])
def test_legacy_activation_preserves_unrelated_requests(people, case):
    event, request, prompt = old_request(*people)
    now = timezone.now()
    if case == 'expired':
        request.expires_at = now - timedelta(seconds=1)
    elif case == 'rejected':
        request.status = 'rejected'
    elif case == 'update':
        request.kind = 'update_event'
        request.changes = {'status': 'cancelled'}
    elif case == 'already_started':
        event.starts_at = now - timedelta(hours=1)
    elif case == 'stale':
        event.version += 1
    elif case == 'other_only':
        event.participants.remove(people[0])
    elif case == 'outstanding_edit':
        ActionRequest.objects.create(initiator=people[0], approver=people[1], kind='update_event',
                                     target_type='event', target_id=event.pk, target_version=event.version,
                                     changes={'status': 'cancelled'}, expires_at=request.expires_at)
    else:
        prompt.status = 'running'
    request.save()
    event.save()
    prompt.save()
    assert 'eligible=0 applied=0' in activate(True)
    event.refresh_from_db()
    assert event.status == 'proposed'
    assert not Job.objects.filter(kind='google_event').exists()
