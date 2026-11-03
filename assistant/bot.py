"""Telegram transport. Database mutations and the outgoing queue commit together.

Long polling only acknowledges an update after enqueueing it. The same durable
processing path is used by webhook ingestion. No Telegram API calls occur in
unit tests unless explicitly supplied with a fake Bot.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import uuid
from datetime import date, datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (
    ActionRequest, Checklist, ChecklistItem, Conversation, DailyPlan, Document, Event,
    IncomingUpdate, Person, Project, Task, Trip, TripSegment,
)
from .queue import enqueue
from . import services

log = logging.getLogger(__name__)

HELP = """Семейный ассистент · команды

Сначала: /id — ваш Telegram ID; /start — подключить личные сообщения.
Списки: /tasks /events /projects /trips /files
/project Название | private или shared
/task Название | 2026-11-10 18:00 или - | минуты | private/shared
/event 2026-11-10 15:00 | минуты | Название | Место | private/shared/busy_only
/trip Название | 2026-11-15 | 2026-11-25 | private/shared

/show task|event|project|trip ID — карточка
/done ID_задачи
/undo_task ID_задачи — отменить выполнение
/move ID_события | 2026-11-10 16:00 | минуты
/cancel ID_события
/checklist event|trip ID | паспорт; билеты; фотоаппарат
/things ID_чеклиста
/link ID_файла | event|trip|task | ID
/get ID_файла — оригинал
/checktrip ID_поездки — проверить стыковки
/segment — добавить участок поездки
/share document|event|trip|task|project ID shared|private — видимость

/today или /plan 2026-11-10 — план дня, затем кнопка принятия
/accept 2026-11-10 — принять текущий черновик дня
/settings — настройки и формат изменений
/agent — режим и доступные возможности ИИ
/agent autonomy personal или read_only — автономность в личном чате
/agent_history — последние запуски агента
/google — подключение Google (в личном чате)
/calendars — доступные календари
/calendar ID_календаря | personal/shared | rw/ro
/sync — синхронизация
/resolve ID_события local|remote — разрешить конфликт Google

Пришлите PDF/фото — сохраню оригинал. Необязательная подпись:
/attach event|trip|task ID
/repeat — примеры повторений.
Можно написать обычным текстом при настроенном LLM API: агент сам выбирает необходимые поиски и действия. Режим выполнения — /agent. Совместные события проходят согласование второго участника. Даты команд — в вашем часовом поясе; «shared» означает видимость обоим. Для участия двоих добавьте к /event шестое поле: | 1001,1002 (ваши Telegram ID); к /trip — пятое поле. /call имеет формат /event и создаёт Meet при подключённом Google.
"""

SETTINGS_HELP = """Настройки меняются в личном чате:
/settings timezone Europe/Moscow
/settings morning 08:00
/settings quiet 23:00 07:00
/settings work_start 09:00
/settings work_end 18:00
/settings work_days 0,1,2,3,4 (понедельник=0)
/settings sleep_start 23:00
/settings sleep_end 07:00
/settings max_daily_minutes 360
/settings min_block_minutes 30
/settings break_minutes 15
/settings transport_mode walking
/settings home Адрес дома
/settings work Адрес работы
"""

WRITE_ACTIONS = {
    'create_project', 'create_trip', 'create_task', 'create_event', 'update_event',
    'complete_task', 'make_checklist',
}
MODEL_BY_KIND = {'project': Project, 'task': Task, 'event': Event, 'trip': Trip, 'document': Document, 'checklist': Checklist}
MAX_UPLOAD = 15 * 1024 * 1024


def _group_id():
    return int(getattr(settings, 'TELEGRAM_GROUP_ID', 0) or getattr(settings, 'SHARED_CHAT_ID', 0) or 0)


def _allowed_ids():
    value = getattr(settings, 'ALLOWED_TELEGRAM_IDS', ())
    if isinstance(value, str):
        value = [part.strip() for part in value.split(',') if part.strip()]
    return {int(x) for x in value}


def _parts(text):
    return [p.strip() for p in text.split('|')]


def _command(text):
    if not text.startswith('/'):
        return '', text
    name, _, args = text.partition(' ')
    return name.split('@', 1)[0].lower(), args.strip()


def _time(text):
    if not re.fullmatch(r'[0-2][0-9]:[0-5][0-9]', text):
        raise ValueError('Время укажите в формате ЧЧ:ММ.')
    return time.fromisoformat(text)


def _datetime(text, person, timezone_name=None):
    value = datetime.fromisoformat(text.strip())
    if value.tzinfo is None:
        from dateutil.tz import datetime_ambiguous
        zone = ZoneInfo(timezone_name or person.timezone)
        value = value.replace(tzinfo=zone)
        if datetime_ambiguous(value):
            raise ValueError('Это время повторяется при переходе часов. Укажите UTC-смещение, например +03:00.')
        # Reject nonexistent clock times rather than silently shifting them.
        if value.astimezone(ZoneInfo('UTC')).astimezone(zone).replace(tzinfo=None) != value.replace(tzinfo=None):
            raise ValueError('Это местное время не существует из-за перехода часов.')
    return value


def _visibility(value, shared):
    result = value or ('shared' if shared else 'private')
    if result not in {'private', 'shared', 'busy_only'}:
        raise ValueError('Видимость: private, shared или busy_only.')
    if shared and result != 'shared':
        raise PermissionDenied('Личные записи создавайте в личном чате.')
    return result


def _participant_ids(raw):
    if not raw or raw=='-':
        return None
    ids={int(value.strip()) for value in raw.split(',')}
    if not ids or not ids.issubset(_allowed_ids()):
        raise PermissionDenied('Участники — только Telegram ID из разрешённого списка.')
    people=list(Person.objects.filter(telegram_id__in=ids))
    if len(people)!=len(ids):
        raise ValueError('Сначала выполните bootstrap_users для обоих участников.')
    return [str(person.pk) for person in people]


def _link_fields(parts,indices,person,shared):
    result={}
    for index,kind in indices:
        if len(parts)>index and parts[index] not in {'','-'}:
            result[kind+'_id']=str(services.get_entity(kind,parts[index],person,shared_context=shared).pk)
    return result


def _markup(rows):
    return {'inline_keyboard': [[{'text': text, 'callback_data': callback} for text, callback in row] for row in rows]}


def _result(text, **kwargs):
    return {'text': text, **kwargs}


def _entity_meta(kind, obj):
    return {'entity_type': kind, 'entity_id': str(obj.pk)}


def _visible_items(kind, person, shared):
    model = MODEL_BY_KIND[kind]
    qs = model.objects.all()
    qs = services.visible(qs, person, shared_context=shared, include_busy=kind == 'event')
    if kind == 'event':
        qs = qs.filter(ends_at__gte=timezone.now() - timedelta(days=1)).exclude(status='cancelled').order_by('starts_at')
    elif kind == 'task':
        qs = qs.exclude(status__in=['done', 'cancelled']).order_by('due_at', '-created_at')
    else:
        qs = qs.order_by('-created_at')
    return list(qs[:25])


def _listing(kind, person, shared):
    labels = {'task': 'Дела', 'event': 'События', 'project': 'Проекты', 'trip': 'Поездки', 'document': 'Файлы'}
    lines = [labels[kind]]
    buttons = []
    refs = []
    for obj in _visible_items(kind, person, shared):
        refs.append({'kind':kind,'id':str(obj.pk),**({'mode':'busy'} if kind=='event' and obj.visibility=='busy_only' and obj.owner_id!=person.pk else {})})
        if kind == 'event':
            stamp = obj.starts_at.astimezone(ZoneInfo(person.timezone)).strftime('%d.%m %H:%M')
            if obj.owner_id != person.pk and obj.visibility == 'busy_only':
                lines.append(f'{stamp} — Занят')
                continue
            lines.append(f'{stamp} — {obj.title}\nID: {obj.pk}')
        else:
            lines.append(f'{obj.title}\nID: {obj.pk}')
        if len(buttons) < 10:
            cb = f'get:{obj.pk}' if kind == 'document' else f'show:{kind}:{obj.pk}'
            buttons.append([(obj.title[:45], cb)])
    if len(lines) == 1:
        lines.append('Пока пусто.')
    return _result('\n\n'.join(lines), reply_markup=_markup(buttons),permission_refs=refs)


def _checklist_reply(checklist, person, shared=False, page=0):
    parent = checklist.parent
    if not services.can_view(parent, person, shared_context=shared):
        raise PermissionDenied('Нет доступа к списку.')
    items = list(checklist.items.select_related('assignee').all())
    page = max(0, min(int(page), max(0, (len(items)-1)//10)))
    chosen = items[page*10:page*10+10]
    done = sum(bool(item.checked_at) for item in items)
    lines = [f'{parent.title} · {checklist.title} · собрано {done} из {len(items)}']
    rows = []
    for item in chosen:
        mark = '☑' if item.checked_at else '☐'
        who = f' — {item.assignee.name}' if item.assignee_id else ''
        qty = f' ({item.quantity})' if item.quantity else ''
        lines.append(f'{mark} {item.text}{qty}{who}')
        rows.append([(f'{mark} {item.text}'[:45], f'item:{item.pk}:{0 if item.checked_at else 1}')])
    nav = []
    if page > 0:
        nav.append(('←', f'things:{checklist.pk}:{page-1}'))
    if (page+1)*10 < len(items):
        nav.append(('→', f'things:{checklist.pk}:{page+1}'))
    if nav:
        rows.append(nav)
    return _result('\n'.join(lines), reply_markup=_markup(rows), **_entity_meta('checklist', checklist))


def _card(kind, identifier, person, shared=False):
    obj = services.get_entity(kind, identifier, person, shared_context=shared)
    if kind == 'checklist':
        return _checklist_reply(obj, person, shared)
    rows = []
    refs = [{'kind':kind,'id':str(obj.pk)}]
    lines = [obj.title, f'ID: {obj.pk}']
    if obj.description:
        lines.append(obj.description)
    if kind == 'event':
        z = ZoneInfo(person.timezone)
        lines.append(f'{obj.starts_at.astimezone(z):%d.%m.%Y %H:%M} — {obj.ends_at.astimezone(z):%d.%m.%Y %H:%M}')
        if obj.location:
            lines.append(obj.location)
        if obj.meeting_url:
            lines.append(obj.meeting_url)
        lines.append(f'Статус: {obj.status}; календарь: {obj.sync_status}')
        for checklist in obj.checklists.all()[:3]:
            rows.append([(checklist.title, f'things:{checklist.pk}:0')])
    if kind == 'trip':
        lines.append(f'{obj.starts_on or "?"} — {obj.ends_on or "?"}')
        for segment in obj.segments.select_related('event').order_by('event__starts_at')[:20]:
            if services.can_view(segment.event, person, shared_context=shared):
                event = segment.event
                refs.append({'kind':'event','id':str(event.pk)})
                lines.append(f'{event.starts_at.astimezone(ZoneInfo(event.timezone)):%d.%m %H:%M %Z} — {event.title}: {segment.origin} → {segment.destination} ({segment.booking_status})')
                rows.append([(event.title[:40], f'show:event:{event.pk}')])
        for checklist in obj.checklists.all()[:3]:
            rows.append([(checklist.title, f'things:{checklist.pk}:0')])
    if kind == 'document':
        lines.append(f'Оригинал: {obj.original_name}; размер: {obj.size} байт')
        if obj.metadata:
            review={key:value for key,value in obj.metadata.items() if key not in {'extracted_text'} and value not in (None,'',[])}
            lines.append('Распознанные сведения требуют проверки:\n'+json.dumps(review,ensure_ascii=False,indent=2)[:2500])
        rows.append([('Скачать оригинал',f'get:{obj.pk}')])
    if kind == 'task':
        lines.append(f'Статус: {obj.status}; срок: {obj.due_at or "не задан"}')
        rows.append([('Готово', f'done:{obj.pk}')])
    if kind in {'task', 'trip', 'event'}:
        for document in services.visible(obj.documents.all(), person, shared_context=shared)[:5]:
            refs.append({'kind':'document','id':str(document.pk)})
            rows.append([('Билет: '+document.title[:35], f'get:{document.pk}')])
    return _result('\n'.join(lines), reply_markup=_markup(rows),permission_refs=refs, **_entity_meta(kind, obj))


def _settings(person, args, shared):
    if shared:
        raise PermissionDenied('Настройки доступны в личном чате.')
    if not args:
        return _result(f'Часовой пояс: {person.timezone}\nУтренний план: {person.morning_time}\nТихие часы: {person.quiet_start}–{person.quiet_end}\n\n'+SETTINGS_HELP)
    key, _, value = args.partition(' ')
    value = value.strip()
    if key == 'timezone':
        ZoneInfo(value)
        person.timezone = value
    elif key == 'morning':
        person.morning_time = _time(value)
    elif key == 'quiet':
        pieces = value.split()
        if len(pieces) != 2:
            raise ValueError('/settings quiet 23:00 07:00')
        person.quiet_start, person.quiet_end = map(_time, pieces)
    else:
        profile = dict(person.profile)
        if key in {'work_start','work_end','sleep_start','sleep_end'}:
            profile[key] = _time(value).strftime('%H:%M')
        elif key in {'max_daily_minutes','min_block_minutes','break_minutes'}:
            number = int(value)
            if not (0 <= number <= 1440):
                raise ValueError('Число минут должно быть от 0 до 1440.')
            if key == 'min_block_minutes' and number < 5:
                raise ValueError('Минимальный блок — 5 минут.')
            profile[key] = number
        elif key == 'work_days':
            days = sorted(set(int(x) for x in value.split(',')))
            if not days or any(x < 0 or x > 6 for x in days):
                raise ValueError('Дни недели: числа от 0 до 6 через запятую.')
            profile[key] = days
        elif key == 'transport_mode':
            if value not in {'walking','driving','transit','bicycling'}:
                raise ValueError('Транспорт: walking, driving, transit, bicycling.')
            profile[key] = value
        elif key in {'home','work'}:
            locations = dict(profile.get('locations', {}))
            locations[key] = value[:500]
            profile['locations'] = locations
        else:
            raise ValueError(SETTINGS_HELP)
        person.profile = profile
    person.full_clean()
    person.save()
    return _result('Настройки сохранены.')


def _record_pending(person, chat_id, action):
    conversation, _ = Conversation.objects.get_or_create(person=person, chat_id=chat_id)
    token = uuid.uuid4().hex[:12]
    conversation.pending = {'token': token, 'action': action}
    conversation.expires_at = timezone.now() + timedelta(minutes=30)
    conversation.save(update_fields=['pending','expires_at','updated_at'])
    args = action.get('args', {})
    preview = json.dumps(args, ensure_ascii=False, indent=2, default=str)
    # Show exactly the proposed operation, not an unverified model claim of success.
    text = f'Предложение: {action["action"]}\n{preview}\n\nПрименить?'
    return _result(text,permission_refs=action.get('permission_refs',[]), reply_markup=_markup([[('Применить', f'apply:{conversation.pk}:{token}'), ('Отмена', f'discard:{conversation.pk}:{token}')]]))


def _action_result(result, person, shared):
    if not isinstance(result, dict):
        return _result(str(result))
    reply = _result(result.get('text') or result.get('reply') or 'Готово.',permission_refs=result.get('permission_refs',[]))
    kind = result.get('entity_type')
    ident = result.get('entity_id')
    if kind and ident:
        reply.update(entity_type=kind, entity_id=str(ident))
        if kind in MODEL_BY_KIND:
            reply['reply_markup'] = _markup([[('Открыть', f'show:{kind}:{ident}')]])
    if result.get('request_id'):
        reply['text'] += '\nИзменение отправлено на согласование.'
    return reply


def _repeat(person, args, shared):
    from .recurrence import create_rule_from_event, create_rule_from_task
    parts = _parts(args)
    example = '/repeat task ID | after 1 month\n/repeat event ID | FREQ=WEEKLY;BYDAY=MO\n/repeat event ID | cycle 1,1,0,0\n/repeat task ID | FREQ=MONTHLY;BYMONTHDAY=1\nСовместные серии пока не поддерживаются.'
    if len(parts) != 2 or len(parts[0].split()) != 2:
        return _result(example)
    kind, identifier = parts[0].split()
    if kind not in {'task','event'}:
        raise ValueError(example)
    obj = services.get_entity(kind, identifier, person, shared_context=shared)
    if kind == 'task':
        match = re.fullmatch(r'after\s+(\d+)\s+(day|week|month)', parts[1])
        if match:
            create_rule_from_task(person,obj,interval=int(match[1]),unit=match[2])
        else:
            create_rule_from_task(person,obj,mode='calendar',rrule=parts[1])
    else:
        if parts[1].startswith('cycle '):
            days = parts[1][6:].split(',')
            if any(x.strip() not in {'0','1'} for x in days):
                raise ValueError('Цикл: 1,1,0,0 — два рабочих дня, два выходных.')
            create_rule_from_event(person,obj,mode='cycle',cycle=[x.strip()=='1' for x in days])
        else:
            create_rule_from_event(person,obj,rrule=parts[1])
    return _result('Повторение сохранено. Ближайшие повторения появятся после прохода планировщика.', **_entity_meta(kind,obj))


def _segment(person,args,shared):
    example='/segment ID_поездки | 2026-11-15T10:00+03:00 | 2026-11-15T13:00+04:00 | transport/stay/activity | Откуда | Куда | Название | регистрация,багаж,пересадка (минуты) | Europe/Moscow,Asia/Dubai (необязательно)'
    parts=_parts(args)
    if len(parts)<7:
        return _result(example)
    trip=services.get_entity('trip',parts[0],person,shared_context=shared)
    if not services.can_edit(trip,person):
        raise PermissionDenied('Добавлять участки может владелец или редактор поездки.')
    if parts[3] not in {'transport','stay','activity'}:
        raise ValueError('Тип участка: transport, stay или activity.')
    zones=[v.strip() for v in parts[8].split(',')] if len(parts)>8 and parts[8] else [person.timezone,person.timezone]
    if len(zones)!=2:
        raise ValueError('Часовые пояса отправления и прибытия: Europe/Moscow,Asia/Dubai')
    for name in zones:
        ZoneInfo(name)
    start,end=_datetime(parts[1],person,zones[0]),_datetime(parts[2],person,zones[1])
    buffers=[int(v.strip()) for v in parts[7].split(',')] if len(parts)>7 and parts[7] else [0,0,0]
    if len(buffers)!=3 or any(v<0 or v>1440 for v in buffers):
        raise ValueError('Буферы: три значения 0–1440 минут через запятую.')
    people=list(trip.participants.values_list('id',flat=True)) or [person.pk]
    event=services.create_event(person,title=parts[6],starts_at=start,ends_at=end,trip_id=str(trip.pk),
                                participants=[str(x) for x in people],visibility=trip.visibility,domain=trip.domain,
                                location=parts[4]+' → '+parts[5],kind=parts[3],timezone=zones[0],end_timezone=zones[1],
                                buffer_before=buffers[0],buffer_after=buffers[1])
    segment=TripSegment(trip=trip,event=event,position=trip.segments.count(),kind=parts[3],origin=parts[4],destination=parts[5],
                        checkin_minutes=buffers[0],baggage_minutes=buffers[1],transfer_minutes=buffers[2],travelers=[str(x) for x in people])
    segment.full_clean();segment.save()
    return _card('event',event.pk,person,shared)


def _agent_settings(person,args,shared):
    if shared:
        raise PermissionDenied('Режим агента меняется и просматривается в личном чате.')
    parts=args.split()
    if parts:
        if len(parts)!=2 or parts[0]!='autonomy' or parts[1] not in {'personal','read_only'}:
            raise ValueError('/agent autonomy personal — выполнять разрешённые действия; /agent autonomy read_only — только читать и предлагать.')
        profile=dict(person.profile or {})
        profile['agent_autonomy']=parts[1]
        person.profile=profile
        person.save(update_fields=['profile','updated_at'])
    mode=(person.profile or {}).get('agent_autonomy',getattr(settings,'AGENT_AUTONOMY','personal'))
    if mode not in {'personal','read_only'}:
        mode='personal'
    enabled=bool(getattr(settings,'AGENT_ENABLED',True))
    connected=bool(getattr(settings,'LLM_API_KEY','') and getattr(settings,'LLM_MODEL',''))
    lines=[f'Агент: {"включён" if enabled else "отключён; используется прежний режим с подтверждением"}.',
           f'Модель: {"подключена в настройках сервера" if connected else "требуется настроить ключ и модель на сервере"}.',
           'Режим: '+('автономное выполнение разрешённых действий.' if mode=='personal' else 'только чтение, поиск и рекомендации.'),
           'Совместные события требуют согласования второго участника.',
           'Поиск по расписанию и загруженным документам доступен с учётом ваших прав.',
           '/agent autonomy personal — выполнять действия\n/agent autonomy read_only — только читать\n/agent_history — последние запуски']
    return _result('\n'.join(lines))


def _agent_history(person,args,shared):
    if shared:
        raise PermissionDenied('История агента доступна только в личном чате.')
    if args:
        raise ValueError('/agent_history — последние пять ваших запусков.')
    from .models import AgentRun
    runs=AgentRun.objects.filter(person=person,chat_id=person.chat_id).order_by('-created_at')[:5]
    labels={'running':'в работе','completed':'завершён','limited':'достигнут лимит','failed':'ошибка','interrupted':'прерван'}
    lines=['Последние запуски агента:']
    refs=[]
    for run in runs:
        status=labels.get(run.status,'неизвестное состояние')
        stamp=run.created_at.astimezone(ZoneInfo(person.timezone)).strftime('%d.%m %H:%M')
        lines.append(f'\n{stamp} — {status}; вызовов инструментов: {run.tool_count}')
        run_refs=list(run.permission_refs or [])+list((run.final_reply or {}).get('permission_refs') or [])
        readable=True
        for ref in run_refs:
            if not isinstance(ref,dict):
                readable=False
                break
            try:
                services.get_entity(ref['kind'],ref['id'],person)
            except (PermissionDenied,KeyError,TypeError):
                if ref.get('mode')=='busy' and ref.get('kind')=='event':
                    try:obj=Event.objects.filter(pk=ref.get('id')).first()
                    except (ValidationError,ValueError,TypeError):obj=None
                    if obj and obj.visibility=='busy_only':
                        continue
                readable=False
                break
        if readable:
            # The request may itself quote now-private data, so it is hidden
            # together with the answer when any recorded source is revoked.
            request=(run.input_text or '').replace('\n',' ')[:180]
            answer=str((run.final_reply or {}).get('text') or '')[:650]
            if request:lines.append('Запрос: '+request)
            if answer:lines.append(answer)
            refs.extend(run_refs)
        else:
            lines.append('Подробности скрыты: доступ к источникам изменился.')
    if len(lines)==1:
        lines.append('Пока запусков нет.')
    return _result('\n'.join(lines),permission_refs=refs)


def dispatch_command(person, chat_id, text, shared=False):
    """Synchronous command handler. Caller owns transaction and durable outbox."""
    command, args = _command(text)
    if command in {'/start','/help'}:
        return _result(HELP)
    if command == '/id':
        return _result(f'Ваш Telegram ID: {person.telegram_id}\nID чата: {chat_id}')
    if command == '/agent':
        return _agent_settings(person,args,shared)
    if command == '/agent_history':
        return _agent_history(person,args,shared)
    if command in {'/tasks','/events','/projects','/trips','/files'}:
        kind = {'/tasks':'task','/events':'event','/projects':'project','/trips':'trip','/files':'document'}[command]
        return _listing(kind, person, shared)
    if command == '/project':
        p = _parts(args)
        if not p[0]:
            raise ValueError('/project Название | private/shared')
        obj = services.create_project(person, title=p[0], visibility=_visibility(p[1] if len(p)>1 else '',shared),participants=_participant_ids(p[2]) if len(p)>2 else None)
        return _card('project',obj.pk,person,shared)
    if command == '/task':
        p = _parts(args)
        if not p[0]:
            raise ValueError('/task Название | срок или - | минуты | private/shared')
        duration = int(p[2]) if len(p)>2 and p[2] else 60
        data = {'title':p[0], 'estimated_minutes':duration, 'remaining_minutes':duration, 'visibility':_visibility(p[3] if len(p)>3 else '',shared)}
        if len(p)>1 and p[1] and p[1] != '-':
            data['due_at'] = _datetime(p[1],person).isoformat()
        data.update(_link_fields(p,[(4,'project'),(6,'trip')],person,shared))
        if len(p)>5 and p[5] not in {'','-'}:
            assignees=_participant_ids(p[5])
            if len(assignees)!=1:
                raise ValueError('Укажите один Telegram ID исполнителя.')
            data['assignee_id']=assignees[0]
        obj = services.create_task(person,**data)
        return _card('task',obj.pk,person,shared)
    if command in {'/event','/call'}:
        p = _parts(args)
        if len(p)<3:
            raise ValueError('/event 2026-11-10 15:00 | 60 | Название | Место | private/shared')
        start = _datetime(p[0],person)
        duration = int(p[1])
        if not 1 <= duration <= 10080:
            raise ValueError('Продолжительность: 1–10080 минут.')
        obj = services.create_event(person,title=p[2],starts_at=start.isoformat(),ends_at=(start.astimezone(ZoneInfo("UTC"))+timedelta(minutes=duration)).isoformat(),timezone=person.timezone,end_timezone=person.timezone,location=p[3] if len(p)>3 else '',visibility=_visibility(p[4] if len(p)>4 else '',shared),participants=_participant_ids(p[5]) if len(p)>5 else None,meeting=command=='/call',**_link_fields(p,[(6,'project'),(7,'task'),(8,'trip')],person,shared))
        return _card('event',obj.pk,person,shared)
    if command == '/trip':
        p=_parts(args)
        if len(p)<3:
            raise ValueError('/trip Название | 2026-11-15 | 2026-11-25 | private/shared')
        obj=services.create_trip(person,title=p[0],starts_on=date.fromisoformat(p[1]),ends_on=date.fromisoformat(p[2]),visibility=_visibility(p[3] if len(p)>3 else '',shared),participants=_participant_ids(p[4]) if len(p)>4 else None,**_link_fields(p,[(5,'project')],person,shared))
        return _card('trip',obj.pk,person,shared)
    if command == '/show':
        kind, identifier = args.split(maxsplit=1)
        return _card(kind,identifier,person,shared)
    if command == '/share':
        if shared:
            raise PermissionDenied('Видимость записей меняется в личном чате.')
        fields=args.split()
        if len(fields)!=3 or fields[0] not in {'document','event','task','trip','project'}:
            raise ValueError('/share document|event|task|trip|project ID private|shared|busy_only')
        obj=services.share_entity(person,fields[0],fields[1],fields[2])
        if isinstance(obj,ActionRequest):
            return _result('Изменение видимости ожидает согласования.')
        return _result(f'Видимость изменена: {fields[2]}. Доступ к связанным объектам меняется отдельно.',**_entity_meta(fields[0],obj))
    if command == '/undo_task':
        task=services.get_entity('task',args,person,shared_context=shared)
        services.undo_task_completion(person,task.pk)
        return _result('Дело снова открыто.',**_entity_meta('task',task))
    if command == '/done':
        task=services.get_entity('task',args,person,shared_context=shared)
        services.complete_task(person,task.pk)
        return _result('Дело выполнено.',**_entity_meta('task',task))
    if command in {'/move','/cancel'}:
        p=_parts(args)
        event=services.get_entity('event',p[0],person,shared_context=shared)
        changes={'status':'cancelled'}
        if command == '/move':
            if len(p)<2:
                raise ValueError('/move ID | 2026-11-10 16:00 | минуты')
            start=_datetime(p[1],person)
            duration=int(p[2]) if len(p)>2 else int((event.ends_at-event.starts_at).total_seconds()/60)
            changes={'starts_at':start.isoformat(),'ends_at':(start.astimezone(ZoneInfo("UTC"))+timedelta(minutes=duration)).isoformat()}
        result=services.update_event(person,event.pk,changes,expected_version=event.version)
        if isinstance(result,ActionRequest):
            return _result('Изменение ожидает согласования второго участника.')
        return _card('event',event.pk,person,shared)
    if command == '/checklist':
        p=_parts(args)
        if len(p)!=2 or len(p[0].split())!=2:
            raise ValueError('/checklist event|trip ID | паспорт; билеты; фотоаппарат')
        kind,identifier=p[0].split()
        services.get_entity(kind,identifier,person,shared_context=shared)
        checklist=services.make_checklist(person,kind,identifier,p[1])
        return _checklist_reply(checklist,person,shared)
    if command == '/things':
        return _card('checklist',args,person,shared)
    if command == '/get':
        doc=services.get_entity('document',args,person,shared_context=shared)
        return _result('',document_path=doc.file.path,filename=doc.original_name,caption=doc.title,**_entity_meta('document',doc))
    if command == '/link':
        p=_parts(args)
        if len(p)!=3:
            raise ValueError('/link ID_файла | event|trip|task | ID')
        services.get_entity('document',p[0],person,shared_context=shared)
        services.get_entity(p[1],p[2],person,shared_context=shared)
        doc=services.link_document(person,p[0],p[1],p[2])
        return _result('Файл связан с записью.',**_entity_meta('document',doc))
    if command in {'/today','/plan'}:
        if shared:
            raise PermissionDenied('Персональный план доступен в личном чате с ботом.')
        from .planning import render_daily_plan, plan_permission_refs
        day=date.fromisoformat(args) if args else timezone.now().astimezone(ZoneInfo(person.timezone)).date()
        text=render_daily_plan(person,day)
        plan=DailyPlan.objects.get(person=person,day=day)
        return _result(text,permission_refs=plan_permission_refs(person,day),reply_markup=_markup([[('Принять предложенные блоки',f'plan:{day.isoformat()}:{plan.version}')]]))
    if command == '/accept':
        if shared:
            raise PermissionDenied('План принимается в личном чате.')
        from .planning import accept_plan
        day=date.fromisoformat(args)
        events=accept_plan(person,day)
        return _result(f'План принят. Создано блоков: {len(events)}.')
    if command == '/checktrip':
        from .planning import check_trip
        trip=services.get_entity('trip',args,person,shared_context=shared)
        rows=check_trip(trip,user=person,shared_context=shared)
        refs=[{'kind':'event','id':str(segment.event.pk)} for segment in trip.segments.select_related('event') if services.can_view(segment.event,person,shared_context=shared)]
        return _result('Стыковки:\n'+('\n'.join(json.dumps(x,ensure_ascii=False,default=str) for x in rows) if rows else 'Нет доступных пар участков для проверки.'),permission_refs=refs,**_entity_meta('trip',trip))
    if command == '/segment':
        return _segment(person,args,shared)
    if command in {'/settings','/profile'}:
        if command=='/profile':
            args=args.replace('=', ' ', 1)
        return _settings(person,args,shared)
    if command in {'/repeat','/recurrence'}:
        return _repeat(person,args,shared)
    if command == '/resolve':
        if shared:
            raise PermissionDenied('Конфликт календаря разрешается в личном чате.')
        from .integrations.google import resolve_conflict
        fields=args.split()
        if len(fields)!=2 or fields[1] not in {'local','remote'}:
            raise ValueError('/resolve ID_события local|remote')
        event=services.get_entity('event',fields[0],person)
        resolve_conflict(person,str(event.pk),choice=fields[1])
        return _result('Конфликт разрешён.',**_entity_meta('event',event))
    if command in {'/google','/calendars','/calendar'}:
        if shared:
            raise PermissionDenied('Подключение календаря выполняется в личном чате.')
        from .integrations import google
        if command == '/google':
            return _result('Подключите Google:\n'+google.authorization_url(person))
        if command == '/calendars':
            calendars=google.list_calendars(person)
            lines=['Календари Google:']
            for cal in calendars:
                lines.append(f'{cal.get("summary",cal.get("id",""))}\n{cal.get("id","")}')
            return _result('\n\n'.join(lines)+'\n\n/calendar ID | personal/shared | rw/ro')
        p=_parts(args)
        if not p[0]:
            raise ValueError('/calendar ID | personal/shared | rw/ro')
        role=p[1] if len(p)>1 else 'personal'
        access=p[2] if len(p)>2 else 'ro'
        if role not in {'personal','shared'} or access not in {'rw','ro'}:
            raise ValueError('/calendar ID | personal/shared | rw/ro')
        google.select_calendar(person,p[0],role=role,writable=access=='rw')
        return _result('Календарь подключён. /sync — получить изменения.')
    if command == '/sync':
        if shared:
            raise PermissionDenied('Синхронизацию запускайте в личном чате.')
        enqueue('google_sync',{},f'manual-sync:{person.pk}:{timezone.now().strftime("%Y%m%d%H%M")}',queue='service')
        return _result('Синхронизация поставлена в очередь.')
    if command:
        raise ValueError('Неизвестная команда. /help — список.')
    return None


def dispatch_callback(person,chat_id,data,shared=False):
    parts=data.split(':')
    operation=parts[0]
    if operation == 'show' and len(parts)==3:
        return _card(parts[1],parts[2],person,shared)
    if operation == 'get' and len(parts)==2:
        return dispatch_command(person,chat_id,'/get '+parts[1],shared)
    if operation == 'done' and len(parts)==2:
        return dispatch_command(person,chat_id,'/done '+parts[1],shared)
    if operation == 'things' and len(parts)==3:
        obj=services.get_entity('checklist',parts[1],person,shared_context=shared)
        return _checklist_reply(obj,person,shared,int(parts[2]))
    if operation == 'item' and len(parts)==3 and parts[2] in {'0','1'}:
        item=ChecklistItem.objects.select_related('checklist__event','checklist__trip').get(pk=parts[1])
        if not services.can_view(item.checklist.parent,person,shared_context=shared):
            raise PermissionDenied('Нет доступа к списку.')
        item=services.set_checklist_item(person,item.pk,parts[2]=='1')
        return _checklist_reply(item.checklist,person,shared)
    if operation in {'apply','discard'} and len(parts)==3:
        conversation=Conversation.objects.select_for_update().get(pk=parts[1],person=person,chat_id=chat_id)
        if not conversation.pending or conversation.pending.get('token') != parts[2]:
            return _result('Это предложение уже обработано или заменено новым.')
        if not conversation.expires_at or conversation.expires_at < timezone.now():
            conversation.pending={}; conversation.save(update_fields=['pending','updated_at'])
            return _result('Срок предложения истёк. Повторите запрос.')
        action=conversation.pending['action']
        conversation.pending={}; conversation.save(update_fields=['pending','updated_at'])
        if operation == 'discard':
            return _result('Предложение отменено.')
        reply=_action_result(services.execute_action(person,action,shared_context=shared),person,shared)
        reply['permission_refs']=reply.get('permission_refs',[])+action.get('permission_refs',[])
        return reply
    if operation in {'approve','reject'} and len(parts)==2:
        if shared:
            raise PermissionDenied('Согласование выполняется в личном чате.')
        services.approve_action(person,parts[1],approve=operation=='approve')
        return _result('Изменение согласовано.' if operation=='approve' else 'Предложение отклонено.')
    if operation == 'plan' and len(parts)==3:
        if shared:
            raise PermissionDenied('План принимается в личном чате.')
        from .planning import accept_plan
        day=date.fromisoformat(parts[1])
        plan=DailyPlan.objects.select_for_update().filter(person=person,day=day).first()
        if not plan or plan.version!=int(parts[2]):
            raise ValidationError('План уже обновлён. Откройте /plan заново и проверьте новые блоки.')
        events=accept_plan(person,day)
        return _result(f'План принят. Создано блоков: {len(events)}.')
    raise ValueError('Кнопка устарела. Откройте карточку заново.')


def _identity(payload):
    callback=payload.get('callback_query')
    message=(callback or {}).get('message') or payload.get('message')
    sender=(callback or {}).get('from') or (message or {}).get('from')
    if not message or not sender or sender.get('is_bot'):
        return None
    chat=message.get('chat') or {}
    if chat.get('type') not in {'private','group','supergroup'}:
        return None
    chat_id=int(chat['id']); user_id=int(sender['id'])
    shared=chat.get('type') != 'private'
    if shared and (not _group_id() or chat_id != _group_id()):
        return None
    if not shared and chat_id != user_id:
        return None
    return user_id,chat_id,shared,message,callback


def _find_person(identity):
    user_id,chat_id,shared,*_=identity
    if user_id not in _allowed_ids():
        return None
    person=Person.objects.filter(telegram_id=user_id).first()
    if person and not shared and person.chat_id != chat_id:
        person.chat_id=chat_id
        person.save(update_fields=['chat_id','updated_at'])
    return person


def ingest_update(payload):
    """Save an update before a poll offset or webhook response acknowledges it."""
    if not isinstance(payload.get('update_id'),int):
        raise ValueError('Invalid update_id')
    with transaction.atomic():
        update,created=IncomingUpdate.objects.get_or_create(update_id=payload['update_id'],defaults={'payload':payload})
        if created:
            enqueue('telegram_update',{'update_id':update.update_id},f'tg-update:{update.update_id}',queue='chat')
    return created


def _queue_reply(update_id,chat_id,reply,person=None,shared=False,index=0):
    payload={'chat_id':chat_id,**reply,'shared_context':shared}
    if person:
        payload['actor_id']=str(person.pk)
    text=payload.get('text','')
    # Telegram plain text limit is 4096. Split safely, attach buttons to last part.
    if len(text)>3900 and not payload.get('document_path'):
        chunks=[text[n:n+3900] for n in range(0,len(text),3900)]
        for n,chunk in enumerate(chunks):
            item={**payload,'text':chunk}
            item.pop('edit_message_id',None)
            if n < len(chunks)-1:
                item.pop('reply_markup',None)
            enqueue('telegram_send',item,f'tg-reply:{update_id}:{index}:{n}',queue='notifications')
    else:
        enqueue('telegram_send',payload,f'tg-reply:{update_id}:{index}',queue='notifications')


def _finish_sync(payload,identity,person_id,prepared=None):
    user_id,chat_id,shared,message,callback=identity
    with transaction.atomic():
        incoming,_=IncomingUpdate.objects.get_or_create(update_id=payload['update_id'],defaults={'payload':payload})
        incoming=IncomingUpdate.objects.select_for_update().get(pk=incoming.pk)
        if incoming.status == 'completed' or incoming.payload.get('_bot_processed'):
            return
        person=Person.objects.get(pk=person_id) if person_id else None
        try:
            with transaction.atomic():
                reply=_handle_prepared(person,user_id,chat_id,shared,message,callback,prepared)
                if reply:
                    if callback and reply.get('entity_type')=='checklist':
                        reply['edit_message_id']=message.get('message_id')
                    _queue_reply(incoming.update_id,chat_id,reply,person,shared)
        except (ValueError,ValidationError,PermissionDenied,ZoneInfoNotFoundError) as exc:
            error=' '.join(exc.messages) if isinstance(exc,ValidationError) else str(exc)
            _queue_reply(incoming.update_id,chat_id,_result(error[:3000] or 'Нет доступа или неверный формат.'),person,shared)
        except (KeyError,TypeError,IndexError,Person.DoesNotExist,ChecklistItem.DoesNotExist,Conversation.DoesNotExist):
            _queue_reply(incoming.update_id,chat_id,_result('Запись не найдена, кнопка устарела или формат неверен. /help — примеры.'),person,shared)
        incoming.status='completed'
        incoming.payload={**incoming.payload,'_bot_processed':True}
        incoming.save(update_fields=['status','payload','updated_at'])


def _handle_prepared(person,user_id,chat_id,shared,message,callback,prepared):
    if person is None:
        text=(message.get('text') or '').strip()
        reply=_result(f'Ваш Telegram ID: {user_id}\nID чата: {chat_id}\nДобавьте ваш ID в ALLOWED_TELEGRAM_IDS и выполните bootstrap_users на сервере.',onboarding_id=user_id) if _command(text)[0]=='/id' and not shared else None
    elif callback:
        reply=dispatch_callback(person,chat_id,callback.get('data',''),shared)
    elif prepared and prepared.get('reply') is not None:
        reply=prepared['reply']
    elif prepared and prepared.get('upload'):
        data=prepared['upload']
        caption=message.get('caption','')
        command,args=_command(caption)
        if command=='/attach':
            kind,identifier=args.split(maxsplit=1)
            parent=services.get_entity(kind,identifier,person,shared_context=shared)
            if kind not in {'event','trip','task'} or not services.can_edit(parent,person):
                raise PermissionDenied('Для привязки файла нужны права редактора записи.')
        document=services.save_document(person,data['filename'],data['content'],data['mime_type'],telegram_file_id=data['file_id'],metadata=data.get('metadata',{}))
        if command=='/attach':
            services.link_document(person,document.pk,kind,identifier)
        reply=_result(f'Файл сохранён: {document.original_name}\nID: {document.pk}\nСвязать: /link {document.pk} | event или trip | ID\nРеквизиты для проверки: /show document {document.pk}\nФайл личный; открыть супруге: /share document {document.pk} shared',**_entity_meta('document',document))
    elif prepared and prepared.get('error'):
        reply=_result(prepared['error'])
    elif prepared and prepared.get('action'):
        action=prepared['action']
        if action.get('action') in WRITE_ACTIONS:
            reply=_record_pending(person,chat_id,action)
        else:
            reply=_action_result(services.execute_action(person,action,shared_context=shared),person,shared)
            reply['permission_refs']=reply.get('permission_refs',[])+action.get('permission_refs',[])
    else:
        text=(prepared or {}).get('text') or message.get('text','')
        reply=dispatch_command(person,chat_id,text,shared)
        if reply is None:
            reply=_result('Для свободного текста настройте LLM_API_KEY и LLM_MODEL. Команды доступны через /help.')
    if person and not callback and (message.get('text') or (prepared or {}).get('text')):
        user_text=(prepared or {}).get('text') or message.get('text','')
        conversation,_=Conversation.objects.get_or_create(person=person,chat_id=chat_id)
        conversation.messages=(conversation.messages+[{'role':'user','content':user_text[:2000]}])[-6:]
        conversation.save(update_fields=['messages','updated_at'])
    return reply


def _history(person,chat_id,shared):
    if shared:
        return []
    conversation=Conversation.objects.filter(person=person,chat_id=chat_id).first()
    return conversation.messages if conversation else []


async def process_update(payload):
    """Process one durable update. Network/LLM outside mutation transaction."""
    identity=_identity(payload)
    if not identity:
        return
    user_id,chat_id,shared,message,callback=identity
    processed=await sync_to_async(lambda: IncomingUpdate.objects.filter(update_id=payload['update_id'],payload___bot_processed=True).exists())()
    if processed:
        return
    person=await sync_to_async(_find_person)(identity)
    if not person:
        await sync_to_async(_finish_sync)(payload,identity,None)
        return
    if callback:
        try:
            async with Bot(settings.TELEGRAM_BOT_TOKEN) as bot:
                await bot.answer_callback_query(callback['id'],request_timeout=5)
        except Exception as exc:
            log.debug('Callback acknowledgement failed: %s',type(exc).__name__)
    prepared=None
    if not callback:
        text=message.get('text','')
        document=message.get('document')
        photo=(message.get('photo') or [None])[-1]
        voice=message.get('voice')
        attachment=document or photo or voice
        if attachment:
            if shared:
                prepared={'error':'Билеты, документы и голосовые отправляйте в личный чат с ботом. После сохранения файл можно связать с общей поездкой.'}
            elif int(attachment.get('file_size') or 0)>MAX_UPLOAD:
                prepared={'error':'Файл слишком большой. Максимум — 15 МБ.'}
            else:
                try:
                    async with Bot(settings.TELEGRAM_BOT_TOKEN) as bot:
                        file_info=await bot.get_file(attachment['file_id'])
                        buf=io.BytesIO()
                        await bot.download_file(file_info.file_path,destination=buf,timeout=60)
                        content=buf.getvalue()
                    if len(content)>MAX_UPLOAD:
                        raise ValueError('Файл слишком большой. Максимум — 15 МБ.')
                    filename=(document or {}).get('file_name') or ('voice.ogg' if voice else 'photo.jpg')
                    mime=(document or voice or {}).get('mime_type') or ('audio/ogg' if voice else 'image/jpeg')
                    if voice:
                        from .integrations.llm import transcribe
                        text=await sync_to_async(transcribe,thread_sensitive=False)(content,filename)
                    else:
                        from .integrations.llm import extract_document
                        metadata=await sync_to_async(extract_document,thread_sensitive=False)(content,mime,filename)
                        prepared={'upload':{'filename':Path(filename).name,'content':content,'mime_type':mime,'file_id':attachment['file_id'],'metadata':metadata}}
                except Exception as exc:
                    log.warning('Attachment preparation failed: %s',type(exc).__name__)
                    prepared={'error':'Не удалось получить файл или распознать голос. Проверьте настройки API либо пришлите текст.'}
        if prepared is None and text and not text.startswith('/'):
            try:
                if getattr(settings,'AGENT_ENABLED',True):
                    from .agent.runner import run_agent
                    reply=await run_agent(person_id=person.pk,text=text,chat_id=chat_id,
                                          update_id=payload['update_id'],shared_context=shared)
                    # Runner output is a plain answer, not a transport command.
                    # Destination, identity, files and buttons stay code-owned.
                    if not isinstance(reply,dict) or not isinstance(reply.get('text'),str):
                        raise ValueError('Invalid agent response')
                    prepared={'reply':{'text':reply['text'],'permission_refs':reply.get('permission_refs',[])},'text':text}
                else:
                    from .integrations.llm import interpret
                    history=await sync_to_async(_history)(person,chat_id,shared)
                    action=await sync_to_async(interpret,thread_sensitive=False)(person,text,shared_context=shared,history=history)
                    prepared={'action':action,'text':text}
            except Exception as exc:
                log.warning('Language interpretation failed: %s',type(exc).__name__)
                if getattr(settings,'AGENT_ENABLED',True):
                    prepared={'error':'Работа агента прервана. Уже выполненные действия могли сохраниться: проверьте /agent_history и нужные записи. Команды из /help доступны без ИИ.'}
                else:
                    prepared={'error':'Не удалось обработать свободный текст. Проверьте настройки LLM; команды из /help доступны без него.'}
        elif prepared is None and text:
            if _command(text)[0] in {'/google','/calendars','/calendar','/resolve'}:
                # OAuth refresh and Google sync may perform HTTP and persist token state.
                # Keep those operations outside the update/outbox transaction.
                try:
                    reply=await sync_to_async(dispatch_command,thread_sensitive=False)(person,chat_id,text,shared)
                    prepared={'reply':reply,'text':text}
                except (ValueError,ValidationError,PermissionDenied) as exc:
                    message_text=' '.join(exc.messages) if isinstance(exc,ValidationError) else str(exc)
                    prepared={'error':message_text[:3000]}
            else:
                prepared={'text':text}
    await sync_to_async(_finish_sync)(payload,identity,person.pk,prepared)


async def poll_forever():
    """Polling only ingests; separate workers execute jobs and send messages."""
    if not settings.TELEGRAM_BOT_TOKEN:
        raise RuntimeError('TELEGRAM_BOT_TOKEN is required')
    async with Bot(settings.TELEGRAM_BOT_TOKEN) as bot:
        webhook=await bot.get_webhook_info()
        if webhook.url:
            raise RuntimeError('Webhook already configured. Remove it before starting polling; pending updates will not be dropped.')
        offset=None
        while True:
            try:
                updates=await bot.get_updates(offset=offset,timeout=30,allowed_updates=['message','callback_query'])
                for update in updates:
                    await sync_to_async(ingest_update)(update.model_dump(mode='json',exclude_none=True))
                    offset=update.update_id+1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('Polling retry: %s',type(exc).__name__)
                await asyncio.sleep(3)
