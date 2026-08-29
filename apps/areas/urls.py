from django.urls import path

from . import views

app_name = "areas"

urlpatterns = [
    path("webbyra/", views.area_list, name="area_list"),
    path("webbyra/<slug:slug>/", views.area_detail, name="area_detail"),
    # Gamla adressen. Ortssidorna låg på /digitalbyra/ fram till 2026-08-29,
    # då sökordet byttes till "webbyrå" (ingen söker på "digitalbyrå").
    # 301 och inte borttagning: adresserna kan redan vara indexerade eller
    # länkade, och en permanent redirect flyttar över det som fanns.
    path("digitalbyra/", views.area_list_legacy),
    path("digitalbyra/<slug:slug>/", views.area_detail_legacy),
]
