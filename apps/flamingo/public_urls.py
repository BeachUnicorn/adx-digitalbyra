"""Kundens landningssidor, monterade på /lp/ i config/urls.py (före sajtens
slug-catchall)."""

from django.urls import path

from . import public_views

app_name = "flamingo_public"

urlpatterns = [
    path("<slug:slug>/", public_views.landing, name="landing"),
    path("<slug:slug>/tack/", public_views.thanks, name="thanks"),
    path("<slug:slug>/ring/", public_views.call_click, name="call_click"),
    # Tiden på sidan för besök från ett utskick (apps/utskick, E.4). Ingen
    # kaka; ut-token i kroppen. Sentry spårar inte adresser som slutar på /besok/.
    path("<slug:slug>/besok/", public_views.visit_beacon, name="visit_beacon"),
]
