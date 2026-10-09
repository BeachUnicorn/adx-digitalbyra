"""
Utskick i panelen. Inkluderas i apps/manage/urls.py utan eget namnrum, så
att namnen blir manage:utskick_... som resten av panelen (som apps.sms).

S2: en modul per byggare (S2-HANDOFF.md): manage_inbound.py (inkommande
sms), manage_links.py (länkvärdarna), manage_sending.py (undantag och
provsms).
"""

from django.urls import path

from . import manage_inbound, manage_links, manage_sending
from . import manage_views as v

urlpatterns = [
    path("utskick/", v.overview, name="utskick_overview"),
    path("utskick/nodstopp/", v.switch, name="utskick_switch"),
    path("utskick/avtal/", v.dpa_publish, name="utskick_dpa_publish"),
    path("kunder/<int:pk>/utskick/", v.customer_update, name="utskick_customer_update"),
    path("kunder/<int:pk>/utskick/radera/", v.customer_end, name="utskick_customer_end"),
    # S2
    path(
        "utskick/inkommande/<int:pk>/",
        manage_inbound.inbound_route,
        name="utskick_inbound_route",
    ),
    path("utskick/vardar/<int:pk>/", manage_links.host_decide, name="utskick_host_decide"),
    path(
        "utskick/utskick/<int:pk>/undantag/",
        manage_sending.info_override,
        name="utskick_info_override",
    ),
    path("utskick/prov/", manage_sending.probe, name="utskick_probe"),
]
