"""Permission-checked application services. The bot and LLM never write arbitrary ORM fields."""
import hashlib
import io
import re
import uuid
from datetime import UTC, date, datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q, F
from django.utils import timezone
from django.utils.dateparse import parse_datetime, parse_date
from django.utils.text import get_valid_filename
from .models import (Person,Project,Trip,Task,Event,Document,Checklist,ChecklistItem,Reminder,
                     ActionRequest,AuditLog,RecurrenceRule,OccurrenceOverride,TripSegment,Place,Contact)
from .queue import enqueue
from .reminder_defaults import event_reminder_minutes, event_reminder_due
from .event_timing import normalise_event_timing, TIMING_FIELDS
from .place_links import prepare_segment_places

ENTITY_MODELS={'project':Project,'trip':Trip,'task':Task,'event':Event,'document':Document,'checklist':Checklist,
               'place':Place,'contact':Contact}


def visible(qs,user,shared_context=False,include_busy=False):
    if user.telegram_id not in settings.ALLOWED_TELEGRAM_IDS:
        return qs.none()
    # The configured family chat is a permitted destination for the sender's
    # own records. It does not grant the other sender access to those records.
    q=Q(owner=user)|Q(visibility='shared')
    if include_busy and not shared_context: q|=Q(visibility='busy_only')
    return qs.filter(q).distinct()


def can_view(obj,user,shared_context=False):
    if user.telegram_id not in settings.ALLOWED_TELEGRAM_IDS: return False
    if isinstance(obj,Checklist): obj=obj.parent
    if isinstance(obj,ChecklistItem): obj=obj.checklist.parent
    return obj.owner_id==user.id or obj.visibility=='shared'


def can_edit(obj,user):
    if isinstance(obj,Checklist): obj=obj.parent
    if isinstance(obj,ChecklistItem): obj=obj.checklist.parent
    if user.telegram_id not in settings.ALLOWED_TELEGRAM_IDS: return False
    if obj.owner_id==user.id: return True
    if obj.visibility!='shared': return False
    # Shared family events/trips/tasks belong to both spouses for editing purposes.
    # Resolve this at access time so existing records and checklists work too.
    if isinstance(obj,(Event,Trip,Task,Project,Place,Contact)): return True
    return obj.editors.filter(pk=user.pk).exists()


def get_entity(kind,identifier,user,shared_context=False):
    model=ENTITY_MODELS.get(kind)
    if model is None:raise PermissionDenied('Объект не найден или недоступен')
    try:
        obj=model.objects.get(pk=identifier)
    except (model.DoesNotExist,ValidationError,ValueError,TypeError):
        raise PermissionDenied('Объект не найден или недоступен')
    if not can_view(obj,user,shared_context):raise PermissionDenied('Объект не найден или недоступен')
    return obj


def _edit(kind,identifier,user):
    obj=get_entity(kind,identifier,user)
    if not can_edit(obj,user): raise PermissionDenied('Изменение доступно владельцу или назначенному редактору')
    return obj


def _dt(value,user,required=False,tz=None):
    if value in (None,''):
        if required: raise ValidationError('Укажите дату и время')
        return None
    dt=value if isinstance(value,datetime) else parse_datetime(str(value))
    if not dt: raise ValidationError('Ожидается дата ISO: 2026-11-20T15:00')
    if timezone.is_naive(dt):
        from dateutil.tz import datetime_exists, datetime_ambiguous
        zone=ZoneInfo(tz or user.timezone)
        dt=dt.replace(tzinfo=zone)
        if not datetime_exists(dt) or datetime_ambiguous(dt):
            raise ValidationError('Неоднозначное местное время: укажите UTC-смещение')
    return dt


def _people(ids,user):
    ids=ids or [str(user.id)]
    found=[]
    for value in ids:
        try:
            p=Person.objects.get(pk=value)
        except (Person.DoesNotExist,ValueError,ValidationError):
            raise ValidationError('Неизвестный участник')
        if p.telegram_id not in settings.ALLOWED_TELEGRAM_IDS: raise PermissionDenied('Участник не разрешён')
        found.append(p)
    return list({p.id:p for p in found}.values())


def _base(user,data,allow_busy=False):
    title=str(data.get('title','')).strip()
    if not title or len(title)>300: raise ValidationError('Название: от 1 до 300 символов')
    visibility=data.get('visibility','private')
    if visibility not in (['private','shared','busy_only'] if allow_busy else ['private','shared']):
        raise ValidationError('Некорректная видимость')
    domain=data.get('domain','personal')
    if domain not in ('work','personal'): raise ValidationError('Категория: work или personal')
    return dict(owner=user,title=title,visibility=visibility,domain=domain,description=str(data.get('description',''))[:10000])


def _linked(data,user):
    out={}
    for key,kind in [('project_id','project'),('trip_id','trip'),('task_id','task')]:
        if data.get(key):
            out[key]=get_entity(kind,data[key],user).id
    if out.get('task_id'):
        task=Task.objects.get(pk=out['task_id'])
        if out.get('project_id') and task.project_id and out['project_id']!=task.project_id:
            raise ValidationError('Проект события не совпадает с проектом дела')
        if task.project_id: out['project_id']=task.project_id
    return out


def _date_span(data):
    values={}
    for key in ('starts_on','ends_on'):
        value=data.get(key)
        try:parsed=parse_date(str(value)) if value else None
        except ValueError:parsed=None
        if value and parsed is None:raise ValidationError('Дата должна иметь формат YYYY-MM-DD')
        values[key]=parsed
    if all(values.values()) and values['ends_on']<values['starts_on']:
        raise ValidationError('Дата окончания не может быть раньше начала')
    return values


def _audit(user,obj,action,before=None):
    AuditLog.objects.create(actor=user,action=action,target_type=obj._meta.model_name,target_id=obj.id,before=before or {},after={'version':obj.version})


def _outgoing(user,text,key,reply_markup=None):
    if user.chat_id:
        enqueue('telegram_send',{'chat_id':user.chat_id,'text':text,'actor_id':str(user.id),'reply_markup':reply_markup},key,queue='notifications')


@transaction.atomic
def create_project(user,**data):
    obj=Project.objects.create(**_base(user,data),goal=str(data.get('goal',''))[:5000],**_date_span(data))
    obj.participants.set(_people(data.get('participants'),user))
    _audit(user,obj,'create'); return obj


@transaction.atomic
def create_trip(user,**data):
    obj=Trip.objects.create(**_base(user,data,True),project_id=_linked(data,user).get('project_id'),**_date_span(data),packing_notes=str(data.get('packing_notes','')))
    obj.participants.set(_people(data.get('participants'),user))
    if obj.packing_notes.strip():make_checklist(user,'trip',obj.id,obj.packing_notes)
    _audit(user,obj,'create'); return obj


@transaction.atomic
def create_task(user,**data):
    base=_base(user,data)
    assigned=_people([data['assignee_id']],user)[0] if data.get('assignee_id') else user
    people=_people(data.get('participants'),assigned)
    if any(p.id!=user.id for p in people+[assigned]) and base['visibility']!='shared':
        raise ValidationError('Для другого исполнителя/совместного дела нужна общая видимость')
    minutes=int(data.get('estimated_minutes',60))
    if not 1<=minutes<=10000: raise ValidationError('Длительность должна быть от 1 до 10000 минут')
    priority=int(data.get('priority',2))
    if priority not in range(1,6): raise ValidationError('Приоритет: 1–5')
    links=_linked(data,user); links.pop('task_id',None)
    due_at,not_before=_dt(data.get('due_at'),user),_dt(data.get('not_before'),user)
    if due_at and not_before and not_before>due_at:raise ValidationError('Начало выполнения не может быть позже срока.')
    obj=Task.objects.create(**base,**links,assignee=assigned,due_at=due_at,not_before=not_before,priority=priority,estimated_minutes=minutes,remaining_minutes=minutes,allow_split=bool(data.get('allow_split',False)),category=str(data.get('category',''))[:64])
    obj.participants.set(list({p.id:p for p in people+[assigned]}.values()))
    deps=[get_entity('task',i,user) for i in data.get('dependencies',[])]
    obj.dependencies.set(deps)
    if data.get('parent_id'): obj.parent=get_entity('task',data['parent_id'],user);obj.save()
    if obj.due_at:
        Reminder.objects.create(task=obj,recipient=assigned,due_at=obj.due_at-timedelta(minutes=60),offset_minutes=60,object_version=obj.version)
    _audit(user,obj,'create'); return obj


def _request(user,event,changes,kind='update_event'):
    other=event.participants.exclude(pk=user.pk).first()
    if not other and event.owner_id!=user.id: other=event.owner
    if not other: return None
    req=ActionRequest.objects.create(initiator=user,approver=other,kind=kind,target_type='event',target_id=event.id,target_version=event.version,changes=changes,expires_at=timezone.now()+timedelta(hours=24))
    keyboard={'inline_keyboard':[[{'text':'Подтвердить','callback_data':f'approve:{req.id}'},{'text':'Отклонить','callback_data':f'reject:{req.id}'}]]}
    _outgoing(other,f'Согласование: {event.title}\n{user.name} предлагает: {changes}',f'action:{req.id}',keyboard)
    return req


@transaction.atomic
def create_event(user,shared_context=False,**data):
    base=_base(user,data,True)
    from .place_links import event_place_fields
    place_fields=event_place_fields(user,data,base['visibility'],shared_context)
    data={**data,**place_fields}
    timing=normalise_event_timing(user,data)
    start=timing['starts_at']
    people=_people(data.get('participants'),user)
    if len(people) > 2:
        raise ValidationError('У события может быть не более двух участников.')
    if any(p.id!=user.id for p in people) and base['visibility']!='shared':
        raise ValidationError('Совместное событие должно быть общим')
    list(Person.objects.select_for_update().filter(pk__in=[p.id for p in people]).order_by('pk'))
    obj=Event.objects.create(**base,**_linked(data,user),**timing,counts_toward_load=data.get('counts_toward_load'),location=str(data.get('location',''))[:500],kind='call' if data.get('meeting') else data.get('kind','meeting'),flexible=bool(data.get('flexible',False)),buffer_before=max(0,int(data.get('buffer_before',0))),buffer_after=max(0,int(data.get('buffer_after',0))),meeting_url=data.get('meeting_url',''))
    if data.get('place_id'):
        obj.place_id=data['place_id'];obj.place_snapshot=data['place_snapshot']
        obj.save(update_fields=['place','place_snapshot'])
    offsets = data.get('remind_minutes')
    minutes = [event_reminder_minutes(obj)] if offsets is None else [int(x) for x in offsets]
    if any(not 0 <= x <= 525600 for x in minutes):
        raise ValidationError('Некорректный интервал напоминания')
    obj.participants.set(people)
    # Either attendee can book for both. A booking solely for someone else
    # still requires that person's agreement.
    if user not in people:
        obj.status = 'proposed'
        obj.save(update_fields=['status'])
        _request(user, obj, {'status': 'confirmed'}, 'confirm_event')
    for person in people:
        for offset in sorted(set(minutes)):
            Reminder.objects.create(event=obj, recipient=person, due_at=event_reminder_due(obj,offset,'custom' if offsets is not None else 'reminder'),
                                    kind='custom' if offsets is not None else 'reminder', offset_minutes=offset,
                                    object_version=obj.version, status='pending' if obj.status=='confirmed' else 'paused')
        if obj.status == 'confirmed' and person != user and person.chat_id:
            from .event_display import event_span
            enqueue('telegram_send', {
                'actor_id': str(person.pk), 'chat_id': person.chat_id,
                'text': f'В вашем общем расписании появилось событие «{obj.title}».\n'
                        f'{event_span(obj,person.timezone)}\n'
                        f'Организатор — {user.name}.\n/show event {obj.pk}',
                'permission_refs': [{'kind': 'event', 'id': str(obj.pk)}],
            }, f'joint-event:{obj.pk}:{person.pk}', queue='notifications')
    if data.get('checklist'): make_checklist(user,'event',obj.id,', '.join(data['checklist']) if isinstance(data['checklist'],list) else data['checklist'])
    if obj.status=='confirmed':
        enqueue('google_event',{'event_id':str(obj.id),'create_meet':bool(data.get('meeting'))},f'google:{obj.id}:v{obj.version}')
    _audit(user,obj,'create');return obj


def reschedule_event(event, restore_cancelled=False):
    from .reminder_actions import refresh_event_reminders
    refresh_event_reminders(event, restore_cancelled=restore_cancelled)
    enqueue('google_event',{'event_id':str(event.id),'create_meet':event.kind=='call' and not event.meeting_url},f'google:{event.id}:v{event.version}')


def _apply_event(user,event,changes):
    old_status = event.status
    from .place_links import validate_event_place
    allowed={'title','description','location','status','visibility','flexible','meeting_url','buffer_before','buffer_after','place_id','place_snapshot','participants','kind','counts_toward_load','reset_load_policy'} | TIMING_FIELDS
    if set(changes)-allowed: raise ValidationError('Недопустимые поля изменения')
    changes=dict(changes)
    if changes.pop('reset_load_policy',False):changes['counts_toward_load']=None
    participants=changes.pop('participants',None)
    before_participants=[str(p) for p in event.participants.values_list('pk',flat=True)]
    if participants is not None:
        from .editing import update_event_participants
        participants=update_event_participants(user,event,participants,changes.get('visibility',event.visibility))
    validate_event_place(user,event,changes)
    changes={**changes,**normalise_event_timing(user,changes,event)}
    before={k:(getattr(event,k).isoformat() if isinstance(getattr(event,k),date) else str(getattr(event,k)) if isinstance(getattr(event,k),uuid.UUID) else getattr(event,k)) for k in changes}
    for k,value in changes.items():
        if k in ('starts_at','ends_at'):value=_dt(value,user,True)
        if k=='visibility' and value not in ('private','shared','busy_only'):raise ValidationError('Некорректная видимость')
        if k=='status' and value not in ('proposed','confirmed','cancelled'):raise ValidationError('Некорректный статус')
        setattr(event,k,value)
    if event.ends_at<=event.starts_at:raise ValidationError('Конец должен быть позже начала')
    if event.visibility!='shared' and event.participants.exclude(pk=event.owner_id).exists():
        raise ValidationError('Сначала удалите других участников; совместная встреча должна оставаться общей')
    event.version+=1; event.full_clean();event.save()
    if participants is not None:
        before['participants']=before_participants
        changes['participants']=participants
    if event.recurrence_id and event.occurrence_key:
        OccurrenceOverride.objects.update_or_create(rule=event.recurrence,occurrence_key=event.occurrence_key,defaults={'action':'cancel' if event.status=='cancelled' else 'modify','changes':{k:v.isoformat() if isinstance(v,date) else v for k,v in changes.items()}})
    reschedule_event(event, restore_cancelled=old_status=='cancelled' and event.status=='confirmed');_audit(user,event,'update',before)
    return event


@transaction.atomic
def update_event(user,event_id,changes,expected_version=None,shared_context=False):
    event=Event.objects.select_for_update().get(pk=event_id)
    if not can_edit(event,user): raise PermissionDenied('Нет прав на изменение')
    if expected_version is not None and event.version!=expected_version:raise ValidationError('Запись уже изменилась, откройте её заново')
    if 'place_snapshot' in changes:raise ValidationError('Адрес выбирается по place_id, снимок формируется приложением.')
    from .place_links import event_place_fields,validate_event_place
    safe_changes={k:v.isoformat() if isinstance(v,date) else str(v) if isinstance(v,uuid.UUID) else v for k,v in changes.items()}
    safe_changes.update(event_place_fields(user,changes,changes.get('visibility',event.visibility),shared_context))
    validate_event_place(user,event,safe_changes)
    # Both family members may edit shared events immediately. Creation of a
    # booking solely for the other person retains its separate confirmation.
    return _apply_event(user,event,safe_changes)


@transaction.atomic
def approve_action(user,request_id,approve=True):
    req=ActionRequest.objects.select_for_update().get(pk=request_id)
    if req.approver_id!=user.id: raise PermissionDenied('Согласование предназначено другому пользователю')
    event=Event.objects.select_for_update().get(pk=req.target_id)
    if req.status!='proposed':return event
    if req.expires_at<timezone.now():req.status='expired';req.save();raise ValidationError('Срок согласования истёк')
    if event.version!=req.target_version:raise ValidationError('Событие изменилось; откройте актуальную запись и повторите нужную правку')
    if approve and not can_edit(event,req.initiator):
        raise PermissionDenied('Права автора запроса на изменение события отозваны.')
    req.status='applied' if approve else 'rejected';req.save()
    if approve:
        _apply_event(req.initiator,event,req.changes)
    elif req.kind=='confirm_event':
        _apply_event(req.initiator,event,{'status':'cancelled'})
    _outgoing(req.initiator,f'{event.title}: '+('подтверждено' if approve else 'отклонено'),f'action-result:{req.id}')
    return event


@transaction.atomic
def complete_task(user,task_id):
    list(Person.objects.select_for_update().filter(telegram_id__in=settings.ALLOWED_TELEGRAM_IDS).order_by('pk'))
    task=Task.objects.select_for_update().get(pk=task_id)
    if not can_edit(task,user):raise PermissionDenied('Нет прав на завершение дела')
    if task.status=='done':return task
    if task.status=='cancelled':raise ValidationError('Задача отменена; сначала верните её в работу, если она снова нужна.')
    if task.dependencies.exclude(status='done').exists():raise ValidationError('Сначала завершите зависимости')
    task.status='done';task.completed_at=timezone.now();task.remaining_minutes=0;task.version+=1;task.save()
    task.reminders.exclude(status='disabled').update(status='cancelled')
    from .planning_actions import cancel_task_reservations
    task._cancelled_reservations=len(cancel_task_reservations(task,user,'task_completed'))
    from .recurrence import on_task_completed
    on_task_completed(task)
    _audit(user,task,'complete');return task


@transaction.atomic
def make_checklist(user,parent_type,parent_id,text):
    if parent_type not in ('event','trip'):raise ValidationError('Чек-лист относится к событию или поездке')
    parent=_edit(parent_type,parent_id,user)
    if not text.strip():raise ValidationError('Список пуст')
    checklist=Checklist.objects.create(**{parent_type:parent},source_text=text)
    members=list(parent.participants.all()) or [parent.owner]
    for i,part in enumerate(re.split(r'[,;\n]+',text)):
        part=part.strip(' -☐☑');
        if not part:continue
        each=bool(re.search(r'\bкаждому\b',part,re.I))
        label=re.sub(r'\bкаждому\b','',part,flags=re.I).strip()
        for person in (members if each else [None]):
            ChecklistItem.objects.create(checklist=checklist,text=label[:300],assignee=person,position=i)
    return checklist


@transaction.atomic
def set_checklist_item(user,item_id,checked):
    # Lock the item only: optional event/trip relations use LEFT OUTER JOIN,
    # whose nullable side PostgreSQL cannot lock with an unrestricted FOR UPDATE.
    item=ChecklistItem.objects.select_for_update(of=('self',)).select_related('checklist__event','checklist__trip').get(pk=item_id)
    parent=item.checklist.parent
    participant=parent.participants.filter(pk=user.pk).exists()
    if not can_view(parent,user) or not (can_edit(parent,user) or item.assignee_id==user.id or (not item.assignee_id and participant)):
        raise PermissionDenied('Нет прав на этот пункт')
    if bool(item.checked_at)==bool(checked):return item
    item.checked_at=timezone.now() if checked else None;item.checked_by=user if checked else None
    item.version+=1;item.save();return item


def safe_event(event,user,shared_context=False):
    readable=can_view(event,user,shared_context)
    if not readable and (shared_context or event.visibility!='busy_only'):return {}
    base={'id':str(event.id),'starts_at':event.starts_at.isoformat(),'ends_at':event.ends_at.isoformat(),'title':event.title if readable else 'Занят','status':event.status}
    if readable:base.update(location=event.location,meeting_url=event.meeting_url,version=event.version,sync_status=event.sync_status,participants=[str(p.id) for p in event.participants.all()],all_day=event.all_day,start_date=event.start_date.isoformat() if event.start_date else None,end_date=event.end_date.isoformat() if event.end_date else None,blocks_time=event.blocks_time)
    return base


def context_for(user,shared_context=False):
    now=timezone.now()
    events=visible(Event.objects.filter(starts_at__gte=now-timedelta(days=1),starts_at__lt=now+timedelta(days=60)).exclude(status='cancelled'),user,shared_context,True).order_by('starts_at')[:100]
    tasks=visible(Task.objects.exclude(status__in=['done','cancelled']),user,shared_context).order_by('due_at')[:100]
    return {'now':now.astimezone(ZoneInfo(user.timezone)).isoformat(),'timezone':user.timezone,'user_id':str(user.id),'people':[{'id':str(p.id),'name':p.name} for p in Person.objects.filter(telegram_id__in=settings.ALLOWED_TELEGRAM_IDS)],'events':[safe_event(e,user,shared_context) for e in events],'tasks':[{'id':str(t.id),'title':t.title,'due_at':t.due_at.isoformat() if t.due_at else None,'assignee_id':str(t.assignee_id),'status':t.status,'version':t.version} for t in tasks],'projects':[{'id':str(p.id),'title':p.title} for p in visible(Project.objects.all(),user,shared_context)[:50]],'trips':[{'id':str(p.id),'title':p.title} for p in visible(Trip.objects.all(),user,shared_context)[:30]]}


@transaction.atomic
def save_document(user,filename,content,mime_type,*,telegram_caption='',**metadata):
    if len(content)>settings.MAX_UPLOAD_MB*1024*1024:raise ValidationError('Файл превышает допустимый размер')
    name=get_valid_filename(Path(filename.replace('\\','/')).name)[:200] or 'document'
    sha=hashlib.sha256(content).hexdigest()
    caption = telegram_caption if telegram_caption and telegram_caption.strip() else ''
    # Two workers can receive the same upload before either document exists.
    Person.objects.select_for_update().only('pk').get(pk=user.pk)
    for existing in Document.objects.filter(owner=user, sha256=sha).order_by('created_at', 'pk'):
        if existing.telegram_caption == caption:
            return existing
    fields = dict(metadata.get('metadata') or {})
    fields.pop('telegram_caption', None)
    if caption:
        fields['telegram_caption'] = caption
    doc=Document(owner=user,title=name,original_name=name,mime_type=mime_type[:100],size=len(content),sha256=sha,document_type=metadata.get('document_type','other'),telegram_file_id=metadata.get('telegram_file_id',''),metadata=fields)
    if mime_type=='application/pdf':
        try:
            from .integrations.pdf_text import extract_pdf
            parsed=extract_pdf(content,max_pages=30,max_chars=30000,max_page_chars=30000)
            doc.extracted_text='\n'.join(page['text'] for page in parsed['pages'])[:30000]
        except Exception:doc.extracted_text=''
    doc.file.save(f'{uuid.uuid4()}_{name}',ContentFile(content),save=False);doc.save()
    _audit(user,doc,'upload');return doc


@transaction.atomic
def link_document(user,document_id,parent_type,parent_id):
    doc=_edit('document',document_id,user);parent=_edit(parent_type,parent_id,user)
    relation={'event':'events','trip':'trips','task':'tasks'}.get(parent_type)
    if not relation:raise ValidationError('Привяжите файл к событию, задаче или поездке')
    getattr(doc,relation).add(parent);doc.version+=1;doc.save()
    return doc


@transaction.atomic
def share_entity(user,kind,identifier,visibility):
    obj=get_entity(kind,identifier,user)
    obj=type(obj).objects.select_for_update().get(pk=obj.pk)
    if obj.owner_id!=user.id:raise PermissionDenied('Видимость меняет только владелец')
    if kind in ('place','contact'):
        from .addressbook import save_place,save_contact
        return (save_place if kind=='place' else save_contact)(user,{
            'id':obj.pk,'expected_version':obj.version,'visibility':visibility})
    allowed=('private','shared','busy_only') if kind in ('event','trip') else ('private','shared')
    if visibility not in allowed:raise ValidationError('Недопустимый режим видимости')
    if kind=='event':return update_event(user,obj.id,{'visibility':visibility},obj.version)
    if visibility!='shared' and isinstance(obj,Task) and (obj.assignee_id!=user.id or obj.participants.exclude(pk=user.pk).exists()):
        raise ValidationError('Сначала измените состав исполнителей совместного дела')
    before={'visibility':obj.visibility}
    obj.visibility=visibility;obj.version+=1;obj.save()
    if isinstance(obj,Task):
        obj.reminders.filter(status__in=['pending','queued']).update(object_version=obj.version,status='pending')
    if visibility!='shared':obj.editors.clear()
    _audit(user,obj,'share',before);return obj


@transaction.atomic
def undo_task_completion(user,task_id):
    list(Person.objects.select_for_update().filter(telegram_id__in=settings.ALLOWED_TELEGRAM_IDS).order_by('pk'))
    task=Task.objects.select_for_update().get(pk=task_id)
    if not can_edit(task,user):raise PermissionDenied('Нет прав на изменение')
    if task.status!='done':return task
    from .task_actions import update_task
    return update_task(user,task.pk,{'status':'todo'},task.version)


def execute_action(user,action,shared_context=False):
    name=action.get('action');args=dict(action.get('args') or {})
    if shared_context and name.startswith('create_'):
        args.setdefault('visibility','shared')
    if shared_context:
        for key,kind in [('project_id','project'),('trip_id','trip'),('task_id','task'),('event_id','event'),('document_id','document'),('place_id','place')]:
            if args.get(key):get_entity(kind,args[key],user,True)
    creates={'create_project':('project',create_project),'create_trip':('trip',create_trip),'create_task':('task',create_task),'create_event':('event',create_event)}
    if name in creates:
        kind,fn=creates[name];obj=fn(user,**args,**({'shared_context':shared_context} if kind=='event' else {}))
        return {'text':f'Сохранено: {obj.title}\nID: {obj.id}'+ ('\nОжидается согласование второго участника.' if getattr(obj,'status','')=='proposed' else ''),'entity_type':kind,'entity_id':str(obj.id)}
    if name=='update_event':
        obj=update_event(user,args['event_id'],args.get('changes',{}),args.get('expected_version'),shared_context)
        if isinstance(obj,ActionRequest):return {'text':'Отправлено на согласование. До подтверждения событие не меняется.','request_id':str(obj.id)}
        return {'text':f'Событие обновлено: {obj.title}','entity_type':'event','entity_id':str(obj.id)}
    if name=='update_task':
        from .task_actions import update_task
        obj=update_task(user,args['task_id'],args['changes'],args['expected_version'],shared_context)
        return {'text':f'Дело {"отменено" if obj.status=="cancelled" else "обновлено"}: {obj.title}',
                'entity_type':'task','entity_id':str(obj.pk),'status':obj.status,'version':obj.version,
                'cancelled_reservations':getattr(obj,'_cancelled_reservations',0)}
    if name=='convert_task_to_event':
        from .task_actions import convert_task_to_event
        obj=convert_task_to_event(user,args['task_id'],args['expected_version'],args['event'],shared_context)
        return {'text':f'Создано событие «{obj.title}». Исходная ошибочная задача отменена.',
                'entity_type':'event','entity_id':str(obj.pk),
                'permission_refs':[{'kind':'task','id':str(args['task_id'])}]}
    if name=='complete_task':
        obj=complete_task(user,args['task_id']);return {'text':f'Готово: {obj.title}','entity_type':'task','entity_id':str(obj.id),'cancelled_reservations':getattr(obj,'_cancelled_reservations',0)}
    if name=='make_checklist':
        if shared_context:get_entity(args['parent_type'],args['parent_id'],user,True)
        obj=make_checklist(user,args['parent_type'],args['parent_id'],args['text']);return {'text':'Чек-лист сохранён','entity_type':'checklist','entity_id':str(obj.id)}
    if name=='list':
        kind=args.get('kind','event');model=ENTITY_MODELS.get(kind)
        if kind=='checklist' or model is None:raise ValidationError('Неизвестный раздел')
        qs=visible(model.objects.all(),user,shared_context,kind=='event')
        if kind=='event':
            day=parse_date(args.get('day','')) if args.get('day') else timezone.now().astimezone(ZoneInfo(user.timezone)).date()
            if not day:raise ValidationError('Некорректная дата')
            begin=datetime.combine(day,time.min,tzinfo=ZoneInfo(user.timezone));end=datetime.combine(day+timedelta(days=1),time.min,tzinfo=ZoneInfo(user.timezone))
            from .event_queries import event_overlap
            qs=qs.filter(event_overlap(begin,end,user.timezone)).exclude(status='cancelled').order_by('starts_at')
            objects=list(qs[:50])
            from .event_display import event_span
            lines=[f"{event_span(e,user.timezone)} — {safe_event(e,user,shared_context)['title']}\n{e.id}" for e in objects]
        else:
            objects=list(qs.order_by('-created_at')[:50])
            lines=[f'{o.title}\n{o.id}' for o in objects]
        refs=[{'kind':kind,'id':str(o.id),**({'mode':'busy'} if kind=='event' and not can_view(o,user,shared_context) else {})} for o in objects]
        return {'text':'\n\n'.join(lines) or 'Пока ничего нет.','permission_refs':refs}
    if name=='answer':return {'text':str(args.get('text') or action.get('reply') or 'Уточните запрос.')[:10000]}
    raise ValidationError('Действие не поддерживается; используйте /help')
