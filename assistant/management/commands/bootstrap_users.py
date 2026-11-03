import os
from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from assistant.models import Person
class Command(BaseCommand):
    help='Create allowed users from environment; /start registers their private chat'
    def add_arguments(self,parser):parser.add_argument('--ids',default='')
    def handle(self,*args,**opts):
        ids=[int(v) for v in opts['ids'].split(',') if v] if opts['ids'] else settings.ALLOWED_TELEGRAM_IDS
        if len(ids)>2:raise CommandError('Draft supports at most two authorised users')
        names=os.getenv('USER_NAMES','Борис,Анна').split(',')
        for i,tid in enumerate(ids):
            Person.objects.get_or_create(telegram_id=tid,defaults={'name':names[i] if i<len(names) else f'Участник {i+1}','timezone':settings.TIME_ZONE})
        self.stdout.write(f'Configured users: {len(ids)}. Each must send /start to the bot.')
