"""SMS-API:t under /api/sms/ (config/urls.py). Snedstrecket sist är valfritt
för kundens anrop: ett API-anrop ska inte bli en omdirigering (POST
överlever inte den). Leveransadressen byggs av oss och är exakt."""

from django.urls import re_path

from . import api

app_name = "sms_api"

urlpatterns = [
    re_path(r"^v1/messages/?$", api.messages, name="messages"),
    re_path(r"^v1/messages/(?P<pk>\d+)/?$", api.message_detail, name="message"),
    re_path(r"^v1/usage/?$", api.usage, name="usage"),
    re_path(r"^v1/senders/?$", api.senders, name="senders"),
    re_path(
        r"^46elks/dlr/(?P<pk>\d+)/(?P<signature>[0-9a-f]{32})/$",
        api.dlr,
        name="dlr",
    ),
]
