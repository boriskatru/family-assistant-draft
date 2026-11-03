import time
import logging
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from assistant.runtime import schedule_tick
logger=logging.getLogger(__name__)
class Command(BaseCommand):
    help='Create due jobs every 30 seconds; run only one scheduler process'
    def add_arguments(self,parser):parser.add_argument('--once',action='store_true')
    def handle(self,*args,**opts):
        while True:
            try:
                close_old_connections();schedule_tick()
            except Exception as exc:
                logger.error('Scheduler failed (%s)',type(exc).__name__)
                if opts['once']:raise
            if opts['once']:return
            time.sleep(30)
