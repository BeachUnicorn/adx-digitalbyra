"""Local development settings."""

from .base import *  # noqa: F401,F403
from .base import _host_list, env

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Utskickens länkvärdar lokalt (apps/utskick/README.md, C.4). *.localhost
# pekar på den egna datorn i webbläsarna; servern lyssnar på 8770
# (.claude/launch.json).
UTSKICK_LINK_HOSTS = _host_list("UTSKICK_LINK_HOSTS", ["k.localhost", "klick.localhost"])
UTSKICK_SMS_LINK_BASE = env.str("UTSKICK_SMS_LINK_BASE", default="") or "http://k.localhost:8770"
UTSKICK_EMAIL_LINK_BASE = (
    env.str("UTSKICK_EMAIL_LINK_BASE", default="") or "http://klick.localhost:8770"
)
