"""Explicit, permission checked application capabilities for the agent runner."""
import json
from datetime import date

from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction

from assistant import services
from assistant.integrations.llm import ACTION_TYPES,validate_action
from assistant.models import ActionRequest,DailyPlan,Event,TripSegment,ChecklistItem
from assistant.queue import enqueue


def _object(ctx,kind,identifier,edit=False):
    obj=services.get_entity(kind,identifier,ctx.person,ctx.shared_context)
    if edit and not services.can_edit(obj,ctx.person):raise PermissionDenied('Нет прав на изменение объекта.')
    return obj


def _ref(kind,obj):
    return {'kind':kind,'id':str(obj.pk)}


def _day(value):
    try:return date.fromisoformat(str(value))
    except (TypeError,ValueError):raise ValidationError('Дата должна иметь формат YYYY-MM-DD.')


def execute_operation(ctx,name,args):
    if name in ('save_place','save_contact'):
        from assistant import addressbook
        kind = 'place' if name == 'save_place' else 'contact'
        obj = getattr(addressbook,name)(ctx.person,args,shared_context=ctx.shared_context)
        result = addressbook.read_addressbook(ctx.person,kind,obj.pk,shared_context=ctx.shared_context)
        return {**result,'text':'Место сохранено.' if kind=='place' else 'Контакт сохранён.',
                'entity_type':kind,'entity_id':str(obj.pk)}

    if name == 'set_place_alias':
        from assistant.addressbook import set_place_alias
        return set_place_alias(ctx.person,shared_context=ctx.shared_context,**args)

    if name == 'resolve_route_places':
        points = [_object(ctx,'place',args[key]) for key in ('origin_place_id','destination_place_id')]
        values = []
        for place in points:
            if place.latitude is not None and place.longitude is not None:
                values.append(f'{place.latitude},{place.longitude}')
            elif place.address.strip() and place.address_confirmed:
                values.append(place.address.strip())
            else:
                raise ValidationError('Для маршрута нужны координаты или подтверждённый полный адрес обоих мест. Уточните адрес в карточке места.')
        return {'origin':values[0],'destination':values[1],
                'permission_refs':[_ref('place',obj) for obj in points]}

    if name in ACTION_TYPES and name not in ('list','answer'):
        action=validate_action({'action':name,'args_json':json.dumps(args,ensure_ascii=False,default=str),'reply':'Выполнить действие по запросу пользователя.'},shared_context=ctx.shared_context)
        if action['action']=='answer':return {'ok':False,'error':action['args']['text']}
        result=services.execute_action(ctx.person,action,ctx.shared_context)
        refs=list(result.get('permission_refs',[]))
        if result.get('entity_type') and result.get('entity_id'):
            refs.append({'kind':result['entity_type'],'id':str(result['entity_id'])})
        if result.get('request_id'):
            req=ActionRequest.objects.get(pk=result['request_id'])
            refs.append({'kind':'event','id':str(req.target_id)})
            result['status']='awaiting_spouse_approval'
        elif result.get('entity_type')=='event':
            event=Event.objects.get(pk=result['entity_id'])
            result['status']='awaiting_spouse_approval' if event.status=='proposed' else event.status
        result['permission_refs']=refs
        return result

    if name=='plan_day':
        if ctx.shared_context:raise PermissionDenied('Личный план доступен в личном чате.')
        from assistant.planning import render_daily_plan,plan_permission_refs
        day=_day(args['day'])
        text=render_daily_plan(ctx.person,day)
        plan=DailyPlan.objects.get(person=ctx.person,day=day)
        return {'text':text,'day':str(day),'version':plan.version,'status':'draft','permission_refs':plan_permission_refs(ctx.person,day)}

    if name=='accept_daily_plan':
        if ctx.shared_context:raise PermissionDenied('Личный план принимается в личном чате.')
        from assistant.planning import accept_plan,plan_permission_refs
        day=_day(args['day'])
        plan=DailyPlan.objects.select_for_update().get(person=ctx.person,day=day)
        if plan.version!=int(args['expected_version']):raise ValidationError('План изменился. Получите свежий plan_day.')
        events=accept_plan(ctx.person,day)
        return {'text':f'План принят: создано блоков {len(events)}.','events':[{'id':str(e.id),'status':e.status} for e in events],'permission_refs':plan_permission_refs(ctx.person,day)+[_ref('event',e) for e in events]}

    if name=='check_trip':
        from assistant.planning import check_trip
        trip=_object(ctx,'trip',args['trip_id'])
        checks=check_trip(trip,user=ctx.person,shared_context=ctx.shared_context)
        refs=[_ref('trip',trip)]
        for check in checks:
            refs += [{'kind':'event','id':check['from_event_id']},{'kind':'event','id':check['to_event_id']}]
        return {'checks':checks,'permission_refs':refs,'note':'Стыковки рассчитаны по заданным вручную буферам. Для актуального пути вызовите route_between.'}

    if name=='add_trip_segment':
        trip=_object(ctx,'trip',args['trip_id'],True)
        event=_object(ctx,'event',args['event_id'],True)
        if event.trip_id and event.trip_id!=trip.pk:raise ValidationError('Событие уже относится к другой поездке.')
        if hasattr(event,'segment'):raise ValidationError('У события уже есть этап поездки.')
        values={key:args.get(key,default) for key,default in [('origin',''),('destination',''),('kind','transport'),('position',0),('booking_status','planned'),('checkin_minutes',0),('baggage_minutes',0),('transfer_minutes',0)]}
        if values['kind'] not in ('transport','flight','train','car','bus','walk','hotel','activity'):raise ValidationError('Неизвестный тип участка.')
        if values['booking_status'] not in ('planned','booked','cancelled'):raise ValidationError('Неизвестный статус бронирования.')
        for key in ('position','checkin_minutes','baggage_minutes','transfer_minutes'):
            values[key]=int(values[key])
            if not 0<=values[key]<=10000:raise ValidationError('Некорректный запас времени или позиция.')
        for key in ('origin','destination'):values[key]=str(values[key])[:500]
        target_visibility = 'shared' if 'shared' in (event.visibility,trip.visibility) else event.visibility
        values.update(services.prepare_segment_places(ctx.person,args,target_visibility,ctx.shared_context))
        segment=TripSegment.objects.create(trip=trip,event=event,**values)
        if not event.trip_id:
            previous_version=event.version
            event.trip=trip;event.version+=1;event.save()
            if event.status=='proposed':
                # Linking metadata does not change the time/participants the
                # spouse is approving; keep the original confirmation usable.
                ActionRequest.objects.filter(target_type='event',target_id=event.pk,kind='confirm_event',status='proposed',target_version=previous_version).update(target_version=event.version)
            services.reschedule_event(event)
        services._audit(ctx.person,segment,'create_segment')
        return {'text':f'Этап поездки сохранён: {event.title}.','segment_id':str(segment.pk),'permission_refs':[_ref('trip',trip),_ref('event',event)]}

    if name=='link_document':
        doc=_object(ctx,'document',args['document_id'],True)
        parent=_object(ctx,args['parent_type'],args['parent_id'],True)
        services.link_document(ctx.person,doc.pk,args['parent_type'],parent.pk)
        return {'text':'Документ привязан. Видимость файла не менялась.','permission_refs':[_ref('document',doc),_ref(args['parent_type'],parent)]}

    if name=='send_document':
        doc=_object(ctx,'document',args['document_id'])
        enqueue('telegram_send',{'actor_id':str(ctx.person.id),'chat_id':ctx.chat_id,'entity_type':'document','entity_id':str(doc.pk),'document_path':doc.file.path,'caption':doc.title},f'agent-file:{ctx.run.id}:{doc.pk}',queue='notifications')
        return {'text':f'Оригинал «{doc.original_name}» поставлен в очередь отправки в этот чат.','status':'queued','permission_refs':[_ref('document',doc)]}

    if name=='set_checklist_item':
        item=ChecklistItem.objects.select_related('checklist__event','checklist__trip').get(pk=args['item_id'])
        _object(ctx,'checklist',item.checklist_id)
        item=services.set_checklist_item(ctx.person,item.pk,args['checked'])
        return {'text':'Пункт отмечен.' if item.checked_at else 'Отметка снята.','permission_refs':[_ref('checklist',item.checklist)]}

    if name=='configure_recurrence':
        from assistant.recurrence import create_rule_from_event,create_rule_from_task
        kind=args['kind']
        if kind not in ('event','task'):raise ValidationError('Повторяется событие или дело.')
        obj=_object(ctx,kind,args['record_id'],True)
        if kind=='event':
            rule=create_rule_from_event(ctx.person,obj,rrule=args.get('rrule',''),mode=args.get('mode','calendar'),cycle=args.get('cycle'))
        else:
            rule=create_rule_from_task(ctx.person,obj,mode=args.get('mode','calendar'),interval=args.get('interval',1),unit=args.get('unit','month'),rrule=args.get('rrule',''))
        enqueue('recurrence',{},f'agent-recurrence:{ctx.run.pk}:{rule.pk}')
        return {'text':'Правило повторения создано; будущие экземпляры создаст планировщик.','rule_id':str(rule.pk),'status':'queued','permission_refs':[_ref(kind,obj)]}
    raise ValidationError('Неизвестный инструмент действия.')
