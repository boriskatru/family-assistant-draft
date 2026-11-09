from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone

from assistant.models import Event, Task

pytestmark = pytest.mark.django_db


def fixture_block(person, **task_fields):
    task = Task.objects.create(owner=person, assignee=person, title='Дело', **task_fields)
    task.participants.add(person)
    event = Event.objects.create(owner=person, task=task, title='Блок', kind='task', flexible=True,
        starts_at=timezone.now()+timedelta(days=2), ends_at=timezone.now()+timedelta(days=2, hours=1))
    event.participants.add(person)
    return task, event


def test_repair_preview_is_read_only_and_apply_is_repeatable(people):
    person, _ = people
    _, obsolete = fixture_block(person, status='done')
    _, active = fixture_block(person)
    output = StringIO()
    with patch('assistant.integrations.google.repair_shared_calendar_participants', return_value={'events': 0, 'participants_added': 0}) as google:
        call_command('repair_schedule_state', stdout=output)
        google.assert_called_once_with(apply=False)
    obsolete.refresh_from_db()
    assert obsolete.status == 'confirmed'
    assert 'dry-run task_blocks_eligible=1 task_blocks_repaired=0' in output.getvalue()
    output = StringIO()
    with patch('assistant.integrations.google.repair_shared_calendar_participants', return_value={'events': 0, 'participants_added': 0}) as google:
        call_command('repair_schedule_state', apply=True, stdout=output)
        google.assert_called_once_with(apply=True)
    obsolete.refresh_from_db()
    active.refresh_from_db()
    assert obsolete.status == 'cancelled' and active.status == 'confirmed'
    assert 'task_blocks_repaired=1' in output.getvalue()
    output = StringIO()
    call_command('repair_schedule_state', apply=True, stdout=output)
    assert 'task_blocks_eligible=0 task_blocks_repaired=0' in output.getvalue()


def test_repair_cancels_only_future_generated_blocks_outside_new_release_window(people):
    person, _ = people
    task, obsolete = fixture_block(person, not_before=timezone.now()+timedelta(days=3))
    meeting = Event.objects.create(owner=person, task=task, title='Настоящая встреча',
        starts_at=obsolete.starts_at, ends_at=obsolete.ends_at, kind='meeting', flexible=True)
    meeting.participants.add(person)
    past = Event.objects.create(owner=person, task=task, title='История', kind='task', flexible=True,
        starts_at=timezone.now()-timedelta(days=2), ends_at=timezone.now()-timedelta(days=2, hours=-1))
    past.participants.add(person)
    call_command('repair_schedule_state', apply=True, stdout=StringIO())
    for record in (obsolete, meeting, past):
        record.refresh_from_db()
    assert obsolete.status == 'cancelled'
    assert meeting.status == past.status == 'confirmed'
