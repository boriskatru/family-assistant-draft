import os
from pathlib import Path
from urllib.parse import urlparse, unquote

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv('DEBUG','0') == '1'
TESTING = bool(os.getenv('SQLITE_PATH'))
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY','')
if not SECRET_KEY:
    if DEBUG or TESTING:
        SECRET_KEY = 'development-only-do-not-deploy-this-key'
    else:
        raise RuntimeError('Set DJANGO_SECRET_KEY; use scripts/generate_env.py')
if not (DEBUG or TESTING) and len(SECRET_KEY)<32:
    raise RuntimeError('DJANGO_SECRET_KEY must contain at least 32 characters')
ALLOWED_HOSTS = [h.strip() for h in os.getenv('ALLOWED_HOSTS','localhost,127.0.0.1').split(',') if h.strip()]
INSTALLED_APPS = ['django.contrib.contenttypes','assistant']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware','django.middleware.common.CommonMiddleware']
ROOT_URLCONF='config.urls'
WSGI_APPLICATION='config.wsgi.application'
TIME_ZONE=os.getenv('TIME_ZONE','Europe/Moscow')
USE_TZ=True
LANGUAGE_CODE='ru'
DEFAULT_AUTO_FIELD='django.db.models.BigAutoField'
MEDIA_ROOT=Path(os.getenv('MEDIA_ROOT',str(BASE_DIR/'data'/'media')))
MAX_UPLOAD_MB=int(os.getenv('MAX_UPLOAD_MB','15'))
DATA_UPLOAD_MAX_MEMORY_SIZE=MAX_UPLOAD_MB*1024*1024
FILE_UPLOAD_MAX_MEMORY_SIZE=DATA_UPLOAD_MAX_MEMORY_SIZE
DATABASE_URL=os.getenv('DATABASE_URL','')
if DATABASE_URL:
    d=urlparse(DATABASE_URL)
    if d.scheme not in ('postgres','postgresql'):
        raise RuntimeError('Only PostgreSQL DATABASE_URL is supported')
    DATABASES={'default':{'ENGINE':'django.db.backends.postgresql','NAME':d.path.lstrip('/'),'USER':unquote(d.username or ''),'PASSWORD':unquote(d.password or ''),'HOST':d.hostname,'PORT':d.port or 5432,'CONN_MAX_AGE':60}}
else:
    if not (DEBUG or TESTING):
        raise RuntimeError('Production requires DATABASE_URL (PostgreSQL)')
    DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':os.getenv('SQLITE_PATH',str(BASE_DIR/'data'/'dev.sqlite3')),'OPTIONS':{'timeout':30}}}
    if DATABASES['default']['NAME']!=':memory:':
        Path(DATABASES['default']['NAME']).parent.mkdir(parents=True,exist_ok=True)
TELEGRAM_BOT_TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','')
ALLOWED_TELEGRAM_IDS=tuple(int(x.strip()) for x in os.getenv('ALLOWED_TELEGRAM_IDS','').split(',') if x.strip())
SHARED_CHAT_ID=int(os.getenv('SHARED_CHAT_ID','0') or 0)
TELEGRAM_GROUP_ID=SHARED_CHAT_ID
TELEGRAM_WEBHOOK_SECRET=os.getenv('TELEGRAM_WEBHOOK_SECRET','')
TOKEN_ENCRYPTION_KEY=os.getenv('TOKEN_ENCRYPTION_KEY','')
PUBLIC_BASE_URL=os.getenv('PUBLIC_BASE_URL','').rstrip('/')
GOOGLE_CLIENT_ID=os.getenv('GOOGLE_CLIENT_ID','')
GOOGLE_CLIENT_SECRET=os.getenv('GOOGLE_CLIENT_SECRET','')
if GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET:
    from cryptography.fernet import Fernet
    try:
        Fernet(TOKEN_ENCRYPTION_KEY.encode())
    except (ValueError, TypeError) as exc:
        raise RuntimeError('Google integration requires a valid TOKEN_ENCRYPTION_KEY; use scripts/generate_env.py') from exc
GOOGLE_REDIRECT_URI=os.getenv('GOOGLE_REDIRECT_URI',PUBLIC_BASE_URL+'/oauth/google/callback/')
LLM_API_KEY=os.getenv('LLM_API_KEY','')
LLM_MODEL=os.getenv('LLM_MODEL','')
LLM_BASE_URL=os.getenv('LLM_BASE_URL','https://api.openai.com/v1').rstrip('/')
LLM_TRANSCRIPTION_MODEL=os.getenv('LLM_TRANSCRIPTION_MODEL','')
LLM_DOCUMENT_MODEL=os.getenv('LLM_DOCUMENT_MODEL','')
LLM_WEB_SEARCH_MODEL=os.getenv('LLM_WEB_SEARCH_MODEL','')
LLM_REASONING_EFFORT = os.getenv('LLM_REASONING_EFFORT', '').strip()
if LLM_REASONING_EFFORT not in ('', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
    raise RuntimeError('Invalid LLM_REASONING_EFFORT. Use a supported reasoning effort or leave it empty.')
LLM_PROMPT_CACHE_TTL = os.getenv('LLM_PROMPT_CACHE_TTL', '30m').strip()
if LLM_PROMPT_CACHE_TTL not in ('30m', '12h'):
    raise RuntimeError('LLM_PROMPT_CACHE_TTL must be 30m or 12h')
BRAVE_SEARCH_API_KEY=os.getenv('BRAVE_SEARCH_API_KEY','')
GOOGLE_MAPS_API_KEY=os.getenv('GOOGLE_MAPS_API_KEY','')
AGENT_ENABLED=os.getenv('AGENT_ENABLED','1')=='1'
AGENT_AUTONOMY=os.getenv('AGENT_AUTONOMY','personal')
if AGENT_AUTONOMY not in ('personal','read_only'):
    raise RuntimeError('AGENT_AUTONOMY must be personal or read_only')


def _positive_integer(name, default):
    try:
        value = int(os.getenv(name, default))
    except ValueError:
        raise RuntimeError(f'{name} must be a positive integer') from None
    if value <= 0:
        raise RuntimeError(f'{name} must be a positive integer')
    return value


AGENT_MAX_TURNS = _positive_integer('AGENT_MAX_TURNS', 12)
AGENT_MAX_TOOL_CALLS = _positive_integer('AGENT_MAX_TOOL_CALLS', 24)
AGENT_MAX_EXTERNAL_CALLS = _positive_integer('AGENT_MAX_EXTERNAL_CALLS', 8)
AGENT_TIMEOUT_SECONDS = _positive_integer('AGENT_TIMEOUT_SECONDS', 180)
AGENT_MAX_OUTPUT_TOKENS = _positive_integer('AGENT_MAX_OUTPUT_TOKENS', 2000)
AGENT_MAX_TOTAL_TOKENS = _positive_integer('AGENT_MAX_TOTAL_TOKENS', 80000)
LLM_WEB_SEARCH_MAX_OUTPUT_TOKENS = _positive_integer('LLM_WEB_SEARCH_MAX_OUTPUT_TOKENS', 1800)
LLM_STRUCTURED_MAX_OUTPUT_TOKENS = _positive_integer('LLM_STRUCTURED_MAX_OUTPUT_TOKENS', 4000)
AGENT_HISTORY_USER_LIMIT = _positive_integer('AGENT_HISTORY_USER_LIMIT', 50)
if AGENT_HISTORY_USER_LIMIT > 50:
    raise RuntimeError('AGENT_HISTORY_USER_LIMIT must be between 1 and 50')
# Empty disables the optional server-managed family profile. Missing/empty
# files also leave the built-in instructions unchanged.
PERSONAL_CONTEXT_FILE = os.getenv('PERSONAL_CONTEXT_FILE', str(BASE_DIR/'private'/'personal_context.md')).strip()
PERSONAL_CONTEXT_MAX_CHARS = _positive_integer('PERSONAL_CONTEXT_MAX_CHARS', 20000)
PERSONAL_CONTEXT_IN_GROUPS = os.getenv('PERSONAL_CONTEXT_IN_GROUPS', '0') == '1'
SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO','https')
SECURE_CONTENT_TYPE_NOSNIFF=True
X_FRAME_OPTIONS='DENY'
LOGGING={'version':1,'disable_existing_loggers':False,'handlers':{'console':{'class':'logging.StreamHandler'}},'root':{'handlers':['console'],'level':'INFO'},'loggers':{'httpx':{'level':'WARNING'},'httpcore':{'level':'WARNING'}}}
