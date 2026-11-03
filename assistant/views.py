import json
import secrets
from django.conf import settings
from django.db import transaction,connection
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from .models import IncomingUpdate
from .queue import enqueue


def health(request):
    try:
        with connection.cursor() as cur:cur.execute('SELECT 1')
        return JsonResponse({'status':'ok'})
    except Exception:return JsonResponse({'status':'unhealthy'},status=503)


@csrf_exempt
@require_POST
def telegram_webhook(request):
    expected=settings.TELEGRAM_WEBHOOK_SECRET
    provided=request.headers.get('X-Telegram-Bot-Api-Secret-Token','')
    if not expected or not secrets.compare_digest(expected,provided):return JsonResponse({'error':'forbidden'},status=403)
    if len(request.body)>1048576:return JsonResponse({'error':'too large'},status=413)
    try:
        payload=json.loads(request.body);update_id=payload['update_id']
        if not isinstance(update_id,int):raise ValueError()
    except (ValueError,KeyError,TypeError):return JsonResponse({'error':'invalid update'},status=400)
    with transaction.atomic():
        IncomingUpdate.objects.get_or_create(update_id=update_id,defaults={'payload':payload})
        enqueue('telegram_update',{'update_id':update_id},f'update:{update_id}',queue='chat')
    return JsonResponse({'ok':True})


def google_callback(request):
    from .google_views import oauth_callback
    return oauth_callback(request)
