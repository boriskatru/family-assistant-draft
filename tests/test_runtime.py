from datetime import datetime, timedelta, timezone as utc
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied
from assistant import services
from assistant.models import Delivery, Job, Reminder
from assistant.queue import enqueue
from assistant.runtime import schedule_tick, handle_job, send_payload


def test_unknown_id_reply_cannot_send_arbitrary_content(db, settings):
    settings.ALLOWED_TELEGRAM_IDS=()
    job=enqueue('telegram_send',{},'onboarding',queue='notifications')
    with patch('assistant.runtime._tg',return_value={'message_id':42}) as tg:
        send_payload(job,{'chat_id':9988,'onboarding_id':9988,'text':'injected','document_path':'/etc/passwd'})
        assert tg.call_args.args[0]=='sendMessage'
        assert 'injected' not in tg.call_args.args[1]['text']
        assert '9988' in tg.call_args.args[1]['text']
    with pytest.raises(PermissionError):
        send_payload(job,{'chat_id':1234,'onboarding_id':9988})


def test_due_reminder_checklist_dedup_and_revocation(people):
    a,b=people
    now=datetime(2026,11,20,10,0,tzinfo=utc.utc)
    e=services.create_event(a,title='Музей',starts_at=now+timedelta(minutes=15),ends_at=now+timedelta(hours=2),checklist='Билет, камера')
    with patch('assistant.runtime.timezone.now',return_value=now):
        schedule_tick(now)
        schedule_tick(now)
        job=Job.objects.get(kind='reminder')
        with patch('assistant.runtime._tg',return_value={'message_id':123}) as tg:
            handle_job(job)
            handle_job(job)
            assert tg.call_count==1
            assert 'камера' in tg.call_args.args[1]['text']
            assert tg.call_args.args[1]['reply_markup']['inline_keyboard']
    assert Delivery.objects.get(job=job).status=='sent'
    # An original document queued while shared must not be sent after revocation.
    e.visibility='shared';e.save()
    payload={'chat_id':b.chat_id,'actor_id':str(b.id),'entity_type':'event','entity_id':str(e.id),'text':e.title}
    e.visibility='private';e.save()
    with patch('assistant.runtime._tg') as tg, pytest.raises(PermissionDenied):
        send_payload(enqueue('telegram_send',payload,'revoked'),payload)
    tg.assert_not_called()


def test_quiet_hours_defer_reminder_and_downtime_summarizes(people):
    a,_=people
    now=datetime(2026,11,20,22,0,tzinfo=utc.utc)  # 01:00 Moscow
    event=services.create_event(a,title='Позже',starts_at=now+timedelta(hours=8),ends_at=now+timedelta(hours=9),remind_minutes=[480])
    schedule_tick(now)
    job=Job.objects.get(kind='reminder')
    assert job.run_at==datetime(2026,11,21,4,0,tzinfo=utc.utc)  # 07:00 local
    reminder=event.reminders.get()
    assert reminder.status=='queued'
    late=services.create_event(a,title='Пропущено',starts_at=now-timedelta(hours=1),ends_at=now+timedelta(hours=1))
    schedule_tick(now+timedelta(hours=6))
    late.reminders.get().refresh_from_db()
    assert late.reminders.get().status=='summarized'
    assert any('пропущено напоминаний' in j.payload.get('text','') for j in Job.objects.filter(kind='telegram_send'))


def test_trip_notes_become_persistent_checklist(people):
    trip=services.create_trip(people[0],title='Отпуск',packing_notes='Паспорт\nКамера')
    assert list(trip.checklists.get().items.values_list('text',flat=True))==['Паспорт','Камера']


def test_delayed_list_hides_child_after_access_revoked(people):
    a,b=people
    trip=services.create_trip(a,title='Общая поездка',visibility='shared')
    task=services.create_task(a,title='Секретное дело',visibility='shared')
    payload={'chat_id':b.chat_id,'actor_id':str(b.id),'entity_type':'trip','entity_id':str(trip.id),'text':task.title,'reply_markup':{'inline_keyboard':[]},'permission_refs':[{'kind':'task','id':str(task.id)}]}
    services.share_entity(a,'task',task.id,'private')
    with patch('assistant.runtime._tg',return_value={'message_id':90}) as tg:
        send_payload(enqueue('telegram_send',payload,'snapshot'),payload)
        assert 'Секретное' not in tg.call_args.args[1]['text']
        assert 'reply_markup' not in tg.call_args.args[1]


def test_sharing_task_preserves_pending_reminder(people):
    a,_=people
    due=datetime(2026,11,20,14,0,tzinfo=utc.utc)
    task=services.create_task(a,title='Срок',due_at=due)
    services.share_entity(a,'task',task.id,'shared')
    task.refresh_from_db()
    assert task.reminders.get().object_version==task.version
    schedule_tick(due-timedelta(hours=1))
    assert task.reminders.get().status=='queued'


def test_bot_reschedule_does_not_resurrect_google_reminder_history(people):
    a,_=people
    now=datetime(2026,11,20,10,0,tzinfo=utc.utc)
    event=services.create_event(a,title='История',starts_at=now+timedelta(hours=1),ends_at=now+timedelta(hours=2))
    older=event.reminders.get()
    older.status='sent';older.save()
    newer=Reminder.objects.create(event=event,recipient=a,due_at=older.due_at+timedelta(days=1),offset_minutes=15,object_version=2)
    event.version=3;event.starts_at+=timedelta(days=2);event.ends_at+=timedelta(days=2);event.save()
    services.reschedule_event(event)
    assert event.reminders.filter(status='pending').count()==1
    older.refresh_from_db();newer.refresh_from_db()
    assert older.status=='sent' and newer.object_version==3
    from assistant.recurrence import _reminder_template
    assert len(_reminder_template(event))==1
