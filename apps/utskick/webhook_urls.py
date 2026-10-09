"""
Adresser som andra tjänster anropar, under /api/utskick/ (config/urls.py,
README C.3 och G.1). Namnrymd utskick_api. nginx loggar inte /api/utskick/
(hemligheten står i adressen, C.5), och Sentry varken spårar eller visar den.
"""

from django.urls import path

from .inbound import elks

app_name = "utskick_api"

urlpatterns = [
    path("46elks/inkommande/<str:token>/", elks.inbound, name="elks_inbound"),
]
