from django.urls import path

from . import views
from .app_views import campaigns, inbox, onboarding, overview

app_name = "flamingo"

# Ordningen spelar roll: verktyget (app/...) före sidornas <slug>/, och sluggen
# "app" är reserverad för Flamingo-sidor (BlockPageForm.RESERVED_SLUGS).
urlpatterns = [
    path("", views.flamingo_page, name="home"),
    # Verktyget (app_views/). Varje vy filtrerar på request.flamingo.customer.
    path("app/", overview.overview, name="app"),
    path("app/kund/", overview.choose_customer, name="app_customer"),
    path("app/forslag/", onboarding.proposal, name="app_proposal"),
    path("app/foretaget/", onboarding.business, name="app_business"),
    path("app/google/", onboarding.google, name="app_google"),
    path("app/installningar/", onboarding.settings_view, name="app_settings"),
    path("app/kampanjer/", campaigns.campaign_list, name="app_campaigns"),
    path("app/kampanjer/ny/", campaigns.campaign_new, name="app_campaign_new"),
    path("app/kampanjer/<int:pk>/", campaigns.campaign_detail, name="app_campaign"),
    path("app/kampanjer/<int:pk>/skicka/", campaigns.campaign_submit, name="app_campaign_submit"),
    path(
        "app/kampanjer/<int:pk>/godkann/",
        campaigns.campaign_approve,
        name="app_campaign_approve",
    ),
    path("app/inkorg/", inbox.lead_list, name="app_inbox"),
    path("app/inkorg/<int:pk>/", inbox.lead_detail, name="app_lead"),
    path("<slug:slug>/", views.flamingo_page, name="page"),
]
