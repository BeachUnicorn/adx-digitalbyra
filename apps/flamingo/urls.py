from django.urls import path

from . import views

app_name = "flamingo"

# Ordningen spelar roll: verktyget (app/) före sidornas <slug>/, och sluggen
# "app" är reserverad för Flamingo-sidor (BlockPageForm.RESERVED_SLUGS).
urlpatterns = [
    path("", views.flamingo_page, name="home"),
    path("app/", views.app_home, name="app"),
    path("<slug:slug>/", views.flamingo_page, name="page"),
]
