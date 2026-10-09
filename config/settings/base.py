"""
Base settings shared by all environments.

Everything that varies between machines/sites is read from the environment
(.env in dev, systemd EnvironmentFile in production). No secrets live here.
"""

from pathlib import Path

import environ

# config/settings/base.py -> BASE_DIR is the repo root (app dir).
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
)

# Read .env from the project dir (one level above the app dir) if present,
# otherwise from the app dir. Production reads it via systemd instead.
for candidate in (BASE_DIR.parent / ".env", BASE_DIR / ".env"):
    if candidate.exists():
        environ.Env.read_env(str(candidate))
        break

SECRET_KEY = env("SECRET_KEY", default="dev-insecure-key-change-me")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["127.0.0.1", "localhost"])
SITE_SLUG = env("SITE_SLUG", default="dev")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sitemaps",
    "apps.core",
    "apps.common",
    "apps.website",
    "apps.services",
    "apps.areas",
    "apps.faq",
    "apps.inquiries",
    "apps.analytics",
    "apps.manage",
    "apps.tools",
    "apps.offers",
    "apps.projects",
    "apps.monitor",
    "apps.aidocs",
    "apps.cloud",
    "apps.flamingo",
    "apps.sms",
    # Flamingo 2.0: kontakter och utskick (apps/utskick/README.md).
    "apps.utskick",
    "reversion",
    "apps.assistant",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Utskickens länkvärdar k.adx.se och klick.adx.se: där svarar bara
    # config.urls_links (apps/utskick/links.py, README E.1).
    "apps.utskick.links.LinkHostMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Kundportalens användare hålls borta från /manage/ (apps/projects).
    "apps.projects.middleware.PortalGateMiddleware",
    # Versionerar alla skrivande /manage/-requests (apps/assistant/revisions.py).
    "apps.assistant.revisions.ManageRevisionMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    # ADX Flamingo: obehöriga routas som om /flamingo/ inte fanns (apps/flamingo).
    # Efter Messages: kundvyns skrivskydd lämnar ett meddelande.
    "apps.flamingo.middleware.FlamingoGateMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.analytics.middleware.AnalyticsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.manage.context_processors.inquiry_badge",
                "apps.manage.context_processors.static_version",
                "apps.website.context_processors.site_chrome",
                "apps.projects.context_processors.running_timer",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Database - one Postgres DB per site, supplied as a URL.
DATABASES = {
    "default": env.db("DATABASE_URL", default="sqlite:///" + str(BASE_DIR / "db.sqlite3")),
}

# Vakt född ur en verklig incident (2026-08-27): den här kodbasen började
# som en kopia av systersajten, och kopians .env pekade kvar på HENNES
# lokala databas - migrate + seed_site skrev rakt in i fel projekt innan
# någon märkte det. En kopierad .env är en laddad pistol. Spärren är
# medvetet smal (kända främmande namn, inte en tillåtlista) så den aldrig
# stoppar legitima namn - samma kalibreringsprincip som junk-gaten i
# mönsterkatalogen.
_FOREIGN_DB_NAMES = ("skandivvs", "kronan", "jungfru")


def _refuse_foreign_database(name):
    lowered = str(name).lower()
    if any(foreign in lowered for foreign in _FOREIGN_DB_NAMES):
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured(
            f"DATABASE_URL pekar på en främmande databas ({lowered!r}) - detta är "
            "ADX-projektet. Kopierade .env-filer ärver systerprojektens pekare; "
            "peka om DATABASE_URL till en adx-databas innan något körs."
        )


_refuse_foreign_database(DATABASES["default"].get("NAME", ""))

# Parallella testkörningar (flera agenter samtidigt) krockar annars på samma
# testdatabas: den ena droppar den andras mitt i körningen.
if env("TEST_DB_NAME", default=""):
    DATABASES["default"]["TEST"] = {"NAME": env("TEST_DB_NAME")}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "sv"
TIME_ZONE = "Europe/Stockholm"
USE_I18N = True
USE_TZ = True

# Authentication redirects for the customer control panel (/manage/).
LOGIN_URL = "login"

# Absolut bas-URL, används där en länk måste fungera utanför en request -
# t.ex. granskningslänken MCP-servern ger AI:n. Tom = relativa länkar.
SITE_BASE_URL = env("SITE_BASE_URL", default="")

# Den inbyggda AI-assistenten. Servern kör i AWS, så modellen går via
# Bedrock med instansrollen - inga API-nycklar att distribuera eller rotera.
# "anthropic" är reservläget för utveckling på en maskin utan AWS-uppgifter.
ASSISTANT_PROVIDER = env("ASSISTANT_PROVIDER", default="bedrock")
ASSISTANT_BEDROCK_REGION = env("ASSISTANT_BEDROCK_REGION", default="eu-central-1")
# Opus 4.6 är den starkaste modellen konto 200810847648 släpper fram - Opus 5
# och Sonnet 5 nekas med "not available for this account" (kontobegränsning,
# inte region). Måste vara Sonnet/Opus 4.6+; llm.assert_model_allowed() spärrar.
ASSISTANT_BEDROCK_MODEL = env("ASSISTANT_BEDROCK_MODEL", default="eu.anthropic.claude-sonnet-5")
# Lokalt: namnet på en profil i ~/.aws. På servern tom - instansrollen gäller.
ASSISTANT_AWS_PROFILE = env("ASSISTANT_AWS_PROFILE", default="")

# Kundernas AWS-konton (apps/cloud). I drift antar serverns instansroll kundens
# läsroll; lokalt kan en namngiven profil vara utgångspunkten. Byråns konto-ID
# är det kundrollerna litar på (mallen på kundkortet).
ADX_AWS_PROFILE = env("ADX_AWS_PROFILE", default="")
ADX_AWS_ACCOUNT_ID = env("ADX_AWS_ACCOUNT_ID", default="500841883756")
# Reservläget.
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")
ASSISTANT_MODEL = env("ASSISTANT_MODEL", default="claude-opus-5")
# Moduler i AI:ns verktygsyta. En avstängd modul syns varken i
# verktygslistan eller går att anropa - kunden betalar per modul, och en
# obetald modul ska inte kunna erbjudas av misstag. Slå på med en rad i
# .env; ingen kodändring behövs.
ASSISTANT_FEATURES = {
    "statistik": env.bool("ASSISTANT_FEATURE_STATISTIK", default=False),
}
# Dygnstak i USD, kontrollerat FÖRE varje anrop. Det här är en nödbroms mot
# en loopande modell eller en bugg hos oss - INTE en kundgräns. Kunden har
# inget tak att förhålla sig till och ser aldrig siffran; taket ska ligga så
# högt att normal användning aldrig når det.
ASSISTANT_DAILY_BUDGET_USD = env.float("ASSISTANT_DAILY_BUDGET_USD", default=50.0)
LOGIN_REDIRECT_URL = "manage:dashboard"
LOGOUT_REDIRECT_URL = "login"

# Static + media live one level above the app dir, matching the server layout:
#   /home/djangouser/sites/<site>/{app,collected-staticfiles,user-uploaded-media}
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR.parent / "collected-staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR.parent / "user-uploaded-media"
# Bilagor i ärendesystemet: aldrig under MEDIA_ROOT (som nginx serverar
# publikt). Lämnas ut av gated vyer i apps/projects.
PRIVATE_MEDIA_ROOT = BASE_DIR.parent / "private-uploads"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Error monitoring. No-op when SENTRY_DSN is empty (e.g. in dev).
# Alla inställningar - och framför allt vad som maskas innan något lämnar
# servern (offerttoken, AI-koder, statusnyckeln) - bor i apps/common/sentry.py.
SENTRY_DSN = env("SENTRY_DSN", default="")
if SENTRY_DSN:
    from apps.common import sentry as _sentry

    _sentry.init(SENTRY_DSN, SITE_SLUG, BASE_DIR)

# Structured-ish logging to stdout so journald/CloudWatch can pick it up.

# Email - AWS SES via SMTP.
# Two modes:
#   STARTTLS (ports 25/587/2587): EMAIL_USE_TLS=True  (default)
#   TLS wrapper (ports 465/2465): EMAIL_USE_SSL=True, set EMAIL_PORT=465
# Django requires exactly one of USE_TLS / USE_SSL - we derive SSL from the
# port so you only ever flip EMAIL_PORT in .env.
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST", default="email-smtp.eu-north-1.amazonaws.com")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=EMAIL_PORT in (465, 2465))
EMAIL_USE_TLS = not EMAIL_USE_SSL
EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT", default=10)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="info@adx.se")
INQUIRY_NOTIFICATION_EMAIL = env("INQUIRY_NOTIFICATION_EMAIL", default="info@adx.se")
# Svarsadressen på allt som går TILL kunder (inbjudan, kod, ärendesvar,
# loggsammanställning, offert). Skild från INQUIRY_NOTIFICATION_EMAIL, som är
# vart byråns egna notiser går - de två ska kunna peka på olika inkorgar.
CUSTOMER_REPLY_TO_EMAIL = env("CUSTOMER_REPLY_TO_EMAIL", default="giovanni@adx.se")

# Övervakningen (apps/monitor). ADX_STATUS_KEY är den delade nyckeln som
# /status/adx/ kräver - samma på alla sajter vi driftar, bara vi känner den.
# Tomt = endpointet finns inte. Sentry-uppgifterna gäller BYRÅNS organisation
# (API-token med project:read), inte kundens DSN.
ADX_STATUS_KEY = env("ADX_STATUS_KEY", default="")
SENTRY_ORG_SLUG = env("SENTRY_ORG_SLUG", default="")
SENTRY_API_TOKEN = env("SENTRY_API_TOKEN", default="")
PAGESPEED_API_KEY = env("PAGESPEED_API_KEY", default="")
# Chrome UX Report History API (riktiga besökare, apps/monitor/google_checks.py).
# Tomt = PAGESPEED_API_KEY. Chrome UX Report API måste vara påslaget i
# nyckelns Google Cloud-projekt. Tomt i båda = CrUX hämtas inte.
CRUX_API_KEY = env("CRUX_API_KEY", default="")
# Optional blind-copy recipients for the staff notification (comma-separated).
INQUIRY_NOTIFICATION_BCC = env("INQUIRY_NOTIFICATION_BCC", default="")

# Built-in analytics. Collects first-party visitor/session data for later
# productization. No /manage/ UI - staff view raw data in /admin/.
ANALYTICS_ENABLED = env.bool("ANALYTICS_ENABLED", default=True)

# Google Maps browser key for the service-area maps. Restricted by HTTP
# referrer on Google's side, so it is not a secret - but it is per
# environment, so it lives in env rather than in the database. Empty means
# no maps are rendered at all.
GOOGLE_MAPS_API_KEY = env.str("GOOGLE_MAPS_API_KEY", default="")

# ADX Flamingo (apps/flamingo/README.md). Tomt = integrationen är av och
# verktyget kör den manuella vägen: Editor-CSV och kopplingen som byrån bockar
# av i stället för Google Ads API, fakta från hemsidan och kunden i stället för
# Google Places, och inget sms (det loggas att inget skickades) utan 46elks.
# Valfri och avvecklad hos Google sedan 2026-09-09 (headern ignoreras):
# åtkomsten hör till Google Cloud-projektet som äger OAuth-klienten.
GOOGLE_ADS_DEVELOPER_TOKEN = env.str("GOOGLE_ADS_DEVELOPER_TOKEN", default="")
# Byråns förvaltarkonto (MCC), tio siffror.
GOOGLE_ADS_LOGIN_CUSTOMER_ID = env.str("GOOGLE_ADS_LOGIN_CUSTOMER_ID", default="")
# OAuth-klienten (typ "Webbprogram") i Google Cloud-projektet där Google Ads
# API är påslaget. Inloggningen görs från /manage/ och nyckeln sparas
# krypterad i databasen (apps/flamingo/google_ads.py).
GOOGLE_ADS_CLIENT_ID = env.str("GOOGLE_ADS_CLIENT_ID", default="")
GOOGLE_ADS_CLIENT_SECRET = env.str("GOOGLE_ADS_CLIENT_SECRET", default="")
# Valfri: en långlivad nyckel (refresh token) som vinner över den sparade.
GOOGLE_ADS_REFRESH_TOKEN = env.str("GOOGLE_ADS_REFRESH_TOKEN", default="")
# Tomt = google_ads.DEFAULT_VERSION (v25). Varje version har ett slutdatum hos Google.
GOOGLE_ADS_API_VERSION = env.str("GOOGLE_ADS_API_VERSION", default="")
# Konverteringarnas väg till Google (apps/flamingo/google_conversions.py):
# "datamanager" (standard) Data Manager API, som kräver att API:t är påslaget
# i Cloud-projektet och att inloggningen har behörigheten; "googleads" den
# gamla uploadClickConversions, som Google inte öppnar för nya användare sedan
# 2026-06-15; "off" bara CSV-filen. Det som inte går fram står kvar för filen.
FLAMINGO_CONVERSIONS_UPLOAD = env.str("FLAMINGO_CONVERSIONS_UPLOAD", default="datamanager")
# Av från början: ADX Cloud-projekt står inte på Googles tillåtelselista för
# att bjuda in en administratör när ett konto skapas (createCustomerClient,
# emailAddress). Slå på bara när Google bekräftat det.
GOOGLE_ADS_INVITE_ON_CREATE = env.bool("GOOGLE_ADS_INVITE_ON_CREATE", default=False)
# Valfri: nyckeln som krypterar Google-nyckeln i databasen. Tomt = härledd ur
# SECRET_KEY (byts den kopplar byrån Google igen).
FLAMINGO_TOKEN_KEY = env.str("FLAMINGO_TOKEN_KEY", default="")
GOOGLE_PLACES_API_KEY = env.str("GOOGLE_PLACES_API_KEY", default="")
# 46elks: ett konto för både Flamingos sms och SMS-API:t (apps/sms). De äldre
# namnen SMS_46ELKS_USER/SMS_46ELKS_PASSWORD gäller när ELKS_* saknas.
ELKS_API_USERNAME = env.str("ELKS_API_USERNAME", default=env.str("SMS_46ELKS_USER", default=""))
ELKS_API_PASSWORD = env.str("ELKS_API_PASSWORD", default=env.str("SMS_46ELKS_PASSWORD", default=""))
# Avsändaren i sms: högst elva tecken (bokstäver och siffror) eller ett nummer.
ELKS_SENDER = env.str("ELKS_SENDER", default="")
# Landningssidornas domän i Editor-filen (tomt = https://adx.se) och namnet på
# offline-konverteringen i kundernas Google Ads-konton (tomt = "ADX Flamingo
# affär"). Se apps/flamingo/exports.py.
FLAMINGO_LANDING_BASE_URL = env.str("FLAMINGO_LANDING_BASE_URL", default="")
FLAMINGO_CONVERSION_NAME = env.str("FLAMINGO_CONVERSION_NAME", default="")
# Utvalda omdömen från Reco (apps/flamingo/reco.py): kundens valda omdömen
# från profilsidan på Reco, ritade på landningssidorna. false stänger av det
# för alla direkt, som byråns brytare på /manage/flamingo/: sidorna visar
# Recos egen ruta och inget hämtas.
FLAMINGO_RECO_SELECTED_ENABLED = env.bool("FLAMINGO_RECO_SELECTED_ENABLED", default=True)

# SMS-API:t för kunderna (apps/sms/README.md), via 46elks med uppgifterna ovan.
SMS_PROVIDER = env.str("SMS_PROVIDER", default="46elks")
# Av = varje sändning går till 46elks med dryrun=yes: inget sms skickas och
# raden märks som provkörning. Slås på i produktionens .env, aldrig lokalt.
SMS_SEND_LIVE = env.bool("SMS_SEND_LIVE", default=False)
# Basen för leveransrapporternas adress. Tomt = SITE_BASE_URL. Måste vara
# https och nåbar från 46elks; annars begärs ingen rapport.
SMS_CALLBACK_BASE_URL = env.str("SMS_CALLBACK_BASE_URL", default="")
# Valfri spärr: leveransrapporter bara från de här adresserna (46elks listar
# sina på /docs/verify-callback-origin). Tomt = ingen IP-spärr; signaturen
# i adressen räcker för att stoppa förfalskningar.
SMS_DLR_ALLOWED_IPS = env.list("SMS_DLR_ALLOWED_IPS", default=[])
# Gränserna (apps/sms/ratelimit.py). Minutgränsen gäller per nyckel och per
# kund; byråns gräns gäller alla kunders sms tillsammans och håller dem under
# 46elks 100 i minuten för kontot, med plats kvar för Flamingos sms.
SMS_RATE_PER_SECOND = env.int("SMS_RATE_PER_SECOND", default=20)
SMS_RATE_PER_MINUTE = env.int("SMS_RATE_PER_MINUTE", default=60)
SMS_GLOBAL_PER_MINUTE = env.int("SMS_GLOBAL_PER_MINUTE", default=80)
SMS_DAILY_MAX_PER_KEY = env.int("SMS_DAILY_MAX_PER_KEY", default=5000)

# Flamingo 2.0: kontakter och utskick (apps/utskick/README.md, C.4). Allt
# är av tills byrån aktiverat kunden och slagit på brytarna på
# /manage/utskick/nodstopp/; en deploy startar aldrig någon sändning.


def _utskick_dev_key(purpose):
    """Lokalt och i testerna: en nyckel härledd ur SECRET_KEY. Produktionen
    startar inte utan de riktiga (production.py)."""
    import hashlib

    return hashlib.sha256(f"{purpose}:{SECRET_KEY}".encode()).hexdigest()


# Nyckeln för spärrlistan och samtyckena: HMAC av varje telefonnummer och
# e-postadress (keys.value_hash). Stabil för alltid: byts den hittar ingen
# spärr sin adress längre. Krävs i produktion (production.py), finns i
# lösenordshanteraren och i anteckningarna för databasens backup.
UTSKICK_HASH_KEY = env.str("UTSKICK_HASH_KEY", default="") or _utskick_dev_key("utskick-hash")
# Nyckeln som signerar avregistrerings-, val- och bekräftelselänkar,
# Reply-To och formulärens engångsvärden (E.2). Aldrig SECRET_KEY, så att
# ett byte av den inte bryter länkarna i redan skickade sms och mejl. Samma
# regler som nyckeln ovan.
UTSKICK_LINK_KEY = env.str("UTSKICK_LINK_KEY", default="") or _utskick_dev_key("utskick-link")
# Avsändaren för bekräftelsemejlen (dubbel opt-in); namnet är kundens
# display_name. Tomma rader i .env ger standardvärdena.
UTSKICK_DOI_FROM = env.str("UTSKICK_DOI_FROM", default="") or "bekrafta@utskick.adx.se"
# Tidsbudget per tick (utskick_tick, varje minut) och tickens minnestak i MB
# (RLIMIT_AS). Taket sätts från en uppmätt topp plus 30 % i S1-kontrollen.
UTSKICK_TICK_SECONDS = env.int("UTSKICK_TICK_SECONDS", default=50)
UTSKICK_TICK_MAX_MB = env.int("UTSKICK_TICK_MAX_MB", default=700)
# Av = ingen post alls från utskick: bekräftelsemejl och e-postkanalen nekas.
# Av med DEBUG skrivs mejlen som .eml-filer i PRIVATE_MEDIA_ROOT/utskick-mail/
# (aldrig i produktion). Slås på i produktionens .env när SES i eu-west-1
# har gett produktionsåtkomst.
UTSKICK_EMAIL_LIVE = env.bool("UTSKICK_EMAIL_LIVE", default=False)
# Rollen adx-utskick som antas från cloud.aws.base_session() (H.8). Tomt =
# ingen AWS; lokalt används ADX_AWS_PROFILE.
UTSKICK_AWS_ROLE_ARN = env.str("UTSKICK_AWS_ROLE_ARN", default="")
UTSKICK_AWS_EXTERNAL_ID = env.str("UTSKICK_AWS_EXTERNAL_ID", default="")
# All post från utskick går från SES i eu-west-1 (samma EU-region som
# inkommande post), skild från ADX egen post i eu-north-1.
UTSKICK_SES_REGION = env.str("UTSKICK_SES_REGION", default="") or "eu-west-1"
UTSKICK_ADX_MAIL_DOMAIN = env.str("UTSKICK_ADX_MAIL_DOMAIN", default="") or "utskick.adx.se"


def _host_list(name, default):
    """Kommaseparerade värdnamn i gemener; en tom rad ger standardvärdet."""
    hosts = [h.strip().lower() for h in env.list(name, default=[]) if h.strip()]
    return hosts or list(default)


# S2: sms-utskicken (README C.4). Värdarna som bara svarar på utskickens
# länkar (apps/utskick/links.py, LinkHostMiddleware): k.adx.se i sms,
# klick.adx.se i mejl. Lokalt k.localhost och klick.localhost
# (development.py). I produktion läggs de också sist i ALLOWED_HOSTS och
# CSRF_TRUSTED_ORIGINS (lankrapport läser den första värden).
UTSKICK_LINK_HOSTS = _host_list("UTSKICK_LINK_HOSTS", ["k.adx.se", "klick.adx.se"])
# Länkarnas bas: i sms skrivs den utan schema (k.adx.se/a8Kf2X).
UTSKICK_SMS_LINK_BASE = env.str("UTSKICK_SMS_LINK_BASE", default="") or "https://k.adx.se"
UTSKICK_EMAIL_LINK_BASE = env.str("UTSKICK_EMAIL_LINK_BASE", default="") or "https://klick.adx.se"
# Det delade svarsnumret hos 46elks (D4): svar och STOPP hamnar i Inkorgen
# hos kunden som senast skickade från det till numret.
UTSKICK_REPLY_NUMBER = env.str("UTSKICK_REPLY_NUMBER", default="") or "+46766860046"
# Hemligheten i adressen 46elks skickar inkommande sms till
# (/api/utskick/46elks/inkommande/<token>/), minst 32 tecken. Tomt = inkommande
# sms är av. I produktion vägrar adressen också när SMS_DLR_ALLOWED_IPS är tom.
UTSKICK_ELKS_INBOUND_TOKEN = env.str("UTSKICK_ELKS_INBOUND_TOKEN", default="")
# Utskickens del av minutgränserna i apps/sms: per kund (resten, 60 - 45,
# lämnas åt kundens API) och för hela byrån (80 - 60 åt API:t; Flamingos
# egna sms räknas också av, D.4).
UTSKICK_SMS_ACCOUNT_PER_MINUTE = env.int("UTSKICK_SMS_ACCOUNT_PER_MINUTE", default=45)
UTSKICK_SMS_GLOBAL_PER_MINUTE = env.int("UTSKICK_SMS_GLOBAL_PER_MINUTE", default=60)

# Testerna når aldrig nätet: bara loopback och unix-socklar (Postgres), och
# inga sms eller mejl skickas på riktigt även om .env säger det.
TEST_RUNNER = "config.test_runner.NoNetworkRunner"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
