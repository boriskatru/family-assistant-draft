from django.urls import path
from assistant import views
urlpatterns=[path('healthz/',views.health),path('telegram/webhook/',views.telegram_webhook),path('oauth/google/callback/',views.google_callback)]
