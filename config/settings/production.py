"""
Production settings.

Inherits base and turns on the security knobs that should always be on
behind nginx + Let's Encrypt. DEBUG is forced off regardless of .env.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False

# Utskick (apps/utskick/README.md, H.7): spärrlistans och länkarnas nycklar
# härleds aldrig ur SECRET_KEY i produktion. Utan dem startar sajten inte,
# hellre än att en process räknar andra hashar än den som skrev spärrarna.
_missing_utskick_keys = [
    name for name in ("UTSKICK_HASH_KEY", "UTSKICK_LINK_KEY") if not env.str(name, default="")
]
if _missing_utskick_keys:
    raise ImproperlyConfigured(
        "Saknas i .env: "
        + ", ".join(_missing_utskick_keys)
        + '. Skapa dem med python -c "import secrets; print(secrets.token_urlsafe(48))", '
        "spara kopior i lösenordshanteraren och starta om (apps/utskick/README.md, J S1)."
    )

# Must be set explicitly per site; no wildcard fallback in production.
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# nginx terminates TLS and sets this header; trust it so Django knows it's HTTPS.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

SECURE_HSTS_SECONDS = 31536000  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
