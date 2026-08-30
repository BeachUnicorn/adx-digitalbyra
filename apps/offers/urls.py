"""Publika offertadresser. Token-länken är kundens enda väg in."""

from django.urls import path

from . import public_views

app_name = "offers"

urlpatterns = [
    path("offert/<str:token>/", public_views.offer_public, name="public"),
    path("offert/<str:token>/acceptera/", public_views.offer_accept, name="accept"),
    path("offert/<str:token>/fraga/", public_views.offer_question, name="question"),
]
