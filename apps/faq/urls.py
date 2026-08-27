from django.urls import path

from . import views

app_name = "faq"

urlpatterns = [
    path("", views.section_list, name="section_list"),
    path("<slug:slug>/", views.section_detail, name="section_detail"),
]
