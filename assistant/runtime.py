"""Durable scheduler and side-effect worker; no LLM required for saved reminders."""
import asyncio
import json
import logging
from datetime import datetime,timedelta,time
from pathlib import Path
from zoneinfo import ZoneInfo
import httpx
from django.conf import settings
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.utils import timezone
from .models import Person,Reminder,Event,Task,Job,Delivery,IncomingUpdate,Document
from .queue import enqueue
from .services import can_view, get_entity, ENTITY_MODELS

logger=logging.getLogger(__name__)


def _tg(method, data, files=None):
    if not settings.TELEGRAM_BOT_TOKEN: raise RuntimeError('Telegram is not configured')
    with httpx.Client(timeout=30) as client:
        response=client.post(f'https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/{method}', data=data if files else None, json=None if files else data, files=files)
    if response.status_code==400 and method=='editMessageText' and 'message is not modified' in str(response.json().get('description','')):
        return {'message_id':data.get('message_id')}
    if response.status_code!=200: raise RuntimeError(f'Telegram request failed ({response.status_code})')
    result=response.json()
    if not result.get('ok'):raise RuntimeError('Telegram rejected request')
    return result.get('result',{})


def send_payload(job,payload):
    chat_id=int(payload['chat_id'])
    if payload.get('onboarding_id'):
        # Unknown users may request their own ID, never arbitrary text/files.
        user_id=int(payload['onboarding_id'])
        if user_id<=0 or chat_id!=user_id:raise PermissionError('Invalid onboarding destination')
        payload={'chat_id':chat_id,'onboarding_id':user_id,'text':f'Ваш Telegram ID: {user_id}\nID чата: {chat_id}\nДобавьте ваш ID в ALLOWED_TELEGRAM_IDS и выполните bootstrap_users на сервере.'}
    actor=None
    if payload.get('actor_id'):
        actor=Person.objects.get(pk=payload['actor_id'])
        if actor.telegram_id not in settings.ALLOWED_TELEGRAM_IDS: return
        if chat_id!=actor.chat_id and chat_id!=settings.SHARED_CHAT_ID:raise PermissionError('Invalid destination')
        if payload.get('entity_id') and payload.get('entity_type'):
            get_entity(payload['entity_type'],payload['entity_id'],actor,shared_context=chat_id==settings.SHARED_CHAT_ID)
        # Lists/AI answers may mention several objects. Recheck every source
        # before sending a queued snapshot after an access change.
        shared=chat_id==settings.SHARED_CHAT_ID
        for ref in payload.get('permission_refs',[]):
            model=ENTITY_MODELS.get(ref.get('kind'))
            readable=False
            if model:
                try:
                    obj=model.objects.get(pk=ref.get('id'))
                    readable=can_view(obj,actor,shared)
                    if not readable and ref.get('mode')=='busy' and isinstance(obj,Event) and not shared:
                        readable=obj.visibility=='busy_only'
                except (model.DoesNotExist,ValidationError,ValueError,TypeError):
                    pass
            if not readable:
                payload={'chat_id':chat_id,'text':'Доступ к данным изменился. Повторите запрос, чтобы получить актуальный ответ.'}
                break
    else:
        # Only authorised private chats / explicitly configured group can receive queued output.
        if not payload.get('onboarding_id') and not Person.objects.filter(chat_id=chat_id,telegram_id__in=settings.ALLOWED_TELEGRAM_IDS).exists() and (not settings.SHARED_CHAT_ID or chat_id!=settings.SHARED_CHAT_ID):
            raise PermissionError('Unknown destination')
    delivery,_=Delivery.objects.get_or_create(job=job,chat_id=chat_id,defaults={'recipient':actor})
    if delivery.status=='sent':return
    data={'chat_id':chat_id}
    markup=payload.get('reply_markup')
    if markup:data['reply_markup']=markup
    try:
        if payload.get('document_path'):
            if not actor or payload.get('entity_type')!='document' or not payload.get('entity_id'):raise PermissionError('Missing file authorisation')
            doc=get_entity('document',payload['entity_id'],actor,chat_id==settings.SHARED_CHAT_ID)
            path=Path(doc.file.path).resolve()
            if Path(settings.MEDIA_ROOT).resolve() not in path.parents:raise PermissionError('Invalid file path')
            data['caption']=str(payload.get('caption',''))[:1024]
            if markup:data['reply_markup']=json.dumps(markup)
            with path.open('rb') as f:
                result=_tg('sendDocument',data,files={'document':(doc.original_name,f,doc.mime_type)})
        else:
            text=str(payload.get('text','')) or 'Готово.'
            chunks=[text[i:i+3900] for i in range(0,len(text),3900)]
            result={}
            for i,chunk in enumerate(chunks):
                part={'chat_id':chat_id,'text':chunk}
                if markup and i==len(chunks)-1:part['reply_markup']=markup
                if payload.get('edit_message_id') and len(chunks)==1:
                    part['message_id']=int(payload['edit_message_id'])
                    result=_tg('editMessageText',part)
                else:
                    result=_tg('sendMessage',part)
        delivery.status='sent';delivery.message_id=result.get('message_id');delivery.sent_at=timezone.now();delivery.save()
    except Exception:
        delivery.status='unknown';delivery.save(update_fields=['status','updated_at']);raise


def _quiet_end(person,now):
    local=now.astimezone(ZoneInfo(person.timezone));current=local.time().replace(tzinfo=None)
    start=person.quiet_start;end=person.quiet_end
    if start==end:return None
    quiet=(current>=start or current<end) if start>end else start<=current<end
    if not quiet:return None
    day=local.date()+(timedelta(days=1) if start>end and current>=start else timedelta())
    return datetime.combine(day,end,tzinfo=ZoneInfo(person.timezone))


def schedule_tick(now=None):
    now=now or timezone.now()
    for reminder in Reminder.objects.filter(status='pending',due_at__lte=now).select_related('event','task','recipient')[:500]:
        obj=reminder.event if reminder.event_id else reminder.task
        if obj.version!=reminder.object_version or obj.status in ('cancelled','done','proposed'):
            reminder.status='cancelled';reminder.save(update_fields=['status']);continue
        if not can_view(obj,reminder.recipient):
            reminder.status='cancelled';reminder.save(update_fields=['status']);continue
        if now-reminder.due_at>timedelta(minutes=15):
            reminder.status='missed';reminder.save(update_fields=['status']);continue
        quiet_end=_quiet_end(reminder.recipient,now)
        due=quiet_end or now
        if reminder.event_id and due>=obj.ends_at:
            reminder.status='missed';reminder.save(update_fields=['status']);continue
        enqueue('reminder',{'reminder_id':str(reminder.id),'object_version':obj.version},f'reminder:{reminder.id}:v{obj.version}',due,queue='notifications')
        reminder.status='queued';reminder.save(update_fields=['status'])
    for p in Person.objects.filter(chat_id__isnull=False,telegram_id__in=settings.ALLOWED_TELEGRAM_IDS):
        local=now.astimezone(ZoneInfo(p.timezone));scheduled=datetime.combine(local.date(),p.morning_time,tzinfo=ZoneInfo(p.timezone))
        if scheduled<=local<scheduled+timedelta(hours=3):
            enqueue('daily_plan',{'person_id':str(p.id),'day':str(local.date())},f'daily:{p.id}:{local.date()}',queue='notifications')
        missed=list(Reminder.objects.filter(recipient=p,status='missed').values_list('id',flat=True)[:100])
        if missed and not _quiet_end(p,now):
            key='missed:'+str(p.id)+':'+str(missed[0])
            enqueue('telegram_send',{'actor_id':str(p.id),'chat_id':p.chat_id,'text':f'Пока сервис был недоступен или действовали тихие часы, пропущено напоминаний: {len(missed)}. Проверьте /today.'},key,queue='notifications')
            Reminder.objects.filter(id__in=missed).update(status='summarized')
    hour=now.strftime('%Y%m%d%H');minute=int(now.timestamp())//120
    enqueue('recurrence',{},f'recurrence:{hour}')
    enqueue('google_sync',{},f'google_sync:{minute}')


def handle_job(job):
    p=job.payload
    if job.kind=='telegram_send':return send_payload(job,p)
    if job.kind=='telegram_update':
        update=IncomingUpdate.objects.get(update_id=p['update_id'])
        if update.status=='completed':return
        from .bot import process_update
        asyncio.run(process_update(update.payload))
        IncomingUpdate.objects.filter(pk=update.pk).update(status='completed')
        return
    if job.kind=='google_event':
        from .integrations.google import sync_event
        return sync_event(p['event_id'],create_meet=p.get('create_meet',False))
    if job.kind=='google_sync':
        from .integrations.google import sync_all
        return sync_all()
    if job.kind=='recurrence':
        from .recurrence import materialize_recurrences
        return materialize_recurrences()
    if job.kind=='daily_plan':
        person=Person.objects.get(pk=p['person_id'])
        from .planning import render_daily_plan,plan_permission_refs
        from django.utils.dateparse import parse_date
        text=render_daily_plan(person,parse_date(p['day']))
        return send_payload(job,{'chat_id':person.chat_id,'actor_id':str(person.id),'text':text,'permission_refs':plan_permission_refs(person,parse_date(p['day']))})
    if job.kind=='reminder':
        reminder=Reminder.objects.select_related('event','task','recipient').get(pk=p['reminder_id'])
        obj=reminder.event if reminder.event_id else reminder.task
        if reminder.status not in ('queued','pending') or obj.version!=p['object_version'] or obj.status in ('cancelled','done','proposed') or not can_view(obj,reminder.recipient):return
        if reminder.event_id and timezone.now()>=obj.ends_at:
            reminder.status='missed';reminder.save();return
        local=(obj.starts_at if reminder.event_id else obj.due_at).astimezone(ZoneInfo(reminder.recipient.timezone))
        text=f'Напоминание: {obj.title}\n{local:%d.%m %H:%M} ({reminder.recipient.timezone})'
        buttons=[]
        if reminder.event_id:
            if obj.location:text+='\n'+obj.location
            if obj.meeting_url:text+='\n'+obj.meeting_url
            for checklist in obj.checklists.all():
                remaining=checklist.items.filter(checked_at__isnull=True)
                if remaining.exists():text+='\nЧто взять: '+', '.join(remaining.values_list('text',flat=True))
                if len(buttons)<5:buttons.append([{'text':'Что взять: '+checklist.title[:30],'callback_data':f'things:{checklist.id}:0'}])
        send_payload(job,{'chat_id':reminder.recipient.chat_id,'actor_id':str(reminder.recipient.id),'entity_type':'event' if reminder.event_id else 'task','entity_id':str(obj.id),'text':text,'reply_markup':{'inline_keyboard':buttons} if buttons else None})
        reminder.status='sent';reminder.save(update_fields=['status']);return
    raise ValueError('Unknown job kind')
