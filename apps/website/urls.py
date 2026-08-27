from django.urls import path

from . import views

app_name = "website"

urlpatterns = [
    path("", views.homepage, name="homepage"),
    path("<slug:slug>/", views.page_detail, name="page_detail"),
]
