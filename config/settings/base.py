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
    "reversion",
    "apps.assistant",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Versionerar alla skrivande /manage/-requests (apps/assistant/revisions.py).
    "apps.assistant.revisions.ManageRevisionMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
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
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Database - one Postgres DB per site, supplied as a URL.
DATABASES = {
    "default": env.db("DATABASE_URL", default="sqlite:///" + str(BASE_DIR / "db.sqlite3")),
}

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
ASSISTANT_BEDROCK_MODEL = env("ASSISTANT_BEDROCK_MODEL", default="eu.anthropic.claude-opus-4-6-v1")
# Lokalt: namnet på en profil i ~/.aws. På servern tom - instansrollen gäller.
ASSISTANT_AWS_PROFILE = env("ASSISTANT_AWS_PROFILE", default="")
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

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Error monitoring. No-op when SENTRY_DSN is empty (e.g. in dev).
SENTRY_DSN = env("SENTRY_DSN", default="")
if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=SITE_SLUG,
        traces_sample_rate=0.1,
        send_default_pii=False,
    )

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
