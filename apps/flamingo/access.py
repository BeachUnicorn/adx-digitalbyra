"""
Vem får se ADX Flamingo. Reglerna bor här och ingen annanstans.

    byrån (staff)              allt, även opublicerade sidor
    byrån i kundvyn            exakt som kunden, med samma formulär; det
                               byrån sparar gäller på riktigt och sparas i
                               byråns namn. En notis om kunden saknar
                               Flamingo (kunden själv får 404)
    kontakt hos en kund med    Flamingo-sidorna och verktyget för den kunden
    Flamingo aktiverat
    alla andra                 samma 404 som för vilken okänd adress som helst

Ingen omdirigering till inloggning och inget 403: båda avslöjar att något
finns här. Behörigheten läses från databasen vid varje anrop, så en
avaktivering gäller direkt.
"""

from dataclasses import dataclass, field

from apps.projects.access import is_agency_user, viewing_customer
from apps.projects.models import Customer

from .models import has_flamingo

PREFIX = "/flamingo/"

#: Sessionens val av Flamingo-kund för en kontakt i flera kunder. Prövas
#: alltid mot kontaktens aktuella Flamingo-kunder - aldrig betrott rakt av.
SESSION_KEY = "flamingo_kund"

STAFF = "staff"
VIEWING_AS = "viewing_as"
CONTACT = "contact"
PREVIEW = "preview"
#: En besökare utan behörighet på Flamingos publicerade sidor (inte verktyget).
PUBLIC = "public"


@dataclass
class FlamingoAccess:
    mode: str
    #: Kunden verktyget visar: kontaktens valda Flamingo-kund, eller kunden
    #: byrån tittar som. None för byrån utanför kundvyn och i förhandsvisning.
    customer: Customer | None = None
    #: Kontaktens alla Flamingo-kunder (för kundväljaren).
    customers: list = field(default_factory=list)
    #: Byrån i kundvyn på en kund som inte har Flamingo aktiverat.
    customer_lacks_flamingo: bool = False

    @property
    def is_agency(self):
        return self.mode in (STAFF, VIEWING_AS, PREVIEW)

    @property
    def read_only(self):
        """Bara utkastförhandsvisningen i /manage/ är skrivskyddad. Byrån i
        kundvyn ser och gör det kunden ser och gör (Giovanni 2026-10-03:
        "visa som kund" ska visa det kunden ser)."""
        return self.mode == PREVIEW

    @property
    def sees_drafts(self):
        """Opublicerade Flamingo-sidor syns för byrån, aldrig för kunder."""
        return self.is_agency


APP_PREFIX = PREFIX + "app/"


def is_app_path(path):
    """Verktyget: kräver alltid Flamingo-behörighet."""
    return path == APP_PREFIX.rstrip("/") or path.startswith(APP_PREFIX)


def is_flamingo_path(path):
    return path == PREFIX.rstrip("/") or path.startswith(PREFIX)


def flamingo_customers(user):
    """Kontaktens aktiva kunder med Flamingo aktiverat. Byrån har inga."""
    if not user or not user.is_authenticated or is_agency_user(user):
        return Customer.objects.none()
    return user.customers.filter(is_active=True, flamingo__is_enabled=True).order_by("name")


def resolve(request):
    """FlamingoAccess för förfrågan, eller None när den ska få 404."""
    # Utkastförhandsvisningen i /manage/ anropar vyn direkt, utan middleware,
    # med en syntetisk förfrågan (apps/assistant/preview.py). Flaggan går
    # inte att sätta utifrån.
    if getattr(request, "adx_preview", False):
        return FlamingoAccess(PREVIEW)

    user = getattr(request, "user", None)
    if is_agency_user(user):
        viewed = viewing_customer(request)
        if viewed is not None:
            enabled = has_flamingo(viewed)
            return FlamingoAccess(
                VIEWING_AS, customer=viewed, customers=[viewed], customer_lacks_flamingo=not enabled
            )
        return FlamingoAccess(STAFF)

    customers = list(flamingo_customers(user))
    if not customers:
        return None
    chosen_pk = request.session.get(SESSION_KEY) if hasattr(request, "session") else None
    chosen = next((c for c in customers if c.pk == chosen_pk), customers[0])
    return FlamingoAccess(CONTACT, customer=chosen, customers=customers)


def access_for(request):
    """Samma svar som middleware gav, eller räknat nu (förhandsvisningen)."""
    access = getattr(request, "flamingo", None)
    if access is None:
        access = resolve(request)
        request.flamingo = access
    return access


def portal_shows_flamingo(request):
    """Ska portalen visa vägen in? Kontakt med Flamingo, eller byrån i kundvyn
    på en kund med Flamingo."""
    user = getattr(request, "user", None)
    if is_agency_user(user):
        return has_flamingo(viewing_customer(request))
    return flamingo_customers(user).exists()
