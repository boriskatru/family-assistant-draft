import asyncio
from django.core.management.base import BaseCommand
from assistant.bot import poll_forever

class Command(BaseCommand):
    help = 'Run Telegram long-polling ingestion; workers process the durable queue.'
    def handle(self, *args, **options):
        try:
            asyncio.run(poll_forever())
        except KeyboardInterrupt:
            pass
