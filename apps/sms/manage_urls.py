"""
SMS-API:t i panelen. Inkluderas i apps/manage/urls.py utan eget namnrum, så
att namnen blir manage:sms_... som resten av panelen.
"""

from django.urls import path

from . import manage_views as v

urlpatterns = [
    path("sms/", v.overview, name="sms_overview"),
    path("sms/stang-manad/", v.close_month, name="sms_close_month"),
    path("sms/avstam/<int:pk>/", v.resolve_check, name="sms_resolve_check"),
    path(
        "sms/underlag/<int:year>-<int:month>.csv",
        v.statements_csv,
        name="sms_statements_csv",
    ),
    path("kunder/<int:pk>/sms/", v.customer_update, name="sms_customer_update"),
    path("kunder/<int:pk>/sms/visa/", v.view_as, name="sms_view_as"),
    path("kunder/<int:pk>/sms/nycklar/", v.keys, name="sms_keys"),
    path(
        "kunder/<int:pk>/sms/nycklar/<int:key_pk>/aterkalla/",
        v.key_revoke,
        name="sms_key_revoke",
    ),
]
