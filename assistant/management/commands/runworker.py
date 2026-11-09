import logging
import threading
import time
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from assistant.queue import MAX_ATTEMPTS,claim,finish,heartbeat
from assistant.runtime import handle_job
from assistant.error_reporting import report_error
from assistant.integrations.google import GoogleIntegrationError, GoogleRetryableError
logger=logging.getLogger(__name__)
class Command(BaseCommand):
    help='Process durable database jobs'
    def add_arguments(self,parser):
        parser.add_argument('--queue',default='service',choices=['service','chat','notifications'])
        parser.add_argument('--once',action='store_true')
    def handle(self,*args,**opts):
        while True:
            close_old_connections()
            job=claim(opts['queue'])
            if not job:
                if opts['once']:return
                time.sleep(1);continue
            stopped=threading.Event()
            def pulse():
                while not stopped.wait(30):
                    close_old_connections();heartbeat(job);close_old_connections()
            thread=threading.Thread(target=pulse,daemon=True);thread.start()
            try:
                handle_job(job);finish(job)
            except Exception as exc:
                temporary_google_error = isinstance(exc, GoogleRetryableError)
                retryable = not isinstance(exc, GoogleIntegrationError) or temporary_google_error
                # Persist the outcome under the lease before reporting it; a
                # worker which lost its lease must not send a stale alert.
                if finish(job,error=exc,retryable=retryable):
                    if not job.payload.get('error_alert'):
                        exhausted = temporary_google_error and job.attempts >= MAX_ATTEMPTS
                        report_error(exc,'worker.'+opts['queue'],job_id=job.pk,
                                     notify=not temporary_google_error or exhausted,
                                     retry_exhausted=exhausted)
                    else:
                        logger.error('Error alert delivery failed job=%s error=%s',job.id,type(exc).__name__)
            finally:
                stopped.set();thread.join(timeout=2)
            if opts['once']:return
