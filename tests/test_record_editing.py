from datetime import date, timedelta
import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from assistant import editing, services
from assistant.models import Event, Reminder, Task, TripSegment
from assistant.task_actions import update_task

pytestmark=pytest.mark.django_db


def event(owner, **kwargs):
    start=timezone.now()+timedelta(days=10)
    return services.create_event(owner,title='Встреча',starts_at=start,ends_at=start+timedelta(hours=1),**kwargs)


def test_project_lifecycle_and_stale_write(people):
    owner,spouse=people
    obj=services.create_project(owner,title='Проект',visibility='shared',participants=[p.pk for p in people])
    updated=editing.update_project(owner,obj.pk,{'title':'Готовый проект','status':'completed'},obj.version)
    assert updated.status=='completed' and updated.title=='Готовый проект'
    with pytest.raises(ValidationError):editing.update_project(owner,obj.pk,{'status':'archived'},1)
    with pytest.raises(ValidationError):editing.update_project(owner,obj.pk,{'starts_on':'2030-12-05','ends_on':'2030-12-04'},updated.version)
    updated.refresh_from_db();assert updated.starts_on is None


def test_trip_shift_is_explicit_and_moves_all_day_and_timed_events(people):
    owner,spouse=people
    trip=services.create_trip(owner,title='Поездка',starts_on='2030-12-01',ends_on='2030-12-03',visibility='shared')
    timed=services.create_event(owner,title='Поезд',starts_at='2030-12-01T12:00:00+03:00',ends_at='2030-12-01T14:00:00+03:00',trip_id=trip.pk,visibility='shared')
    day=services.create_event(owner,title='Отель',all_day=True,start_date='2030-12-01',end_date='2030-12-03',trip_id=trip.pk,visibility='shared')
    updated=editing.update_trip(spouse,trip.pk,{'starts_on':'2030-12-08'},trip.version,shift_events=True)
    timed.refresh_from_db();day.refresh_from_db()
    assert updated.ends_on==date(2030,12,10) and len(updated._shifted_events)==2
    assert timed.starts_at.date()==date(2030,12,8)
    assert day.start_date==date(2030,12,8) and day.end_date==date(2030,12,10)
    assert not updated._shifted_events==[]


def test_trip_shift_rolls_back_if_other_spouses_private_event(people):
    owner,spouse=people
    trip=services.create_trip(owner,title='Поездка',visibility='shared',starts_on='2030-12-01')
    own=event(owner,trip_id=trip.pk)
    hidden=event(spouse,trip_id=trip.pk)
    with pytest.raises(PermissionDenied):
        editing.update_trip(owner,trip.pk,{'starts_on':'2030-12-02'},trip.version,shift_events=True)
    trip.refresh_from_db();own.refresh_from_db()
    assert trip.starts_on==date(2030,12,1) and own.version==1


def test_event_participants_can_change_together_with_visibility(people):
    owner,spouse=people
    obj=event(owner,visibility='shared',participants=[p.pk for p in people])
    result=services.update_event(spouse,obj.pk,{'participants':[str(owner.pk)],'visibility':'private'},obj.version)
    assert result.visibility=='private' and set(result.participants.all())=={owner}
    with pytest.raises(PermissionDenied):services.update_event(spouse,obj.pk,{'participants':[str(spouse.pk)]},result.version)


def test_checklist_composition_versions_assignment_and_acl(people):
    owner,spouse=people
    obj=event(owner,visibility='shared',participants=[p.pk for p in people])
    checklist=services.make_checklist(owner,'event',obj.pk,'Плавки')
    old=checklist.items.get()
    item=editing.add_checklist_item(spouse,checklist.pk,{'text':'Камера','quantity':'1','assignee_id':str(spouse.pk)},checklist.version)
    checklist.refresh_from_db();assert checklist.version==2 and item.assignee==spouse
    item=editing.update_checklist_item(owner,item.pk,{'quantity':'2','checked':True},item.version)
    assert item.quantity=='2' and item.checked_at and item.checked_by==owner
    with pytest.raises(ValidationError):editing.update_checklist_item(owner,item.pk,{'text':'stale'},1)
    updated=editing.remove_checklist_item(spouse,old.pk,old.version)
    assert updated.items.count()==1 and updated.version==4
    private=event(owner)
    private_list=services.make_checklist(owner,'event',private.pk,'Секрет')
    with pytest.raises(PermissionDenied):editing.add_checklist_item(spouse,private_list.pk,{'text':'Чужое'},private_list.version)


def test_unlink_document_keeps_bytes_and_other_links(people,tmp_path,settings):
    owner,spouse=people;settings.MEDIA_ROOT=tmp_path
    a,b=event(owner),event(owner)
    doc=services.save_document(owner,'ticket.txt',b'ticket','text/plain')
    services.link_document(owner,doc.pk,'event',a.pk)
    doc=services.link_document(owner,doc.pk,'event',b.pk)
    doc=editing.unlink_document(owner,doc.pk,'event',a.pk,doc.version)
    assert list(doc.events.all())==[b] and doc.file.size==6
    with pytest.raises(PermissionDenied):editing.unlink_document(spouse,doc.pk,'event',b.pk,doc.version)


def test_trip_segment_update_booking_and_buffers(people):
    owner,spouse=people
    trip=services.create_trip(owner,title='Маршрут',visibility='shared')
    obj=event(owner,trip_id=trip.pk,visibility='shared')
    segment=TripSegment.objects.create(trip=trip,event=obj,origin='Москва',destination='Токио')
    updated=editing.update_trip_segment(spouse,segment.pk,{'booking_status':'booked','booking_reference':'PNR123','checkin_minutes':120},segment.version)
    assert updated.booking_status=='booked' and updated.checkin_minutes==120
    with pytest.raises(ValidationError):editing.update_trip_segment(owner,segment.pk,{'checkin_minutes':-1},updated.version)


def test_task_reassignment_moves_reminder_and_invalidates_reserved_time(people):
    owner,spouse=people
    task=services.create_task(owner,title='Купить',visibility='shared',due_at=timezone.now()+timedelta(days=20))
    block=event(owner,task_id=task.pk,kind='task',flexible=True,visibility='shared')
    updated=update_task(spouse,task.pk,{'assignee_id':str(spouse.pk)},task.version)
    block.refresh_from_db()
    assert updated.assignee==spouse and set(updated.participants.all())=={spouse}
    assert block.status=='cancelled' and updated._cancelled_reservations==1
    assert set(updated.reminders.filter(status='pending').values_list('recipient_id',flat=True))=={spouse.pk}


def test_task_links_and_cycles_and_not_before(people):
    owner,spouse=people
    a=services.create_task(owner,title='A',visibility='shared')
    b=services.create_task(owner,title='B',visibility='shared')
    project=services.create_project(owner,title='Проект',visibility='shared')
    a=update_task(spouse,a.pk,{'project_id':str(project.pk),'dependencies':[str(b.pk)],'not_before':'2030-12-01T12:00:00+03:00','due_at':'2030-12-02T12:00:00+03:00'},a.version)
    assert a.project==project and a.dependencies.get()==b and a.not_before.day==1
    with pytest.raises(ValidationError,match='цикл'):update_task(owner,b.pk,{'dependencies':[str(a.pk)]},b.version)
    with pytest.raises(ValidationError):update_task(owner,a.pk,{'due_at':'2030-11-30T12:00:00+03:00'},a.version)
    a=update_task(owner,a.pk,{'clear_not_before':True,'clear_project':True,'dependencies':[]},a.version)
    assert a.not_before is None and a.project_id is None and not a.dependencies.exists()
    a=update_task(owner,a.pk,{'parent_id':str(b.pk)},a.version)
    with pytest.raises(ValidationError):update_task(owner,b.pk,{'parent_id':str(a.pk)},b.version)


def test_reopen_completed_task_is_available_to_spouse(people):
    owner,spouse=people
    task=services.create_task(owner,title='Готово',visibility='shared')
    task=services.complete_task(owner,task.pk)
    task=update_task(spouse,task.pk,{'status':'todo'},task.version)
    assert task.status=='todo' and task.completed_at is None and task.remaining_minutes==task.estimated_minutes


def test_disabled_task_reminder_stays_disabled_on_deadline_change(people):
    owner,_=people
    task=services.create_task(owner,title='Не напоминай',due_at=timezone.now()+timedelta(days=5))
    task.reminders.update(status='disabled')
    update_task(owner,task.pk,{'due_at':timezone.now()+timedelta(days=6)},task.version)
    assert not task.reminders.filter(status='pending').exists()


def test_recurring_task_can_be_undone_redone_and_undone_again(people):
    from assistant.recurrence import create_rule_from_task
    owner,spouse=people
    task=services.create_task(owner,title='Повторяющееся дело',visibility='shared',
        due_at=timezone.now()+timedelta(days=2))
    create_rule_from_task(owner,task,mode='after_completion',interval=1,unit='week')
    task=services.complete_task(owner,task.pk)
    successor=Task.objects.get(previous_task=task)
    for _ in range(3):
        reopened=services.undo_task_completion(spouse,task.pk)
        successor.refresh_from_db()
        assert reopened.status=='todo' and successor.status=='cancelled'
        task=services.complete_task(owner,task.pk)
        successor.refresh_from_db()
        assert successor.status=='todo' and Task.objects.filter(previous_task=task).count()==1


def test_recurring_task_undo_preserves_real_successor_edits_after_redo(people):
    from assistant.recurrence import create_rule_from_task
    owner,spouse=people
    task=services.create_task(owner,title='Повторяющееся дело',visibility='shared',
        due_at=timezone.now()+timedelta(days=2))
    create_rule_from_task(owner,task,mode='after_completion',interval=1,unit='week')
    task=services.complete_task(owner,task.pk)
    services.undo_task_completion(spouse,task.pk)
    task=services.complete_task(owner,task.pk)
    successor=Task.objects.get(previous_task=task)
    successor=update_task(spouse,successor.pk,{'description':'Важное изменение следующего раза'},successor.version)
    with pytest.raises(ValidationError,match='Следующая задача уже изменена'):
        services.undo_task_completion(owner,task.pk)
    task.refresh_from_db();successor.refresh_from_db()
    assert task.status=='done' and successor.status=='todo'
    assert successor.description=='Важное изменение следующего раза'
