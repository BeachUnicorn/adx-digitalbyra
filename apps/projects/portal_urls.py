from django.urls import path

from apps.monitor import portal_views as monitor

from . import portal_views as v

app_name = "portal"

urlpatterns = [
    path("", v.landing, name="landing"),
    path("logga-in/", v.login_view, name="login"),
    path("kod/", v.code_view, name="code"),
    path("logga-ut/", v.PortalLogoutView.as_view(), name="logout"),
    path("tavla/", v.home, name="home"),
    path("logg/", v.log, name="log"),
    path("status/", monitor.status, name="status"),
    path("rapporter/", monitor.reports, name="reports"),
    path("rapport/<int:year>/<int:month>/", monitor.report, name="report"),
    path("lamna-kundvyn/", v.leave_view_as, name="leave_view_as"),
    path("arenden/nytt/", v.issue_create, name="issue_create"),
    path("arenden/<int:pk>/", v.issue_detail, name="issue_detail"),
    path("bilagor/<int:pk>/", v.attachment_download, name="attachment"),
]
