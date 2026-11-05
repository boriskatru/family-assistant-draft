"""Best-effort, private operational alerts containing no request or document data."""
import hashlib
import json
import logging
import re
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parent.parent

# Provider text is untrusted and may quote prompts, credentials or tool arguments.
# Translate only recognised machine-readable codes; never echo the full message.
_REASONS = {
    'invalid_function_parameters': 'API отклонил JSON-схему инструмента. Нужно проверить описание его параметров.',
    'invalid_json_schema': 'API отклонил JSON-схему ответа или инструмента.',
    'model_not_found': 'Модель не найдена либо недоступна этому API-ключу. Проверьте LLM_MODEL и LLM_BASE_URL.',
    'invalid_api_key': 'API отклонил ключ. Проверьте LLM_API_KEY и выбранного провайдера.',
    'insufficient_quota': 'У провайдера закончилась доступная квота. Проверьте баланс и ограничения проекта.',
    'rate_limit_exceeded': 'Провайдер ограничил частоту запросов. Повторите запрос позже.',
    'context_length_exceeded': 'Запрос превышает контекст модели. Сократите историю или размер запроса.',
    'unsupported_parameter': 'Модель не поддерживает один из переданных параметров.',
    'unsupported_value': 'Модель не поддерживает выбранное значение параметра.',
    'invalid_request_error': 'API отклонил параметры запроса; проверьте модель и её настройки.',
    'invalid_value': 'API отклонил значение одного из параметров запроса.',
    'invalid_type': 'API отклонил тип одного из параметров запроса.',
    'permission_denied': 'API отказал в доступе. Проверьте разрешения ключа и проекта.',
    'account_deactivated': 'Учётная запись провайдера недоступна.',
    'server_error': 'Внутренняя ошибка провайдера. Повторите запрос позже.',
}
_STATUS_REASONS = {
    400: 'API отклонил параметры запроса.',
    401: 'API отклонил авторизацию. Проверьте ключ нужного сервиса.',
    403: 'Сервис отказал в доступе.',
    404: 'Сервис или модель не найдены. Проверьте адрес API и имя модели.',
    409: 'Конфликт запроса. Для Telegram проверьте, не запущен ли второй экземпляр polling.',
    429: 'Сервис ограничил запросы или исчерпана квота.',
}
_TYPE_REASONS = {
    'APITimeoutError': 'Истекло время ожидания API.',
    'TimeoutError': 'Истекло время выполнения операции.',
    'ReadTimeout': 'Истекло время ожидания ответа сервиса.',
    'ConnectTimeout': 'Не удалось подключиться к сервису за отведённое время.',
    'APIConnectionError': 'Не удалось подключиться к API. Проверьте сеть и LLM_BASE_URL.',
    'ConnectError': 'Не удалось подключиться к внешнему сервису.',
    'AuthenticationError': _STATUS_REASONS[401],
    'RateLimitError': _STATUS_REASONS[429],
    'TelegramConflictError': _STATUS_REASONS[409],
    'TelegramUnauthorizedError': 'Telegram отклонил токен бота. Проверьте TELEGRAM_BOT_TOKEN.',
    'TelegramForbiddenError': 'Telegram запретил отправку: проверьте, не заблокирован ли бот и есть ли у него доступ к чату.',
    'TelegramNetworkError': 'Не удалось подключиться к Telegram. Проверьте сеть сервера.',
    'TelegramRetryAfter': 'Telegram ограничил частоту запросов. Отправка будет повторена позже.',
    'PersonalContextError': 'Не удалось загрузить персональный контекст: проверьте UTF-8, права файла и PERSONAL_CONTEXT_MAX_CHARS.',
    'PydanticSerializationError': 'Не удалось сериализовать данные. Нужна проверка версии библиотек и кода сериализации.',
    'ValidationError': 'Данные не прошли проверку формата.',
    'ModelBehaviorError': 'Ответ модели не соответствует ожидаемому формату.',
    'UserError': 'Не удалось подготовить или выполнить инструмент агента.',
    'OperationalError': 'Ошибка подключения или операции с базой данных. Проверьте PostgreSQL и DATABASE_URL.',
    'ProgrammingError': 'Ошибка запроса к базе данных. Проверьте применённые миграции и версии приложения.',
    'MaxTurnsExceeded': 'Агент достиг AGENT_MAX_TURNS. Часть действий могла сохраниться.',
    'AgentBudgetExceeded': 'Агент достиг лимита токенов. Часть действий могла сохраниться.',
    'AgentLimitExceeded': 'Агент достиг лимита вызовов инструментов. Часть действий могла сохраниться.',
}
_TOOL_NAMES = {'save_place', 'save_contact', 'search_addressbook', 'read_addressbook',
               'set_place_alias', 'route_places', 'create_event', 'update_event',
               'add_trip_segment', 'create_trip', 'create_task', 'set_checklist_item'}
_PARAMS = {'model', 'input', 'max_output_tokens', 'reasoning', 'reasoning.effort',
           'temperature', 'top_p', 'parallel_tool_calls', 'store',
           'prompt_cache_options', 'prompt_cache_options.ttl', 'prompt_cache_key'}


def _exception_chain(exc):
    seen = set()
    while isinstance(exc, BaseException) and id(exc) not in seen and len(seen) < 8:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _safe_class(exc):
    name = type(exc).__name__
    return name if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}', name) else 'Exception'


def _safe_stage(stage):
    return stage if isinstance(stage, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:-]{0,63}', stage) else 'application'


def _safe_uuid(value):
    try:
        return str(uuid.UUID(str(value))) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _project_frames(chain):
    frames = []
    for exc in chain:
        trace = exc.__traceback__
        while trace:
            code = trace.tb_frame.f_code
            try:
                file = Path(code.co_filename).resolve().relative_to(_ROOT).as_posix()
            except (ValueError, OSError):
                file = ''
            if file.startswith(('assistant/', 'config/')) and re.fullmatch(r'[A-Za-z0-9_./-]+\.py', file):
                # Only paths and line numbers from application code. No source,
                # locals or absolute server paths are included.
                frame = f'{file}:{trace.tb_lineno}'
                if frame not in frames:
                    frames.append(frame)
            trace = trace.tb_next
    return frames[-4:]


def safe_error_details(exc, stage):
    """Return bounded, allowlisted technical metadata; never raw exception text."""
    chain = list(_exception_chain(exc)) or [RuntimeError()]
    details = {'stage': _safe_stage(stage), 'type': _safe_class(chain[0])}
    for error in chain:
        status = getattr(error, 'status_code', None)
        if type(status) is int and 400 <= status <= 599 and 'status' not in details:
            details['status'] = status
        code = getattr(error, 'code', None)
        if isinstance(code, str) and code in _REASONS and 'code' not in details:
            details['code'] = code
        param = getattr(error, 'param', None)
        if isinstance(param, str) and (param in _PARAMS or re.fullmatch(r'tools(?:\[\d{1,3}\])?(?:\.parameters)?', param)):
            details.setdefault('param', param)
        # A tool name is useful for schema rejection, but the rest of the API
        # message can contain user input. Only fixed, code-owned names survive.
        if details.get('code') in ('invalid_function_parameters', 'invalid_json_schema'):
            message = getattr(error, 'message', '')
            if isinstance(message, str):
                match = re.search(r"(?:function|tool) ['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]", message[:1000])
                if match and match.group(1) in _TOOL_NAMES:
                    details.setdefault('tool', match.group(1))
    details['reason'] = _REASONS.get(details.get('code')) or next(
        (_TYPE_REASONS[_safe_class(error)] for error in chain if _safe_class(error) in _TYPE_REASONS),
        _STATUS_REASONS.get(details.get('status'),
            'Временная ошибка сервиса.' if details.get('status', 0) >= 500 else
            'Внутренняя ошибка приложения. Для диагностики используйте код ошибки и место в коде.'))
    details['frames'] = _project_frames(chain)
    return details


def _admin_id():
    admin_id = getattr(settings, 'ERROR_ADMIN_TELEGRAM_ID', 0)
    if type(admin_id) is not int or admin_id <= 0 or admin_id not in settings.ALLOWED_TELEGRAM_IDS:
        return 0
    return admin_id


def _admin(lock=False):
    from assistant.models import Person
    admin_id = _admin_id()
    if not admin_id:
        return None
    query = Person.objects.filter(telegram_id=admin_id, chat_id=admin_id)
    if lock:
        query = query.select_for_update()
    return query.first()


def may_deliver_alert(payload):
    """Recheck administrator and private destination immediately before send."""
    admin = _admin()
    return bool(admin and type(payload.get('chat_id')) is int and
                payload['chat_id'] == admin.telegram_id and
                str(payload.get('actor_id', '')) == str(admin.pk))


def _text(error_id, details, run_id, job_id):
    lines = [f'Ошибка бота · {error_id}', f"Этап: {details['stage']}", f"Тип: {details['type']}"]
    if details.get('status'):
        lines.append(f"HTTP: {details['status']}")
    for key, label in (('code', 'Код API'), ('param', 'Параметр'), ('tool', 'Инструмент')):
        if key in details:
            lines.append(f'{label}: {details[key]}')
    lines.append(details['reason'])
    if details['frames']:
        lines.append('Код: ' + ' → '.join(details['frames']))
    if run_id:
        lines.append('Запуск: ' + run_id)
    if job_id:
        lines.append('Задание: ' + job_id)
    lines.append('Ключи API, содержимое сообщений и документов в отчёт не включены.')
    return '\n'.join(lines)


def report_error(exc, stage, run_id=None, job_id=None):
    """Log safe diagnostics and enqueue at most one private alert per cooldown interval.

    Never raise from an exception handler, even if the database is unavailable.
    Returns a short correlation ID suitable for the normal user-facing reply.
    """
    error_id = uuid.uuid4().hex[:12]
    try:
        details = safe_error_details(exc, stage)
        run_id, job_id = _safe_uuid(run_id), _safe_uuid(job_id)
        if _admin_id():
            with transaction.atomic():
                admin = _admin(lock=True)
                if admin:
                    from assistant.models import Job
                    from assistant.queue import enqueue
                    cooldown = max(1, int(getattr(settings, 'ERROR_ALERT_COOLDOWN_SECONDS', 600)))
                    fingerprint = hashlib.sha256(json.dumps({key: details.get(key) for key in
                        ('stage', 'type', 'code', 'status', 'param', 'tool', 'frames')}, sort_keys=True).encode()).hexdigest()[:24]
                    prefix = f'error-alert:{admin.telegram_id}:{fingerprint}:'
                    # The administrator row serializes reporters across all worker
                    # processes. A rolling interval avoids bursts at bucket borders.
                    previous = Job.objects.filter(dedup_key__startswith=prefix,
                        created_at__gte=timezone.now()-timedelta(seconds=cooldown)).order_by('-created_at').first()
                    if previous:
                        error_id = previous.payload.get('error_id', error_id)
                    else:
                        payload = {'actor_id': str(admin.pk), 'chat_id': admin.telegram_id,
                                   'text': _text(error_id, details, run_id, job_id),
                                   'error_alert': True, 'error_id': error_id}
                        enqueue('telegram_send', payload, prefix+error_id, queue='notifications')
        logger.error('Bot error id=%s details=%s run=%s job=%s', error_id,
                     json.dumps(details, ensure_ascii=False, sort_keys=True), run_id or '-', job_id or '-')
    except Exception:
        # Do not log the reporting exception: it may include credentials from
        # DATABASE_URL. Preserve only safe original metadata when possible.
        try:
            logger.error('Bot error id=%s details=%s; diagnostic delivery unavailable',
                         error_id, json.dumps(safe_error_details(exc, stage), ensure_ascii=False, sort_keys=True))
        except Exception:
            pass
    return error_id
