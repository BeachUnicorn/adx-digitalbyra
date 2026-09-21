from django.urls import path

from . import views

app_name = "aidocs"

urlpatterns = [
    path("aiz/", views.handshake, name="handshake"),
    path("aiz/guide/", views.guide_index, name="index"),
    path("aiz/guide/<slug:slug>/", views.guide, name="guide"),
]
