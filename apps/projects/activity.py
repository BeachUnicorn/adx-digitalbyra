"""
Kundkontakternas senaste aktivitet: när en portalkontakt senast öppnade en
sida i kundportalen (/kund/) eller i Flamingo (/flamingo/app/), var, och
för vilken kund.

Djangos last_login räcker inte: den sätts bara vid inloggningen med kod,
och inloggningen gäller i två veckor. Här sparas i stället den senaste sida
kontakten faktiskt öppnade (ContactActivity, en rad per användare, inte per
kund). Kundkortet visar den per kontakt, kundlistan den senaste bland
kundens kontakter.

Vad som räknas som en sidvisning (is_page_view):
- GET med ett svar 2xx som är en HTML-sida, inte en fil. En 3xx räknas
  inte: sidan den leder till räknas när webbläsaren öppnar den. 4xx och
  5xx räknas aldrig (en Flamingo-404 för en kontakt utan Flamingo, ett 403
  för en kontakt utan aktiv kund).
- En navigering i webbläsaren: Sec-Fetch-Dest: document och
  Sec-Fetch-Mode: navigate. Utan Sec-Fetch-huvuden (äldre webbläsare):
  Accept med text/html och ingen X-Requested-With. Förhämtningar
  (Sec-Purpose eller Purpose: prefetch) räknas inte.
  Bakgrundsanrop (fetch, XHR, en lista som uppdaterar sig själv), JSON,
  bilagor, fakturor och HEAD räknas alltså aldrig.
- En sida som visades för en kund (served_customer): åtkomstlagret måste
  ha släppt in kontakten till en kund. Inloggningen och kodsidan räknas
  alltså inte, inte heller för en inloggad kontakt vars kunder alla är
  inaktiva.

Vem: bara inloggade kontakter. Byrån (is_staff) räknas aldrig, inte heller
i kundvyn (VIEW_AS_KEY, i portalen och i Flamingo).

Hur ofta: högst en skrivning per användare och fem minuter, utom att den
första sidan efter en inloggning alltid sparas. Processens cache svarar
först, så nästan alla sidvisningar gör ingen databasfråga alls. Cachen
håller tiden som är sparad, och spärren räknas från den, så en annan
workers skrivning gör inte tiden mer än fem minuter gammal. Sedan en
villkorad UPDATE (WHERE last_seen_at < nu - 5 min), som gäller över båda
workers och alla flikar. Sessionen rörs inte: Django sparar hela
sessionen, och en skrivning härifrån kunde skriva över en samtidig
förfrågans ändring (Flamingos kundval).

Ett fel här fäller aldrig sidan: allt efter svaret ligger i try/except
och loggas. Ingen mejlas.
"""

import logging
import math
from datetime import timedelta

from django.core.cache import cache
from django.db.models import F, Q
from django.utils import timezone

from .access import VIEW_AS_KEY, is_agency_user
from .models import ActivityArea, ContactActivity

logger = logging.getLogger(__name__)

#: Högst en skrivning per användare inom det här fönstret.
THROTTLE = timedelta(minutes=5)
_CACHE_KEY = "kontaktaktivitet:{}"


def area_for(path):
    """Kundportalen, Flamingos verktyg eller None (räknas inte)."""
    if path == "/kund" or path.startswith("/kund/"):
        return ActivityArea.PORTAL
    if path.startswith("/flamingo/"):
        from apps.flamingo.access import is_app_path

        if is_app_path(path):
            return ActivityArea.FLAMINGO
    return None


def is_page_view(request, response):
    """En sida kontakten öppnade i webbläsaren, inte ett bakgrundsanrop."""
    if request.method != "GET" or not 200 <= response.status_code < 300:
        return False
    # FileResponse: bilagor, fakturor och allt annat som laddas ner.
    if getattr(response, "streaming", False):
        return False
    if not response.get("Content-Type", "").startswith("text/html"):
        return False
    if "attachment" in response.get("Content-Disposition", ""):
        return False
    headers = request.headers
    if "prefetch" in (headers.get("Sec-Purpose", "") + headers.get("Purpose", "")):
        return False
    dest, mode = headers.get("Sec-Fetch-Dest"), headers.get("Sec-Fetch-Mode")
    if dest or mode:
        return dest == "document" and mode == "navigate"
    return "text/html" in headers.get("Accept", "") and not headers.get("X-Requested-With")


def counts_as_contact(request):
    """Inloggad kontakt. Aldrig byrån, aldrig byrån i kundvyn."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or is_agency_user(user):
        return False
    session = getattr(request, "session", None)
    return not (session is not None and session.get(VIEW_AS_KEY))


def served_customer(request, area):
    """
    Kunden sidan visades för, eller None. Läses ur det åtkomstlagret satte:
    request.customer (customer_required, sms_portal) i portalen och
    request.flamingo (Flamingogrinden) i verktyget. Inloggningen, kodsidan,
    en kontakt utan aktiv kund och byrån i kundvyn ger None.
    """
    if area == ActivityArea.FLAMINGO:
        from apps.flamingo.access import CONTACT

        access = getattr(request, "flamingo", None)
        return access.customer if access is not None and access.mode == CONTACT else None
    if getattr(request, "viewing_as", False):
        return None
    return getattr(request, "customer", None)


def record(user, area, customer, now=None, logged_in=None):
    """
    Spara sidvisningen om den förra är äldre än THROTTLE, eller äldre än
    inloggningen (logged_in): första sidan efter en inloggning sparas alltid.
    Returnerar tiden som är sparad efteråt: now, eller den förra om spärren
    höll. En UPDATE i det vanliga fallet; en SELECT till när spärren höll
    (en annan worker skrev nyss), och en INSERT första gången.
    """
    now = now or timezone.now()
    due = Q(last_seen_at__lt=now - THROTTLE)
    if logged_in is not None:
        due |= Q(last_seen_at__lt=logged_in)
    values = {"last_seen_at": now, "last_area": area, "last_customer": customer}
    if ContactActivity.objects.filter(due, user=user).update(**values):
        return now
    row, _ = ContactActivity.objects.get_or_create(user=user, defaults=values)
    return row.last_seen_at


def _held(seen, now, logged_in):
    """Spärren håller: sparat för mindre än THROTTLE sedan, och inte före
    inloggningen. seen är cachens tid i epoch-sekunder, eller None."""
    if seen is None or now.timestamp() - seen >= THROTTLE.total_seconds():
        return False
    return logged_in is None or logged_in.timestamp() <= seen


class ContactActivityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            self._note(request, response)
        except Exception:  # noqa: BLE001 - aktiviteten får aldrig fälla en sida
            logger.exception("Kontaktens aktivitet kunde inte sparas")
        return response

    @staticmethod
    def _note(request, response):
        area = area_for(request.path_info)
        if area is None or not is_page_view(request, response) or not counts_as_contact(request):
            return
        customer = served_customer(request, area)
        if customer is None:
            return
        user, now = request.user, timezone.now()
        key = _CACHE_KEY.format(user.pk)
        if _held(cache.get(key), now, user.last_login):
            return
        # Före skrivningen: ett fel försöks igen om fem minuter, inte vid
        # varje sida.
        cache.set(key, now.timestamp(), int(THROTTLE.total_seconds()))
        seen = record(user, area, customer, now, user.last_login)
        # Spärren räknas från den sparade tiden, inte från nu.
        left = (seen + THROTTLE - now).total_seconds()
        cache.set(key, seen.timestamp(), max(1, math.ceil(left)))


# ------------------------------------------------------------------ visning


def _when(moment, now):
    """'i dag 14:05', 'i går 09:12' eller '2 okt' i svensk tid."""
    from apps.flamingo.rules import when_text

    return when_text(moment, now)


def _area_label(area):
    return ActivityArea(area).label if area in ActivityArea.values else ""


def status_parts(user, customer, now=None):
    """
    Kundkortets rad om kontakten, i delar som mallen skiljer med en punkt.
    Varje del är {"text", "when", "after"}; mallen håller ihop "when" på en
    rad: "aktiv senast | i dag 14:05 | i Flamingo", "inloggad | 3 okt", eller
    "har inte loggat in än". Gällde sidan en annan kund än kortets står den
    kundens namn efter ("i Flamingo för Annan AB").
    """
    if is_agency_user(user):
        # Byrån räknas aldrig, och inloggningarna är byråns egna.
        return [{"text": "byråkonto"}]
    now = now or timezone.now()
    parts = []
    activity = getattr(user, "contact_activity", None)
    if activity is not None:
        after = []
        label = _area_label(activity.last_area)
        if label:
            after.append(f"i {label}")
        if activity.last_customer_id not in (None, customer.pk):
            after.append(f"för {activity.last_customer.name}")
        parts.append(
            {
                "text": "aktiv senast",
                "when": _when(activity.last_seen_at, now),
                "after": " ".join(after),
            }
        )
    if user.last_login:
        parts.append({"text": "inloggad", "when": _when(user.last_login, now)})
    return parts or [{"text": "har inte loggat in än"}]


def contacts_for_card(customer, now=None):
    """Kundens kontakter, var och en med status_parts. En fråga oavsett antal."""
    now = now or timezone.now()
    contacts = list(customer.users.select_related("contact_activity__last_customer"))
    for user in contacts:
        user.status_parts = status_parts(user, customer, now)
    return contacts


def latest_by_customer(customer_ids, now=None):
    """
    {kund_id: {"when": "i dag 14:05", "area": "Flamingo"}} för kundlistan:
    den senaste sidan någon av kundens nuvarande kontakter öppnade för just
    den kunden. En kontakt i två kunder räknas bara hos kunden den senaste
    sidan gällde (raden är per kontakt). En fråga oavsett antal kunder och
    kontakter.
    """
    now = now or timezone.now()
    latest = {}
    rows = ContactActivity.objects.filter(
        last_customer__in=customer_ids,
        user__customers=F("last_customer"),
        user__is_staff=False,
    ).values_list("last_customer", "last_seen_at", "last_area")
    for customer_id, seen, area in rows:
        if customer_id not in latest or seen > latest[customer_id][0]:
            latest[customer_id] = (seen, area)
    return {
        customer_id: {"when": _when(seen, now), "area": _area_label(area)}
        for customer_id, (seen, area) in latest.items()
    }
