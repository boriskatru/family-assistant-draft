import asyncio
import os
os.environ.setdefault('SQLITE_PATH', ':memory:')
os.environ.setdefault('DJANGO_SECRET_KEY', 'unit-test-secret-not-for-production')
os.environ.setdefault('ALLOWED_TELEGRAM_IDS', '1001,1002')
import pytest
from asgiref.sync import sync_to_async
from django.db import connections
from assistant.models import Person


@pytest.fixture(scope='session', autouse=True)
def close_async_database_connections(django_db_setup, django_db_blocker):
    # asyncio.run() leaves asgiref's thread-sensitive executor alive. Its
    # thread-local Django connection must close before pytest-django drops the
    # test database; closing connections in the main thread cannot reach it.
    yield
    with django_db_blocker.unblock():
        asyncio.run(sync_to_async(connections.close_all)())


@pytest.fixture
def people(db, settings):
    settings.ALLOWED_TELEGRAM_IDS = (1001, 1002)
    return (Person.objects.create(telegram_id=1001,chat_id=1001,name='Борис'),Person.objects.create(telegram_id=1002,chat_id=1002,name='Анна'))
