from django.urls import path

from . import views
from .app_views import campaigns, inbox, onboarding, overview, page_ai, pages
from .app_views import media as media_views
from .app_views import reviews as reviews_views

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
    # Sidbyggaren: kontots landningssidor (app_views/pages.py).
    path("app/sidor/", pages.page_list, name="app_pages"),
    path("app/sidor/ny/", pages.page_new, name="app_page_new"),
    path("app/sidor/<int:pk>/", pages.page_detail, name="app_page"),
    path("app/sidor/<int:pk>/spara/", pages.page_save, name="app_page_save"),
    path("app/sidor/<int:pk>/rita/", pages.page_render_block, name="app_page_render_block"),
    path("app/sidor/<int:pk>/nytt-block/", pages.page_block_new, name="app_page_block_new"),
    path("app/sidor/<int:pk>/installningar/", pages.page_settings, name="app_page_settings"),
    path("app/sidor/<int:pk>/kopiera/", pages.page_copy, name="app_page_copy"),
    path("app/sidor/<int:pk>/ta-bort/", pages.page_delete, name="app_page_delete"),
    path("app/sidor/<int:pk>/publicera/", pages.page_publish, name="app_page_publish"),
    # AI och Konverteringskollen i sidbyggaren (app_views/page_ai.py), JSON.
    path("app/sidor/<int:pk>/ai/bygg/", page_ai.page_ai_build, name="app_page_ai_build"),
    path("app/sidor/<int:pk>/ai/skriv-om/", page_ai.page_ai_rewrite, name="app_page_ai_rewrite"),
    path("app/sidor/<int:pk>/konverteringskoll/", page_ai.page_koll, name="app_page_koll"),
    # Mediaarkivet (app_views/media.py) och omdömena från Google (app_views/reviews.py).
    path("app/media/", media_views.media_archive, name="app_media"),
    path("app/media/lista/", media_views.media_json, name="app_media_json"),
    path("app/media/ladda-upp/", media_views.media_upload, name="app_media_upload"),
    path("app/omdomen/", reviews_views.reviews_view, name="app_reviews"),
    path("app/inkorg/", inbox.lead_list, name="app_inbox"),
    path("app/inkorg/<int:pk>/", inbox.lead_detail, name="app_lead"),
    path("<slug:slug>/", views.flamingo_page, name="page"),
]
