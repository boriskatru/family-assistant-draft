import os
os.environ.setdefault('SQLITE_PATH', ':memory:')
os.environ.setdefault('DJANGO_SECRET_KEY', 'unit-test-secret-not-for-production')
os.environ.setdefault('ALLOWED_TELEGRAM_IDS', '1001,1002')
import pytest
from assistant.models import Person
@pytest.fixture
def people(db, settings):
    settings.ALLOWED_TELEGRAM_IDS = (1001, 1002)
    return (Person.objects.create(telegram_id=1001,chat_id=1001,name='Борис'),Person.objects.create(telegram_id=1002,chat_id=1002,name='Анна'))
