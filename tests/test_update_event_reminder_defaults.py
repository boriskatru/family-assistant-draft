from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.utils import timezone

from assistant import recurrence, services
from assistant.models import Event


def run(apply=False, target=None):
    output = StringIO()
    call_command('update_event_reminder_defaults', apply=apply, id=target, stdout=output)
    return output.getvalue()


def saved(owner, title='Созвон', offsets=(15,), ahead=300):
    start = timezone.now() + timedelta(minutes=ahead)
    event = services.create_event(owner, title=title, starts_at=start, ends_at=start + timedelta(hours=1),
                                  remind_minutes=list(offsets))
    event.reminders.update(kind='reminder')  # v8 had no marker for explicit offsets.
    return event


def test_retime_only_upcoming_matching_single_offset(people):
    owner, _ = people
    call = saved(owner)
    trip = saved(owner, 'Поездка в Истру', ahead=3000)
    untouched = [saved(owner, 'Театр'), saved(owner, offsets=(30,)), saved(owner, offsets=(60, 15)),
                 saved(owner, offsets=()), saved(owner, ahead=40), saved(owner, ahead=-10)]
    now = timezone.now()
    recent = services.create_event(owner, title='Созвон', starts_at=now+timedelta(days=2),
                                   ends_at=now+timedelta(days=2, hours=1), remind_minutes=[15])
    sent = saved(owner)
    sent.reminders.update(status='sent')
    assert 'eligible=2 applied=0' in run()
    assert call.reminders.get().offset_minutes == 15
    assert 'eligible=1 applied=1' in run(True, call.pk)
    reminder = call.reminders.get()
    assert (reminder.offset_minutes, reminder.due_at) == (60, call.starts_at - timedelta(hours=1))
    assert 'eligible=1 applied=1' in run(True)
    assert trip.reminders.get().due_at == trip.starts_at - timedelta(days=1)
    assert 'eligible=0 applied=0' in run(True)
    assert [sorted(e.reminders.values_list('offset_minutes', flat=True)) for e in untouched] == [
        [15], [30], [15, 60], [], [15], [15],
    ]
    assert sent.reminders.get().status == 'sent'
    assert recent.reminders.get().offset_minutes == 15


def test_retime_series_including_dates_materialized_before_and_after_conversion(people):
    owner, _ = people
    first = saved(owner, ahead=3*1440)
    rule = recurrence.create_rule_from_event(owner, first, 'FREQ=DAILY;COUNT=5')
    with patch.object(recurrence, 'HORIZON_DAYS', 1):
        recurrence.materialize_recurrences(first.starts_at)
    assert Event.objects.filter(recurrence=rule).count() > 1
    run(True, rule.pk)
    rule.refresh_from_db()
    assert [r['offset_minutes'] for r in rule.template['reminders']] == [60]
    recurrence.materialize_recurrences(first.starts_at)
    for instance in Event.objects.filter(recurrence=rule):
        assert instance.reminders.get().offset_minutes == 60
    assert 'eligible=0' in run(True)
