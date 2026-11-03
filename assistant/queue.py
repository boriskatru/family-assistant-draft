import uuid
from datetime import timedelta
from django.db import transaction, connection
from django.db.models import Q
from django.utils import timezone
from .models import Job


def enqueue(kind,payload,dedup_key,run_at=None,queue='service'):
    return Job.objects.get_or_create(dedup_key=dedup_key,defaults={'kind':kind,'payload':payload,'run_at':run_at or timezone.now(),'queue':queue})[0]


def claim(queue,now=None):
    now=now or timezone.now()
    with transaction.atomic():
        qs=Job.objects.filter(queue=queue,run_at__lte=now).filter(Q(status='pending')|Q(status='running',locked_until__lt=now)).order_by('run_at','created_at')
        if connection.features.has_select_for_update_skip_locked:
            qs=qs.select_for_update(skip_locked=True)
        else:
            qs=qs.select_for_update()
        job=qs.first()
        if not job: return None
        job.status='running'; job.attempts+=1
        job.lock_token=uuid.uuid4(); job.locked_until=now+timedelta(minutes=3)
        job.save(update_fields=['status','attempts','lock_token','locked_until','updated_at'])
        return job


def heartbeat(job):
    return Job.objects.filter(pk=job.pk,status='running',lock_token=job.lock_token).update(locked_until=timezone.now()+timedelta(minutes=3))


def finish(job,error=None):
    fields={'locked_until':None,'lock_token':None}
    if error:
        fields.update(status='failed' if job.attempts>=5 else 'pending',error=type(error).__name__,run_at=timezone.now()+timedelta(seconds=min(900,15*2**job.attempts)))
    else:
        fields.update(status='done',error='')
    return Job.objects.filter(pk=job.pk,status='running',lock_token=job.lock_token).update(**fields)
