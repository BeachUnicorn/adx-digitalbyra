"""
46elks-klienten för SMS-API:t: bara urllib, en fast adress och tidsgränser.

Kontrollerat mot https://46elks.com/docs/send-sms, /docs/sms-delivery-reports
och 46elks OpenAPI-specifikation, och med dryrun mot kontot 2026-10-03:

- POST https://api.46elks.com/a1/sms med basic auth och formulärfälten
  from, to, message, och valfria dryrun=yes och whendelivered=<url>.
- Svaret är JSON: id, status ("created"), from, to, message, created,
  direction, parts och cost i tiotusendels krona (5200 = 52 öre). Med
  dryrun kommer estimated_cost i stället för cost, och inget id.
- Fel kommer som HTTP 403 (eller annan kod) med en rad text, till exempel
  "Alphanumeric numbers may not start with a digit" eller att landet är
  spärrat ("disallowed by default").
- Provkörningen prövar inte numret ("+4612" godtas): det gör numbers.py.

Uppgifterna (ELKS_API_USERNAME och ELKS_API_PASSWORD, samma som Flamingos
sms) skrivs aldrig till loggen, och felmeddelandena härifrån innehåller
bara 46elks egen text.

Ingenting skickas på riktigt utan SMS_SEND_LIVE=True: då får varje anrop
dryrun=yes, och sms:et sparas som en provkörning (test_mode).

Ett fel är antingen säkert eller oklart (ElksError.ambiguous). Säkert:
46elks svarade 4xx, adressen gick inte att slå upp eller anslutningen
nekades; då gick inget sms iväg. Oklart: tidsgränsen löpte ut, förbindelsen
bröts, 46elks svarade 5xx eller med något som inte var JSON. Då kan 46elks
ha tagit emot och skickat sms:et, och service.py får aldrig skicka det igen.

46elks 429 (för många sms i minuten för byråns konto) är ett säkert fel med
throttled=True: inget skickades, och service.py svarar rate_limited (429)
i stället för provider_error, utan larm (apps/utskick/README.md, C.1).

list_messages(since) läser 46elks sms-historik (GET /a1/sms, nyast först,
sida för sida med next) för utskickens avstämning av inkommande sms till
svarsnumret (apps/utskick README G.1 punkt 4). Sidans parameter är
LIST_PAGE_PARAM; slingan stannar när en sida bara har äldre eller redan
sedda sms, så ett fel där ger högst en sida, aldrig en evig slinga. Med
deadline (time.monotonic()) stannar den också när tiden är slut, och varje
anrop får högst den tid som är kvar; svaret säger då complete=False.
"""

import base64
import http.client
import json
import logging
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings

logger = logging.getLogger(__name__)

API_URL = "https://api.46elks.com/a1/sms"
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 64 * 1024
#: Så mycket av ett felsvar från 46elks som sparas och visas.
ERROR_TEXT_MAX = 200


class ElksError(Exception):
    """46elks svarade inte, svarade med fel eller med något oväntat.

    ambiguous: anropet kan ha nått fram och sms:et kan ha skickats.
    throttled: 46elks svarade 429; inget skickades, försök igen om en minut."""

    def __init__(self, message, status=None, ambiguous=False, throttled=False):
        super().__init__(message)
        self.status = status
        self.ambiguous = ambiguous
        self.throttled = throttled


#: Fel som uppstår innan något nått 46elks: inget kan ha skickats.
_NEVER_SENT = (socket.gaierror, ConnectionRefusedError, ssl.SSLCertVerificationError)


@dataclass(frozen=True)
class ElksResult:
    id: str
    status: str
    #: Tiotusendels krona: cost, eller estimated_cost vid provkörning.
    cost: int
    parts: int
    dryrun: bool


def is_configured():
    """46elks är valt och uppgifterna finns."""
    return (
        getattr(settings, "SMS_PROVIDER", "46elks") == "46elks"
        and bool(getattr(settings, "ELKS_API_USERNAME", ""))
        and bool(getattr(settings, "ELKS_API_PASSWORD", ""))
    )


def is_live():
    """Skickas sms på riktigt? Annars är varje sändning en provkörning."""
    return bool(getattr(settings, "SMS_SEND_LIVE", False))


def _auth_header():
    credentials = f"{settings.ELKS_API_USERNAME}:{settings.ELKS_API_PASSWORD}"
    return "Basic " + base64.b64encode(credentials.encode()).decode()


def _post(fields):
    """Ett anrop. Returnerar svaret som dict, eller kastar ElksError."""
    if not is_configured():
        raise ElksError("SMS-leverantören är inte inkopplad.")
    request = Request(  # noqa: S310 - fast https-adress, inget från användaren
        API_URL,
        data=urlencode(fields).encode(),
        method="POST",
        headers={
            "Authorization": _auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            raw = response.read(MAX_RESPONSE_BYTES)
    except HTTPError as exc:
        try:
            text = exc.read(1024).decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001 - felsvaret är bara en förklaring
            text = ""
        text = " ".join(text.split())[:ERROR_TEXT_MAX]
        # 4xx: 46elks sade nej. 5xx: något gick fel hos 46elks, kanske efter
        # att sms:et skickats.
        raise ElksError(
            f"46elks svarade {exc.code}: {text or 'inget svar'}",
            exc.code,
            ambiguous=exc.code >= 500,
            throttled=exc.code == 429,
        ) from None
    except URLError as exc:
        # urlopen slår in fel under anslutningen och själva sändningen i
        # URLError. Uppslagning och nekad anslutning: inget kom fram. Annat
        # (tidsgräns, bruten förbindelse) kan ha kommit fram.
        reason = exc.reason
        raise ElksError(
            f"46elks gick inte att nå ({type(reason).__name__}).",
            ambiguous=not isinstance(reason, _NEVER_SENT),
        ) from None
    except TimeoutError:
        raise ElksError("46elks svarade inte i tid.", ambiguous=True) from None
    except (OSError, http.client.HTTPException) as exc:
        # Under svaret: anropet var redan skickat.
        raise ElksError(
            f"46elks svar bröts ({type(exc).__name__}).",
            ambiguous=not isinstance(exc, _NEVER_SENT),
        ) from None
    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        raise ElksError("46elks svarade med något som inte är JSON.", ambiguous=True) from None
    if not isinstance(payload, dict):
        raise ElksError("46elks svarade med något oväntat.", ambiguous=True)
    return payload


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _result(payload, dryrun):
    status = str(payload.get("status", ""))
    if status == "failed":
        raise ElksError("46elks svarade failed.")
    cost = payload.get("estimated_cost") if dryrun else payload.get("cost")
    if cost is None:
        cost = payload.get("cost", payload.get("estimated_cost"))
    return ElksResult(
        id=str(payload.get("id", "") or "")[:64],
        status=status,
        cost=max(_int(cost), 0),
        parts=max(_int(payload.get("parts"), 0), 0),
        dryrun=dryrun,
    )


def estimate(sender, to, body):
    """Provkörning: 46elks pris och delar för sms:et. Inget skickas."""
    try:
        payload = _post({"from": sender, "to": to, "message": body, "dryrun": "yes"})
        return _result(payload, dryrun=True)
    except ElksError as exc:
        exc.ambiguous = False  # en provkörning skickar aldrig något
        raise


def send(sender, to, body, whendelivered=""):
    """Skicka sms:et, eller provkör det om SMS_SEND_LIVE är av. Kastar
    ElksError; anroparen (service.py) släpper reservationen vid ett säkert
    fel och håller den vid ett oklart (ambiguous)."""
    fields = {"from": sender, "to": to, "message": body}
    if whendelivered:
        fields["whendelivered"] = whendelivered
    dryrun = not is_live()
    if dryrun:
        fields["dryrun"] = "yes"
    try:
        payload = _post(fields)
        result = _result(payload, dryrun=dryrun)
    except ElksError as exc:
        if dryrun:
            exc.ambiguous = False
        raise
    if not dryrun and not result.id:
        # 46elks svarade ja men utan id: sms:et kan mycket väl ha skickats.
        raise ElksError("46elks svarade utan id.", ambiguous=True)
    return result


# ---------------------------------------------------------------- historiken

#: Sms per sida och högsta antal sidor i en avstämning (2 000 sms).
LIST_PAGE_SIZE = 100
LIST_MAX_PAGES = 20
#: Parametern som tar svarets next för nästa (äldre) sida. 46elks
#: dokumentation (/docs/sms-history): start = "hämta sms före den här tiden".
#: Kontrolleras mot dokumentationen i S2-checklistan (README J S2 steg 6).
LIST_PAGE_PARAM = "start"


def _parse_time(value):
    """46elks tider (UTC, ofta utan tidszon) som medvetna datetime, eller None."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _get(params, timeout=TIMEOUT_SECONDS):
    """GET /a1/sms med parametrarna. dict, eller ElksError (aldrig oklart:
    en läsning skickar inget)."""
    if not is_configured():
        raise ElksError("SMS-leverantören är inte inkopplad.")
    request = Request(  # noqa: S310 - fast https-adress, inget från användaren
        f"{API_URL}?{urlencode(params)}",
        method="GET",
        headers={"Authorization": _auth_header(), "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read(4 * MAX_RESPONSE_BYTES)
    except HTTPError as exc:
        code = exc.code
        raise ElksError(f"46elks svarade {code}.", code, throttled=code == 429) from None
    except (URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise ElksError(f"46elks gick inte att nå ({type(exc).__name__}).") from None
    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        raise ElksError("46elks svarade med något som inte är JSON.") from None
    if not isinstance(payload, dict):
        raise ElksError("46elks svarade med något oväntat.")
    return payload


class Messages(list):
    """list_messages svar: en vanlig lista, och complete=False när läsningen
    stannade för att tiden (deadline) tog slut innan since nåddes."""

    complete = True


def list_messages(since, *, direction="incoming", to=None, max_pages=LIST_MAX_PAGES, deadline=None):
    """46elks sms från since till nu, nyast först, som
    [{"id", "from", "to", "message", "created", "direction"}] (created som
    datetime). Filtrerar på direction och to (svarsnumret). Kastar ElksError.
    deadline (time.monotonic()): inga fler sidor efter den, och varje anrop
    får högst tiden som är kvar (svaret har då complete=False)."""
    found = Messages()
    seen = set()
    cursor = None
    for _ in range(max(1, int(max_pages))):
        timeout = TIMEOUT_SECONDS
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0.5:
                found.complete = False
                break
            timeout = min(TIMEOUT_SECONDS, max(1.0, left))
        params = {"limit": LIST_PAGE_SIZE}
        if cursor:
            params[LIST_PAGE_PARAM] = cursor
        payload = _get(params, timeout=timeout)
        rows = payload.get("data") or []
        fresh = False
        older = False
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            created = _parse_time(row.get("created"))
            if created is not None and created < since:
                older = True
                continue
            sms_id = str(row.get("id") or "")[:120]
            if not sms_id or sms_id in seen:
                continue
            seen.add(sms_id)
            fresh = True
            if direction and row.get("direction") != direction:
                continue
            if to and row.get("to") != to:
                continue
            found.append(
                {
                    "id": sms_id,
                    "from": str(row.get("from") or ""),
                    "to": str(row.get("to") or ""),
                    "message": str(row.get("message") or ""),
                    "created": created,
                    "direction": str(row.get("direction") or ""),
                }
            )
        cursor = payload.get("next")
        if older or not fresh or not cursor:
            break
    return found
