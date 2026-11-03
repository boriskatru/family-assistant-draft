"""Safe local test defaults; DATABASE_URL can select PostgreSQL in CI."""
import os

os.environ.setdefault('SQLITE_PATH', ':memory:')
os.environ.setdefault('DJANGO_SECRET_KEY', 'unit-test-secret-not-for-production')
os.environ.setdefault('ALLOWED_TELEGRAM_IDS', '1001,1002')

from .settings import *  # noqa: F403,F401,E402
