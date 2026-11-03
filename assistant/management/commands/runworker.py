import logging
import threading
import time
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from assistant.queue import claim,finish,heartbeat
from assistant.runtime import handle_job
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
                # Exception type only: URLs may contain service credentials or ticket data.
                logger.error('Job %s kind=%s failed (%s)',job.id,job.kind,type(exc).__name__)
                finish(job,error=exc)
            finally:
                stopped.set();thread.join(timeout=2)
            if opts['once']:return
