"""Route the finite edit tools into versioned domain services."""
from assistant import editing, preferences
from assistant import recurrence_actions, reminder_actions

EDIT_OPERATIONS = {
    'update_project','update_trip','update_trip_segment','update_checklist',
    'add_checklist_item','update_checklist_item','remove_checklist_item','unlink_document',
    'update_reminders','update_recurrence','stop_recurrence','read_preferences','update_preferences',
}


def execute_edit_operation(ctx,name,args):
    common={'user':ctx.person,'shared_context':ctx.shared_context,**args}
    if name in ('read_preferences','update_preferences'):
        return getattr(preferences,name)(**common)
    if name in ('update_recurrence','stop_recurrence'):
        result=getattr(recurrence_actions,name)(**common)
        ids={str(args['record_id']),str(result['record_id'])}
        return {**result,'text':'Серия изменена.' if name=='update_recurrence' else 'Повторение остановлено.',
                'permission_refs':[{'kind':args['kind'],'id':identifier} for identifier in ids]}
    if name=='update_reminders':
        obj=reminder_actions.update_reminders(**common)
        kind=args['kind']; text='Напоминания обновлены.'
    else:
        obj=getattr(editing,name)(**common)
        kind={'update_project':'project','update_trip':'trip','update_trip_segment':'trip',
              'update_checklist':'checklist','add_checklist_item':'checklist',
              'update_checklist_item':'checklist','remove_checklist_item':'checklist',
              'unlink_document':'document'}[name]
        text={'update_project':'Проект обновлён.','update_trip':'Поездка обновлена.',
              'update_trip_segment':'Этап поездки обновлён.','update_checklist':'Список обновлён.',
              'add_checklist_item':'Пункт добавлен в список.','update_checklist_item':'Пункт обновлён.',
              'remove_checklist_item':'Пункт удалён из списка.', 'unlink_document':'Связь с документом удалена.'}[name]
    result={'text':text,'version':obj.version}
    target=obj
    refs=[]
    if name in ('add_checklist_item','update_checklist_item'):
        result.update(item_id=str(obj.pk),item_version=obj.version)
        target=obj.checklist; result['checklist_version']=target.version
    if name=='update_trip_segment':
        target=obj.trip
        result.update(segment_id=str(obj.pk),segment_version=obj.version)
        refs.append({'kind':'event','id':str(obj.event_id)})
    if name=='update_trip':
        shifted=getattr(obj,'_shifted_events',[])
        result['shifted_events']=len(shifted)
        refs.extend({'kind':'event','id':str(identifier)} for identifier in shifted)
    refs.append({'kind':kind,'id':str(target.pk)})
    # Top-level version belongs to entity_id; item/segment versions are explicit.
    result.update(entity_type=kind,entity_id=str(target.pk),version=target.version,permission_refs=refs)
    return result
