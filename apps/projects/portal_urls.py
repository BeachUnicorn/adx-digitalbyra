from django.urls import path

from . import portal_views as v

app_name = "portal"

urlpatterns = [
    path("", v.landing, name="landing"),
    path("logga-in/", v.PortalLoginView.as_view(), name="login"),
    path("logga-ut/", v.PortalLogoutView.as_view(), name="logout"),
    path("tavla/", v.home, name="home"),
    path("arenden/nytt/", v.issue_create, name="issue_create"),
    path("arenden/<int:pk>/", v.issue_detail, name="issue_detail"),
    path("bilagor/<int:pk>/", v.attachment_download, name="attachment"),
    path("glomt-losenord/", v.PortalPasswordResetView.as_view(), name="password_reset"),
    path(
        "glomt-losenord/skickat/",
        v.PortalPasswordResetDoneView.as_view(),
        name="password_reset_done",
    ),
    path(
        "aterstall/<uidb64>/<token>/",
        v.PortalPasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "aterstall/klart/",
        v.PortalPasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),
]
