"""Kundens landningssidor, monterade på /lp/ i config/urls.py (före sajtens
slug-catchall).

Kampanjens adress (/lp/<kampanj>/ med tack/, ring/ och besok/) först, sedan
sidans egen adress (/lp/<konto>/<sida>/, README Steg 2). Ordningen spelar
roll: /lp/<x>/tack/ är alltid kampanjens tacksida, och en sida får därför
aldrig adressen tack, ring eller besok (addresses.RESERVED_PAGE_SLUGS)."""

from django.urls import path

from . import public_views

app_name = "flamingo_public"

urlpatterns = [
    path("<slug:slug>/", public_views.landing, name="landing"),
    path("<slug:slug>/tack/", public_views.thanks, name="thanks"),
    path("<slug:slug>/ring/", public_views.call_click, name="call_click"),
    # Tiden på sidan för besök från ett utskick (apps/utskick, E.4). Ingen
    # kaka; ut-token i kroppen. Sentry spårar inte adresser som slutar på /besok/.
    path("<slug:slug>/besok/", public_views.visit_beacon, name="visit_beacon"),
    # Sidans egen adress (README Steg 2).
    path("<slug:account>/<slug:page>/", public_views.page_landing, name="page"),
    path("<slug:account>/<slug:page>/tack/", public_views.page_thanks, name="page_thanks"),
    path("<slug:account>/<slug:page>/ring/", public_views.page_call_click, name="page_call_click"),
    path(
        "<slug:account>/<slug:page>/besok/",
        public_views.page_visit_beacon,
        name="page_visit_beacon",
    ),
]
