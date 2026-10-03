"""
Omdömen från Google (sidbyggarens block "Omdömen från Google"): kunden
pekar ut sin Google-profil, bekräftar "Det här är vi", och väljer vilka
omdömen som syns och i vilken ordning. Texten ändras aldrig.

    is_configured()                 GOOGLE_PLACES_API_KEY finns (samma som places.py)
    parse_maps_link(text)           MapsLink ur en länk från Google Maps, eller None
    clean_place_id(value)           ett Place ID som går att använda, eller ""
    search(account, query)          [Hit] ur Text Search (högst SEARCH_DAILY_MAX per dag)
    connect(account, place_id, user=None)
                                    Place Details: betyget, antalet, länken och
                                    omdömena sparas (högst DETAILS_DAILY_MAX per dag)
    save_place_id(account, place_id)
                                    utan nyckeln: bara id:t sparas, inget hämtas
    select(account, ids)            kundens val och ordning
    confirm_owner(account, user)    "Profilen är vår": kunden eller byrån intygar
                                    att en profil som inte liknar företaget är dess
    disconnect(account)             "Koppla bort profilen"
    google_link(value)              en https-länk till Google, eller "" (prövas
                                    när den sparas och när den ritas)
    refresh_due(now=None, account_pk=None)
                                    cron (flamingo_google_sync): profiler som inte
                                    hämtats på REFRESH_EVERY hämtas igen, och
                                    innehåll äldre än MAX_AGE tas bort

Googles villkor (Places API, developers.google.com/maps/documentation/places/
web-service/policies, läst 2026-10-03, senast ändrad 2026-09-28, och Google
Maps Platform EEA Service Specific Terms avsnitt 15, cloud.google.com/terms/
maps-platform/eea/maps-service-terms):

- Place ID får sparas hur länge som helst. Annat innehåll från Places API
  får enligt EEA-villkoren inte cachas utöver det som uttryckligen tillåts
  (latitud och longitud i 30 dagar). Omdömena sparas ändå här så att /lp/
  inte anropar Google för varje besökare: de hämtas igen var REFRESH_EVERY,
  och går en hämtning inte på MAX_AGE tas texterna, betyget och namnet bort
  (expire). Byråns beslut (Giovanni 2026-10-03): behåll upplägget, med
  MAX_AGE 90 dagar, trots att villkoren strängt taget bara tillåter Place ID.
- Varje omdöme visas med författarens namn och länk till profilen, och med
  en länk till omdömet på Google Maps (googleMapsUri). Blocket säger att
  omdömena kommer från Google och hur de valts ut (kunden väljer och
  ordnar), och texten "Google Maps" står oförändrad (translate="no").
- Texten visas som Google skrev den: originaltexten (originalText) när
  Google översatt den, så att ingen översättning visas.
- Författarens bild hämtas aldrig (besökarens integritet): sidan ritar
  initialerna. Google kräver bilden "när utrymmet räcker"; det är ett
  medvetet avsteg som byrån ska ta ställning till.

Profilen måste vara kundens. Vem som helst kan peka ut vilken plats som
helst hos Google, så store_details prövar profilen mot kunden: samma domän
som hemsidan eller ett gemensamt ord i namnet (places.matches, samma regel
som läsningen av hemsidan), eller samma telefonnummer som en bekräftad
uppgift. Liknar den inte kunden sparas profilen, men betyget blir en
OBEKRÄFTAD uppgift, omdömena och betyget syns inte på sidorna eller i
förslagen (FlamingoAccount.google_profile_trusted), och byrån larmas, tills
kunden eller byrån intygar att profilen är deras (confirm_owner). Samma sak
när cron hämtar profilen igen. En profil som liknar kunden blir en
bekräftad uppgift med källan google, som förut.

Ett demokonto anropar aldrig Google. Utan GOOGLE_PLACES_API_KEY görs inget
anrop: kunden kan ange ett Place ID, och omdömena hämtas av cron när
nyckeln finns. Varje anrop går till en fast https-adress hos Google;
kundens text styr bara frågan, aldrig vart anropet går, och Place ID prövas
mot PLACE_ID_RE innan det blir en del av adressen. Nyckeln skrivs aldrig i
en logg eller ett felmeddelande.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, unquote, unquote_plus, urlsplit
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import alerts, limits, places
from .models import Fact, FlamingoAccount
from .scan import FACT_LABELS, FACT_ORDER, KEY_RATING

logger = logging.getLogger(__name__)

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
SEARCH_FIELDS = ",".join(
    "places." + name
    for name in (
        "id",
        "displayName",
        "formattedAddress",
        "rating",
        "userRatingCount",
        "googleMapsUri",
    )
)
DETAILS_FIELDS = ",".join(
    (
        "id",
        "displayName",
        "formattedAddress",
        "rating",
        "userRatingCount",
        "googleMapsUri",
        "reviews",
        # Bara för att pröva att profilen är kundens (_owned).
        "websiteUri",
        "nationalPhoneNumber",
    )
)
TIMEOUT = 8
MAX_BYTES = 512 * 1024
SEARCH_RESULTS = 5
#: Google ger högst fem omdömen per plats.
MAX_REVIEWS = 5
REVIEW_TEXT_MAX = 4000

#: Anrop per konto och svenskt dygn (Text Search och Place Details kostar
#: per anrop). Cronens hämtning räknas inte här.
SEARCH_DAILY_MAX = 10
DETAILS_DAILY_MAX = 3
USAGE_SEARCH = "places_search"
USAGE_DETAILS = "places_details"

#: Profilen hämtas igen så här ofta (cron).
REFRESH_EVERY = timedelta(days=7)
#: Äldre än så tas omdömena, betyget och namnet bort om en ny hämtning
#: inte gått (se villkoren ovan).
MAX_AGE = timedelta(days=90)
#: Högst så många profiler per körning av cron.
REFRESH_PER_RUN = 100

#: Ett Place ID: bokstäver, siffror, _ och - (det går in i adressen).
#: Prövas med fullmatch.
PLACE_ID_RE = re.compile(r"[A-Za-z0-9_-]{10,200}")
_GOOGLE_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*google\.[a-z]{2,3}(?:\.[a-z]{2})?$")
_SHORT_HOSTS = ("maps.app.goo.gl", "goo.gl", "g.co")

NOT_CONFIGURED = (
    "Kopplingen till Google är inte påslagen än. Du kan ange ditt Place ID, så hämtas "
    "omdömena när kopplingen är på."
)
DEMO_REFUSED = "Det här är ett demokonto, så Google anropas aldrig."
GOOGLE_DOWN = "Google svarade inte. Försök igen om en stund."
NOT_FOUND = "Google hittar ingen plats med det id:t. Kontrollera det och försök igen."
BAD_PLACE_ID = "Det där ser inte ut som ett Place ID. Det börjar ofta med ChIJ."
SEARCH_LIMIT = (
    f"Du har sökt {SEARCH_DAILY_MAX} gånger hos Google i dag, och det är gränsen. "
    "Ange Place ID eller klistra in länken från Google Maps, eller sök igen i morgon."
)
DETAILS_LIMIT = (
    f"Profilen har hämtats från Google {DETAILS_DAILY_MAX} gånger i dag, och det är "
    "gränsen. Försök igen i morgon."
)
SHORT_LINK = (
    "Korta länkar (maps.app.goo.gl) går inte att läsa här. Öppna länken, kopiera adressen "
    "ur webbläsarens adressfält och klistra in den, eller sök på företagets namn."
)
NOT_A_MAPS_LINK = "Länken går inte till Google Maps. Klistra in länken från Google Maps."


class ReviewsError(ValueError):
    """Något stoppade anropet eller ändringen. message är en svensk text för
    kunden; Googles eget fel visas aldrig."""

    def __init__(self, message):
        self.message = str(message)
        super().__init__(self.message)


def is_configured():
    return places.is_configured()


def refusal(account):
    """Varför Google inte får anropas för kontot, eller "" (det får det)."""
    if getattr(account, "is_demo", False):
        return DEMO_REFUSED
    if not is_configured():
        return NOT_CONFIGURED
    return ""


# ---------------------------------------------------------------------------
# Länken från Google Maps och Place ID
# ---------------------------------------------------------------------------


def clean_place_id(value):
    value = str(value or "").strip()
    if value.lower().startswith("place_id:"):
        value = value[len("place_id:") :].strip()
    return value if PLACE_ID_RE.fullmatch(value) else ""


@dataclass
class MapsLink:
    place_id: str = ""
    #: Namnet (eller frågan) i länken, för Text Search.
    query: str = ""
    error: str = ""


def _looks_like_link(text):
    lowered = text.lower()
    return "://" in lowered or lowered.startswith(
        ("www.google.", "google.", "maps.google.", "maps.app.goo.gl", "goo.gl/")
    )


def parse_maps_link(text):
    """Det som går att läsa ur en länk från Google Maps, utan anrop:

        ...?q=place_id:ChIJ...             place_id
        ...&query_place_id=ChIJ...         place_id
        .../data=...!1sChIJ... (!19s)      place_id
        /maps/place/Exempelr%C3%B6r+AB/@... namnet, för en sökning
        ...?q=Exempelrör+Nacka             frågan, för en sökning

    None om texten inte är en länk (den är då en sökning). En kort länk
    (maps.app.goo.gl) eller en länk till något annat än Google ger error."""
    text = str(text or "").strip()
    if not text or not _looks_like_link(text):
        return None
    parts = urlsplit(text if "://" in text else "https://" + text)
    host = (parts.hostname or "").lower()
    if host in _SHORT_HOSTS:
        return MapsLink(error=SHORT_LINK)
    if not _GOOGLE_HOST.match(host):
        return MapsLink(error=NOT_A_MAPS_LINK)
    decoded = unquote(text)
    for pattern in (
        r"place_id[:=]([A-Za-z0-9_-]{10,200})",
        r"query_place_id=([A-Za-z0-9_-]{10,200})",
        r"!(?:1|19)s(ChIJ[A-Za-z0-9_-]{6,200})",
    ):
        match = re.search(pattern, decoded)
        if match and clean_place_id(match.group(1)):
            return MapsLink(place_id=match.group(1))
    query = ""
    match = re.search(r"/maps/place/([^/@?#]+)", parts.path)
    if match:
        query = unquote_plus(match.group(1))
    if not query:
        params = parse_qs(parts.query)
        query = (params.get("q") or params.get("query") or [""])[0]
    query = " ".join(query.replace("+", " ").split())[:200]
    if not query:
        return MapsLink(error="Länken har inget Place ID eller namn. Sök på företagets namn.")
    return MapsLink(query=query)


# ---------------------------------------------------------------------------
# Anropen
# ---------------------------------------------------------------------------


def _call(url, *, field_mask, body=None):
    """Ett anrop till Places API (New): svaret som dict. Kastar ReviewsError
    med en allmän text; Googles status loggas, aldrig nyckeln."""
    headers = {
        "X-Goog-Api-Key": settings.GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": field_mask,
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(  # noqa: S310 - fasta https-adresser hos Google
        url, data=data, method="POST" if data is not None else "GET", headers=headers
    )
    try:
        with urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            raw = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        status = _google_status(exc)
        logger.warning("Flamingo: Google Places svarade %s (%s)", exc.code, status)
        if exc.code == 404 or status == "NOT_FOUND":
            raise ReviewsError(NOT_FOUND) from None
        if exc.code == 400 and status == "INVALID_ARGUMENT" and "/places/" in url:
            raise ReviewsError(NOT_FOUND) from None
        raise ReviewsError(GOOGLE_DOWN) from None
    except Exception as exc:  # noqa: BLE001 - nätet, tidsgränsen: samma text
        logger.warning("Flamingo: Google Places svarade inte: %s", type(exc).__name__)
        raise ReviewsError(GOOGLE_DOWN) from None
    if len(raw) > MAX_BYTES:
        raise ReviewsError(GOOGLE_DOWN)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ReviewsError(GOOGLE_DOWN) from None
    if not isinstance(payload, dict):
        raise ReviewsError(GOOGLE_DOWN)
    return payload


def _google_status(error):
    try:
        payload = json.loads(error.read(16 * 1024).decode("utf-8"))
        return str(payload.get("error", {}).get("status", ""))[:60]
    except Exception:  # noqa: BLE001
        return ""


def _text(value):
    if isinstance(value, dict):
        value = value.get("text")
    return " ".join(str(value or "").split())[:200] if isinstance(value, str | int) else ""


#: Tecken som aldrig får stå i en länk vi sparar eller ritar: blanktecken,
#: kontrolltecken, citattecken, vinkelparenteser och bakåtsnedstreck (en
#: webbläsare läser "https://evil.example\\@maps.google.com/" som en länk till
#: evil.example, Pythons urlsplit som en till maps.google.com).
_BAD_URL_CHARS = re.compile(r"[\s\x00-\x1f\x7f\\\\\"'<>`]")


def _https(value, hosts=None):
    """value som en https-länk, eller "": inga användaruppgifter (@), ingen
    annan port än 443, inga tecken som webbläsaren och urlsplit läser olika,
    och (med hosts) bara de värdarna och deras underdomäner."""
    value = str(value or "").strip()
    if not value.startswith("https://") or len(value) > 500 or _BAD_URL_CHARS.search(value):
        return ""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or "@" in parts.netloc or port not in (None, 443):
        return ""
    if not re.fullmatch(r"[a-z0-9.-]+", host) or parts.netloc.lower().split(":")[0] != host:
        return ""
    if hosts is not None and not any(host == h or host.endswith("." + h) for h in hosts):
        return ""
    return value


_GOOGLE_LINK_HOSTS = ("google.com", "goo.gl", "google.se")


def google_link(value):
    """En https-länk till Google (profilen, omdömet, författaren), eller "".
    Prövas när den sparas och igen när sidan ritas (render.py)."""
    return _https(value, _GOOGLE_LINK_HOSTS)


@dataclass
class Hit:
    place_id: str
    name: str
    address: str = ""
    rating: str = ""
    count: int | None = None
    maps_uri: str = ""


def _hit(place):
    place_id = clean_place_id(place.get("id"))
    if not place_id:
        return None
    count = place.get("userRatingCount")
    return Hit(
        place_id=place_id,
        name=_text(place.get("displayName")) or place_id,
        address=_text(place.get("formattedAddress")),
        rating=_rating_number(place.get("rating")),
        count=count if isinstance(count, int) else None,
        maps_uri=google_link(place.get("googleMapsUri")),
    )


def _rating_number(value):
    if not isinstance(value, int | float) or isinstance(value, bool):
        return ""
    return f"{value:.1f}".replace(".", ",")


def search(account, query, *, now=None):
    """Upp till SEARCH_RESULTS träffar för query (namn och ort, eller
    namnet ur en länk). Kastar ReviewsError (demot, ingen nyckel, gränsen,
    Google svarade inte)."""
    refused = refusal(account)
    if refused:
        raise ReviewsError(refused)
    query = " ".join(str(query or "").split())[:200]
    if len(query) < 2:
        raise ReviewsError("Skriv företagets namn, gärna med orten.")
    if not limits.reserve_daily(account, USAGE_SEARCH, SEARCH_DAILY_MAX, now=now):
        raise ReviewsError(SEARCH_LIMIT)
    payload = _call(
        SEARCH_URL,
        field_mask=SEARCH_FIELDS,
        body={
            "textQuery": query,
            "languageCode": "sv",
            "regionCode": "SE",
            "pageSize": SEARCH_RESULTS,
        },
    )
    found = payload.get("places")
    hits = []
    for place in found if isinstance(found, list) else []:
        hit = _hit(place) if isinstance(place, dict) else None
        if hit is not None:
            hits.append(hit)
    return hits[:SEARCH_RESULTS]


def fetch_details(place_id):
    """Place Details för place_id (prövat), som Googles dict. Kastar
    ReviewsError. Ingen spärr här: den som anropar bokför (connect, cron)."""
    place_id = clean_place_id(place_id)
    if not place_id:
        raise ReviewsError(BAD_PLACE_ID)
    url = DETAILS_URL.format(place_id=quote(place_id, safe="")) + "?languageCode=sv&regionCode=SE"
    return _call(url, field_mask=DETAILS_FIELDS)


# ---------------------------------------------------------------------------
# Att spara: profilen, omdömena och betyget som en bekräftad uppgift
# ---------------------------------------------------------------------------


def _review(raw):
    """Ett omdöme i formen som FlamingoAccount.google_reviews beskriver, eller
    None. Texten är originalet (originalText) när Google översatt den."""
    if not isinstance(raw, dict):
        return None
    review_id = str(raw.get("name") or "").strip()
    if not review_id.startswith("places/") or len(review_id) > 300:
        return None
    author = raw.get("authorAttribution") if isinstance(raw.get("authorAttribution"), dict) else {}
    name = _text(author.get("displayName"))
    if not name:
        return None
    rating = raw.get("rating")
    try:
        rating = max(1, min(5, int(rating)))
    except (TypeError, ValueError):
        return None
    original = raw.get("originalText") if isinstance(raw.get("originalText"), dict) else {}
    shown = raw.get("text") if isinstance(raw.get("text"), dict) else {}
    text = str(original.get("text") or shown.get("text") or "")
    text = text.replace("\x00", "")[:REVIEW_TEXT_MAX]
    return {
        "id": review_id,
        "author": name,
        "author_uri": google_link(author.get("uri")),
        "rating": rating,
        "text": text,
        "time": str(raw.get("publishTime") or "")[:40],
        "relative": _text(raw.get("relativePublishTimeDescription")),
        # Omdömet på Google Maps (Googles villkor: varje omdöme ska gå att
        # öppna där). render.py läser det som "uri".
        "uri": google_link(raw.get("googleMapsUri")),
    }


def _decimal_rating(value):
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        rating = Decimal(str(round(float(value), 1)))
    except (InvalidOperation, ValueError):
        return None
    return rating if Decimal("0") <= rating <= Decimal("5") else None


def store_rating_fact(account, place, *, confirmed=True):
    """Betyget som en uppgift med källan google, i samma form som places.py
    skriver det ("4,8 av 37 omdömen", scan.KEY_RATING): BEKRÄFTAD när
    profilen är kundens (kunden har sagt "Det här är vi" och profilen liknar
    företaget, eller någon har intygat den), annars OBEKRÄFTAD, så att
    varken annonserna eller sidan använder den. Ett betyg som ADX skrivit
    rörs aldrig. Utan betyg hos Google tas en uppgift från Google bort."""
    value = places._rating(place)
    fact = account.facts.filter(key=KEY_RATING).first()
    if fact is not None and fact.source == Fact.SOURCE_ADX:
        return
    if not value:
        if fact is not None and fact.source == Fact.SOURCE_GOOGLE:
            fact.delete()
        return
    if fact is None:
        Fact.objects.create(
            account=account,
            key=KEY_RATING,
            label=FACT_LABELS[KEY_RATING],
            value=value,
            source=Fact.SOURCE_GOOGLE,
            confirmed=confirmed,
            order=FACT_ORDER[KEY_RATING],
        )
        return
    fact.label = FACT_LABELS[KEY_RATING]
    fact.value = value
    fact.source = Fact.SOURCE_GOOGLE
    fact.confirmed = confirmed
    fact.save(update_fields=["label", "value", "source", "confirmed", "updated_at"])


def _forget_rating_fact(account):
    account.facts.filter(key=KEY_RATING, source=Fact.SOURCE_GOOGLE).delete()


PROFILE_FIELDS = [
    "google_place_id",
    "google_place_name",
    "google_maps_uri",
    "google_rating",
    "google_review_count",
    "google_reviews",
    "google_reviews_selected",
    "google_reviews_fetched_at",
    "google_place_unverified",
    "google_place_confirmed_at",
    "google_place_confirmed_by",
    "updated_at",
]


def _phones(account):
    """Kontots bekräftade telefonnummer i internationell form."""
    from . import generator
    from .sms import normalize_phone

    out = set()
    for fact in account.usable_fact_rows():
        if generator.fact_kind(fact) != "phone":
            continue
        for match in generator._PHONE_RUN.finditer(" ".join(fact.value.split())):
            number = normalize_phone(match.group())
            if number:
                out.add(number)
    return out


def _owned(place, account):
    """Liknar profilen kunden? Samma domän som hemsidan eller ett gemensamt
    ord i namnet (places.matches), eller samma telefonnummer som en
    bekräftad uppgift."""
    from .sms import normalize_phone

    if places.matches(place, account):
        return True
    phone = normalize_phone(str(place.get("nationalPhoneNumber") or ""))
    return bool(phone) and phone in _phones(account)


def store_details(account, place, place_id, now=None):
    """Spara Place Details på kontot: namnet, länken, betyget, antalet och
    omdömena (nyast först). Kundens val står kvar, också för ett omdöme som
    inte kom med den här gången (selected_google_reviews hoppar över det
    som saknas, och det syns igen när Google ger det igen); ett nytt Place
    ID nollställer valet.

    Profilen prövas mot kunden (_owned). Liknar den kunden, eller har någon
    intygat att just den här profilen är kundens (confirm_owner), blir
    betyget en bekräftad uppgift. Annars sätts google_place_unverified,
    betyget sparas obekräftat och byrån larmas (en gång, när profilen blir
    misstänkt)."""
    now = now or timezone.now()
    reviews = [r for r in (_review(raw) for raw in place.get("reviews") or []) if r]
    reviews.sort(key=lambda r: r["time"], reverse=True)
    reviews = reviews[:MAX_REVIEWS]
    count = place.get("userRatingCount")
    owned = _owned(place, account)
    with transaction.atomic():
        row = FlamingoAccount.objects.select_for_update().get(pk=account.pk)
        same_place = row.google_place_id == place_id
        selected = list(row.google_reviews_selected or []) if same_place else []
        confirmed_at = row.google_place_confirmed_at if same_place else None
        confirmed_by_id = row.google_place_confirmed_by_id if same_place else None
        was_unverified = row.google_place_unverified and same_place
        unverified = not owned and confirmed_at is None
        account.google_place_id = place_id
        account.google_place_name = _text(place.get("displayName"))[:200]
        account.google_maps_uri = google_link(place.get("googleMapsUri"))
        account.google_rating = _decimal_rating(place.get("rating"))
        account.google_review_count = count if isinstance(count, int) and count >= 0 else None
        account.google_reviews = reviews
        account.google_reviews_selected = selected
        account.google_reviews_fetched_at = now
        account.google_place_unverified = unverified
        account.google_place_confirmed_at = confirmed_at
        account.google_place_confirmed_by_id = confirmed_by_id
        account.save(update_fields=PROFILE_FIELDS)
        store_rating_fact(account, place, confirmed=not unverified)
    if unverified and not was_unverified:
        _alert_unverified(account)
    return account


def _alert_unverified(account):
    """Byråns larm om en profil som inte liknar kunden (aldrig kunden)."""
    name = account.google_place_name or account.google_place_id
    alerts.send_account_alert(
        account,
        f"Flamingo: Google-profilen {name} liknar inte {account.customer.name}",
        [
            f"{account.customer.name} har kopplat Google-profilen {name} "
            f"(Place ID {account.google_place_id}) till sina landningssidor.",
            "Profilen har inte samma hemsida, inget gemensamt ord i namnet och inte samma "
            "telefonnummer som kundens uppgifter. Betyget sparades obekräftat, och varken "
            "omdömena eller betyget syns på sidorna förrän kunden eller ADX intygat att "
            'profilen är kundens (Omdömen från Google, "Profilen är vår").',
            f"Profilen på Google Maps: {account.google_maps_uri or 'saknas'}",
            "",
            "Kunden har inte mejlats.",
        ],
    )


def confirm_owner(account, user=None, now=None):
    """ "Profilen är vår": kunden eller byrån intygar att profilen är
    kundens. Omdömena och betyget får då synas, och betyget blir en
    bekräftad uppgift. Byrån larmas med vem som intygade (aldrig kunden).
    Kastar ReviewsError utan profil."""
    if not account.google_place_id:
        raise ReviewsError("Det finns ingen profil att intyga. Koppla din Google-profil först.")
    now = now or timezone.now()
    with transaction.atomic():
        FlamingoAccount.objects.select_for_update().only("pk").get(pk=account.pk)
        account.google_place_unverified = False
        account.google_place_confirmed_at = now
        account.google_place_confirmed_by = user if getattr(user, "pk", None) else None
        account.save(
            update_fields=[
                "google_place_unverified",
                "google_place_confirmed_at",
                "google_place_confirmed_by",
                "updated_at",
            ]
        )
        place = {
            "rating": float(account.google_rating) if account.google_rating is not None else None,
            "userRatingCount": account.google_review_count,
        }
        store_rating_fact(account, place, confirmed=True)
    who = ""
    if user is not None and getattr(user, "pk", None):
        who = user.get_full_name() or user.get_username()
    alerts.send_account_alert(
        account,
        f"Flamingo: Google-profilen för {account.customer.name} intygad",
        [
            f"{who or 'Någon'} intygade att Google-profilen "
            f"{account.google_place_name or account.google_place_id} "
            f"(Place ID {account.google_place_id}) är {account.customer.name}s.",
            "Profilen liknade inte kunden när den hämtades. Omdömena och betyget får nu "
            "synas på sidorna. Kontrollera gärna att den är rätt:",
            f"Profilen på Google Maps: {account.google_maps_uri or 'saknas'}",
            "",
            "Kunden har inte mejlats.",
        ],
    )
    return account


def connect(account, place_id, *, now=None):
    """ "Det här är vi": Place Details hämtas och sparas. Kastar ReviewsError
    (demot, ingen nyckel, fel id, gränsen, Google svarade inte)."""
    refused = refusal(account)
    if refused:
        raise ReviewsError(refused)
    place_id = clean_place_id(place_id)
    if not place_id:
        raise ReviewsError(BAD_PLACE_ID)
    if not limits.reserve_daily(account, USAGE_DETAILS, DETAILS_DAILY_MAX, now=now):
        raise ReviewsError(DETAILS_LIMIT)
    place = fetch_details(place_id)
    returned = clean_place_id(place.get("id")) or place_id
    return store_details(account, place, returned, now=now)


def save_place_id(account, place_id):
    """Utan nyckeln (eller i demot): bara id:t sparas, inget hämtas. Byts
    id:t glöms allt som gällde den förra profilen."""
    place_id = clean_place_id(place_id)
    if not place_id:
        raise ReviewsError(BAD_PLACE_ID)
    if account.google_place_id != place_id:
        _clear(account, keep_place_id=False)
    account.google_place_id = place_id
    account.save(update_fields=["google_place_id", "updated_at"])
    return account


def select(account, ids):
    """Kundens val i kundens ordning: bara id:n bland de hämtade omdömena,
    varje id en gång."""
    known = {str(r.get("id")) for r in account.google_reviews or [] if isinstance(r, dict)}
    chosen = []
    for review_id in ids or []:
        review_id = str(review_id)
        if review_id in known and review_id not in chosen:
            chosen.append(review_id)
    account.google_reviews_selected = chosen[:MAX_REVIEWS]
    account.save(update_fields=["google_reviews_selected", "updated_at"])
    return chosen


def _clear(account, *, keep_place_id):
    """Allt innehåll från Google bort. keep_place_id: id:t, länken, kundens
    val och vem som intygat profilen står kvar (de får sparas; se
    villkoren)."""
    account.google_place_name = ""
    account.google_rating = None
    account.google_review_count = None
    account.google_reviews = []
    if not keep_place_id:
        account.google_place_id = ""
        account.google_maps_uri = ""
        account.google_reviews_selected = []
        account.google_reviews_fetched_at = None
        account.google_place_unverified = False
        account.google_place_confirmed_at = None
        account.google_place_confirmed_by = None
    account.save(update_fields=PROFILE_FIELDS)
    _forget_rating_fact(account)


def disconnect(account):
    """ "Koppla bort profilen": id:t, namnet, betyget, omdömena och kundens
    val tas bort, och betygsuppgiften från Google. Blocket syns inte längre."""
    with transaction.atomic():
        _clear(account, keep_place_id=False)


# ---------------------------------------------------------------------------
# Cron: hämta igen, och ta bort det som blivit för gammalt
# ---------------------------------------------------------------------------


@dataclass
class RefreshSummary:
    fetched: int = 0
    failed: int = 0
    expired: int = 0
    skipped: str = ""
    errors: list = field(default_factory=list)


def _real_accounts():
    return FlamingoAccount.objects.filter(
        is_enabled=True, is_demo=False, customer__is_active=True
    ).select_related("customer")


def expire(now=None, account_pk=None):
    """Profiler vars innehåll inte hämtats på MAX_AGE: texterna, betyget,
    antalet och namnet tas bort (Place ID, länken och valet står kvar, så
    allt kommer tillbaka när en hämtning går igen). Aldrig demot. Returnerar
    antalet konton."""
    now = now or timezone.now()
    stale = (
        FlamingoAccount.objects.filter(is_demo=False, google_reviews_fetched_at__lt=now - MAX_AGE)
        .exclude(google_reviews=[], google_rating__isnull=True, google_place_name="")
        .order_by("pk")
    )
    if account_pk is not None:
        stale = stale.filter(pk=account_pk)
    count = 0
    for account in stale:
        with transaction.atomic():
            _clear(account, keep_place_id=True)
        count += 1
    return count


def refresh_due(now=None, account_pk=None):
    """Profilerna som ska hämtas igen (aldrig hämtade, eller hämtade för mer
    än REFRESH_EVERY sedan), sedan expire. Utan nyckeln hämtas inget, men
    det som blivit för gammalt tas ändå bort. Demot rörs aldrig."""
    now = now or timezone.now()
    summary = RefreshSummary()
    if is_configured():
        due = (
            _real_accounts()
            .exclude(google_place_id="")
            .filter(
                Q(google_reviews_fetched_at__isnull=True)
                | Q(google_reviews_fetched_at__lt=now - REFRESH_EVERY)
            )
            .order_by("google_reviews_fetched_at", "pk")
        )
        if account_pk is not None:
            due = due.filter(pk=account_pk)
        for account in due[:REFRESH_PER_RUN]:
            try:
                place = fetch_details(account.google_place_id)
                returned = clean_place_id(place.get("id")) or account.google_place_id
                store_details(account, place, returned, now=now)
                summary.fetched += 1
            except ReviewsError as exc:
                summary.failed += 1
                summary.errors.append(f"{account.customer.name}: {exc.message}")
            except Exception:  # noqa: BLE001 - ett konto stoppar inte de andra
                logger.exception("Flamingo: omdömena för konto %s", account.pk)
                summary.failed += 1
                summary.errors.append(f"{account.customer.name}: oväntat fel, se loggen.")
    else:
        summary.skipped = "GOOGLE_PLACES_API_KEY saknas"
    summary.expired = expire(now, account_pk=account_pk)
    return summary
