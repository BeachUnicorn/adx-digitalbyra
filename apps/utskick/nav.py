"""
Menyerna: Kontakter (och från S2 Utskick) i verktygets sidomeny, och
flikraden under rubriken i varje del (README C.2 och I.1a).

    nav_for(account)            APP_NAV med Kontakter och Utskick efter Kampanjer när utskick är på
    contacts_tabs(active)       flikarna i Kontakter, med den aktiva markerad
    utskick_tabs(active)        flikarna i Utskick (S3: Utskick, Leveranshälsa, Inställningar)

APP_NAV i apps/flamingo/app_views/__init__.py är standarden och ändras
inte. Ett konto utan utskick ser exakt den menyn; ett med utskick får
Kontakter och Utskick efter Kampanjer (tio länkar, README I.2). Ingen
"ny"-etikett (README, avvikelser).

Varje vy under kontakter/ sätter app_active till "contacts" (sidomenyn) och
en flik ur CONTACT_TABS (flikraden); varje vy under utskick/ sätter
"utskick" och en flik ur UTSKICK_TABS. Flikraden ritas av
templates/flamingo/app/kontakter/_tabs.html: en .fl-tabs-rad i bred skärm,
under 560 px en <details class="fl-subnav"> med länkarna som lista (inget
JS, inget klipps). Stilen bor i static/css/flamingo-app-utskick.css.
"""

from django.urls import reverse

#: Sidomenyns punkt för Kontakter (S1) och Utskick (S2).
CONTACTS_ITEM = ("contacts", "flamingo:app_contacts", "Kontakter")
UTSKICK_ITEM = ("utskick", "flamingo:app_utskick_list", "Utskick")

#: Flikarna i Utskick (I.1a). Flöden (S5) och Länkar (S4) läggs till i
#: steget som bygger dem, före Leveranshälsa (S3) och Inställningar.
UTSKICK_TABS = (
    ("utskick", "flamingo:app_utskick_list", "Utskick"),
    ("health", "flamingo:app_utskick_health", "Leveranshälsa"),
    ("settings", "flamingo:app_utskick_settings", "Inställningar"),
)

#: Flikarna i Kontakter i ordning: (nyckel, url-namn, rubrik). Segment
#: läggs i Listor från S4; inga flikar för det som inte är byggt.
CONTACT_TABS = (
    ("contacts", "flamingo:app_contacts", "Kontakter"),
    ("lists", "flamingo:app_lists", "Listor"),
    ("import", "flamingo:app_import", "Import"),
    ("fields", "flamingo:app_fields", "Fält"),
    ("signup", "flamingo:app_signup", "Anmälan"),
    ("settings", "flamingo:app_contacts_settings", "Inställningar"),
)


def nav_for(account):
    """Sidomenyn för kontot: APP_NAV, med Kontakter och Utskick efter
    Kampanjer när utskick är aktiverat (D2)."""
    from apps.flamingo.app_views import APP_NAV

    from .access import is_enabled

    if account is None or not is_enabled(account):
        return APP_NAV
    ours = (CONTACTS_ITEM, UTSKICK_ITEM)
    items = []
    for item in APP_NAV:
        items.append(item)
        if item[0] == "campaigns":
            items.extend(ours)
    for item in ours:
        if item not in items:
            items.append(item)
    return tuple(items)


def _tabs(rows, active, part):
    tabs = [
        {"key": key, "url": reverse(name), "label": label, "current": key == active}
        for key, name, label in rows
    ]
    # Första fliken heter som delen: sammanfattningen blir "Kontakter",
    # inte "Kontakter: Kontakter".
    current = next((t["label"] for t in tabs[1:] if t["current"]), "")
    return {"tabs": tabs, "part": part, "current_label": current}


def contacts_tabs(active):
    """Flikarna för flikraden: [{"key", "url", "label", "current"}], och
    rubriken för den aktiva fliken (sammanfattningen under 560 px)."""
    return _tabs(CONTACT_TABS, active, "Kontakter")


def utskick_tabs(active):
    """Som contacts_tabs, för Utskick ("utskick", "health" eller "settings")."""
    return _tabs(UTSKICK_TABS, active, "Utskick")
