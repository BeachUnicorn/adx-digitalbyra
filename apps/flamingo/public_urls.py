"""Kundens landningssidor, monterade på /lp/ i config/urls.py (före sajtens
slug-catchall)."""

from django.urls import path

from . import public_views

app_name = "flamingo_public"

urlpatterns = [
    path("<slug:slug>/", public_views.landing, name="landing"),
    path("<slug:slug>/tack/", public_views.thanks, name="thanks"),
    path("<slug:slug>/ring/", public_views.call_click, name="call_click"),
]
