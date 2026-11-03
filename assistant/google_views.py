from django.http import HttpResponse
from django.views.decorators.http import require_GET
from assistant.integrations.google import complete_oauth, GoogleIntegrationError


@require_GET
def oauth_callback(request):
    if request.GET.get('error'):
        return HttpResponse('Google не подключён. Вернитесь в Telegram и выполните /google.', status=400, content_type='text/plain; charset=utf-8')
    try:
        complete_oauth(request.GET.get('state', ''), request.GET.get('code', ''))
    except GoogleIntegrationError as exc:
        return HttpResponse(str(exc), status=400, content_type='text/plain; charset=utf-8')
    response = HttpResponse('Google подключён. Вернитесь в Telegram: /calendars — список. '
        'Выбрать личный календарь для чтения и записи: /calendar ID | personal | rw; '
        'только для чтения: /calendar ID | personal | ro. Затем выполните /sync.',
        content_type='text/plain; charset=utf-8')
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response
