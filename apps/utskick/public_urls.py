"""
Publika adresser på adx.se under /utskick/ (README I.1, namnrymd
utskick_public). Inkluderas i config/urls.py före sajtens slug-catchall.

bekrafta/ och val/ står före <public_slug>/; båda är reserverade och kan
aldrig bli en kunds adress (models.RESERVED_PUBLIC_SLUGS).
"""

from django.urls import path

from . import public_views as v

app_name = "utskick_public"

urlpatterns = [
    path("bekrafta/<str:token>/", v.confirm, name="confirm"),
    path("val/<str:token>/", v.preferences, name="preferences"),
    path("<slug:public_slug>/", v.signup, name="signup"),
    path("<slug:public_slug>/tack/", v.signup_thanks, name="signup_thanks"),
    path("<slug:public_slug>/integritet/", v.privacy, name="privacy"),
]
