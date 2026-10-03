"""
Omdömen från Reco (reco.se) i sidbyggaren, Giovannis beslut 2026-10-04:
kunden klistrar in länken till sin sida på Reco, eller Recos id, säger "Det
här är vi", och blocket "Omdömen från Reco" visar Recos egen ruta (en iframe
från widget.reco.se) på sidorna.

    parse_link(text)              RecoLink ur en länk eller ett id, utan anrop
    clean_venue_id(value)         id:t som siffror, eller ""
    clean_slug(value)             företagets namn i Recos adress, eller ""
    profile_url(slug)             https://www.reco.se/<slug>, eller ""
    profile_link(value)           en sparad länk till en profil på Reco, prövad, eller ""
    widget_src(venue_id, orientation, size)
                                  iframe-adressen, byggd bara av siffrorna i id:t
    frames(variant, venue_id)     blockets iframes för variant: [Frame]
    parse_profile(html)           Profile ur profilsidan, eller None
    parse_widget(html, venue_id)  företagets namn i Recos adress ur widgeten, eller ""
    refusal(account)              varför Reco inte får anropas för kontot, eller ""
    connect(account, link, now=None)
                                  "Det här är vi": profilsidan hämtas (högst
                                  LOOKUP_DAILY_MAX gånger per konto och dag),
                                  prövas mot kunden och sparas
    store(account, profile, now=None)
    owner_match(profile, account) vad som stämmer med kunden, eller ""
    confirm_owner(account, user=None)
                                  "Profilen är vår": kunden eller byrån intygar
    disconnect(account)           "Koppla bort profilen"
    expire(now=None, account_pk=None)
                                  cron (flamingo_google_sync): namnet, betyget och
                                  antalet bort efter MAX_AGE utan en ny hämtning
    venue_id_taken(venue_id, exclude_pk=None)

Undersökt 2026-10-04 (bara publika sidor, några få anrop):

- Id:t. Profilsidan (https://www.reco.se/cs-auto-ab) har id:t (5998572 för
  CS Auto AB) i window.VenueData ("venueId", med namnet, betyget, antalet,
  hemsidan och telefonnumret), i window.PaginationData och i data-venue-id
  på knapparna. JSON-LD (schema.org) har namnet, adressen, telefonnumret,
  hemsidan (sameAs), betyget och de fem senaste omdömena, men inte id:t.
  parse_profile läser VenueData först, sedan PaginationData, och sist det
  id som flest data-venue-id har (andra företag kan stå på samma sida).
  Säger VenueData och PaginationData olika ges inget id alls.
- Bara id:t. https://www.reco.se/v/venue/<id> ger 404. Med ett id hämtas i
  stället widgeten (widget.reco.se/v2/venues/<id>/vertical/small), som har
  företagets adressnamn (slug), och sedan profilsidan.
- Widgeten är en SvelteKit-sida som ritas på Recos server. Betyget,
  antalet och omdömena (texten avkortad till ungefär 90 tecken, förnamn och
  initial, betyg, datum och trustState) står i sidan; inget öppet JSON-API
  anropas, och SvelteKits __data.json svarar 404. Skriptet i rutan anropar
  widget.reco.se/widget/loaded (Reco räknar visningen) och länkarna går via
  widget.reco.se/widget/clicked. Inga kakor sätts.
- Villkoren (Medlemsvillkor för webbsöktjänsten reco.se,
  https://www.reco.se/info/terms, uppdaterade 2026-09-22, avsnitt 7,
  Immateriella rättigheter): Reco Sverige AB äger rättigheterna till
  omdömena och materialet på sajten, och materialet får inte kopieras,
  spridas eller göras tillgängligt för andra i kommersiella sammanhang utan
  Recos uttryckliga skriftliga medgivande. Widgetarna ingår i Recos
  lösning för företag, och ett API erbjuds bara som en skräddarsydd lösning
  (https://www.reco.se/foretag/priser). Därför visar sidorna bara Recos
  egna widgetar, och inga omdömestexter hämtas eller sparas här. En egen
  ruta med utvalda omdömen kräver Recos skriftliga medgivande eller deras
  API (README, "Omdömen från Reco").

Profilen måste vara kundens: vem som helst kan klistra in vilken länk som
helst. store prövar profilen mot kunden: samma domän som hemsidan
(FlamingoAccount.website_url) eller samma telefonnummer som en bekräftad
uppgift. Namnet räcker inte (två verkstäder kan båda heta något med
"Auto"). Liknar den inte kunden sparas den med reco_unverified, inget från
Reco syns på sidorna, och byrån larmas, tills kunden eller byrån intygat den
(confirm_owner). Ett id som ett annat riktigt konto redan har tas aldrig
emot (och databasen har en regel för det, flamingo_reco_id_unique); byrån
larmas.

Varje anrop går till https://www.reco.se/<slug> eller
https://widget.reco.se/v2/venues/<id>/vertical/small, där slug och id prövats
mot SLUG_RE och VENUE_ID_RE: kundens text styr aldrig vart anropet går.
Hämtningen går genom apps/tools/analyzer.fetch (SSRF-skyddet, storleken och
tidsgränsen), och varje omdirigering prövas mot RECO_HOSTS innan den följs.
Ett demokonto anropar aldrig Reco.

Namnet, betyget och antalet sparas bara för verktyget (kunden ser att
profilen är rätt) och tas bort efter MAX_AGE utan en ny hämtning (samma
regel som för Google, Giovannis beslut 2026-10-03). Betyget blir aldrig en
uppgift (Fact) och används aldrig i annonserna eller förslagen
(models.RATING_SOURCES); det syns bara i Recos egen ruta.
"""

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote, urlsplit

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.common.security import normalize_typography
from apps.tools.analyzer import AnalysError, fetch

from . import alerts, limits
from .models import FlamingoAccount
from .scan import registrable

logger = logging.getLogger(__name__)

PROFILE_HOST = "www.reco.se"
WIDGET_HOST = "widget.reco.se"
#: Värdarna en hämtning får gå till, också efter en omdirigering.
RECO_HOSTS = (PROFILE_HOST, WIDGET_HOST)
PROFILE_BASE = f"https://{PROFILE_HOST}/"
WIDGET_URL = (
    f"https://{WIDGET_HOST}/v2/venues/{{venue_id}}/{{orientation}}/{{size}}"
    "?inverted=false&border=true&lang=sv"
)

#: Recos id: bara siffror, ingen nolla först (prövas med fullmatch).
VENUE_ID_RE = re.compile(r"[1-9][0-9]{0,11}")
#: Företagets namn i Recos adress (reco.se/cs-auto-ab), fullmatch.
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SLUG_MAX = 100
#: Första delen av en adress på reco.se som inte är ett företag.
RESERVED = frozenset(
    {
        "api",
        "assets",
        "blog",
        "foretag",
        "images",
        "info",
        "kategori",
        "logga-in",
        "m",
        "mobile",
        "object",
        "p",
        "profile",
        "q",
        "r",
        "s",
        "searchapi",
        "searchapi-branding",
        "share",
        "sok",
        "u",
        "user",
        "v",
        "widget",
    }
)

#: Profilsidan är ungefär 280 kB; id:t och uppgifterna står i de första 15.
MAX_BYTES = 512 * 1024
TIME_LIMIT = 10

#: Hämtningar per konto och svenskt dygn ("Det här är vi" och "Hämta
#: igen"; en hämtning med bara id:t är två anrop men räknas som en).
LOOKUP_DAILY_MAX = 5
USAGE_LOOKUP = "reco_lookup"
#: Namnet, betyget och antalet tas bort så här länge efter den senaste
#: hämtningen (samma regel som Googles omdömen).
MAX_AGE = timedelta(days=90)

#: Giovannis widgetar från Reco: {orientation: {size: höjd i px}}. De
#: liggande är 100 % breda, de stående 300 px (VERTICAL_WIDTH).
WIDGET_SIZES = {
    "horizontal": {"xlarge": 225, "large": 60, "medium": 64, "small": 27},
    "vertical": {"medium": 150, "small": 120},
}
VERTICAL_WIDTH = 300
#: Blockets varianter (registry.py): (bred skärm, smal skärm), där None
#: betyder samma ruta på alla skärmar. Smal skärm är under 720 px
#: (flamingo-lp-ren.css). Den stora liggande rutan ryms i mobilen; den medel
#: blir den stående i mobilen, som Giovanni säger passar mobilen bäst.
VARIANT_WIDGETS = {
    "stor": (("horizontal", "xlarge"), None),
    "medel": (("horizontal", "large"), ("vertical", "medium")),
    "liten": (("horizontal", "small"), None),
    "staende": (("vertical", "medium"), None),
}
DEFAULT_VARIANT = "stor"
IFRAME_TITLE = "Omdömen på Reco"

#: Värdar som många företag delar: samma domän där säger inget om ägaren.
SHARED_HOSTS = frozenset(
    {
        "facebook.com",
        "instagram.com",
        "linkedin.com",
        "google.com",
        "business.site",
        "wixsite.com",
        "wordpress.com",
        "blogspot.com",
        "one.com",
        "hitta.se",
        "eniro.se",
        "reco.se",
        "bokadirekt.se",
        "mittanbud.se",
        "offerta.se",
    }
)

EMPTY = "Klistra in länken till er sida på Reco, eller Recos id."
NOT_A_LINK = (
    "Det där är inte en länk till Reco. Klistra in länken till er sida på Reco "
    "(reco.se/ert-foretag), eller Recos id."
)
NOT_A_PROFILE = "Länken går inte till ett företag på Reco. Klistra in länken till er sida på Reco."
REVIEW_LINK = "Det där är en länk till ett omdöme. Klistra in länken till företagets sida på Reco."
WIDGET_WITHOUT_ID = (
    "Länken till widgeten har inget id. Klistra in hela adressen ur widgetens kod, "
    "eller länken till er sida på Reco."
)
BAD_ID = "Recos id är bara siffror, till exempel 5998572."
DEMO_REFUSED = "Det här är ett demokonto, så Reco anropas aldrig."
RECO_DOWN = "Reco svarade inte. Försök igen om en stund."
NOT_FOUND = "Reco har ingen sida på den adressen. Kontrollera länken och försök igen."
ID_NOT_FOUND = "Reco har inget företag med det id:t. Kontrollera id:t, eller klistra in länken."
NO_ID = "Vi hittade inget id på sidan. Kontrollera att länken går till ett företag på Reco."
ID_MISMATCH = "Sidan på Reco har ett annat id än det du angav. Kontrollera id:t."
LOOKUP_LIMIT = (
    f"Reco har frågats {LOOKUP_DAILY_MAX} gånger i dag, och det är gränsen. Försök igen i morgon."
)
TAKEN = (
    "Den här profilen på Reco är redan kopplad till ett annat företag hos ADX, så den kan "
    "inte kopplas här. ADX har fått veta det. Är profilen er, hör av dig till ADX."
)
NOTHING_TO_CONFIRM = "Det finns ingen profil att intyga. Koppla er profil på Reco först."


class RecoError(ValueError):
    """Något stoppade hämtningen eller ändringen. message är en svensk text
    för kunden; Recos eget svar visas aldrig."""

    def __init__(self, message):
        self.message = str(message)
        super().__init__(self.message)


def refusal(account):
    """Varför Reco inte får anropas för kontot, eller "" (det får det)."""
    if getattr(account, "is_demo", False):
        return DEMO_REFUSED
    return ""


# ---------------------------------------------------------------------------
# Länken, id:t och adresserna
# ---------------------------------------------------------------------------


def clean_venue_id(value):
    if isinstance(value, bool) or value is None:
        return ""
    value = str(value).strip()
    return value if VENUE_ID_RE.fullmatch(value) else ""


def clean_slug(value):
    value = str(value or "").strip().lower()
    if len(value) > SLUG_MAX or value in RESERVED or not SLUG_RE.fullmatch(value):
        return ""
    return value


def profile_url(slug):
    """Profilens adress hos Reco, byggd bara av en prövad slug, eller ""."""
    slug = clean_slug(slug)
    return PROFILE_BASE + slug if slug else ""


def profile_link(value):
    """En sparad länk till en profil på Reco, eller "": exakt
    https://www.reco.se/<slug>. Prövas när den sparas och när den ritas."""
    value = str(value or "")
    if not value.startswith(PROFILE_BASE):
        return ""
    return value if profile_url(value[len(PROFILE_BASE) :]) == value else ""


@dataclass
class RecoLink:
    """Det som går att läsa ur det kunden klistrat in, utan anrop: företagets
    namn i adressen (slug), eller id:t, eller error (en text för kunden)."""

    slug: str = ""
    venue_id: str = ""
    error: str = ""

    @property
    def value(self):
        """Det formuläret "Det här är vi" skickar, och som läses om när det
        kommer tillbaka (aldrig något som sparas direkt)."""
        return profile_url(self.slug) or self.venue_id

    @property
    def shown(self):
        """Så som kunden ser det: reco.se/cs-auto-ab eller id:t."""
        return f"reco.se/{self.slug}" if self.slug else self.venue_id


_HOSTISH = re.compile(r"^(?:[a-z0-9-]+\.)*reco\.se(?:[/?#:]|$)")
#: En adress i en längre text (Recos inbäddningskod).
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+", re.I)


def parse_link(text):
    """RecoLink ur en länk eller ett id, utan anrop:

        5998572                                          id:t
        https://www.reco.se/cs-auto-ab                   cs-auto-ab
        reco.se/cs-auto-ab/omdomen?sida=2#topp           cs-auto-ab
        https://www.reco.se/share/cs-auto-ab/review/1    cs-auto-ab
        https://widget.reco.se/v2/venues/5998572/...     id:t
        https://www.reco.se/v/venue/5998572/spontaneous  id:t

    Hela inbäddningskoden för Recos widget (<iframe src="...">) går också:
    adressen i den läses. En länk till ett omdöme, till en annan sajt eller
    till något på Reco som inte är ett företag ger error."""
    raw = str(text or "").strip()[:2000]
    embedded = _URL_IN_TEXT.search(raw)
    if embedded and embedded.group(0) != raw:
        raw = embedded.group(0)
    text = "".join(raw.split())[:500]
    if not text:
        return RecoLink(error=EMPTY)
    if text.isdigit():
        venue_id = clean_venue_id(text)
        return RecoLink(venue_id=venue_id) if venue_id else RecoLink(error=BAD_ID)
    lowered = text.lower()
    if "://" not in lowered:
        if not _HOSTISH.match(lowered):
            return RecoLink(error=NOT_A_LINK)
        text = "https://" + text
    try:
        parts = urlsplit(text)
        parts.port  # noqa: B018 - en trasig port kastar ValueError
    except ValueError:
        return RecoLink(error=NOT_A_LINK)
    host = (parts.hostname or "").lower().rstrip(".")
    if (
        parts.scheme.lower() not in ("http", "https")
        or not (host == "reco.se" or host.endswith(".reco.se"))
        or "@" in parts.netloc
    ):
        return RecoLink(error=NOT_A_LINK)
    path = unquote(parts.path)
    match = re.search(r"/venues?/(\d{1,20})(?:/|$)", path)
    if match:
        venue_id = clean_venue_id(match.group(1))
        return RecoLink(venue_id=venue_id) if venue_id else RecoLink(error=BAD_ID)
    if host == WIDGET_HOST:
        return RecoLink(error=WIDGET_WITHOUT_ID)
    segments = [s for s in path.split("/") if s]
    if not segments:
        return RecoLink(error=NOT_A_PROFILE)
    first = segments[0].lower()
    if first == "share" and len(segments) > 1:
        # Recos delningslänk för ett omdöme: /share/<slug>/review/<id>.
        first = segments[1].lower()
    elif first in ("r", "review") or (
        first == "v" and len(segments) > 1 and segments[1].lower() == "review"
    ):
        return RecoLink(error=REVIEW_LINK)
    slug = clean_slug(first)
    return RecoLink(slug=slug) if slug else RecoLink(error=NOT_A_PROFILE)


def widget_src(venue_id, orientation, size):
    """Adressen till Recos widget, eller "": byggd bara av id:t (siffror,
    VENUE_ID_RE) och en av Giovannis storlekar (WIDGET_SIZES)."""
    venue_id = clean_venue_id(venue_id)
    if not venue_id or size not in WIDGET_SIZES.get(orientation, {}):
        return ""
    return WIDGET_URL.format(venue_id=venue_id, orientation=orientation, size=size)


@dataclass(frozen=True)
class Frame:
    """En iframe i blocket. screen är "all", "wide" (från 720 px) eller
    "narrow" (under 720 px); CSS döljer den som inte gäller, och en dold
    iframe med loading=lazy laddas aldrig."""

    src: str
    orientation: str
    size: str
    height: int
    screen: str = "all"
    title: str = IFRAME_TITLE

    @property
    def width(self):
        """Bredden i px för en stående ruta, None för en liggande (100 %)."""
        return VERTICAL_WIDTH if self.orientation == "vertical" else None


def frames(variant, venue_id):
    """Blockets iframes för variant, eller [] när id:t inte går att använda."""
    wide, narrow = VARIANT_WIDGETS.get(variant) or VARIANT_WIDGETS[DEFAULT_VARIANT]
    screens = (("all", wide),) if narrow is None else (("wide", wide), ("narrow", narrow))
    out = []
    for screen, (orientation, size) in screens:
        src = widget_src(venue_id, orientation, size)
        if not src:
            return []
        out.append(Frame(src, orientation, size, WIDGET_SIZES[orientation][size], screen))
    return out


# ---------------------------------------------------------------------------
# Sidorna hos Reco
# ---------------------------------------------------------------------------


@dataclass
class Profile:
    """Det profilsidan säger om företaget. website och phone används bara
    för att pröva att profilen är kundens; de sparas inte."""

    venue_id: str
    slug: str = ""
    name: str = ""
    rating: Decimal | None = None
    count: int | None = None
    website: str = ""
    phone: str = ""
    city: str = ""


_VENUE_DATA = re.compile(
    r"window\.VenueData\s*=\s*\(\s*function\s*\(\s*\)\s*\{\s*var\s+data\s*=\s*"
)
_PAGINATION = re.compile(r"window\.PaginationData\s*=\s*")
_LD_JSON = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S
)
_CANONICAL = re.compile(r"<link\b[^>]*\brel=[\"']canonical[\"'][^>]*>", re.I)
_HREF = re.compile(r"\bhref=[\"']([^\"']+)[\"']", re.I)
_DATA_VENUE_ID = re.compile(r"data-venue-id=[\"'](\d{1,20})[\"']")
_VENUE_LINK = re.compile(r"/v/venue/(\d{1,20})/")


def _json_at(html, pattern):
    """JSON-objektet som börjar där pattern slutar, eller {}."""
    match = pattern.search(html)
    if not match:
        return {}
    try:
        value, _end = json.JSONDecoder().raw_decode(html, match.end())
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _ld_business(html):
    """Företaget i sidans JSON-LD (det med betyg eller en adress på
    reco.se), eller {}."""
    for raw in _LD_JSON.findall(html):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        items = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            items = data["@graph"]
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            if isinstance(item.get("aggregateRating"), dict) or url.startswith(PROFILE_BASE):
                return item
    return {}


def _text(value, limit=200):
    if not isinstance(value, str | int) or isinstance(value, bool):
        return ""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value))
    return " ".join(normalize_typography(text).split())[:limit]


def _rating(value):
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        rating = Decimal(str(round(float(value), 1)))
    except (InvalidOperation, ValueError, OverflowError):
        return None
    return rating if Decimal("0") <= rating <= Decimal("5") else None


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int | str):
        return None
    try:
        count = int(value)
    except ValueError:
        return None
    return count if 0 <= count < 10_000_000 else None


def _first(*values, parse):
    for value in values:
        parsed = parse(value)
        if parsed is not None and parsed != "":
            return parsed
    return None


def _website(value):
    values = value if isinstance(value, list) else [value]
    for item in values:
        item = str(item or "").strip()
        if not item.startswith(("http://", "https://")) or len(item) > 300:
            continue
        try:
            host = urlsplit(item).hostname or ""
        except ValueError:
            continue
        if host and "." in host and not host.endswith("reco.se"):
            return item
    return ""


def _slug_of(url):
    url = str(url or "")
    if not url.startswith(PROFILE_BASE):
        return ""
    return clean_slug(url[len(PROFILE_BASE) :].split("?")[0].split("#")[0].strip("/"))


def _venue_id_of(html, venue_data, pagination):
    """Profilens id: VenueData, sedan PaginationData (de får inte säga olika),
    sist det id som mer än hälften av data-venue-id och skrivlänkarna har."""
    strong = [
        clean_venue_id(source.get("venueId"))
        for source in (venue_data, pagination)
        if source.get("venueId") is not None
    ]
    strong = [value for value in strong if value]
    if len(set(strong)) > 1:
        return ""
    if strong:
        return strong[0]
    counts = Counter(
        clean_venue_id(value) for value in _DATA_VENUE_ID.findall(html) + _VENUE_LINK.findall(html)
    )
    counts.pop("", None)
    if not counts:
        return ""
    top, hits = counts.most_common(1)[0]
    return top if hits * 2 > sum(counts.values()) else ""


def parse_profile(html):
    """Profile ur profilsidan, eller None när inget id går att lita på."""
    html = str(html or "")
    venue_data = _json_at(html, _VENUE_DATA)
    pagination = _json_at(html, _PAGINATION)
    venue_id = _venue_id_of(html, venue_data, pagination)
    if not venue_id:
        return None
    business = _ld_business(html)
    aggregate = business.get("aggregateRating")
    aggregate = aggregate if isinstance(aggregate, dict) else {}
    address = business.get("address") if isinstance(business.get("address"), dict) else {}
    canonical = ""
    tag = _CANONICAL.search(html)
    if tag:
        href = _HREF.search(tag.group(0))
        canonical = _slug_of(href.group(1)) if href else ""
    slug = canonical or _first(
        venue_data.get("slug"),
        venue_data.get("restful"),
        _slug_of(business.get("url")),
        parse=clean_slug,
    )
    return Profile(
        venue_id=venue_id,
        slug=slug or "",
        name=_first(
            venue_data.get("venueName"),
            business.get("name"),
            pagination.get("venueName"),
            parse=_text,
        )
        or "",
        rating=_first(
            venue_data.get("rating"),
            aggregate.get("ratingValue"),
            pagination.get("venueRating"),
            parse=_rating,
        ),
        count=_first(
            venue_data.get("reviewCount"),
            aggregate.get("ratingCount"),
            aggregate.get("reviewCount"),
            pagination.get("venueReviewCount"),
            parse=_count,
        ),
        website=_website(venue_data.get("website")) or _website(business.get("sameAs")),
        phone=_first(
            venue_data.get("phoneNumber"),
            venue_data.get("phone"),
            business.get("telephone"),
            parse=lambda v: _text(v, 40),
        )
        or "",
        city=_first(venue_data.get("city"), address.get("addressLocality"), parse=_text) or "",
    )


_WIDGET_ENTITY = re.compile(r"\"?entityId\"?\s*:\s*\"?(\d{1,20})\"?")
_WIDGET_SLUG = re.compile(r"\"?\bslug\"?\s*:\s*\"([a-z0-9-]{1,100})\"")


def parse_widget(html, venue_id):
    """Företagets adressnamn (slug) ur Recos widget för venue_id, eller "".
    Widgetens data är ett JavaScript-objekt i kit.start(...), inte JSON."""
    html = str(html or "")
    entity = _WIDGET_ENTITY.search(html)
    if not entity or clean_venue_id(entity.group(1)) != clean_venue_id(venue_id):
        return ""
    slug = _WIDGET_SLUG.search(html, entity.start())
    return clean_slug(slug.group(1)) if slug else ""


def _fetch(url):
    """Sidan på url (en av Recos värdar), som analyzer.Sida. Kastar
    RecoError med en allmän text; Recos svar loggas kort, aldrig visas."""
    try:
        return fetch(url, max_bytes=MAX_BYTES, time_limit=TIME_LIMIT, hosts=RECO_HOSTS)
    except AnalysError as exc:
        text = str(exc)
        logger.info("Flamingo: Reco svarade inte som väntat: %s", text[:200])
        if re.search(r"HTTP (404|410)$", text):
            raise RecoError(NOT_FOUND) from None
        raise RecoError(RECO_DOWN) from None
    except Exception as exc:  # noqa: BLE001 - nätet, tidsgränsen: samma text
        logger.warning("Flamingo: Reco svarade inte: %s", type(exc).__name__)
        raise RecoError(RECO_DOWN) from None


def fetch_profile(link):
    """Profilen för link (RecoLink, prövad), som Profile. Kastar RecoError.
    Ingen spärr här: den som anropar bokför (connect)."""
    slug = clean_slug(link.slug)
    venue_id = clean_venue_id(link.venue_id)
    if not slug:
        if not venue_id:
            raise RecoError(BAD_ID)
        try:
            page = _fetch(widget_src(venue_id, "vertical", "small"))
        except RecoError as exc:
            raise RecoError(ID_NOT_FOUND if exc.message == NOT_FOUND else exc.message) from None
        slug = parse_widget(page.html, venue_id)
        if not slug:
            raise RecoError(ID_NOT_FOUND)
    page = _fetch(profile_url(slug))
    profile = parse_profile(page.html)
    if profile is None:
        raise RecoError(NO_ID)
    if venue_id and profile.venue_id != venue_id:
        raise RecoError(ID_MISMATCH)
    if not profile.slug:
        profile.slug = slug
    return profile


# ---------------------------------------------------------------------------
# Att spara: profilen, ägaren och larmen till byrån
# ---------------------------------------------------------------------------

PROFILE_FIELDS = [
    "reco_venue_id",
    "reco_url",
    "reco_name",
    "reco_rating",
    "reco_review_count",
    "reco_fetched_at",
    "reco_unverified",
    "reco_confirmed_at",
    "reco_confirmed_by",
    "updated_at",
]

#: Vad owner_match säger att profilen delar med kunden.
MATCH_WEBSITE = "hemsidan"
MATCH_PHONE = "telefonnumret"


def venue_id_taken(venue_id, exclude_pk=None):
    """Har ett annat konto (inte demot) redan profilen?"""
    venue_id = clean_venue_id(venue_id)
    if not venue_id:
        return False
    others = FlamingoAccount.objects.filter(reco_venue_id=venue_id, is_demo=False)
    if exclude_pk is not None:
        others = others.exclude(pk=exclude_pk)
    return others.exists()


def _domain(url):
    try:
        host = urlsplit(str(url or "")).hostname or ""
    except ValueError:
        return ""
    domain = registrable(host)
    return "" if not domain or domain in SHARED_HOSTS else domain


def owner_match(profile, account):
    """Liknar profilen kunden? MATCH_WEBSITE när hemsidan på Reco har samma
    domän som kundens hemsida (inte en delad värd som facebook.com),
    MATCH_PHONE när numret på Reco finns bland kundens bekräftade nummer,
    annars "". Namnet räcker aldrig."""
    from .reviews import _phones
    from .sms import normalize_phone

    ours = _domain(account.website_url)
    if ours and ours == _domain(profile.website):
        return MATCH_WEBSITE
    phone = normalize_phone(profile.phone)
    if phone and phone in _phones(account):
        return MATCH_PHONE
    return ""


def store(account, profile, now=None):
    """Spara profilen på kontot: id:t, länken, namnet, betyget och antalet.
    Returnerar vad som stämde med kunden (owner_match), eller "".

    Ett id som ett annat riktigt konto har tas aldrig emot: RecoError
    (TAKEN), och byrån larmas. Liknar profilen kunden, eller har någon
    intygat just den här profilen (confirm_owner), syns den på sidorna.
    Annars sätts reco_unverified och byrån larmas (en gång, när profilen
    blir misstänkt). Ett nytt id glömmer vem som intygat det förra."""
    now = now or timezone.now()
    venue_id = clean_venue_id(profile.venue_id)
    if not venue_id:
        raise RecoError(NO_ID)
    if venue_id_taken(venue_id, exclude_pk=account.pk):
        _alert_taken(account, profile)
        raise RecoError(TAKEN)
    match = owner_match(profile, account)
    with transaction.atomic():
        row = FlamingoAccount.objects.select_for_update().get(pk=account.pk)
        same = row.reco_venue_id == venue_id
        confirmed_at = row.reco_confirmed_at if same else None
        confirmed_by_id = row.reco_confirmed_by_id if same else None
        was_unverified = row.reco_unverified and same
        unverified = not match and confirmed_at is None
        account.reco_venue_id = venue_id
        account.reco_url = profile_url(profile.slug)
        account.reco_name = _text(profile.name)
        account.reco_rating = profile.rating
        account.reco_review_count = profile.count
        account.reco_fetched_at = now
        account.reco_unverified = unverified
        account.reco_confirmed_at = confirmed_at
        account.reco_confirmed_by_id = confirmed_by_id
        try:
            with transaction.atomic():
                account.save(update_fields=PROFILE_FIELDS)
        except IntegrityError:
            # Ett annat konto hann spara samma id (flamingo_reco_id_unique).
            account.refresh_from_db()
            raise RecoError(TAKEN) from None
    if unverified and not was_unverified:
        _alert_unverified(account, profile)
    return match


def connect(account, link, *, now=None):
    """ "Det här är vi": profilen hämtas från Reco, prövas och sparas.
    link är en RecoLink eller texten kunden klistrat in. Returnerar vad som
    stämde med kunden (owner_match). Kastar RecoError (demot, en trasig
    länk, gränsen, Reco svarade inte, ett annat kontos profil)."""
    refused = refusal(account)
    if refused:
        raise RecoError(refused)
    if not isinstance(link, RecoLink):
        link = parse_link(link)
    if link.error:
        raise RecoError(link.error)
    if not limits.reserve_daily(account, USAGE_LOOKUP, LOOKUP_DAILY_MAX, now=now):
        raise RecoError(LOOKUP_LIMIT)
    profile = fetch_profile(link)
    return store(account, profile, now=now)


def stored_link(account):
    """Den sparade profilen som RecoLink (för "Hämta igen")."""
    slug = _slug_of(profile_link(account.reco_url))
    return RecoLink(slug=slug, venue_id=clean_venue_id(account.reco_venue_id))


def confirm_owner(account, user=None, now=None):
    """ "Profilen är vår": kunden eller byrån intygar att profilen är
    kundens. Recos ruta får då synas på sidorna. Byrån larmas med vem som
    intygade (aldrig kunden). Kastar RecoError utan profil."""
    if not account.reco_venue_id:
        raise RecoError(NOTHING_TO_CONFIRM)
    now = now or timezone.now()
    with transaction.atomic():
        FlamingoAccount.objects.select_for_update().only("pk").get(pk=account.pk)
        account.reco_unverified = False
        account.reco_confirmed_at = now
        account.reco_confirmed_by = user if getattr(user, "pk", None) else None
        account.save(
            update_fields=[
                "reco_unverified",
                "reco_confirmed_at",
                "reco_confirmed_by",
                "updated_at",
            ]
        )
    who = ""
    if user is not None and getattr(user, "pk", None):
        who = user.get_full_name() or user.get_username()
    alerts.send_account_alert(
        account,
        f"Flamingo: Reco-profilen för {account.customer.name} intygad",
        [
            f"{who or 'Någon'} intygade att profilen på Reco "
            f"{account.reco_name or account.reco_venue_id} (id {account.reco_venue_id}) "
            f"är {account.customer.name}s.",
            "Profilen liknade inte kunden när den hämtades: varken hemsidan eller "
            "telefonnumret stämde. Recos ruta kan nu synas på sidorna. Kontrollera gärna "
            "att den är rätt:",
            f"Profilen på Reco: {profile_link(account.reco_url) or 'saknas'}",
            "",
            "Kunden har inte mejlats.",
        ],
    )
    return account


def _clear(account, *, keep_profile):
    """Allt från Reco bort. keep_profile: id:t, länken och vem som intygat
    profilen står kvar (namnet, betyget och antalet tas bort)."""
    account.reco_name = ""
    account.reco_rating = None
    account.reco_review_count = None
    if not keep_profile:
        account.reco_venue_id = ""
        account.reco_url = ""
        account.reco_fetched_at = None
        account.reco_unverified = False
        account.reco_confirmed_at = None
        account.reco_confirmed_by = None
    account.save(update_fields=PROFILE_FIELDS)


def disconnect(account):
    """ "Koppla bort profilen": allt från Reco tas bort, och blocket syns
    inte längre på sidorna."""
    with transaction.atomic():
        _clear(account, keep_profile=False)


def expire(now=None, account_pk=None):
    """Profiler som inte hämtats på MAX_AGE: namnet, betyget och antalet tas
    bort (id:t och länken står kvar, så Recos ruta syns som förut). Aldrig
    demot. Returnerar antalet konton."""
    now = now or timezone.now()
    stale = (
        FlamingoAccount.objects.filter(is_demo=False, reco_fetched_at__lt=now - MAX_AGE)
        .exclude(reco_name="", reco_rating__isnull=True, reco_review_count__isnull=True)
        .order_by("pk")
    )
    if account_pk is not None:
        stale = stale.filter(pk=account_pk)
    count = 0
    for account in stale:
        with transaction.atomic():
            _clear(account, keep_profile=True)
        count += 1
    return count


def _alert_unverified(account, profile):
    """Byråns larm om en profil som inte liknar kunden (aldrig kunden)."""
    name = account.reco_name or account.reco_venue_id
    alerts.send_account_alert(
        account,
        f"Flamingo: Reco-profilen {name} liknar inte {account.customer.name}",
        [
            f"{account.customer.name} har kopplat profilen {name} på Reco "
            f"(id {account.reco_venue_id}) till sina landningssidor.",
            "Profilen har inte samma hemsida och inte samma telefonnummer som kundens "
            "uppgifter. Recos ruta syns inte på sidorna förrän kunden eller ADX intygat att "
            'profilen är kundens (Omdömen, "Profilen är vår").',
            f"Hemsidan på Reco: {profile.website or 'saknas'}",
            f"Kundens hemsida: {account.website_url or 'saknas'}",
            f"Profilen på Reco: {profile_link(account.reco_url) or 'saknas'}",
            "",
            "Kunden har inte mejlats.",
        ],
    )


def _alert_taken(account, profile):
    """Byråns larm när ett konto försöker koppla en profil som ett annat
    konto redan har."""
    other = (
        FlamingoAccount.objects.filter(reco_venue_id=profile.venue_id, is_demo=False)
        .exclude(pk=account.pk)
        .select_related("customer")
        .first()
    )
    name = _text(profile.name) or "utan namn"
    owner = other.customer.name if other else "ett annat konto"
    alerts.send_account_alert(
        account,
        f"Flamingo: {account.customer.name} försökte koppla en annan kunds Reco-profil",
        [
            f"{account.customer.name} försökte koppla profilen {name} "
            f"på Reco (id {profile.venue_id}).",
            f"Profilen är redan kopplad till {owner}. "
            "Den kopplades inte, och kunden fick veta att ADX har fått veta det.",
            f"Profilen på Reco: {profile_url(profile.slug) or 'saknas'}",
            "",
            "Kunden har inte mejlats.",
        ],
    )
