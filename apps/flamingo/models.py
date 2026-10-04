"""
ADX Flamingo: annonser, sidor och förfrågningar för kunder, mätt till affär.

Tjänsten är stängd: bara kunder som byrån aktiverat ser den (och byrån).
Aktiveringen är en egen rad per kund, inte ett fält på Customer: kundkortets
formulär sparar varje kryssruta det inte ritar som avbockad, och det har
redan stängt av saker en gång (apps/projects/forms.py, 2026-09-20).

Datamodellen följer flödet i README.md (kundresan, steg 2-12):

    GoogleAdsConnection  ADX:s inloggning hos Google Ads (en rad, nyckeln krypterad)
    FlamingoAccount   kundens Flamingo: hemsida, Google-kopplingen, sms-val
    Fact              det vi får säga om företaget, med källa och bekräftelse
    Service           tjänsterna och hur de säljs (ringer / offert / boka tid)
    MediaAsset        en bild i kundens mediaarkiv (uppladdad eller från hemsidan)
    SiteImageCandidate  en bild som läsningen hittade på hemsidan (bara miniatyren)
    LandingPage       en landningssida i sidbyggaren: block i ett utkast och
                      en publicerad version (pagebuilder/)
    Campaign          en kampanj per tjänst: annonser, sökord och sin landningssida
    CampaignDayStats  en dag ur Googles rapport: kostnad, visningar, klick
    Review            en granskningsrunda per inskick, med byråns ändringar
    Lead              en förfrågan från landningssidan, ett samtal eller manuellt
    ConversionUpload  en förfrågan, ett klick på numret eller en affär till Google
    SmsLog            varje sms som skickades, eller varför det inte skickades

Pengar är alltid hela kronor (int). Tider sparas i UTC och visas i
Europe/Stockholm (Django gör det med USE_TZ och TIME_ZONE).
"""

import base64
import hashlib
import hmac
import logging
import re
import secrets

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from apps.projects.models import Customer

logger = logging.getLogger(__name__)

#: Betyg och omdömen används bara när de kommer från Google (places.py) eller
#: ADX. En hemsida, eller text någon lagt in på den, eller kunden själv ska
#: inte kunna ge företaget ett betyg i annonserna eller på sidan, oavsett vad
#: uppgiften kallas ("Trustpilot: 4,9").
RATING_SOURCES = ("google", "adx")
#: Ord i nyckeln eller etiketten (slug utan å, ä, ö, eller etiketten), eller i
#: värdet ihop med en siffra.
RATING_WORDS = (
    "betyg",
    "omdöme",
    "omdome",
    "rating",
    "stjärn",
    "stjarn",
    "review",
    "recension",
    "score",
    "stars",
)
#: Betygssajter: namnet ihop med en siffra är ett betyg.
RATING_SITES = frozenset(
    (
        "trustpilot",
        "yelp",
        "tripadvisor",
        "bokadirekt",
        "google",
        "facebook",
        "servicefinder",
        "offerta",
        "mittanbud",
        # Recos betyg visas bara i Recos egen ruta (reco.py), aldrig som en
        # uppgift i annonserna eller förslagen.
        "reco",
    )
)
#: "4,9 av 5", "4.8/5", "9 av 10", "5 stjärnor", eller bara "4,9".
RATING_VALUE = re.compile(
    r"\b\d(?:[.,]\d)?\s*(?:av|/|of)\s*(?:5|10)\b"
    r"|\b\d(?:[.,]\d)?\s*stj[äa]rn"
    r"|^\s*\d[.,]\d\s*(?:\(\s*\d+\s*\))?\s*$",
    re.I,
)


def is_rating_like(key, label, value=""):
    """Ser uppgiften ut som ett betyg eller omdöme (se RATING_WORDS)?"""
    name = f"{key} {label}".casefold()
    value = str(value or "").casefold()
    if any(word in name for word in RATING_WORDS) or RATING_VALUE.search(value):
        return True
    if not re.search(r"\d", value):
        return False
    if any(word in value for word in RATING_WORDS):
        return True
    return bool(RATING_SITES & set(re.findall(r"[^\W\d_]+", f"{name} {value}")))


# ---------------------------------------------------------------------------
# Gränser som generatorn, formulären och granskningen delar (Googles regler
# för responsiva sökannonser).
# ---------------------------------------------------------------------------

HEADLINE_MAX = 30
DESCRIPTION_MAX = 90
HEADLINE_COUNT = 15
DESCRIPTION_COUNT = 4

#: Budget per dag i hela kronor (checks.validate, formulären, admin).
BUDGET_MIN = 50
BUDGET_MAX = 5000

#: Matchningstyper för sökord: värdet sparas i Campaign.keywords[i]["match"].
MATCH_EXACT = "exact"
MATCH_PHRASE = "phrase"
MATCH_BROAD = "broad"
MATCH_CHOICES = [
    (MATCH_PHRASE, "Fras"),
    (MATCH_EXACT, "Exakt"),
    (MATCH_BROAD, "Bred"),
]

#: Frågetyper på landningssidans formulär (Campaign.page["questions"]).
PAGE_QUESTION_KINDS = ("text", "textarea", "date")

#: Standardtext för autosvaret. Inga tider och inga löften: kunden skriver
#: om den i sina inställningar om hen vill säga mer.
AUTOREPLY_DEFAULT = (
    "Hej! Tack för din förfrågan. Vi har tagit emot den och hör av oss. "
    "Brådskar det går det bra att ringa oss."
)

google_ads_id_validator = RegexValidator(
    r"^\d{3}-\d{3}-\d{4}$",
    "Skriv kontots id som tio siffror, till exempel 123-456-7890.",
)


def format_google_ads_id(value):
    """'1234567890', '123 456 7890' eller '123-456-7890' blir '123-456-7890'.

    Returnerar "" för ett tomt värde och None om det inte är tio siffror.
    """
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return ""
    if len(digits) != 10:
        return None
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"


# ---------------------------------------------------------------------------
# ADX:s koppling till Google Ads
# ---------------------------------------------------------------------------

#: Skiljer nyckeln för hemligheter i databasen från andra saker som härleds
#: ur SECRET_KEY.
_TOKEN_KEY_CONTEXT = b"adx-flamingo-google-ads-token:"


def _token_fernet():
    """Krypteringen för Googles långlivade nyckel i databasen.

    FLAMINGO_TOKEN_KEY om den är satt (en Fernet-nyckel, eller en lång
    hemlig sträng som hashas), annars en nyckel härledd ur SECRET_KEY
    (sha256, urlsafe base64, 32 byte). Byts SECRET_KEY eller
    FLAMINGO_TOKEN_KEY går den sparade nyckeln inte längre att läsa: då
    kopplar byrån Google igen. Inget annat går förlorat."""
    configured = str(getattr(settings, "FLAMINGO_TOKEN_KEY", "") or "").strip()
    if configured:
        try:
            return Fernet(configured.encode())
        except ValueError:
            material = configured
    else:
        material = settings.SECRET_KEY
    digest = hashlib.sha256(_TOKEN_KEY_CONTEXT + str(material).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value):
    """En hemlighet krypterad för databasen (text)."""
    return _token_fernet().encrypt(str(value).encode()).decode()


def decrypt_secret(value):
    """Hemligheten i klartext, eller "" om den inte går att läsa (nyckeln
    har bytts eller raden är trasig)."""
    if not value:
        return ""
    try:
        return _token_fernet().decrypt(str(value).encode()).decode()
    except (InvalidToken, ValueError):
        return ""


def token_fingerprint(token):
    """Ett kort avtryck av en nyckel (HMAC-SHA256 med SECRET_KEY, 16 tecken),
    eller "". Säger vilken nyckel sparade behörigheter gäller
    (GoogleAdsConnection.scopes_for) utan att avslöja nyckeln."""
    token = str(token or "")
    if not token:
        return ""
    key = _TOKEN_KEY_CONTEXT + str(settings.SECRET_KEY).encode()
    return hmac.new(key, b"scopes:" + token.encode(), hashlib.sha256).hexdigest()[:16]


def normalize_scopes(scope):
    """Googles "scope" (behörigheter med mellanslag emellan) sorterat och
    utan dubbletter."""
    return " ".join(sorted(set(str(scope or "").split())))


class GoogleAdsConnection(models.Model):
    """ADX:s egen inloggning hos Google Ads (OAuth), som alla anrop via
    förvaltarkontot görs med. En enda rad: get_solo().

    Den långlivade nyckeln (refresh token) sparas krypterad och visas
    aldrig: inte i panelen, inte i admin och inte i loggarna.
    GOOGLE_ADS_REFRESH_TOKEN i miljön vinner över den sparade (google_ads.py).
    """

    SOLO_PK = 1

    refresh_token_encrypted = models.TextField("Nyckel (krypterad)", blank=True, editable=False)
    google_email = models.EmailField("Google-konto", blank=True)
    connected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="Kopplad av",
    )
    connected_at = models.DateTimeField("Kopplad", null=True, blank=True)
    last_ok_at = models.DateTimeField("Senaste lyckade anropet", null=True, blank=True)
    last_error = models.CharField("Senaste felet", max_length=300, blank=True)
    #: Behörigheterna (OAuth scopes) som Google gav, med mellanslag emellan:
    #: svarets "scope" när byrån kopplar, och när nyckeln förnyas
    #: (google_ads.access_token). scopes_for är avtrycket av nyckeln de gäller
    #: (token_fingerprint), så att nyckeln i miljön och den sparade aldrig
    #: blandas ihop. Tomt avtryck: okänt (kopplat före Data Manager API).
    granted_scopes = models.TextField("Behörigheter hos Google", blank=True)
    scopes_for = models.CharField(
        "Behörigheterna gäller nyckeln", max_length=16, blank=True, editable=False
    )
    #: Google tog inte emot konverteringarna för hela vägen (till exempel
    #: CUSTOMER_NOT_ALLOWLISTED_FOR_THIS_FEATURE med uploadClickConversions,
    #: eller Data Manager API avslaget i Cloud-projektet): inga fler försök
    #: på den vägen förrän byrån ber om det på Google-sidan. Raderna står kvar
    #: för CSV-filen. conversion_upload_blocked_path är vägen
    #: (google_conversions.PATH_*); tomt är uploadClickConversions.
    conversion_upload_blocked_at = models.DateTimeField(
        "Uppladdningen av konverteringar stoppad", null=True, blank=True
    )
    conversion_upload_error = models.CharField(
        "Varför uppladdningen stoppades", max_length=300, blank=True
    )
    conversion_upload_blocked_path = models.CharField(
        "Vägen som stoppades", max_length=20, blank=True
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Google Ads-koppling"
        verbose_name_plural = "Google Ads-koppling"

    def __str__(self):
        if not self.is_connected:
            return "Google Ads: inte kopplat"
        return f"Google Ads: kopplat som {self.google_email or 'okänt konto'}"

    @classmethod
    def get_solo(cls):
        """Den enda raden, skapad vid första behovet."""
        connection, _ = cls.objects.get_or_create(pk=cls.SOLO_PK)
        return connection

    @property
    def is_connected(self):
        """Det finns en sparad nyckel (den kan ändå vara oläslig, se
        token_unreadable)."""
        return bool(self.refresh_token_encrypted)

    @property
    def token_unreadable(self):
        """Nyckeln finns men går inte att läsa: SECRET_KEY eller
        FLAMINGO_TOKEN_KEY har bytts. Byrån kopplar Google igen."""
        return self.is_connected and not self.refresh_token()

    def set_refresh_token(self, token, email="", user=None, scopes=""):
        """Spara en ny koppling (efter Googles inloggning). scopes är
        behörigheterna Google gav (svarets "scope"); utan dem är de okända."""
        token = str(token or "").strip()
        if not token:
            raise ValueError("Google skickade ingen nyckel.")
        self.refresh_token_encrypted = encrypt_secret(token)
        self.google_email = str(email or "")[:254]
        self.connected_by = user if getattr(user, "pk", None) else None
        self.connected_at = timezone.now()
        self.last_error = ""
        self.granted_scopes = normalize_scopes(scopes)
        self.scopes_for = token_fingerprint(token) if self.granted_scopes else ""
        self.save()
        return self

    def refresh_token(self):
        """Nyckeln i klartext, eller "" om ingen finns eller den inte går
        att läsa. Får aldrig loggas eller visas."""
        token = decrypt_secret(self.refresh_token_encrypted)
        if self.refresh_token_encrypted and not token:
            logger.warning("Flamingo: Google-nyckeln går inte att läsa (bytt SECRET_KEY?)")
        return token

    def clear(self):
        """Glöm kopplingen. Nyckeln återkallas hos Google av den som anropar
        (google_ads.revoke) innan raden töms."""
        self.refresh_token_encrypted = ""
        self.google_email = ""
        self.connected_by = None
        self.connected_at = None
        self.last_ok_at = None
        self.last_error = ""
        self.granted_scopes = ""
        self.scopes_for = ""
        self.save()
        return self


# ---------------------------------------------------------------------------
# Kontot
# ---------------------------------------------------------------------------


class FlamingoAccount(models.Model):
    GOOGLE_NOT_STARTED = "not_started"
    GOOGLE_REQUESTED_NEW = "requested_new"
    GOOGLE_ID_GIVEN = "id_given"
    GOOGLE_LINKED = "linked"
    GOOGLE_BILLING_OK = "billing_ok"
    GOOGLE_CHOICES = [
        (GOOGLE_NOT_STARTED, "Inte påbörjat"),
        (GOOGLE_REQUESTED_NEW, "Vill ha ett nytt konto"),
        (GOOGLE_ID_GIVEN, "Konto-id angivet"),
        (GOOGLE_LINKED, "Kopplat under ADX"),
        (GOOGLE_BILLING_OK, "Kopplat och betalning klar"),
    ]

    SCAN_NONE = "none"
    SCAN_RUNNING = "running"
    SCAN_DONE = "done"
    SCAN_FAILED = "failed"
    SCAN_CHOICES = [
        (SCAN_NONE, "Inte hämtad"),
        (SCAN_RUNNING, "Hämtas"),
        (SCAN_DONE, "Hämtad"),
        (SCAN_FAILED, "Misslyckades"),
    ]

    customer = models.OneToOneField(
        Customer, on_delete=models.CASCADE, related_name="flamingo", verbose_name="Kund"
    )
    is_enabled = models.BooleanField("ADX Flamingo aktiverat", default=False)
    enabled_at = models.DateTimeField(null=True, blank=True)
    enabled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    # Steg 1, förslaget: hemsidan som läses av.
    website_url = models.URLField("Hemsida", blank=True)
    scan_status = models.CharField(
        "Hemsidan", max_length=10, choices=SCAN_CHOICES, default=SCAN_NONE
    )
    scanned_at = models.DateTimeField("Hämtad", null=True, blank=True)
    scan_error = models.CharField("Fel vid hämtningen", max_length=300, blank=True)
    # Spärrarna (limits.py): läsningar och AI-förslag per svenskt dygn.
    scan_started_at = models.DateTimeField("Senaste läsningen startade", null=True, blank=True)
    scan_day = models.DateField("Dag för läsningarna", null=True, blank=True)
    scan_count = models.PositiveSmallIntegerField("Läsningar den dagen", default=0)
    ai_day = models.DateField("Dag för AI-förslagen", null=True, blank=True)
    ai_count = models.PositiveSmallIntegerField("AI-förslag den dagen", default=0)
    # Publiceringar hos Google efter kundens inskick eller godkännande per
    # svenskt dygn (limits.reserve_publish).
    publish_day = models.DateField("Dag för publiceringarna", null=True, blank=True)
    publish_count = models.PositiveSmallIntegerField("Publiceringsförsök den dagen", default=0)

    # Steg 3, Google: kunden äger kontot, ADX förvaltar det under sitt MCC.
    google_ads_customer_id = models.CharField(
        "Google Ads-kontots id",
        max_length=20,
        blank=True,
        validators=[google_ads_id_validator],
        help_text="Tio siffror, till exempel 123-456-7890.",
    )
    google_status = models.CharField(
        "Google Ads", max_length=20, choices=GOOGLE_CHOICES, default=GOOGLE_NOT_STARTED
    )
    google_note = models.CharField("Notering om Google", max_length=300, blank=True)
    # Läget hos Google som det senast lästes med API:t (google_ads.py).
    #: billing_setup.status hos Google, till exempel "APPROVED" (klar).
    google_billing_status = models.CharField("Betalningen hos Google", max_length=20, blank=True)
    google_auto_tagging = models.BooleanField("Automatisk taggning", null=True, blank=True)
    google_synced_at = models.DateTimeField("Läst från Google", null=True, blank=True)
    google_sync_error = models.CharField(
        "Fel vid läsningen från Google", max_length=300, blank=True
    )
    google_link_requested_at = models.DateTimeField(
        "Kopplingsinbjudan skickad", null=True, blank=True
    )
    #: Id:t som ADX skickade kopplingsförfrågan till från det här kontot
    #: (google_accounts.request_link). Bara då blir kontot kopplat av sig
    #: självt när Google säger ACTIVE: att kontot ligger under ADX
    #: förvaltarkonto visar inte att det är kundens.
    google_link_requested_for = models.CharField("Förfrågan gällde id", max_length=20, blank=True)
    #: Konverteringsåtgärderna i kundens konto, som resursnamn:
    #:   {"lead": "customers/1/conversionActions/2", "call": ..., "deal": ...}
    google_conversion_actions = models.JSONField(
        "Konverteringar hos Google", default=dict, blank=True
    )

    is_demo = models.BooleanField(
        "Demokonto",
        default=False,
        help_text="Demokonto: inga anrop till Google, inga sms, sidorna bara för byrån.",
    )

    # Inställningar kunden själv slår på (av från början). Sms skickas bara
    # när 46elks är konfigurerat; annars loggas det i SmsLog.
    notify_phone = models.CharField("Mobil för sms om nya förfrågningar", max_length=20, blank=True)
    notify_sms = models.BooleanField("Sms till mig om nya förfrågningar", default=False)
    autoreply_enabled = models.BooleanField("Autosvar till den som frågar", default=False)
    autoreply_text = models.TextField("Autosvarets text", default=AUTOREPLY_DEFAULT)

    # Kundens Google-profil (Google Business Profile), för blocket
    # "Omdömen från Google" i sidbyggaren. Kunden pekar ut profilen (sök,
    # länk från Google Maps eller Place ID) och väljer vilka omdömen som
    # syns; texten ändras aldrig. Hämtningen (reviews.py) anropar aldrig
    # Google för ett demokonto. Sidan visar omdömena med Googles märkning,
    # författarens namn och länken till profilen.
    #
    # Profilen måste vara kundens: reviews.store_details prövar den mot
    # hemsidan, namnet och telefonnumret (places.matches). Liknar den inte
    # kunden sätts google_place_unverified, betyget sparas obekräftat,
    # omdömena och betyget syns inte på sidorna, och byrån larmas, tills
    # kunden eller byrån intygar att profilen är deras (reviews.confirm_owner,
    # google_place_confirmed_at och _by).
    google_place_id = models.CharField("Googles Place ID", max_length=200, blank=True)
    google_place_unverified = models.BooleanField(
        "Profilen liknar inte företaget",
        default=False,
        help_text=(
            "Omdömen och betyg från profilen syns inte förrän någon intygat att den är kundens."
        ),
    )
    google_place_confirmed_at = models.DateTimeField("Profilen intygad", null=True, blank=True)
    google_place_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="Profilen intygad av",
    )
    google_place_name = models.CharField("Namnet på Google", max_length=200, blank=True)
    google_maps_uri = models.URLField("Profilen på Google Maps", max_length=500, blank=True)
    google_rating = models.DecimalField(
        "Betyg på Google", max_digits=2, decimal_places=1, null=True, blank=True
    )
    google_review_count = models.PositiveIntegerField(
        "Antal omdömen på Google", null=True, blank=True
    )
    #: Omdömena som de hämtades, nyast först:
    #:   [{"id": "places/<place>/reviews/<id>",  Googles namn på omdömet (stabilt)
    #:     "author": "Anna L.",                  författarens namn (visas alltid)
    #:     "author_uri": "https://...",          författarens profil hos Google, eller ""
    #:     "rating": 5,                          1-5
    #:     "text": "Kom samma kväll ...",        omdömet, oförändrat (aldrig HTML)
    #:     "time": "2026-09-14T10:12:00Z",       när det skrevs (ISO 8601)
    #:     "relative": "för 3 veckor sedan"}]    Googles relativa tid, eller ""
    #: Författarens bild hämtas aldrig från Google på /lp/ (besökarens
    #: integritet): sidan ritar initialer i stället.
    google_reviews = models.JSONField("Omdömen från Google", default=list, blank=True)
    #: Id:n (google_reviews[i]["id"]) som kunden valt att visa, i visningsordning.
    google_reviews_selected = models.JSONField("Valda omdömen", default=list, blank=True)
    google_reviews_fetched_at = models.DateTimeField("Omdömena hämtade", null=True, blank=True)

    # Kundens profil på Reco (reco.se), för blocket "Omdömen från Reco" i
    # sidbyggaren (Giovannis beslut 2026-10-04, reco.py). Kunden klistrar in
    # länken till sin sida på Reco eller Recos id; profilsidan hämtas (aldrig
    # för ett demokonto) och prövas mot kundens hemsida och telefonnummer.
    # Sidan visar Recos egen ruta (en iframe från widget.reco.se) byggd bara
    # av siffrorna i reco_venue_id, eller (varianterna Utvalda) de omdömen
    # kunden valt ur reco_reviews, ritade i Ren. Utvalda är Giovannis beslut
    # 2026-10-04 trots att Recos villkor säger annat (se reco.py), och kan
    # stängas av för alla (FlamingoSettings, FLAMINGO_RECO_SELECTED_ENABLED).
    #
    # Betyget och antalet visas bara i verktyget, så att kunden ser att det
    # är rätt profil. De är aldrig en uppgift (Fact) och används aldrig i
    # annonserna eller förslagen (RATING_SOURCES).
    #
    # Liknar profilen inte kunden sätts reco_unverified, inget från Reco syns
    # på sidorna, och byrån larmas, tills kunden eller byrån intygat att
    # profilen är deras (reco.confirm_owner, reco_confirmed_at och _by).
    reco_venue_id = models.CharField("Recos id för företaget", max_length=12, blank=True)
    reco_url = models.URLField("Profilen på Reco", max_length=300, blank=True)
    reco_name = models.CharField("Namnet på Reco", max_length=200, blank=True)
    reco_rating = models.DecimalField(
        "Betyg på Reco", max_digits=2, decimal_places=1, null=True, blank=True
    )
    reco_review_count = models.PositiveIntegerField("Antal omdömen på Reco", null=True, blank=True)
    reco_fetched_at = models.DateTimeField("Reco-profilen hämtad", null=True, blank=True)
    reco_unverified = models.BooleanField(
        "Reco-profilen liknar inte företaget",
        default=False,
        help_text="Inget från Reco syns på sidorna förrän någon intygat att profilen är kundens.",
    )
    reco_confirmed_at = models.DateTimeField("Reco-profilen intygad", null=True, blank=True)
    reco_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="Reco-profilen intygad av",
    )
    #: Omdömena från profilsidan på Reco (reco.parse_reviews), nyast först,
    #: högst reco.MAX_STORED. Hämtas bara för en intygad profil och bara när
    #: Utvalda är påslaget; texterna tas bort efter reco.MAX_AGE utan en ny
    #: hämtning (id:t och valet står kvar):
    #:   [{"id": "3361469",                      Recos id för omdömet (siffror)
    #:     "author": "Anna L",                   namnet som Reco visar det
    #:     "date": "2026-10-02",                 dagen (ISO 8601)
    #:     "rating": 5,                          1-5
    #:     "text": "Snabbt svar ...",            omdömet, oförändrat (aldrig HTML)
    #:     "uri": "https://www.reco.se/r/3361469",
    #:     "invited": true}]                     Reco märker "Omdöme från inbjuden kund"
    reco_reviews = models.JSONField("Omdömen från Reco", default=list, blank=True)
    #: Id:n (reco_reviews[i]["id"]) som kunden valt att visa, i visningsordning.
    reco_reviews_selected = models.JSONField("Valda omdömen från Reco", default=list, blank=True)

    #: Dagens räknare för spärrarna som kostar pengar eller bandbredd
    #: (limits.reserve_daily): {"day": "2026-10-03", "places_search": 2,
    #: "places_details": 1, "site_import": 6}. Nollställs när dagen byts.
    daily_usage = models.JSONField("Dagens användning", default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Flamingo-konto"
        verbose_name_plural = "Flamingo-konton"
        constraints = [
            # Ett Google Ads-konto hör till en kund. Demokontots påhittade id
            # räknas inte.
            models.UniqueConstraint(
                fields=["google_ads_customer_id"],
                condition=~models.Q(google_ads_customer_id="") & models.Q(is_demo=False),
                name="flamingo_google_id_unique",
            ),
            # En profil på Reco hör till en kund: en konkurrents profil kan
            # aldrig bli kundens (reco.store nekar ett id som ett annat konto
            # har). Demots påhittade id räknas inte.
            models.UniqueConstraint(
                fields=["reco_venue_id"],
                condition=~models.Q(reco_venue_id="") & models.Q(is_demo=False),
                name="flamingo_reco_id_unique",
            ),
        ]

    def __str__(self):
        state = "på" if self.is_enabled else "av"
        return f"{self.customer.name}: Flamingo {state}"

    @property
    def google_linked(self):
        """Kontot ligger under ADX:s förvaltarkonto (betalningen kan saknas)."""
        return self.google_status in (self.GOOGLE_LINKED, self.GOOGLE_BILLING_OK)

    @property
    def google_ready(self):
        """Kopplat och betalningen klar (avbockad eller läst från Google).
        Stoppar ingenting: en kampanj kan gå live så snart kontot är kopplat
        (google_linked), men annonserna visas först när betalningen finns
        (beslut 2026-10-03)."""
        return self.google_status == self.GOOGLE_BILLING_OK

    @property
    def google_id_shared(self):
        """Ett annat (riktigt) Flamingo-konto har samma Google Ads-id. Då
        pratar Flamingo inte med kontot hos Google (google_publish,
        google_conversions)."""
        if self.is_demo or not self.google_ads_customer_id:
            return False
        return google_id_taken(self.google_ads_customer_id, exclude_pk=self.pk)

    @property
    def google_waiting_on_customer(self):
        """ADX har skickat en kopplingsförfrågan som kunden ska godkänna i
        Google Ads (google_accounts.request_link)."""
        return (
            self.google_status == self.GOOGLE_ID_GIVEN and self.google_link_requested_at is not None
        )

    @property
    def google_waiting_on_adx(self):
        """Kunden har gjort sin del; byrån kopplar. Inte när förfrågan redan
        är skickad: då är det kunden som ska godkänna den."""
        return (
            self.google_status in (self.GOOGLE_REQUESTED_NEW, self.GOOGLE_ID_GIVEN)
            and not self.google_waiting_on_customer
        )

    def usable_fact_rows(self):
        """Bekräftade uppgifter med ett värde, som Fact-rader, utan betyg som
        inte kommer från Google eller ADX (is_rating_like), och utan betyg
        från Google när kundens Google-profil inte är intygad
        (google_profile_trusted)."""
        rows = self.facts.filter(confirmed=True).exclude(value="").order_by("order", "id")
        trusted = self.google_profile_trusted
        return [
            f
            for f in rows
            if f.is_usable and (trusted or not (f.is_rating and f.source == Fact.SOURCE_GOOGLE))
        ]

    def confirmed_facts(self):
        """Bekräftade uppgifter med ett värde, som {key: value}. Det enda
        AI och mallarna får använda (README: AI får bara använda bekräftade
        fakta). Ett betyg från hemsidan eller kunden är aldrig med."""
        return {f.key: f.value for f in self.usable_fact_rows()}

    @property
    def google_profile_trusted(self):
        """Får sidorna och förslagen visa det som hämtats från
        Google-profilen? Inte när profilen inte liknar företaget och ingen
        intygat att den är kundens (google_place_unverified)."""
        return not self.google_place_unverified

    @property
    def trusted_google_rating(self):
        """Betyget från Google-profilen, eller None när profilen inte är
        intygad som kundens (google_profile_trusted)."""
        return self.google_rating if self.google_profile_trusted else None

    def selected_google_reviews(self):
        """Omdömena från Google som kunden valt att visa, i kundens ordning.
        Bara de som finns bland de hämtade (google_reviews) och har en
        författare; tom lista när inget är valt, eller när profilen inte är
        intygad som kundens (google_profile_trusted). Id:n i valet som inte
        finns bland de hämtade hoppas över men står kvar i valet."""
        if not self.google_profile_trusted:
            return []
        by_id = {}
        for review in self.google_reviews or []:
            if isinstance(review, dict) and review.get("id") and review.get("author"):
                by_id.setdefault(str(review["id"]), review)
        chosen = []
        for review_id in self.google_reviews_selected or []:
            review = by_id.pop(str(review_id), None)
            if review is not None:
                chosen.append(review)
        return chosen

    @property
    def reco_trusted(self):
        """Får sidorna visa Recos ruta? Bara med ett id och när profilen
        liknar företaget eller någon intygat att den är kundens
        (reco_unverified)."""
        return bool(self.reco_venue_id) and not self.reco_unverified


class FlamingoSettings(models.Model):
    """Byråns brytare för hela Flamingo. En enda rad: get_solo().

    reco_selected_enabled: blocket Omdömen från Reco får visa de omdömen
    kunden valt (varianterna Utvalda) och omdömena hämtas från Reco. Av
    gäller direkt för alla: sidorna visar Recos egen ruta (Liggande stor),
    inget hämtas, och valet i verktyget göms (reco.selected_enabled, som
    också läser FLAMINGO_RECO_SELECTED_ENABLED)."""

    SOLO_PK = 1

    reco_selected_enabled = models.BooleanField("Utvalda omdömen från Reco", default=True)
    reco_selected_changed_at = models.DateTimeField(
        "Utvalda omdömen från Reco ändrades", null=True, blank=True
    )
    reco_selected_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="Utvalda omdömen från Reco ändrades av",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Flamingos inställningar"
        verbose_name_plural = "Flamingos inställningar"

    def __str__(self):
        return "Flamingos inställningar"

    @classmethod
    def get_solo(cls):
        """Den enda raden, skapad vid första behovet."""
        row, _ = cls.objects.get_or_create(pk=cls.SOLO_PK)
        return row


def google_id_taken(google_id, exclude_pk=None):
    """Har ett annat Flamingo-konto (inte demot) redan id:t? google_id
    jämförs som 123-456-7890 (format_google_ads_id)."""
    formatted = format_google_ads_id(google_id)
    if not formatted:
        return False
    others = FlamingoAccount.objects.filter(google_ads_customer_id=formatted, is_demo=False)
    if exclude_pk is not None:
        others = others.exclude(pk=exclude_pk)
    return others.exists()


def account_for(customer):
    """Kundens Flamingo-rad, skapad vid första behovet."""
    account, _ = FlamingoAccount.objects.get_or_create(customer=customer)
    return account


def has_flamingo(customer):
    """Har kunden ADX Flamingo aktiverat? En aktiv kund krävs också."""
    if customer is None or not customer.is_active:
        return False
    return FlamingoAccount.objects.filter(customer=customer, is_enabled=True).exists()


# ---------------------------------------------------------------------------
# Företaget och tjänsterna
# ---------------------------------------------------------------------------


class Fact(models.Model):
    """En uppgift om företaget med sin källa. Tomt värde betyder "vet inte"
    och skrivs aldrig som en gissning."""

    SOURCE_SITE = "site"
    SOURCE_GOOGLE = "google"
    SOURCE_CUSTOMER = "customer"
    SOURCE_ADX = "adx"
    SOURCE_CHOICES = [
        (SOURCE_SITE, "Hemsidan"),
        (SOURCE_GOOGLE, "Google"),
        (SOURCE_CUSTOMER, "Du"),
        (SOURCE_ADX, "ADX"),
    ]

    account = models.ForeignKey(FlamingoAccount, on_delete=models.CASCADE, related_name="facts")
    key = models.SlugField("Nyckel", max_length=64)
    label = models.CharField("Uppgift", max_length=120)
    value = models.TextField("Värde", blank=True)
    source = models.CharField(
        "Källa", max_length=10, choices=SOURCE_CHOICES, default=SOURCE_CUSTOMER
    )
    confirmed = models.BooleanField("Bekräftad", default=False)
    order = models.PositiveIntegerField("Ordning", default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Uppgift"
        verbose_name_plural = "Uppgifter"
        constraints = [
            models.UniqueConstraint(fields=["account", "key"], name="flamingo_fact_unique_key")
        ]

    def __str__(self):
        return f"{self.label}: {self.value or '(tomt)'}"

    @property
    def is_rating(self):
        return is_rating_like(self.key, self.label, self.value)

    @property
    def is_usable(self):
        """Får annonserna, sidan och kontrollerna använda uppgiften? Betyg
        bara från Google eller ADX."""
        return self.source in RATING_SOURCES or not self.is_rating


class Service(models.Model):
    SALES_CALL = "call"
    SALES_QUOTE = "quote"
    SALES_BOOK = "book"
    SALES_CHOICES = [
        (SALES_CALL, "Ringer direkt"),
        (SALES_QUOTE, "Vill ha offert"),
        (SALES_BOOK, "Vill boka tid"),
    ]

    account = models.ForeignKey(FlamingoAccount, on_delete=models.CASCADE, related_name="services")
    name = models.CharField("Tjänst", max_length=120)
    sales_mode = models.CharField(
        "Hur köper kunderna den?", max_length=10, choices=SALES_CHOICES, default=SALES_QUOTE
    )
    is_active = models.BooleanField("Aktiv", default=True)
    order = models.PositiveIntegerField("Ordning", default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Tjänst"
        verbose_name_plural = "Tjänster"

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Sidbyggaren: mediaarkivet och landningssidorna (apps/flamingo/pagebuilder/)
# ---------------------------------------------------------------------------

#: Mediaarkivets gränser (beslut 2026-10-03): högst så många bilder per
#: konto, längsta sidan i pixlar, och formatet bilderna sparas i.
MEDIA_MAX_PER_ACCOUNT = 200
MEDIA_MAX_SIDE = 2400
MEDIA_FORMAT = "WEBP"
#: Miniatyrens längsta sida (arkivet, srcset på sidan).
MEDIA_THUMB_SIDE = 640


def media_upload_path(instance, filename):
    """flamingo/<slump>/<slump>.<ändelse>: en egen mapp med ett namn som
    inte går att gissa för varje fil (MEDIA_ROOT serveras av nginx utan
    inloggning). Filens ursprungliga namn sparas aldrig i sökvägen."""
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "webp").lower()
    ext = re.sub(r"[^a-z0-9]", "", ext)[:5] or "webp"
    return f"flamingo/{secrets.token_urlsafe(18)}/{secrets.token_hex(8)}.{ext}"


class MediaAsset(models.Model):
    """En bild i kundens mediaarkiv. Sidorna pekar på den med sitt id
    (blockens mediafält, pagebuilder/). Uppladdningen, prövningen,
    nedskalningen till MEDIA_MAX_SIDE och omkodningen till WebP görs i
    media.py. Filerna tas bort när raden tas bort."""

    SOURCE_UPLOAD = "upload"
    SOURCE_SITE = "site"
    SOURCE_CHOICES = [
        (SOURCE_UPLOAD, "Uppladdad"),
        (SOURCE_SITE, "Från hemsidan"),
    ]

    account = models.ForeignKey(FlamingoAccount, on_delete=models.CASCADE, related_name="media")
    file = models.ImageField(
        "Bild",
        upload_to=media_upload_path,
        width_field="width",
        height_field="height",
        max_length=200,
    )
    thumb = models.ImageField("Miniatyr", upload_to=media_upload_path, max_length=200, blank=True)
    width = models.PositiveIntegerField("Bredd", default=0)
    height = models.PositiveIntegerField("Höjd", default=0)
    alt = models.CharField("Alternativtext", max_length=200, blank=True)
    is_logo = models.BooleanField("Logotyp", default=False)
    source = models.CharField("Källa", max_length=10, choices=SOURCE_CHOICES, default=SOURCE_UPLOAD)
    #: Var bilden låg på kundens hemsida (bara för SOURCE_SITE).
    source_url = models.URLField("Adress på hemsidan", max_length=500, blank=True)
    #: Kunden intygade att företaget äger bilden eller har rätt att använda
    #: den ("Vi äger bilderna eller har rätt att använda dem").
    rights_confirmed_at = models.DateTimeField("Rätten intygad", null=True, blank=True)
    rights_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="Intygad av",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Bild"
        verbose_name_plural = "Bilder"
        indexes = [models.Index(fields=["account", "-created_at"], name="flamingo_media_account")]

    def __str__(self):
        return self.alt or f"Bild {self.pk}"


class SiteImageCandidate(models.Model):
    """En bild som läsningen av hemsidan hittade. Bara miniatyren och
    adressen sparas; kunden väljer vilka som hämtas på riktigt till
    arkivet (imported_asset)."""

    account = models.ForeignKey(
        FlamingoAccount, on_delete=models.CASCADE, related_name="site_images"
    )
    source_url = models.URLField("Adress på hemsidan", max_length=500)
    thumb = models.ImageField("Miniatyr", upload_to=media_upload_path, max_length=200, blank=True)
    width = models.PositiveIntegerField("Bredd", null=True, blank=True)
    height = models.PositiveIntegerField("Höjd", null=True, blank=True)
    #: Ser ut som logotypen ("logo" i adressen, alt-texten eller klassen,
    #: eller hemsidans ikon): markeras för kunden (media.image_ref).
    likely_logo = models.BooleanField("Trolig logotyp", default=False)
    #: Sidans egen alt-text, förslaget när bilden hämtas till arkivet.
    alt = models.CharField("Alt-text på hemsidan", max_length=200, blank=True)
    found_at = models.DateTimeField("Hittad", default=timezone.now)
    imported_asset = models.ForeignKey(
        MediaAsset,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="candidates",
        verbose_name="Hämtad som",
    )

    class Meta:
        ordering = ["-found_at", "-id"]
        verbose_name = "Bild från hemsidan"
        verbose_name_plural = "Bilder från hemsidan"
        constraints = [
            models.UniqueConstraint(
                fields=["account", "source_url"], name="flamingo_site_image_unique"
            )
        ]

    def __str__(self):
        return self.source_url


def _delete_image_files(sender, instance, **kwargs):
    """Filerna går med raden, men först när borttagningen är sparad: en
    transaktion som rullas tillbaka ska inte lämna en rad utan fil."""
    names = [f for f in (getattr(instance, "file", None), instance.thumb) if f]
    if not names:
        return

    def remove():
        for image in names:
            try:
                image.storage.delete(image.name)
            except Exception:  # noqa: BLE001 - en kvarglömd fil fäller ingenting
                logger.warning("Flamingo: bildfilen %s kunde inte tas bort", image.name)

    transaction.on_commit(remove)


models.signals.post_delete.connect(_delete_image_files, sender=MediaAsset)
models.signals.post_delete.connect(_delete_image_files, sender=SiteImageCandidate)


def empty_page_content():
    """Ett tomt utkast eller en opublicerad sida: {"blocks": []}."""
    return {"blocks": []}


class LandingPage(models.Model):
    """En landningssida i sidbyggaren. Blocken och deras JSON beskrivs i
    apps/flamingo/pagebuilder/__init__.py.

    draft       det kunden (eller ADX i kundvyn) arbetar med
    published   det besökarna ser; {"blocks": []} tills sidan publicerats
                första gången (published_at)
    rev         ökar med ett för varje sparat utkast (pagebuilder.save_draft):
                en sparning med ett gammalt rev nekas, så att två flikar
                aldrig skriver över varandra utan att veta om det; en
                publicering med ett gammalt rev nekas också
    built_for,  kampanjen vars förslag byggde sidan och utkastets rev då
    built_rev   (pagebuilder.create_page_for_campaign). Ett nytt förslag
                bygger om sidan bara för den kampanjen och bara så länge rev
                är detsamma (pagebuilder.refresh_from_proposal); en sida som
                kunden valt, kopierat eller ändrat byggs aldrig om

    En sida kan användas av flera kampanjer (Campaign.landing_page). Varje
    kampanj har ändå sin egen adress /lp/<page_slug>/, så att förfrågningarna
    räknas till rätt kampanj. Ingen låsning av sidor eller block: en ändring
    på en sida som är live publiceras direkt när kontrollerna går igenom, och
    byrån får ett larm (pagebuilder.publish_page). Det gäller också paletten,
    logotypen och en live-kampanjs byte av sida. Kunden mejlas aldrig."""

    DESIGN_REN = "ren"
    DESIGN_CHOICES = [(DESIGN_REN, "Ren")]

    PALETTE_BLUE = "blue"
    PALETTE_GREEN = "green"
    PALETTE_RED = "red"
    PALETTE_ORANGE = "orange"
    PALETTE_GRAPHITE = "graphite"
    #: Färgerna från kundens logotyp (logo_colors), fylls i av mediaarkivet.
    PALETTE_LOGO = "logo"
    PALETTE_CHOICES = [
        (PALETTE_BLUE, "Blå"),
        (PALETTE_GREEN, "Grön"),
        (PALETTE_RED, "Röd"),
        (PALETTE_ORANGE, "Orange"),
        (PALETTE_GRAPHITE, "Grafit"),
        (PALETTE_LOGO, "Från logotypen"),
    ]

    account = models.ForeignKey(
        FlamingoAccount, on_delete=models.CASCADE, related_name="landing_pages"
    )
    name = models.CharField("Namn", max_length=120)
    design = models.CharField("Design", max_length=20, choices=DESIGN_CHOICES, default=DESIGN_REN)
    palette = models.CharField(
        "Palett", max_length=20, choices=PALETTE_CHOICES, default=PALETTE_BLUE
    )
    #: Färgerna ur logotypen, som mediaarkivet läser ut:
    #:   {"primary": "#1F6FEB", "accent": "#F2994A", "asset": 12}
    #: Bara hexkoder (#RRGGBB). Renderaren justerar dem tills kontrasten
    #: klarar WCAG AA (pagebuilder/render.py, palette_vars).
    logo_colors = models.JSONField("Färger från logotypen", default=dict, blank=True)
    draft = models.JSONField("Utkast", default=empty_page_content, blank=True)
    published = models.JSONField("Publicerad", default=empty_page_content, blank=True)
    published_at = models.DateTimeField("Publicerad", null=True, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="Publicerad av",
    )
    rev = models.PositiveIntegerField("Version av utkastet", default=1)
    built_for = models.ForeignKey(
        "Campaign",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="Byggd av förslaget för",
    )
    built_rev = models.PositiveIntegerField(
        "Utkastets rev när förslaget byggde det", null=True, blank=True
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        verbose_name = "Landningssida"
        verbose_name_plural = "Landningssidor"

    def __str__(self):
        return self.name

    @staticmethod
    def _blocks(content):
        blocks = content.get("blocks") if isinstance(content, dict) else None
        return [b for b in blocks if isinstance(b, dict)] if isinstance(blocks, list) else []

    @property
    def draft_blocks(self):
        return self._blocks(self.draft)

    @property
    def published_blocks(self):
        return self._blocks(self.published)

    @property
    def is_published(self):
        return self.published_at is not None

    @property
    def live_blocks(self):
        """Blocken en kampanj visar: den publicerade versionen, eller
        utkastet för en sida som aldrig publicerats (det är det som
        publiceras när kampanjen går live)."""
        return self.published_blocks if self.is_published else self.draft_blocks

    @property
    def has_unpublished_changes(self):
        return self.is_published and self.draft_blocks != self.published_blocks

    def blocks_for(self, which):
        """which = "draft" eller "published"."""
        return self.published_blocks if which == "published" else self.draft_blocks


# ---------------------------------------------------------------------------
# Kampanjen och granskningen
# ---------------------------------------------------------------------------

#: Bolagsformer som inte hör hemma i en adress ("Lindqvist Rör AB" blir
#: "lindqvist-ror").
_LEGAL_FORMS = {"ab", "hb", "kb", "aktiebolag", "handelsbolag", "kommanditbolag"}


def company_slug(name):
    parts = [p for p in slugify(name or "").split("-") if p and p not in _LEGAL_FORMS]
    return "-".join(parts)


def make_page_slug(customer, service_name, exclude_pk=None):
    """Landningssidans adress: "<kund>-<tjänst>", unik bland alla kampanjer.

    Krockar får -2, -3 ... Längden hålls inom fältets 80 tecken, även med
    suffixet."""
    base = slugify(f"{company_slug(customer.name)} {service_name}")[:80].strip("-") or "sida"
    slug, n = base, 2
    taken = Campaign.objects.exclude(pk=exclude_pk) if exclude_pk else Campaign.objects.all()
    while taken.filter(page_slug=slug).exists():
        suffix = f"-{n}"
        slug = base[: 80 - len(suffix)].rstrip("-") + suffix
        n += 1
    return slug


class Campaign(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_IN_REVIEW = "in_review"
    STATUS_NEEDS_CUSTOMER = "needs_customer"
    STATUS_LIVE = "live"
    STATUS_PAUSED = "paused"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Utkast"),
        (STATUS_IN_REVIEW, "Hos ADX"),
        (STATUS_NEEDS_CUSTOMER, "Väntar på dig"),
        (STATUS_LIVE, "Live"),
        (STATUS_PAUSED, "Pausad"),
    ]
    #: Fälten som utgör kampanjens innehåll: det kunden skickar in, det
    #: byrån granskar och det som jämförs mellan rundorna. Landningssidan är
    #: inte längre ett fält här: den ligger i sidbyggaren (landing_page) och
    #: ändras där, och ögonblicksbilden säger vilken sida och version som
    #: skickades (content_snapshot). Gamla rundor har kvar "page".
    CONTENT_FIELDS = (
        "name",
        "area",
        "radius_km",
        "daily_budget_kr",
        "headlines",
        "descriptions",
        "keywords",
        "negatives",
    )

    account = models.ForeignKey(FlamingoAccount, on_delete=models.CASCADE, related_name="campaigns")
    service = models.ForeignKey(
        Service, on_delete=models.PROTECT, related_name="campaigns", verbose_name="Tjänst"
    )
    name = models.CharField("Namn", max_length=120)
    status = models.CharField("Status", max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    area = models.CharField(
        "Område", max_length=200, blank=True, help_text="Till exempel Nacka + 15 km."
    )
    radius_km = models.PositiveSmallIntegerField("Radie (km)", default=15)
    daily_budget_kr = models.PositiveIntegerField(
        "Budget per dag (kr)",
        default=150,
        validators=[MinValueValidator(BUDGET_MIN), MaxValueValidator(BUDGET_MAX)],
    )
    #: Rubriker (högst HEADLINE_COUNT, var och en högst HEADLINE_MAX tecken).
    headlines = models.JSONField("Rubriker", default=list, blank=True)
    #: Beskrivningar (högst DESCRIPTION_COUNT, högst DESCRIPTION_MAX tecken).
    descriptions = models.JSONField("Beskrivningar", default=list, blank=True)
    #: [{"text": "badrumsrenovering nacka", "match": "phrase"}, ...]
    keywords = models.JSONField("Sökord", default=list, blank=True)
    negatives = models.JSONField("Negativa sökord", default=list, blank=True)
    #: HISTORIK: landningssidans innehåll från tiden före sidbyggaren.
    #: Migreringen 0011 flyttade det till en LandingPage (landing_page), och
    #: inget läser fältet längre; det står kvar så att gamla kampanjer och
    #: granskningar går att förstå. Formen var:
    #:   {"title": "Rörjour i Nacka",                 rubriken
    #:    "lead": "Vattenläcka eller stopp? ...",      ingressen
    #:    "points": ["Säker Vatten-auktoriserade"],    punkter under ingressen
    #:    "phone": "08-000 00 00",                     ringknappen (tom = ingen)
    #:    "form_title": "Berätta om jobbet",           formulärets rubrik
    #:    "questions": [{"key": "storlek",             extra frågor i formuläret;
    #:                   "label": "Ungefär hur stort?", svaren hamnar i
    #:                   "kind": "text"}],              Lead.answers[label]
    #:    "note": "..."}                               text under formuläret
    #: kind är "text", "textarea" eller "date" (boka tid).
    page = models.JSONField("Landningssidan före sidbyggaren", default=dict, blank=True)
    #: Sidan kampanjen visar på /lp/<page_slug>/. Flera kampanjer kan dela en
    #: sida; adressen är ändå kampanjens egen, så förfrågningarna räknas till
    #: rätt kampanj. RESTRICT: en sida som en kampanj använder kan inte tas
    #: bort (en live-annons skulle annars leda till en 404), men kontot kan
    #: tas bort med allt sitt, eftersom kampanjen då tas bort i samma svep.
    landing_page = models.ForeignKey(
        LandingPage,
        null=True,
        blank=True,
        on_delete=models.RESTRICT,
        related_name="campaigns",
        verbose_name="Landningssida",
    )
    page_slug = models.SlugField("Sidans adress", max_length=80, unique=True)
    #: Kundens val vid inskicket: "Jag vill att ADX granskar kampanjen innan
    #: den publiceras" (av från början, beslut 2026-10-03). Utan granskning
    #: är inskicket kundens godkännande och kampanjen publiceras direkt när
    #: det går (google_publish.publish_approved).
    review_requested = models.BooleanField("Kunden bad om granskning", default=False)
    google_campaign_id = models.CharField("Kampanjens id hos Google", max_length=40, blank=True)
    #: Resursnamnen hos Google efter publiceringen med API:t, till exempel
    #:   {"budget": "customers/1/campaignBudgets/2", "campaign": "...",
    #:    "ad_group": "...", "ad": "...", "criteria": ["..."]}
    google_resources = models.JSONField("Resurser hos Google", default=dict, blank=True)
    google_synced_at = models.DateTimeField("Synkad med Google", null=True, blank=True)
    google_error = models.CharField("Fel från Google", max_length=500, blank=True)
    #: Spärren mot dubbla kampanjer hos Google: sätts med en villkorlig
    #: UPDATE innan publiceringen anropar Google (claim_google_publish), så
    #: att två klick aldrig skapar två kampanjer.
    google_publish_started_at = models.DateTimeField(
        "Publiceringen hos Google påbörjad", null=True, blank=True
    )
    #: Det spärrens försök skickar till Google: {"name": kampanjens namn
    #: hos Google, "fingerprint": en hash av hela anropet}. Ett senare
    #: försök som hittar kampanjen hos Google tar bara över den om
    #: innehållet är detsamma (google_publish.go_live).
    google_publish_sent = models.JSONField("Skickat till Google", default=dict, blank=True)
    #: Senaste publiceringen hos Google som kundens inskick eller godkännande
    #: startade och som inte lyckades (google_publish.publish_approved): ett
    #: nytt inskick strax efter anropar inte Google igen.
    google_attempted_at = models.DateTimeField(
        "Senaste misslyckade publiceringen efter kunden", null=True, blank=True
    )
    #: Senaste larmet till byrån om kampanjen och dess ämnesrad: samma larm
    #: skickas inte igen inom en timme (app_views/campaigns._alert_agency).
    agency_alerted_at = models.DateTimeField("Byrån larmad", null=True, blank=True)
    agency_alert_subject = models.CharField("Larmets ämne", max_length=200, blank=True)
    approved_at = models.DateTimeField("Godkänd av kunden", null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    published_at = models.DateTimeField("Publicerad", null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Kampanj"
        verbose_name_plural = "Kampanjer"
        indexes = [models.Index(fields=["account", "status"], name="flamingo_campaign_status")]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.page_slug:
            self.page_slug = make_page_slug(
                self.account.customer, self.service.name if self.service_id else self.name
            )
        super().save(*args, **kwargs)

    @property
    def sales_mode(self):
        return self.service.sales_mode

    def get_sales_mode_display(self):
        return self.service.get_sales_mode_display()

    @property
    def is_public(self):
        """Landningssidan syns för alla bara när kampanjen är live."""
        return self.status == self.STATUS_LIVE

    @property
    def landing_url(self):
        return reverse("flamingo_public:landing", args=[self.page_slug])

    @property
    def customer_can_edit(self):
        """Kunden ändrar i utkastet och efter granskningen, aldrig medan ADX
        granskar eller i en publicerad kampanj (ändringar går samma väg som
        förslaget: ny runda)."""
        return self.status in (self.STATUS_DRAFT, self.STATUS_NEEDS_CUSTOMER)

    @property
    def monthly_budget_kr(self):
        """Ungefär en månad: 30,4 dagar, avrundat till hela kronor."""
        return round(self.daily_budget_kr * 30.4)

    def content_snapshot(self):
        """Kampanjens innehåll som ett JSON-bart dict (Review.snapshot):
        CONTENT_FIELDS och vilken landningssida (id, namn, utkastets rev)
        kampanjen hade när den skickades."""
        snapshot = {field: getattr(self, field) for field in self.CONTENT_FIELDS}
        page = self.landing_page if self.landing_page_id else None
        snapshot["landing"] = (
            {"id": page.pk, "name": page.name, "rev": page.rev} if page is not None else None
        )
        return snapshot

    def latest_review(self):
        return self.reviews.order_by("-round").first()

    def pending_review(self):
        return self.reviews.filter(state=Review.STATE_PENDING).order_by("-round").first()

    def next_round(self):
        last = self.reviews.aggregate(m=models.Max("round"))["m"]
        return (last or 0) + 1

    def claim_google_publish(self, now=None, sent=None):
        """Ta spärren för publiceringen hos Google. True bara för den som
        fick den: en villkorlig UPDATE i databasen, så att ett dubbelklick
        eller två flikar aldrig skapar två kampanjer hos Google. sent (vad
        försöket skickar, google_publish_sent) sparas i samma UPDATE, så att
        det finns kvar även om processen dör mitt i anropet."""
        now = now or timezone.now()
        values = {"google_publish_started_at": now}
        if sent is not None:
            values["google_publish_sent"] = sent
        claimed = Campaign.objects.filter(
            pk=self.pk, google_publish_started_at__isnull=True
        ).update(**values)
        if claimed:
            self.google_publish_started_at = now
            if sent is not None:
                self.google_publish_sent = sent
        return bool(claimed)

    def release_google_publish(self):
        """Släpp spärren när Google sagt nej och inget skapades (allt i
        samma anrop, så inget halvt finns kvar hos Google)."""
        Campaign.objects.filter(pk=self.pk).update(google_publish_started_at=None)
        self.google_publish_started_at = None


class CampaignDayStats(models.Model):
    """En dag ur Googles rapport för en kampanj. Kostnaden i mikros som
    Google skickar den (kronor gånger en miljon)."""

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="day_stats")
    date = models.DateField("Dag")
    cost_micros = models.BigIntegerField("Kostnad (mikros)", default=0)
    impressions = models.PositiveIntegerField("Visningar", default=0)
    clicks = models.PositiveIntegerField("Klick", default=0)
    conversions = models.FloatField("Konverteringar", default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date"]
        verbose_name = "Dag i Googles rapport"
        verbose_name_plural = "Dagar i Googles rapport"
        constraints = [
            models.UniqueConstraint(fields=["campaign", "date"], name="flamingo_daystats_day")
        ]

    def __str__(self):
        return f"{self.campaign}: {self.date}"

    @property
    def cost_kr(self):
        """Kostnaden i hela kronor."""
        return round((self.cost_micros or 0) / 1_000_000)


class Review(models.Model):
    """En granskningsrunda: kunden skickar in, byrån rättar och skriver varför.

    changes är listan kunden ser före godkännandet:
        [{"field": "headlines", "label": "Rubrik", "before": "...",
          "after": "...", "reason": "..."}]
    """

    STATE_PENDING = "pending"
    STATE_DONE = "done"
    STATE_CHOICES = [
        (STATE_PENDING, "Väntar på ADX"),
        (STATE_DONE, "Granskad"),
    ]

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="reviews")
    round = models.PositiveSmallIntegerField("Runda")
    submitted_at = models.DateTimeField("Inskickad", default=timezone.now)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    state = models.CharField("Läge", max_length=10, choices=STATE_CHOICES, default=STATE_PENDING)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="Granskare",
    )
    reviewed_at = models.DateTimeField("Granskad", null=True, blank=True)
    #: Kampanjens innehåll som det såg ut när kunden skickade in
    #: (Campaign.content_snapshot()). Grunden för diffen mot granskningen.
    snapshot = models.JSONField("Inskickat innehåll", default=dict, blank=True)
    changes = models.JSONField("Ändringar", default=list, blank=True)
    note = models.TextField("Meddelande till kunden", blank=True)
    #: Byrån tog tillbaka en godkänd kampanj till granskning (manage_review,
    #: "Tillbaka till granskning"). Inte samma sak som att byrån skickade in
    #: i kundvyn: där skickar byrån som kunden.
    taken_back = models.BooleanField("ADX tog tillbaka kampanjen", default=False)

    class Meta:
        ordering = ["-round"]
        verbose_name = "Granskning"
        verbose_name_plural = "Granskningar"
        constraints = [
            models.UniqueConstraint(fields=["campaign", "round"], name="flamingo_review_round")
        ]

    def __str__(self):
        return f"{self.campaign}: runda {self.round}"


# ---------------------------------------------------------------------------
# Förfrågningar, affärer och sms
# ---------------------------------------------------------------------------


class Lead(models.Model):
    SOURCE_FORM = "form"
    SOURCE_CALL = "call"
    SOURCE_MANUAL = "manual"
    SOURCE_CALL_CLICK = "call_click"
    SOURCE_CHOICES = [
        (SOURCE_FORM, "Formulär"),
        (SOURCE_CALL, "Samtal"),
        (SOURCE_MANUAL, "Manuell"),
        (SOURCE_CALL_CLICK, "Klick på telefonnumret"),
    ]

    #: Besökarens samtycke till att Google får använda uppgifterna för
    #: annonsmätning. Tomt: inte tillfrågad, och så är det i dag:
    #: landningssidan frågar inte (beslut 2026-10-03, konverteringarna går
    #: till Google ändå). Fältet finns kvar för en fråga senare, och
    #: konverteringen får samtycket med sig bara när det är "granted" eller
    #: "denied" (google_conversions.conversion_event och click_conversion).
    #: Fylls aldrig i av oss.
    CONSENT_UNKNOWN = ""
    CONSENT_GRANTED = "granted"
    CONSENT_DENIED = "denied"
    CONSENT_CHOICES = [
        (CONSENT_UNKNOWN, "Inte tillfrågad"),
        (CONSENT_GRANTED, "Ja"),
        (CONSENT_DENIED, "Nej"),
    ]

    STATUS_NEW = "new"
    STATUS_CONTACTED = "contacted"
    STATUS_QUOTE = "quote"
    STATUS_WON = "won"
    STATUS_LOST = "lost"
    STATUS_JUNK = "junk"
    STATUS_CHOICES = [
        (STATUS_NEW, "Ny"),
        (STATUS_CONTACTED, "Kontaktad"),
        (STATUS_QUOTE, "Offert skickad"),
        (STATUS_WON, "Vunnen"),
        (STATUS_LOST, "Förlorad"),
        (STATUS_JUNK, "Skräp"),
    ]
    #: Förfrågningar som räknas i översikten (skräp räknas inte).
    COUNTED_STATUSES = (
        STATUS_NEW,
        STATUS_CONTACTED,
        STATUS_QUOTE,
        STATUS_WON,
        STATUS_LOST,
    )

    account = models.ForeignKey(FlamingoAccount, on_delete=models.CASCADE, related_name="leads")
    campaign = models.ForeignKey(
        Campaign, null=True, blank=True, on_delete=models.SET_NULL, related_name="leads"
    )
    service = models.ForeignKey(
        Service, null=True, blank=True, on_delete=models.SET_NULL, related_name="leads"
    )
    source = models.CharField("Källa", max_length=10, choices=SOURCE_CHOICES, default=SOURCE_FORM)
    name = models.CharField("Namn", max_length=120, blank=True)
    phone = models.CharField("Telefon", max_length=40, blank=True)
    email = models.EmailField("E-post", blank=True)
    message = models.TextField("Meddelande", blank=True)
    #: Svaren på formulärets frågor: {"Ungefär hur stort?": "6 m2", ...}.
    answers = models.JSONField("Svar", default=dict, blank=True)
    gclid = models.CharField("Googles klick-id", max_length=200, blank=True)
    #: Klick-id:n från iOS (appar respektive webben), när gclid saknas.
    gbraid = models.CharField("Googles klick-id (gbraid)", max_length=200, blank=True)
    wbraid = models.CharField("Googles klick-id (wbraid)", max_length=200, blank=True)
    ad_consent = models.CharField(
        "Samtycke till annonsmätning",
        max_length=10,
        choices=CONSENT_CHOICES,
        blank=True,
        default=CONSENT_UNKNOWN,
    )
    #: {"utm_source": "google", "utm_campaign": "...", ...}
    utm = models.JSONField("UTM", default=dict, blank=True)
    keyword = models.CharField("Sökord", max_length=200, blank=True)
    #: HMAC av besökarens IP (limits.ip_hash), för spärren på /lp/. Aldrig
    #: själva adressen.
    ip_hash = models.CharField("IP (hash)", max_length=64, blank=True, db_index=True)
    status = models.CharField("Status", max_length=12, choices=STATUS_CHOICES, default=STATUS_NEW)
    value_kr = models.PositiveIntegerField("Affärens värde (kr)", null=True, blank=True)
    won_at = models.DateTimeField("Vunnen", null=True, blank=True)
    created_at = models.DateTimeField("Kom in", default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Förfrågan"
        verbose_name_plural = "Förfrågningar"
        indexes = [
            models.Index(fields=["account", "status", "-created_at"], name="flamingo_lead_status"),
        ]

    def __str__(self):
        return f"{self.display_name} ({self.get_status_display()})"

    @property
    def display_name(self):
        fallback = "Klick på telefonnumret" if self.source == self.SOURCE_CALL_CLICK else "Okänd"
        return self.name or self.phone or self.email or fallback

    @property
    def click_ids(self):
        """Googles klick-id:n som finns, som {"gclid": ..., "gbraid": ...}."""
        found = {"gclid": self.gclid, "gbraid": self.gbraid, "wbraid": self.wbraid}
        return {key: value for key, value in found.items() if value}

    @property
    def has_click_id(self):
        return bool(self.gclid or self.gbraid or self.wbraid)

    @property
    def service_name(self):
        if self.service_id:
            return self.service.name
        if self.campaign_id:
            return self.campaign.service.name
        return ""

    @property
    def can_send_to_google(self):
        """Kan förfrågan bli en konvertering hos Google? Det kräver ett
        gclid: Flamingos konverteringar räknas en gång per klick
        (ONE_PER_CLICK), och sådana tar Google inte emot med gbraid eller
        wbraid (iOS), varken med Google Ads API eller Data Manager API
        (PROCESSING_ERROR_REASON_ONE_PER_CLICK_CONVERSION_ACTION_NOT_PERMITTED_WITH_BRAID).
        De sparas på förfrågan men laddas inte upp. Samtycket (ad_consent)
        avgör inte: konverteringarna skickas utan fråga på sidan (beslut
        2026-10-03)."""
        return bool(self.gclid)

    @property
    def arrival_kind(self):
        """Konverteringen som förfrågan själv ger (ConversionUpload.kind):
        formuläret en förfrågan, ett klick på numret ett samtal. Förfrågningar
        som lagts in för hand har ingen (inget klick att koppla till)."""
        if self.source == self.SOURCE_FORM:
            return ConversionUpload.KIND_LEAD
        if self.source == self.SOURCE_CALL_CLICK:
            return ConversionUpload.KIND_CALL
        return ""

    def queue_conversion(self, kind, value_kr=None):
        """Köa en konvertering av sorten, om förfrågan får gå till Google
        (can_send_to_google). Högst en per förfrågan och sort: finns den
        redan lämnas den. Returnerar raden, eller None."""
        if not kind or not self.can_send_to_google:
            return None
        upload, _ = ConversionUpload.objects.get_or_create(
            lead=self, kind=kind, defaults={"value_kr": value_kr}
        )
        return upload

    def queue_arrival_conversion(self):
        """Köa förfrågans egen konvertering (arrival_kind) när den kommer in.
        Anropas av leads.create_lead och leads.create_call_click_lead."""
        return self.queue_conversion(self.arrival_kind)

    def _unqueue(self, kind):
        """Ta bort en konvertering som fortfarande står i kö. Villkoret ligger
        i själva DELETE:n, så en rad som just skickats eller exporterats
        (flamingo_google_sync, CSV-exporten) aldrig tas bort."""
        ConversionUpload.objects.filter(
            lead=self, kind=kind, status=ConversionUpload.STATUS_QUEUED
        ).delete()

    @transaction.atomic
    def set_status(self, status, value_kr=None, now=None):
        """Byt status (inkorgen, och senare sms-svaret "VANN 186000").

        Vunnen med ett belopp köar en affär (ConversionUpload, kind=deal) när
        förfrågan kan gå till Google (can_send_to_google: ett gclid).
        Ändras beloppet medan affären står i kö följer det
        med, och lämnar förfrågan "vunnen" tas en affär som ännu inte gått
        iväg bort. Skräp tar bort förfrågans egen konvertering (förfrågan
        eller samtal) om den står i kö, och ångras skräpet köas den igen.
        Det som redan skickats eller exporterats rörs aldrig."""
        if status not in dict(self.STATUS_CHOICES):
            raise ValueError(f"Okänd status: {status}")
        now = now or timezone.now()
        was_junk = self.status == self.STATUS_JUNK
        self.status = status
        if status == self.STATUS_WON:
            if value_kr is not None:
                self.value_kr = int(value_kr)
            if self.won_at is None:
                self.won_at = now
        else:
            self.won_at = None
        self.save()

        deal = ConversionUpload.KIND_DEAL
        if status == self.STATUS_WON and self.value_kr is not None and self.can_send_to_google:
            upload = self.queue_conversion(deal, value_kr=self.value_kr)
            if upload is not None and upload.value_kr != self.value_kr:
                ConversionUpload.objects.filter(
                    pk=upload.pk, status=ConversionUpload.STATUS_QUEUED
                ).update(value_kr=self.value_kr)
        else:
            self._unqueue(deal)

        if self.arrival_kind:
            if status == self.STATUS_JUNK:
                self._unqueue(self.arrival_kind)
            elif was_junk:
                self.queue_arrival_conversion()
        return self


class ConversionUpload(models.Model):
    """En konvertering på väg till Google som offline-konvertering: en
    förfrågan, ett klick på telefonnumret eller en vunnen affär (med värde).
    Högst en av varje sort per förfrågan. Med API laddas den upp
    (google_conversions.py); annars, och så länge den står i kö, exporterar
    byrån den som CSV (/manage/flamingo/granska/).

    En rad står i kö tills Google tagit emot den. Ett nej lämnar den i kö
    med felet (error), räknar attempts och väntar till next_attempt_at.
    Efter MAX_ATTEMPTS skickas den inte längre med API:t av sig själv, men
    finns kvar för CSV-filen.

    En rad går bara en väg: kommer den med i en nedladdad CSV-fil
    (downloaded_at) skickar API:t den aldrig, och en rad som API:t skickat
    står inte längre i kö och kommer aldrig med i en fil."""

    #: Försök med API:t innan raden lämnas åt CSV-filen.
    MAX_ATTEMPTS = 8

    KIND_LEAD = "lead"
    KIND_CALL = "call"
    KIND_DEAL = "deal"
    KIND_CHOICES = [
        (KIND_LEAD, "Förfrågan"),
        (KIND_CALL, "Klick på telefonnumret"),
        (KIND_DEAL, "Affär"),
    ]

    STATUS_QUEUED = "queued"
    STATUS_EXPORTED = "exported"
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "I kö"),
        (STATUS_EXPORTED, "Exporterad"),
        (STATUS_SENT, "Skickad"),
        (STATUS_FAILED, "Misslyckades"),
    ]

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="conversions")
    kind = models.CharField("Sort", max_length=10, choices=KIND_CHOICES, default=KIND_DEAL)
    #: Bara affärer har ett värde.
    value_kr = models.PositiveIntegerField("Värde (kr)", null=True, blank=True)
    status = models.CharField(
        "Status", max_length=10, choices=STATUS_CHOICES, default=STATUS_QUEUED
    )
    exported_at = models.DateTimeField("Exporterad", null=True, blank=True)
    sent_at = models.DateTimeField("Skickad", null=True, blank=True)
    error = models.CharField("Fel", max_length=300, blank=True)
    #: Hur många gånger ett försök med API:t inte gick fram (Googles nej,
    #: inget svar). Nästa försök tidigast next_attempt_at.
    attempts = models.PositiveSmallIntegerField("Försök som inte gick fram", default=0)
    next_attempt_at = models.DateTimeField("Nästa försök", null=True, blank=True)
    #: Data Manager API: id:t för sändningen som Google tog emot (requestId),
    #: och när Googles besked om den lästes (google_conversions.check_sent).
    request_id = models.CharField("Sändningens id hos Google", max_length=100, blank=True)
    checked_at = models.DateTimeField("Googles besked läst", null=True, blank=True)
    #: När raden först kom med i en nedladdad CSV-fil
    #: (manage_review.conversions_csv). En rad i en fil skickas aldrig med
    #: API:t (google_conversions._due), och bara rader som varit i en fil
    #: kan markeras som exporterade.
    downloaded_at = models.DateTimeField("I en nedladdad fil", null=True, blank=True)
    #: Svaret från Google (eller exportens filnamn) för felsökning.
    response = models.JSONField("Svar", default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Konvertering till Google"
        verbose_name_plural = "Konverteringar till Google"
        constraints = [
            models.UniqueConstraint(fields=["lead", "kind"], name="flamingo_conversion_kind")
        ]

    def __str__(self):
        value = f", {self.value_kr} kr" if self.value_kr is not None else ""
        return (
            f"{self.lead.display_name}: {self.get_kind_display()}{value}"
            f" ({self.get_status_display()})"
        )

    @property
    def transaction_id(self):
        """Konverteringens id hos Google (transactionId i Data Manager API,
        orderId i Google Ads API): samma för förfrågan och sort vid varje
        försök, så att Google aldrig räknar den två gånger."""
        return f"adx-flamingo-{self.lead_id}-{self.kind}"

    @property
    def api_gave_up(self):
        """API:t har försökt MAX_ATTEMPTS gånger: raden väntar på CSV-filen
        eller på "Försök ladda upp igen"."""
        return self.attempts >= self.MAX_ATTEMPTS

    @property
    def in_file(self):
        """Raden har varit med i en nedladdad CSV-fil: den går den vägen."""
        return self.downloaded_at is not None


class SmsLog(models.Model):
    """Varje sms Flamingo skickade eller lät bli att skicka, och varför."""

    KIND_OWNER = "owner_notice"
    KIND_AUTOREPLY = "autoreply"
    KIND_CHOICES = [
        (KIND_OWNER, "Till dig om en ny förfrågan"),
        (KIND_AUTOREPLY, "Autosvar"),
    ]

    STATUS_SENDING = "sending"
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_NOT_CONFIGURED = "not_configured"
    STATUS_DISABLED = "disabled"
    STATUS_CHOICES = [
        (STATUS_SENDING, "Skickas"),
        (STATUS_SENT, "Skickat"),
        (STATUS_FAILED, "Misslyckades"),
        (STATUS_NOT_CONFIGURED, "Inte skickat: sms är inte inkopplat"),
        (STATUS_DISABLED, "Inte skickat: avstängt i inställningarna"),
    ]

    account = models.ForeignKey(FlamingoAccount, on_delete=models.CASCADE, related_name="sms_log")
    lead = models.ForeignKey(
        Lead, null=True, blank=True, on_delete=models.SET_NULL, related_name="sms_log"
    )
    kind = models.CharField("Sort", max_length=20, choices=KIND_CHOICES)
    to = models.CharField("Till", max_length=20, blank=True)
    body = models.TextField("Text")
    status = models.CharField("Status", max_length=20, choices=STATUS_CHOICES)
    provider_id = models.CharField("Id hos 46elks", max_length=64, blank=True)
    error = models.CharField("Fel", max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Sms"
        verbose_name_plural = "Sms"

    def __str__(self):
        return f"{self.get_kind_display()} till {self.to or '-'}: {self.get_status_display()}"
