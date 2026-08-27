from django.urls import path

from . import views

app_name = "areas"

urlpatterns = [
    path("digitalbyra/", views.area_list, name="area_list"),
    path("digitalbyra/<slug:slug>/", views.area_detail, name="area_detail"),
]
