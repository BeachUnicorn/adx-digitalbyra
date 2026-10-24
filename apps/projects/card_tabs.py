"""
Kundkortets flikar: /manage/kunder/<pk>/?flik=<nyckel>.

Giovanni 2026-10-24: kundkortet var en enda lång sida där man fick scrolla
i en evighet. Nu är det en vy med en flik i taget, samma mönster som
kampanjens flikar i Flamingo (?flik=sidan). Översikten har ingen nyckel.
En okänd flik visar översikten.

Allt som leder till kortet bygger adressen med card_url, så att en panels
formulär landar på panelens flik. Ankaren (#aws, #utskick) används bara där
de hjälper inne i en flik; vakten i tests.py prövar att varje flik och
ankare som används finns.
"""

from django.shortcuts import redirect
from django.urls import reverse

OVERVIEW = ""
LOG = "logg"
ISSUES = "arenden"
CONTACTS = "kontakter"
OPERATIONS = "drift"
FLAMINGO = "flamingo"
SMS = "sms"

#: Flikarna i menyns ordning: (nyckel, rubrik).
TABS = (
    (OVERVIEW, "Översikt"),
    (LOG, "Logg"),
    (ISSUES, "Ärenden"),
    (CONTACTS, "Kontakter"),
    (OPERATIONS, "Drift"),
    (FLAMINGO, "Flamingo"),
    (SMS, "SMS"),
)
TAB_KEYS = frozenset(key for key, _ in TABS)


def tab_from(request):
    """Fliken ur ?flik=, översikten om den saknas eller är okänd."""
    tab = request.GET.get("flik", OVERVIEW)
    return tab if tab in TAB_KEYS else OVERVIEW


def card_url(customer_id, tab=OVERVIEW, anchor=""):
    """Adressen till kundkortet, på fliken och eventuellt ett ankare i den."""
    if tab not in TAB_KEYS:
        raise ValueError(f"Kundkortet har ingen flik {tab!r}.")
    url = reverse("manage:customer_detail", args=[customer_id])
    if tab:
        url += f"?flik={tab}"
    if anchor:
        url += f"#{anchor}"
    return url


def card_redirect(customer_id, tab=OVERVIEW, anchor=""):
    return redirect(card_url(customer_id, tab, anchor))
