"""
Verktygets adresser för Kontakter (S1) och Utskick (S2 och senare), under
/flamingo/app/. Inkluderas i apps/flamingo/urls.py som
path("app/", include("apps.utskick.app_urls")) före sidornas <slug>/, utan
eget app_name: namnen blir flamingo:app_contacts och så vidare (README I.1).

Varje vy går via access.utskick_view (404 när utskick är av för kontot).

S2 lägger till Utskick (app_views/utskick.py) och svaren i Inkorgen
(app_views/inbox_reply.py: inkorg/<pk>/svara/ och avregistrera/, bredvid
Flamingos egna inkorg/ och inkorg/<pk>/ i apps/flamingo/urls.py).

S3 lägger till e-postredigeraren (app_views/brev.py), Leveranshälsa
(app_views/health.py) och Egen domän med den egna svarsadressen
(app_views/domain.py).

S4 lägger till segmentbyggaren (app_views/segments.py), sms från
kontaktkortet (app_views/contact_sms.py), anmälningssidans QR-kod och
Länkar (app_views/links.py), rapportens Följ upp och Exportera
(app_views/report.py) och Spårningsskript (app_views/snippet.py).
QR-kodernas format står i adressen: qr.svg eller qr.png
(reverse("flamingo:app_link_qr", args=[pk, "svg"])).
"""

from django.urls import path, register_converter

from .app_views import (
    brev,
    contact_sms,
    contacts,
    domain,
    dpa,
    fields,
    health,
    imports,
    inbox_reply,
    links,
    lists,
    report,
    segments,
    signup,
    snippet,
    utskick,
)
from .app_views import settings as settings_views


class _StepConverter:
    """Guidens steg i adressen: mottagare, kanal, innehall, tid, granska."""

    regex = "|".join(key for key, _label in utskick.STEPS)

    def to_python(self, value):
        return value

    def to_url(self, value):
        return value


register_converter(_StepConverter, "utskick_steg")


class _QrFormatConverter:
    """QR-kodens format i adressen (S4): svg eller png (qr.FORMATS)."""

    regex = "svg|png"

    def to_python(self, value):
        return value

    def to_url(self, value):
        return value


register_converter(_QrFormatConverter, "qr_format")

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
    # S4: sms från kontaktkortet (app_views/contact_sms.py)
    path("kontakter/<int:pk>/sms/", contact_sms.contact_sms, name="app_contact_sms"),
    # Importen (app_views/imports.py)
    path("kontakter/import/", imports.import_upload, name="app_import"),
    path("kontakter/import/<int:pk>/", imports.import_job, name="app_import_job"),
    path("kontakter/import/<int:pk>/fel.csv", imports.import_errors, name="app_import_errors"),
    # Listor och taggar (app_views/lists.py)
    path("kontakter/listor/", lists.list_index, name="app_lists"),
    path("kontakter/listor/<int:pk>/", lists.list_detail, name="app_list"),
    # S4: segmentbyggaren (app_views/segments.py), under fliken Listor
    path("kontakter/segment/ny/", segments.segment_new, name="app_segment_new"),
    path("kontakter/segment/antal/", segments.segment_count, name="app_segment_count"),
    path("kontakter/segment/<int:pk>/", segments.segment_detail, name="app_segment"),
    # Extrafält, anmälan, inställningar och biträdesavtalet
    path("kontakter/falt/", fields.field_list, name="app_fields"),
    path("kontakter/anmalan/", signup.signup_settings, name="app_signup"),
    # S4: anmälningssidans QR-kod (app_views/links.py)
    path("kontakter/anmalan/qr.<qr_format:fmt>", links.signup_qr, name="app_signup_qr"),
    path(
        "kontakter/installningar/",
        settings_views.contacts_settings,
        name="app_contacts_settings",
    ),
    path("kontakter/avtal/", dpa.dpa, name="app_dpa"),
    # Utskick (app_views/utskick.py, README I.1 och I.8)
    path("utskick/", utskick.utskick_list, name="app_utskick_list"),
    path("utskick/ny/", utskick.utskick_new, name="app_utskick_new"),
    path(
        "utskick/installningar/",
        utskick.utskick_settings,
        name="app_utskick_settings",
    ),
    # S3: Leveranshälsa, Egen domän och den egna svarsadressen
    path("utskick/halsa/", health.utskick_health, name="app_utskick_health"),
    path("utskick/installningar/doman/", domain.utskick_domain, name="app_utskick_domain"),
    path(
        "utskick/installningar/svarsadress/<str:token>/",
        domain.reply_confirm,
        name="app_utskick_reply_confirm",
    ),
    # S4: Spårningsskript (app_views/snippet.py) och Länkar (app_views/links.py)
    path(
        "utskick/installningar/skript/",
        snippet.snippet_settings,
        name="app_utskick_snippet",
    ),
    path("utskick/lankar/", links.link_list, name="app_links"),
    path("utskick/lankar/ny/", links.link_new, name="app_link_new"),
    path("utskick/lankar/<int:pk>/", links.link_detail, name="app_link"),
    path("utskick/lankar/<int:pk>/qr.<qr_format:fmt>", links.link_qr, name="app_link_qr"),
    path("utskick/<int:pk>/", utskick.utskick_report, name="app_utskick"),
    path(
        "utskick/<int:pk>/steg/<utskick_steg:step>/",
        utskick.utskick_step,
        name="app_utskick_step",
    ),
    path("utskick/<int:pk>/antal/", utskick.utskick_count, name="app_utskick_count"),
    path("utskick/<int:pk>/sms/", utskick.utskick_sms_preview, name="app_utskick_sms_preview"),
    path(
        "utskick/<int:pk>/lankkontroll/",
        utskick.utskick_link_check,
        name="app_utskick_link_check",
    ),
    path("utskick/<int:pk>/test/", utskick.utskick_test, name="app_utskick_test"),
    path("utskick/<int:pk>/skicka/", utskick.utskick_confirm, name="app_utskick_confirm"),
    path("utskick/<int:pk>/lage/", utskick.utskick_state, name="app_utskick_state"),
    path(
        "utskick/<int:pk>/mottagare/",
        utskick.utskick_recipients,
        name="app_utskick_recipients",
    ),
    path(
        "utskick/<int:pk>/mottagare/lista/",
        utskick.utskick_save_list,
        name="app_utskick_save_list",
    ),
    # S4: rapportens Följ upp och Exportera (app_views/report.py)
    path("utskick/<int:pk>/folj-upp/", report.utskick_follow_up, name="app_utskick_follow_up"),
    path("utskick/<int:pk>/export/", report.utskick_export, name="app_utskick_export"),
    # S3: e-postredigeraren (app_views/brev.py, README F och I.1)
    path("utskick/<int:pk>/brev/", brev.brev_editor, name="app_brev"),
    path("utskick/<int:pk>/brev/spara/", brev.brev_save, name="app_brev_save"),
    path("utskick/<int:pk>/brev/rita/", brev.brev_render_block, name="app_brev_render_block"),
    path("utskick/<int:pk>/brev/bild/", brev.brev_image, name="app_brev_image"),
    path("utskick/<int:pk>/brev/kontroller/", brev.brev_checks, name="app_brev_checks"),
    path("utskick/<int:pk>/brev/ai/", brev.brev_ai, name="app_brev_ai"),
    path(
        "utskick/<int:pk>/brev/forhandsvisning/",
        brev.brev_preview,
        name="app_brev_preview",
    ),
    # Svaren i Inkorgen (app_views/inbox_reply.py, README G.2)
    path("inkorg/<int:pk>/svara/", inbox_reply.lead_reply, name="app_lead_reply"),
    path(
        "inkorg/<int:pk>/avregistrera/",
        inbox_reply.lead_unsubscribe,
        name="app_lead_unsubscribe",
    ),
]
