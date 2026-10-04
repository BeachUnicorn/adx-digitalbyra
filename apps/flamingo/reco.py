"""
Omdömen från Reco (reco.se) i sidbyggaren, Giovannis beslut 2026-10-04:
kunden klistrar in länken till sin sida på Reco, eller Recos id, säger "Det
här är vi", och blocket "Omdömen från Reco" visar Recos egen ruta (en iframe
från widget.reco.se) på sidorna, eller (varianterna Utvalda) de omdömen från
profilsidan som kunden valt, ritade i Ren.

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
                                  cron (flamingo_google_sync): namnet, betyget,
                                  antalet och omdömenas texter bort efter MAX_AGE
                                  utan en ny hämtning (id:t och valet står kvar)
    venue_id_taken(venue_id, exclude_pk=None)

    Utvalda (kundens valda omdömen, se Beslutet nedan):
    selected_enabled()            är Utvalda på? FLAMINGO_RECO_SELECTED_ENABLED
                                  och byråns brytare (FlamingoSettings)
    effective_variant(variant, enabled=None)
                                  blockets variant som den ritas: Utvalda blir
                                  Liggande stor (FALLBACK_VARIANT) när det är av
    parse_reviews(html)           [omdöme] ur profilsidans omdömeskort
    review_link(review_id)        https://www.reco.se/r/<id>, eller ""
    selected_reviews(account, enabled=None)
                                  de valda omdömena som får visas, i kundens ordning
    select(account, ids)          kundens val och ordning
    block_shows(account, variant, enabled=None)
                                  ritar blocket något på sidan?
    refresh_due(now=None, account_pk=None)
                                  cron: intygade profiler som används av Utvalda
                                  hämtas igen efter REFRESH_EVERY (högst ett
                                  försök per konto och dag), sedan expire

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
- Omdömena. Profilsidan har ungefär 50 omdömen som HTML
  (<article id="<omdömets id>" class="review-card-v2">: namnet som Reco
  visar det, dagen i <time>, betyget som fem <span> och <em>, där <span> är
  en fylld stjärna, texten i <p class="truncated-text"> och märkningen
  "Omdöme från inbjuden kund"), och JSON-LD har de fem senaste med länken
  https://www.reco.se/r/<id>. Ett svar från företaget står efter kortet,
  utanför <article>, och läses aldrig som ett omdöme. Fler omdömen
  ("Visa fler omdömen") hämtas aldrig: bara första sidan.
- Villkoren (Medlemsvillkor för webbsöktjänsten reco.se,
  https://www.reco.se/info/terms, uppdaterade 2026-09-22, avsnitt 7,
  Immateriella rättigheter): Reco Sverige AB säger sig äga rättigheterna
  till omdömena och materialet på sajten, och att materialet inte får
  kopieras, spridas eller göras tillgängligt för andra i kommersiella
  sammanhang utan Recos uttryckliga skriftliga medgivande. Widgetarna ingår
  i Recos lösning för företag, och ett API erbjuds bara som en skräddarsydd
  lösning (https://www.reco.se/foretag/priser).

Beslutet om Utvalda (Giovanni 2026-10-04). Giovanni beslutade att bygga en
egen ruta med kundens valda omdömen trots avsnitt 7, med motiveringen
(ordagrant): "Reco kan inte äga omdömena, finns inte ens något
upphovsrättsligts verk. Det är kunden som äger sitt omdöme." Recos villkor
säger alltså motsatsen; byrån har valt att bygga med de här skyddsräckena:

- Bara kundens egen profil, intygad med samma prövning som ovan
  (reco_trusted). En profil som inte är intygad, en bortkopplad profil
  och demot visar aldrig några omdömen, och för en profil som inte är
  intygad sparas inga texter.
- Sällan: cron hämtar profilsidan högst en gång i veckan (REFRESH_EVERY,
  och bara när kunden valt omdömen eller har ett block med Utvalda), högst
  ett försök per konto och dag, och "Hämta igen" i verktyget har gränsen
  LOOKUP_DAILY_MAX per dag. Bara det profilsidan visar, en sida, genom
  analyzer.fetch med bara www.reco.se som värd.
- Texterna tas bort efter MAX_AGE (90 dagar) utan en lyckad hämtning, samma
  regel som för Google; id:t och kundens val står kvar.
- På sidan: rubriken "Omdömen från Reco", namnet, dagen, betyget och en
  länk till varje omdöme på Reco, "Omdöme från inbjuden kund" när Reco
  märker det, en länk till företagets sida på Reco och en rad om att
  företaget valt vilka omdömen som visas och i vilken ordning.
- En brytare: FLAMINGO_RECO_SELECTED_ENABLED i miljön och byråns knapp på
  /manage/flamingo/ (FlamingoSettings.reco_selected_enabled). Av gäller
  direkt för alla: inget hämtas, blocken ritar Recos egen ruta (Liggande
  stor) och valet i verktyget göms. De sparade texterna står kvar tills de
  blir för gamla (MAX_AGE), så att allt kommer tillbaka om det slås på igen.

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

Namnet, betyget och antalet tas bort efter MAX_AGE utan en ny hämtning
(samma regel som för Google, Giovannis beslut 2026-10-03). Betyget blir
aldrig en uppgift (Fact) och används aldrig i annonserna eller förslagen
(models.RATING_SOURCES); det syns bara i blocket Omdömen från Reco.
"""

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from html import unescape
from urllib.parse import unquote, urlsplit

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.common.security import normalize_typography
from apps.tools.analyzer import AnalysError, fetch

from . import alerts, limits
from .models import FlamingoAccount, FlamingoSettings, LandingPage
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
#: igen"; en hämtning med bara id:t är två anrop men räknas som en). Samma
#: gräns som Googles Place Details (reviews.DETAILS_DAILY_MAX).
LOOKUP_DAILY_MAX = 3
USAGE_LOOKUP = "reco_lookup"
#: Cronens försök per konto och svenskt dygn: ett misslyckat försök görs
#: inte om varje timme.
USAGE_REFRESH = "reco_refresh"
#: Namnet, betyget, antalet och omdömenas texter tas bort så här länge efter
#: den senaste hämtningen (samma regel som Googles omdömen).
MAX_AGE = timedelta(days=90)
#: Cron hämtar en profil som används av Utvalda igen så här ofta.
REFRESH_EVERY = timedelta(days=7)
#: Högst så många profiler per körning av cron (den går varje timme).
REFRESH_PER_RUN = 20

#: Utvalda: blockets egna varianter (registry.py), som Googles block: tre
#: kort, ett stort citat och betyget i en rad. FALLBACK_VARIANT är Recos
#: egen ruta som ritas i stället när Utvalda är av.
SELECTED_VARIANTS = ("utvalda_kort", "utvalda_citat", "utvalda_rad")
FALLBACK_VARIANT = "stor"
#: Omdömen som sparas (profilsidans första sida har ungefär 50) och som
#: kunden kan välja.
MAX_STORED = 50
MAX_SELECTED = 5
REVIEW_TEXT_MAX = 4000
AUTHOR_MAX = 100
#: En länk till ett omdöme på Reco, prövad med fullmatch när den ritas.
REVIEW_URL_RE = re.compile(r"https://www\.reco\.se/r/[1-9][0-9]{0,11}")

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
SELECTED_OFF = (
    "Utvalda omdömen från Reco är avstängda av ADX just nu. Sidorna visar Recos egen ruta "
    "i stället."
)


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
    #: Omdömena på sidan (parse_reviews), sparas bara för en intygad
    #: profil och när Utvalda är på.
    reviews: list = field(default_factory=list)


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
        reviews=parse_reviews(html),
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


def _fetch(url, host):
    """Sidan på url, som analyzer.Sida. Bara host får anropas, också efter
    en omdirigering. Kastar RecoError med en allmän text; Recos svar
    loggas kort, aldrig visas."""
    try:
        return fetch(url, max_bytes=MAX_BYTES, time_limit=TIME_LIMIT, hosts=(host,))
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
            page = _fetch(widget_src(venue_id, "vertical", "small"), WIDGET_HOST)
        except RecoError as exc:
            raise RecoError(ID_NOT_FOUND if exc.message == NOT_FOUND else exc.message) from None
        slug = parse_widget(page.html, venue_id)
        if not slug:
            raise RecoError(ID_NOT_FOUND)
    page = _fetch(profile_url(slug), PROFILE_HOST)
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
    "reco_reviews",
    "reco_reviews_selected",
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
    """Spara profilen på kontot: id:t, länken, namnet, betyget, antalet och
    (Utvalda) omdömena. Returnerar vad som stämde med kunden (owner_match),
    eller "".

    Ett id som ett annat riktigt konto har tas aldrig emot: RecoError
    (TAKEN), och byrån larmas. Liknar profilen kunden, eller har någon
    intygat just den här profilen (confirm_owner), syns den på sidorna.
    Annars sätts reco_unverified och byrån larmas (en gång, när profilen
    blir misstänkt). Ett nytt id glömmer vem som intygat det förra och
    kundens val.

    Omdömenas texter sparas bara för en intygad profil och bara när Utvalda
    är på; för en profil som inte är intygad tas de bort. När Utvalda är av
    står de som redan finns kvar orörda (tills MAX_AGE)."""
    now = now or timezone.now()
    enabled = selected_enabled()
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
        if unverified or not same:
            stored_reviews = []
        else:
            stored_reviews = list(row.reco_reviews or [])
        if enabled and not unverified:
            stored_reviews = list(profile.reviews or [])[:MAX_STORED]
        account.reco_venue_id = venue_id
        account.reco_url = profile_url(profile.slug)
        account.reco_name = _text(profile.name)
        account.reco_rating = profile.rating
        account.reco_review_count = profile.count
        account.reco_fetched_at = now
        account.reco_unverified = unverified
        account.reco_confirmed_at = confirmed_at
        account.reco_confirmed_by_id = confirmed_by_id
        account.reco_reviews = stored_reviews
        account.reco_reviews_selected = list(row.reco_reviews_selected or []) if same else []
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
    """Allt från Reco bort. keep_profile: id:t, länken, kundens val och vem
    som intygat profilen står kvar (namnet, betyget, antalet och omdömenas
    texter tas bort)."""
    account.reco_name = ""
    account.reco_rating = None
    account.reco_review_count = None
    account.reco_reviews = []
    if not keep_profile:
        account.reco_reviews_selected = []
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
    """Profiler som inte hämtats på MAX_AGE: namnet, betyget, antalet och
    omdömenas texter tas bort (id:t, länken och kundens val står kvar, så
    Recos ruta syns som förut och valet kommer tillbaka med nästa hämtning).
    Aldrig demot. Returnerar antalet konton."""
    now = now or timezone.now()
    stale = (
        FlamingoAccount.objects.filter(is_demo=False, reco_fetched_at__lt=now - MAX_AGE)
        .exclude(
            reco_name="",
            reco_rating__isnull=True,
            reco_review_count__isnull=True,
            reco_reviews=[],
        )
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


# ---------------------------------------------------------------------------
# Utvalda: kundens valda omdömen (Giovannis beslut 2026-10-04, se ovan)
# ---------------------------------------------------------------------------


def selected_enabled():
    """Är Utvalda på? Både inställningen FLAMINGO_RECO_SELECTED_ENABLED och
    byråns brytare (FlamingoSettings.reco_selected_enabled) måste säga ja.
    Läses från databasen varje gång, så att av gäller direkt i alla
    processer."""
    if not getattr(settings, "FLAMINGO_RECO_SELECTED_ENABLED", True):
        return False
    return FlamingoSettings.get_solo().reco_selected_enabled


def off_by_setting():
    """Är Utvalda av i miljön (då hjälper inte byråns knapp)?"""
    return not getattr(settings, "FLAMINGO_RECO_SELECTED_ENABLED", True)


def effective_variant(variant, enabled=None):
    """Blockets variant som den ritas: en Utvalda-variant blir Recos egen ruta
    (FALLBACK_VARIANT) när Utvalda är av."""
    if variant in SELECTED_VARIANTS:
        if enabled is None:
            enabled = selected_enabled()
        if not enabled:
            return FALLBACK_VARIANT
    return variant


def review_link(review_id):
    """Omdömet på Reco, byggt bara av id:t (siffror), eller ""."""
    review_id = clean_venue_id(review_id)
    return f"{PROFILE_BASE}r/{review_id}" if review_id else ""


def safe_review_link(value):
    """En sparad länk till ett omdöme, eller "": exakt
    https://www.reco.se/r/<siffror>."""
    value = str(value or "")
    return value if REVIEW_URL_RE.fullmatch(value) else ""


_ARTICLE = re.compile(r"<article\b([^>]*)>", re.I)
_ATTR_ID = re.compile(r"\bid=[\"'](\d{1,20})[\"']", re.I)
_AUTHOR = re.compile(r"<b\b[^>]*>(.*?)</b>", re.I | re.S)
_DAY = re.compile(r"<time\b[^>]*>\s*(\d{4}-\d{2}-\d{2})", re.I)
_STARS = re.compile(
    r"<div\b[^>]*class=[\"'][^\"']*\bvenue-ratings\b[^\"']*[\"'][^>]*>(.*?)</div>", re.I | re.S
)
_TEXT = re.compile(
    r"<p\b[^>]*class=[\"'][^\"']*\btruncated-text\b[^\"']*[\"'][^>]*>(.*?)</p>", re.I | re.S
)
_INVITED = re.compile(r"inbjuden\s+kund", re.I)
_TAG = re.compile(r"<[^>]*>")
_REVIEW_URL_IN_LD = re.compile(r"^https://www\.reco\.se/r/(\d{1,20})$")


def _plain(fragment, limit):
    """HTML ur Recos sida som vanlig text: radbrytningar behålls, taggar och
    kontrolltecken bort, entiteter avkodade. Texten ändras annars inte."""
    text = re.sub(r"<br\s*/?>", "\n", str(fragment or ""), flags=re.I)
    text = unescape(_TAG.sub("", text))
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text.replace("\r\n", "\n"))
    return text.strip()[:limit]


def _day(value):
    value = str(value or "")[:10]
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def _stars(fragment):
    """Betyget ur Recos fem stjärnor: <span></span> är fylld, <em></em> tom."""
    full = len(re.findall(r"<span\b[^>]*>\s*</span>", fragment or "", re.I))
    empty = len(re.findall(r"<em\b[^>]*>\s*</em>", fragment or "", re.I))
    return full if full + empty == 5 and full >= 1 else None


def _ld_reviews(html):
    """{id: omdöme} ur JSON-LD (de fem senaste), för att fylla i det som
    saknas i ett kort."""
    out = {}
    for raw in _LD_JSON.findall(html):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            reviews = item.get("review") if isinstance(item, dict) else None
            for review in reviews if isinstance(reviews, list) else []:
                if not isinstance(review, dict):
                    continue
                match = _REVIEW_URL_IN_LD.match(str(review.get("url") or ""))
                if not match:
                    continue
                author = review.get("author") if isinstance(review.get("author"), dict) else {}
                rating = review.get("reviewRating")
                rating = rating.get("ratingValue") if isinstance(rating, dict) else None
                out[match.group(1)] = {
                    "author": _text(author.get("name"), AUTHOR_MAX),
                    "date": _day(review.get("datePublished")),
                    "rating": rating if isinstance(rating, int) and 1 <= rating <= 5 else None,
                    "text": _plain(review.get("reviewBody"), REVIEW_TEXT_MAX)
                    if isinstance(review.get("reviewBody"), str)
                    else "",
                }
    return out


def parse_reviews(html):
    """Omdömena på profilsidan, nyast först, högst MAX_STORED, i formen som
    FlamingoAccount.reco_reviews beskriver. Bara omdömeskorten
    (<article class="review-card-v2">) räknas, för de bär Recos märkning
    "Omdöme från inbjuden kund"; JSON-LD fyller bara i det som saknas i ett
    kort. Ett kort utan id, namn eller betyg hoppas över."""
    html = str(html or "")
    extra = _ld_reviews(html)
    found = {}
    for match in _ARTICLE.finditer(html):
        attrs = match.group(1)
        if "review-card-v2" not in attrs:
            continue
        id_match = _ATTR_ID.search(attrs)
        end = html.find("</article>", match.end())
        if not id_match or end < 0:
            continue
        review_id = clean_venue_id(id_match.group(1))
        if not review_id or review_id in found:
            continue
        body = html[match.end() : end]
        fallback = extra.get(review_id, {})
        author = _AUTHOR.search(body)
        day = _DAY.search(body)
        stars = _STARS.search(body)
        text = _TEXT.search(body)
        review = {
            "id": review_id,
            "author": _text(_plain(author.group(1), 400), AUTHOR_MAX) if author else "",
            "date": _day(day.group(1)) if day else "",
            "rating": _stars(stars.group(1)) if stars else None,
            "text": _plain(text.group(1), REVIEW_TEXT_MAX) if text else "",
            "uri": review_link(review_id),
            "invited": bool(_INVITED.search(_TAG.sub(" ", body))),
        }
        for key in ("author", "date", "rating", "text"):
            if not review[key] and fallback.get(key):
                review[key] = fallback[key]
        if review["author"] and review["rating"]:
            found[review_id] = review
    reviews = sorted(found.values(), key=lambda r: (r["date"], int(r["id"])), reverse=True)
    return reviews[:MAX_STORED]


def _stored_review(raw):
    """Ett sparat omdöme, prövat igen innan det visas, eller None. Länken
    byggs om av id:t (den sparade läses aldrig)."""
    if not isinstance(raw, dict):
        return None
    review_id = clean_venue_id(raw.get("id"))
    author = raw.get("author")
    rating = raw.get("rating")
    if not review_id or not isinstance(author, str) or not author.strip():
        return None
    if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        return None
    text = raw.get("text") if isinstance(raw.get("text"), str) else ""
    return {
        "id": review_id,
        "author": author.strip()[:AUTHOR_MAX],
        "date": _day(raw.get("date")),
        "rating": rating,
        "text": text[:REVIEW_TEXT_MAX],
        "uri": review_link(review_id),
        "invited": raw.get("invited") is True,
    }


def stored_reviews(account):
    """Alla sparade omdömen, prövade, i sparad ordning (för valet)."""
    out = []
    for raw in account.reco_reviews or []:
        review = _stored_review(raw)
        if review is not None and all(r["id"] != review["id"] for r in out):
            out.append(review)
    return out


def selected_reviews(account, enabled=None):
    """De valda omdömena som får visas, i kundens ordning. Tom lista när
    Utvalda är av, för ett demokonto, och för en profil som saknas eller
    inte är intygad. Ett id i valet som inte finns bland de sparade hoppas
    över men står kvar i valet."""
    if enabled is None:
        enabled = selected_enabled()
    if not enabled or account.is_demo or not account.reco_trusted:
        return []
    by_id = {review["id"]: review for review in stored_reviews(account)}
    out = []
    for review_id in account.reco_reviews_selected or []:
        review = by_id.pop(str(review_id), None)
        if review is not None:
            out.append(review)
    return out


def select(account, ids):
    """Kundens val i kundens ordning: bara id:n bland de sparade omdömena,
    varje id en gång, högst MAX_SELECTED."""
    known = {review["id"] for review in stored_reviews(account)}
    chosen = []
    for review_id in ids or []:
        review_id = str(review_id)
        if review_id in known and review_id not in chosen:
            chosen.append(review_id)
    chosen = chosen[:MAX_SELECTED]
    account.reco_reviews_selected = chosen
    account.save(update_fields=["reco_reviews_selected", "updated_at"])
    return chosen


def block_shows(account, variant, enabled=None):
    """Ritar blocket Omdömen från Reco något på sidan (render.py och
    Konverteringskollen)? Demot ritar alltid sin exempelruta."""
    if not account.reco_trusted:
        return False
    if account.is_demo:
        return True
    if enabled is None:
        enabled = selected_enabled()
    variant = effective_variant(variant, enabled)
    if variant not in SELECTED_VARIANTS:
        return bool(clean_venue_id(account.reco_venue_id))
    if selected_reviews(account, enabled):
        return True
    return variant == "utvalda_rad" and account.reco_rating is not None


@dataclass
class RefreshSummary:
    fetched: int = 0
    failed: int = 0
    expired: int = 0
    skipped: str = ""
    errors: list = field(default_factory=list)


def _accounts_using_selected():
    """Kontona som har ett block med en Utvalda-variant på någon sida
    (utkastet eller det publicerade)."""
    query = Q()
    for variant in SELECTED_VARIANTS:
        block = [{"type": "reviews_reco", "variant": variant}]
        query |= Q(draft__blocks__contains=block) | Q(published__blocks__contains=block)
    return set(LandingPage.objects.filter(query).values_list("account_id", flat=True))


def refresh_due(now=None, account_pk=None):
    """Cron (flamingo_google_sync): intygade profiler som används av Utvalda
    (kunden har valt omdömen, eller en sida har blocket med Utvalda) och som
    inte hämtats på REFRESH_EVERY hämtas igen, högst REFRESH_PER_RUN per
    körning och ett försök per konto och dag. Sedan expire. Utvalda av:
    inget hämtas, men det som blivit för gammalt tas ändå bort. Aldrig
    demot, aldrig en profil som inte är intygad."""
    now = now or timezone.now()
    summary = RefreshSummary()
    if not selected_enabled():
        summary.skipped = "Utvalda omdömen från Reco är avstängda"
    else:
        candidates = (
            FlamingoAccount.objects.filter(
                is_enabled=True, is_demo=False, customer__is_active=True, reco_unverified=False
            )
            .exclude(reco_venue_id="")
            .filter(Q(reco_fetched_at__isnull=True) | Q(reco_fetched_at__lt=now - REFRESH_EVERY))
            .select_related("customer")
            .order_by("reco_fetched_at", "pk")
        )
        if account_pk is not None:
            candidates = candidates.filter(pk=account_pk)
        using = _accounts_using_selected()
        due = [a for a in candidates if a.reco_reviews_selected or a.pk in using]
        for account in due[:REFRESH_PER_RUN]:
            if not limits.reserve_daily(account, USAGE_REFRESH, 1, now=now):
                continue
            try:
                store(account, fetch_profile(stored_link(account)), now=now)
                summary.fetched += 1
            except RecoError as exc:
                summary.failed += 1
                summary.errors.append(f"{account.customer.name}: {exc.message}")
            except Exception:  # noqa: BLE001 - ett konto stoppar inte de andra
                logger.exception("Flamingo: omdömena från Reco för konto %s", account.pk)
                summary.failed += 1
                summary.errors.append(f"{account.customer.name}: oväntat fel, se loggen.")
    summary.expired = expire(now, account_pk=account_pk)
    return summary
