from django.urls import path

from . import views

app_name = "inquiries"

urlpatterns = [
    path("", views.inquiry_submit, name="submit"),
    path("tack/<str:reference>/", views.inquiry_thank_you, name="thank_you"),
]
