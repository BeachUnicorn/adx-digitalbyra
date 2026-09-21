"""
Felrapporteringen till Sentry - och vad som ALDRIG får följa med dit.

adx.se har adresser som i sig är behörigheter: offertlänken
(/offert/<token>/ ger rätten att acceptera), AI-koden (/aiz/guide/?kod=) och
den delade statusnyckeln (headern X-ADX-Key, samma på alla sajter vi
driftar). Sentry tar med adress, query-sträng, headers och lokala variabler
i varje händelse, så utan den här filen hamnar de hos en tredje part.

Tre lager, eftersom Sentrys eget skydd bara täcker det första till hälften:

1. EventScrubber med våra headernamn. Den matchar på EXAKT namn i gemener
   med bindestreck - "x_adx_key" träffar inte, "X-ADX-Key" gör det.
2. scrub_event går igenom HELA händelsen och maskar på mönster: scrubbern
   tittar aldrig i url eller query_string, och en token kan lika gärna stå
   i ett loggmeddelande eller en lokal variabel.
3. Samma funktion på before_send OCH before_send_transaction. Spårningen
   (traces_sample_rate) skickar request-data för vart tionde anrop även när
   inget gått fel, och before_send körs inte på de händelserna.

Dessutom: inga personuppgifter (send_default_pii=False) och inga
formulärkroppar (max_request_body_size="never") - förfrågningar, ärenden
och offertaccepter innehåller namn, e-post och fritext från kunder.
"""

import json
import re
from pathlib import Path

FILTERED = "[Filtered]"

#: Headers som bär hemligheter, utöver Sentrys egen lista.
SECRET_HEADERS = ["X-ADX-Key", "X-ADX-Code"]

#: Formulärfält och variabelnamn som bär hemligheter, utöver Sentrys lista
#: (som redan har password, token, secret, session, csrf ...).
SECRET_NAMES = ["kod", "login_code", "adx_status_key", "status_key"]

_PATTERNS = [
    # Offertlänken: token i sökvägen.
    (re.compile(r"(/offert/)[A-Za-z0-9_-]{16,}"), r"\1" + FILTERED),
    # AI-koderna, var de än står.
    (re.compile(r"\bADX-[A-Z0-9]{4}-[A-Z0-9]{4}\b"), FILTERED),
    # Hemligheter i query-strängar: ?kod=..., &key=..., token=...
    (
        re.compile(r"(?i)((?:^|[?&\s\"'])(?:kod|key|token|secret|signature)=)[^&\s\"']+"),
        r"\1" + FILTERED,
    ),
]

#: Anrop som inte ska spåras alls: maskinanrop var femte minut som annars
#: äter kvoten, och statusanropet är just det som bär den delade nyckeln.
_UNTRACED_PREFIXES = ("/healthz", "/status/", "/static/", "/media/", "/favicon")


def scrub_text(value):
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def _walk(node):
    if isinstance(node, str):
        return scrub_text(node)
    if isinstance(node, dict):
        return {key: _walk(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_walk(value) for value in node]
    if isinstance(node, tuple):
        return tuple(_walk(value) for value in node)
    return node


def scrub_event(event, hint=None):
    """before_send och before_send_transaction. Får aldrig själv fälla en rapport."""
    try:
        return _walk(event)
    except Exception:  # noqa: BLE001 - hellre ingen rapport än en omaskad
        return None


def traces_sampler(sampling_context):
    environ = sampling_context.get("wsgi_environ") or {}
    scope = sampling_context.get("asgi_scope") or {}
    path = environ.get("PATH_INFO") or scope.get("path") or ""
    if path.startswith(_UNTRACED_PREFIXES):
        return 0.0
    return 0.1


def _release(base_dir):
    """Deployens revision ur release.json, så Sentry visar vilken deploy som införde felet."""
    try:
        rev = json.loads((Path(base_dir).parent / "release.json").read_text()).get("rev")
    except (OSError, ValueError, AttributeError):
        return None
    return f"adx@{rev}" if rev else None


def options(dsn, environment, base_dir):
    """Argumenten till sentry_sdk.init - en funktion, så testerna kör exakt samma."""
    from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber

    return {
        "dsn": dsn,
        "environment": environment,
        "release": _release(base_dir),
        "send_default_pii": False,
        "max_request_body_size": "never",
        "traces_sampler": traces_sampler,
        "event_scrubber": EventScrubber(
            denylist=DEFAULT_DENYLIST + SECRET_HEADERS + SECRET_NAMES, recursive=True
        ),
        "before_send": scrub_event,
        "before_send_transaction": scrub_event,
    }


def init(dsn, environment, base_dir):
    import sentry_sdk

    sentry_sdk.init(**options(dsn, environment, base_dir))
