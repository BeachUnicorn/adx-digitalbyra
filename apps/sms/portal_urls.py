"""Kundportalens SMS-sidor under /kund/sms/ (config/urls.py)."""

from django.urls import path

from . import portal_views as v

app_name = "sms"

urlpatterns = [
    path("", v.dashboard, name="dashboard"),
    path("nycklar/", v.keys, name="keys"),
    path("nycklar/ny/", v.key_create, name="key_create"),
    path("nycklar/<int:pk>/aterkalla/", v.key_revoke, name="key_revoke"),
    path("tak/", v.cap_update, name="cap_update"),
    path("dokumentation/", v.docs, name="docs"),
    path("underlag/", v.statements, name="statements"),
]
