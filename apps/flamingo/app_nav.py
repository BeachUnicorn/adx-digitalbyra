"""
Verktygets meny (/flamingo/app/): sex punkter och flikarna under rubriken.

Godkänd mockup: mockups/flamingo-meny.html (2026-10-28). Steg 1 av 2: bara
menyn, grupperingen och rubrikerna ändras, inga adresser. Steg 2 ger
landningssidorna egna adresser och statistik per källa; då får
Landningssidor flikar här (SECTION_TABS["pages"]) utan att något annat
behöver ändras.

    MAIN_NAV                    de sex punkterna i ordning
    SECTION_TABS                flikarna i varje del
    PAGE_KEYS                   varje giltig sidnyckel ("del" eller "del/flik")
    main_nav(account)           punkterna kontot ser (Sms och e-post bara med utskick)
    section_tabs(section, tab, account)   flikraden med den aktiva markerad
    split_key(key)              "ads/google" -> ("ads", "google")

Varje sida i verktyget säger var den hör hemma med en sidnyckel till
render_app (eller app_context): "inbox", "ads/google", "messaging/contacts"
och så vidare. Delen blir app_active (den markerade punkten i menyn) och
fliken app_tab (den markerade fliken under rubriken). En nyckel som inte
finns i PAGE_KEYS är ett programmeringsfel (ValueError), så att en ny sida
inte råkar markera fel punkt.

Sms och e-post syns bara när utskick är på för kontot (utskick.access
.is_enabled): utan utskick ger varje sida i delen 404, och Sms-API:t finns
redan i kundportalen. Kontot ser då fem punkter. Fliken Sms-API syns bara
när kunden har ett aktiverat sms-konto och leder till kundportalens
/kund/sms/.

Under 900 px blir menyn en fast rad längst ner med ikon och kort rubrik
(flamingo-app.css); flikraden rullar i sidled inne i raden, aldrig sidan.
"""

from dataclasses import dataclass

from django.urls import reverse
from django.utils.html import format_html


@dataclass(frozen=True)
class NavItem:
    key: str
    url_name: str
    label: str
    #: Ikonen i templates/flamingo/app/_nav_icon.html.
    icon: str
    #: Var ett mjukt bindestreck får brytas i den smala raden längst ner
    #: ("Landnings-sidor"); 0 = ingenstans.
    soft_break: int = 0

    @property
    def url(self):
        return reverse(self.url_name)

    @property
    def label_html(self):
        if self.soft_break:
            n = self.soft_break
            return format_html("{}&shy;{}", self.label[:n], self.label[n:])
        if "-" in self.label:
            # "e-post" bryts aldrig vid bindestrecket i raden längst ner
            # ("Sms och e- / post"): ordet med bindestreck hålls ihop.
            head, _, word = self.label.rpartition(" ")
            return format_html('{} <span class="fl-nowrap">{}</span>', head, word)
        return self.label


@dataclass(frozen=True)
class Tab:
    key: str
    url_name: str
    label: str
    #: Fliken syns bara när villkoret gäller för kontot (CONDITIONS).
    needs: str = ""


#: De sex punkterna i ordning (mockupen).
MAIN_NAV = (
    NavItem("overview", "flamingo:app", "Översikt", "home"),
    NavItem("inbox", "flamingo:app_inbox", "Inkorg", "inbox"),
    NavItem("pages", "flamingo:app_pages", "Landningssidor", "page", soft_break=9),
    NavItem("ads", "flamingo:app_campaigns", "Annonser", "ads"),
    NavItem("messaging", "flamingo:app_utskick_list", "Sms och e-post", "mail"),
    NavItem("business", "flamingo:app_business", "Företaget", "business"),
)

#: Flikarna under rubriken i varje del. En del utan flikar har ingen rad.
SECTION_TABS = {
    "ads": (
        Tab("campaigns", "flamingo:app_campaigns", "Kampanjer"),
        Tab("google", "flamingo:app_google", "Google-kontot"),
    ),
    "messaging": (
        Tab("sent", "flamingo:app_utskick_list", "Skickade"),
        Tab("contacts", "flamingo:app_contacts", "Kontakter"),
        Tab("lists", "flamingo:app_lists", "Listor och segment"),
        Tab("links", "flamingo:app_links", "Länkar och QR"),
        Tab("health", "flamingo:app_utskick_health", "Leveranshälsa"),
        Tab("sms_api", "sms:dashboard", "Sms-API", needs="sms_api"),
    ),
    "business": (
        Tab("details", "flamingo:app_business", "Uppgifter och tjänster"),
        Tab("media", "flamingo:app_media", "Media"),
        Tab("settings", "flamingo:app_settings", "Inställningar"),
    ),
}

#: Delar som bara finns för vissa konton.
SECTION_NEEDS = {"messaging": "utskick"}

#: Varje giltig sidnyckel. Förslaget, byråns lista och no_account har ""
#: (ingen punkt markerad). sms_api är ingen sida i verktyget.
PAGE_KEYS = frozenset(
    {""}
    | {item.key for item in MAIN_NAV}
    | {
        f"{section}/{tab.key}"
        for section, tabs in SECTION_TABS.items()
        for tab in tabs
        if tab.needs != "sms_api"
    }
)


def split_key(key):
    """("ads", "google") för "ads/google", ("inbox", "") för "inbox"."""
    if key not in PAGE_KEYS:
        raise ValueError(f"Okänd sidnyckel i verktyget: {key!r}")
    section, _, tab = key.partition("/")
    return section, tab


def _utskick_on(account):
    from apps.utskick.access import is_enabled

    return account is not None and is_enabled(account)


def _sms_api_on(account):
    from apps.sms.models import SmsAccount

    if account is None or not account.customer_id:
        return False
    return SmsAccount.objects.filter(customer_id=account.customer_id, is_enabled=True).exists()


CONDITIONS = {"utskick": _utskick_on, "sms_api": _sms_api_on}


def main_nav(account):
    """Punkterna kontot ser, i ordning."""
    return [
        item
        for item in MAIN_NAV
        if not SECTION_NEEDS.get(item.key) or CONDITIONS[SECTION_NEEDS[item.key]](account)
    ]


def section_tabs(section, tab, account):
    """Flikraden för delen: [{"key", "url", "label", "current"}], tom för
    en del utan flikar."""
    out = []
    for row in SECTION_TABS.get(section, ()):
        if row.needs and not CONDITIONS[row.needs](account):
            continue
        out.append(
            {
                "key": row.key,
                "url": reverse(row.url_name),
                "label": row.label,
                "current": row.key == tab,
            }
        )
    return out


def section_label(section):
    """Delens rubrik ("Annonser"), eller "" för en okänd del."""
    return next((item.label for item in MAIN_NAV if item.key == section), "")
