"""
Verktygets adresser för Kontakter (S1) och Utskick (S2 och senare), under
/flamingo/app/. Inkluderas i apps/flamingo/urls.py som
path("app/", include("apps.utskick.app_urls")) före sidornas <slug>/, utan
eget app_name: namnen blir flamingo:app_contacts och så vidare (README I.1).

Varje vy går via access.utskick_view (404 när utskick är av för kontot).
"""

from django.urls import path

from .app_views import contacts, dpa, fields, imports, lists, signup
from .app_views import settings as settings_views

urlpatterns = [
    # Kontakter (app_views/contacts.py)
    path("kontakter/", contacts.contact_list, name="app_contacts"),
    path("kontakter/massandring/", contacts.contacts_bulk, name="app_contacts_bulk"),
    path("kontakter/ny/", contacts.contact_new, name="app_contact_new"),
    path("kontakter/export/", contacts.contacts_export, name="app_contacts_export"),
    path("kontakter/rensa/", contacts.contacts_prune, name="app_contacts_prune"),
    path("kontakter/<int:pk>/", contacts.contact_detail, name="app_contact"),
    path("kontakter/<int:pk>/andra/", contacts.contact_edit, name="app_contact_edit"),
    path("kontakter/<int:pk>/samtycke/", contacts.contact_consent, name="app_contact_consent"),
    path("kontakter/<int:pk>/export/", contacts.contact_export, name="app_contact_export"),
    path("kontakter/<int:pk>/ta-bort/", contacts.contact_delete, name="app_contact_delete"),
    # Importen (app_views/imports.py)
    path("kontakter/import/", imports.import_upload, name="app_import"),
    path("kontakter/import/<int:pk>/", imports.import_job, name="app_import_job"),
    path("kontakter/import/<int:pk>/fel.csv", imports.import_errors, name="app_import_errors"),
    # Listor och taggar (app_views/lists.py)
    path("kontakter/listor/", lists.list_index, name="app_lists"),
    path("kontakter/listor/<int:pk>/", lists.list_detail, name="app_list"),
    # Extrafält, anmälan, inställningar och biträdesavtalet
    path("kontakter/falt/", fields.field_list, name="app_fields"),
    path("kontakter/anmalan/", signup.signup_settings, name="app_signup"),
    path(
        "kontakter/installningar/",
        settings_views.contacts_settings,
        name="app_contacts_settings",
    ),
    path("kontakter/avtal/", dpa.dpa, name="app_dpa"),
]
