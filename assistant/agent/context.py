"""Per-request execution limits and durable, transactional mutation receipts."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass, field

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError, ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone

from assistant.models import AgentRun, AgentToolCall, Person
from .egress import EgressGuard


class AgentLimitExceeded(RuntimeError):
    pass


class AgentInterrupted(RuntimeError):
    pass


def _json(value):
    return json.loads(json.dumps(value,ensure_ascii=False,default=str))


def _refs_merge(old,new):
    refs={}
    for ref in list(old)+list(new):
        if not isinstance(ref,dict) or not ref.get('kind') or not ref.get('id'):
            continue
        key=(ref['kind'],str(ref['id']))
        if key not in refs or ref.get('mode')!='busy':
            refs[key]=ref
    return list(refs.values())


def _partial_summary(run):
    rows=run.tool_calls.filter(mutation=True,status='completed').order_by('created_at')
    lines=[]
    for row in rows[:30]:
        result=row.result
        detail=result.get('text') or result.get('message') or result.get('status') or 'выполнено'
        lines.append(f'• {row.name}: {str(detail)[:500]}')
    return '\n'.join(lines) or 'Записанных изменений нет.'


@dataclass
class AgentContext:
    person: Person
    run: AgentRun
    shared_context: bool
    chat_id: int
    external_count: int=0
    _lock: asyncio.Lock=field(default_factory=asyncio.Lock)
    _egress: EgressGuard=field(init=False)

    def __post_init__(self):
        self._egress=EgressGuard(self.run.input_text)

    @property
    def autonomy(self):
        return self.person.profile.get('agent_autonomy',settings.AGENT_AUTONOMY)

    def inherit_history(self, messages, refs):
        """Protect answers and public tool calls derived from a previous turn."""
        self._egress.observe({'history': messages})
        if not refs:
            return
        # Preserve known sensitive document fields even if an old answer
        # repeated an unlabelled booking code and the model does not reread it.
        from assistant.models import Document
        documents = [ref['id'] for ref in refs if ref['kind'] == 'document']
        if documents:
            for metadata in Document.objects.filter(pk__in=documents).values_list('metadata', flat=True):
                self._egress.observe({'metadata': metadata})
            for message in messages:
                if message['role'] == 'assistant':
                    self._egress.observe({'document_id': 'history', 'text': message['content']})
        with transaction.atomic():
            run = AgentRun.objects.select_for_update().get(pk=self.run.pk)
            if run.status != 'running':
                raise AgentInterrupted('Обработка уже завершена или прервана.')
            run.permission_refs = _refs_merge(run.permission_refs, refs)
            run.save(update_fields=['permission_refs', 'updated_at'])
            self.run.permission_refs = run.permission_refs

    def _reserve(self,external):
        with transaction.atomic():
            run=AgentRun.objects.select_for_update().get(pk=self.run.pk)
            if run.status!='running':
                raise AgentInterrupted('Обработка уже завершена или прервана.')
            if run.tool_count>=settings.AGENT_MAX_TOOL_CALLS:
                raise AgentLimitExceeded('Достигнут лимит вызовов инструментов.')
            if external and self.external_count>=settings.AGENT_MAX_EXTERNAL_CALLS:
                raise AgentLimitExceeded('Достигнут лимит внешних запросов.')
            run.tool_count+=1
            run.save(update_fields=['tool_count','updated_at'])
            self.run.tool_count=run.tool_count
        if external:self.external_count+=1

    def _record(self,name,args,result,fingerprint,mutation=False,status='completed',error=''):
        AgentToolCall.objects.update_or_create(run=self.run,fingerprint=fingerprint,defaults={
            'name':name,'arguments':_json(args),'result':_json(result),'mutation':mutation,
            'status':status,'error':error[:100],
        })

    def _mutation(self,name,args,executor,fingerprint):
        # DB writes and their receipt commit together; a retry of an identical
        # operation cannot create a second object or enqueue a second delivery.
        with transaction.atomic():
            run=AgentRun.objects.select_for_update().get(pk=self.run.pk)
            if run.status!='running':raise AgentInterrupted('Обработка прервана.')
            previous=AgentToolCall.objects.filter(run=run,fingerprint=fingerprint,status='completed').first()
            if previous:
                return {**previous.result,'replayed':True}
            self.person.refresh_from_db(fields=['profile'])
            if self.autonomy=='read_only' and name!='send_document':
                raise PermissionDenied('Включён режим read_only: изменять расписание нельзя. /agent autonomy personal')
            result=executor()
            if not isinstance(result,dict):result={'result':_json(result)}
            result={'ok':True,**result}
            if not result['ok']:
                raise ValidationError(result.get('error','Действие не выполнено.'))
            self._record(name,args,result,fingerprint,mutation=True)
            # Persist guards in the SAME transaction as effects and receipt.
            # Recovery must never expose a committed result without its ACL refs.
            run.permission_refs=_refs_merge(run.permission_refs,result.get('permission_refs',[]))
            run.save(update_fields=['permission_refs','updated_at'])
            self.run.permission_refs=run.permission_refs
            return result

    def _save_sources(self,result):
        self.run.permission_refs=_refs_merge(self.run.permission_refs,result.get('permission_refs',[]))
        sources=list(self.run.sources)
        for source in result.get('sources',[]):
            if source not in sources:sources.append(source)
        self.run.sources=sources[:100]
        usage=dict(self.run.usage)
        external_usage=result.get('usage') or {}
        if external_usage:
            previous=dict(usage.get('external',{}))
            for key in ('input_tokens','output_tokens','total_tokens'):
                previous[key]=int(previous.get(key,0))+int(external_usage.get(key,0) or 0)
            usage['external']=previous
        usage['external_calls']=self.external_count
        self.run.usage=usage
        AgentRun.objects.filter(pk=self.run.pk).update(permission_refs=self.run.permission_refs,sources=self.run.sources,usage=usage)

    async def dispatch(self,name,args,executor,mutation=False,external=False,call_id=''):
        async with self._lock:
            await sync_to_async(self._reserve)(external)
            body=json.dumps({'name':name,'args':_json(args)},sort_keys=True,ensure_ascii=False)
            if mutation and name in ('set_checklist_item','set_place_alias'):
                # True→false→true is legitimate within one plan. This setter is
                # idempotent itself; only an exact SDK call replay is cached.
                body+=':'+str(call_id or uuid.uuid4())
            fingerprint=hashlib.sha256((body if mutation else str(uuid.uuid4())).encode()).hexdigest()
            try:
                if external:self._egress.check(args)
                if mutation:
                    result=await sync_to_async(self._mutation)(name,args,executor,fingerprint)
                else:
                    result=await sync_to_async(executor,thread_sensitive=not external)()
                    if not isinstance(result,dict):result={'result':_json(result)}
                    result={'ok':True,**result}
                    await sync_to_async(self._record)(name,args,result,fingerprint)
            except (AgentLimitExceeded,AgentInterrupted):
                raise
            except (PermissionDenied,ValidationError,ValueError,ObjectDoesNotExist) as exc:
                message=' '.join(exc.messages) if isinstance(exc,ValidationError) else str(exc)
                if isinstance(exc,ObjectDoesNotExist):message='Объект не найден или недоступен.'
                result={'ok':False,'error':message[:1500],'error_type':type(exc).__name__}
                await sync_to_async(self._record)(name,args,result,fingerprint,mutation,'error',type(exc).__name__)
            except Exception as exc:
                result={'ok':False,'error':'Инструмент временно недоступен. Не утверждайте, что действие выполнено.','error_type':type(exc).__name__}
                await sync_to_async(self._record)(name,args,result,fingerprint,mutation,'error',type(exc).__name__)
            result=_json(result)
            self._egress.observe(result)
            await sync_to_async(self._save_sources)(result)
            return result

    async def save_checkpoint(self,input_items,usage=None):
        # No hidden chain-of-thought: caller supplies only public messages and
        # tool calls/results. This snapshot is for audit, not an authority source.
        items=_json(input_items)
        if len(json.dumps(items,ensure_ascii=False))>250000:
            items=items[-12:]
        self.run.checkpoint=items
        merged={**self.run.usage,**_json(usage or {})}
        self.run.usage=merged
        await sync_to_async(AgentRun.objects.filter(pk=self.run.pk).update)(checkpoint=items,usage=merged)

    async def partial_summary(self):
        return await sync_to_async(_partial_summary)(self.run)

    async def finish(self,status,text):
        reply={'text':str(text),'permission_refs':self.run.permission_refs}
        self.run.status=status
        self.run.final_reply=reply
        self.run.finished_at=timezone.now()
        await sync_to_async(AgentRun.objects.filter(pk=self.run.pk).update)(status=status,final_reply=reply,finished_at=self.run.finished_at)
        return reply


def _create_context(person_id,chat_id,update_id,text,shared_context):
    with transaction.atomic():
        person=Person.objects.get(pk=person_id)
        if person.telegram_id not in settings.ALLOWED_TELEGRAM_IDS:
            raise PermissionDenied('Нет доступа.')
        if shared_context:
            if not settings.SHARED_CHAT_ID or chat_id!=settings.SHARED_CHAT_ID:
                raise PermissionDenied('Неизвестный общий чат.')
        elif chat_id!=person.chat_id:
            raise PermissionDenied('Неизвестный личный чат.')
        run,created=AgentRun.objects.get_or_create(update_id=update_id,defaults={
            'person':person,'chat_id':chat_id,'input_text':text[:12000],
        })
        run=AgentRun.objects.select_for_update().get(pk=run.pk)
        if run.person_id!=person.pk or run.chat_id!=chat_id:
            raise PermissionDenied('Запрос относится к другому диалогу.')
        if not created and run.status=='running':
            # A recovered job must not infer/replay an already partly executed
            # plan under new IDs. Surface committed effects and require a new turn.
            run.status='interrupted'
            run.finished_at=timezone.now()
            for receipt in run.tool_calls.filter(status='completed'):
                run.permission_refs=_refs_merge(run.permission_refs,receipt.result.get('permission_refs',[]))
            run.final_reply={'text':'Предыдущая обработка прервалась. Сохранённые результаты:\n'+_partial_summary(run)+'\nДля продолжения отправьте новый запрос.','permission_refs':run.permission_refs}
            run.save(update_fields=['status','finished_at','final_reply','permission_refs','updated_at'])
        return AgentContext(person,run,shared_context,chat_id,int(run.usage.get('external_calls',0)))


async def create_context(person_id,chat_id,update_id,text,shared_context=False):
    return await sync_to_async(_create_context)(person_id,chat_id,update_id,text,shared_context)
