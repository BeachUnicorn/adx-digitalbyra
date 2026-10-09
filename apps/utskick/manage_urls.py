"""
Utskick i panelen. Inkluderas i apps/manage/urls.py utan eget namnrum, så
att namnen blir manage:utskick_... som resten av panelen (som apps.sms).
"""

from django.urls import path

from . import manage_views as v

urlpatterns = [
    path("utskick/", v.overview, name="utskick_overview"),
    path("utskick/nodstopp/", v.switch, name="utskick_switch"),
    path("utskick/avtal/", v.dpa_publish, name="utskick_dpa_publish"),
    path("kunder/<int:pk>/utskick/", v.customer_update, name="utskick_customer_update"),
    path("kunder/<int:pk>/utskick/radera/", v.customer_end, name="utskick_customer_end"),
]
