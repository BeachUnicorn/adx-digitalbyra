from django.urls import path

from . import api

app_name = "api"

urlpatterns = [
    path("state/", api.get_state, name="state"),
    path("timer/stop/", api.timer_stop, name="timer_stop"),
    path("issues/", api.issue_create, name="issue_create"),
    path("issues/<int:pk>/start/", api.issue_start, name="issue_start"),
    path("issues/<int:pk>/done/", api.issue_done, name="issue_done"),
    path("checklist/<int:pk>/", api.checklist_toggle, name="checklist_toggle"),
]
