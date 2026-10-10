"""
Skriptet på kundens egen webbplats (README B.4, E.3, E.6, H.2, H.5), S4,
länk-byggaren. Reglerna som sidan Spårningsskript
(app_views/snippet.py) och besöksanropet klick.adx.se/v
(link_views.snippet_beacon) delar:

    DomainRefused(text)                 domänen går inte att använda; str(exc) är texten
    clean_domain(raw) -> str            "exempelror.example": gemener, IDNA, utan www.
    origin_host(request) -> str         värden i Origin (http eller https), annars ""
    origin_ok(site, host) -> bool       domänen själv eller en underdomän till den
    clean_path(raw) -> str              "/boka": sökvägen utan fråga, högst 80 tecken
    clean_goal(raw) -> str              adxFlamingo.track(namn): [a-z0-9_-], högst 40
    install_token(site) -> str          ?adx= för provet "Testa installationen"
    read_install_token(token) -> int | None   skriptets pk för en äkta provtoken
    install_url(site) -> str            https://<domän>/?adx=<provtoken>
    seen_text(site, now) -> str         "senast sett i går" eller ""
    status(site, now) -> (rubrik, ton)  ("Installerat", "ok") eller ("Inte sett än", "muted")
    summary(account, now) -> dict       raden under Inställningar för utskick (I.9)
    privacy_domains(account) -> list    domänerna den genererade integritetstexten nämner

Skriptet sätter inga kakor och sparar inget i webbläsaren (LEK 9 kap. 28 §).
Det gör något bara när adressen bär adx=<token>, och den token får bara
länkar till en domän vars skript redan har setts (last_seen_at, E.3) eller
provlänken på sidan Spårningsskript. Innan dess skickar skriptet inget, så
ingen besökare utan en länk från utskicken når klick.adx.se. Provlänken
(install_token) har samma form som ut, men en egen signatur: den är ingen
klickrad och ger aldrig ett spår på en landningssida.

Domänen bevisar inget ägande (kunden skriver den, och Origin går att
förfalska utanför en webbläsare). Därför gör den inga länkar fria från
byråns granskning (links.own_domains, S4-HANDOFF.md avvikelse 2), och
last_seen_at säger bara att skriptet har rapporterat.
"""

import re
from urllib.parse import unquote, urlsplit

from django.conf import settings
from django.utils import timezone

from . import links, tokens

#: Provtokenens syfte i signaturen (en annan än ut, se tokens._sig62).
INSTALL_PURPOSE = "adxsite"
#: Sökvägen i händelsen site_visit (Event.data["sida"]).
PATH_MAX = 80
#: Namnet i adxFlamingo.track(namn).
GOAL_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
#: En provtoken eller adx-token (tokens.adx_token) som skriptet läser: <id62>.<sig10>.
TOKEN_RE = re.compile(r"^[A-Za-z0-9]{1,12}\.[A-Za-z0-9]{10}$")
#: Skriptets nyckel (models.new_snippet_key: 16 tecken base64url).
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16}$")

EMPTY_TEXT = "Skriv domänen för din webbplats, till exempel exempelror.se."
INVALID_TEXT = "Skriv bara domänen, till exempel exempelror.se."
IP_TEXT = "Skriv domänen, inte en IP-adress."
SHARED_TEXT = "Det här är en adress som många delar. Skriv din egen domän."
ADX_TEXT = "Flamingo-sidorna på adx.se spåras redan. Skriv adressen till din egen webbplats."
LINK_HOST_TEXT = "Skriv adressen till din egen webbplats."
EXISTS_TEXT = "Domänen finns redan här."
MAX_TEXT = "Du kan ha högst {n} domäner. Ta bort en först."

INSTALLED_LABEL = "Installerat"
NOT_SEEN_LABEL = "Inte sett än"


class DomainRefused(ValueError):
    """Domänen går inte att använda; str(exc) är texten för kunden."""


def _adx_hosts():
    """Värdarna som är ADX egna (sajten och landningssidorna)."""
    from apps.flamingo.exports import landing_host

    hosts = {landing_host()}
    base = getattr(settings, "SITE_BASE_URL", "") or ""
    if base:
        hosts.add((urlsplit(base).hostname or "").lower().removeprefix("www."))
    hosts.add("adx.se")
    return {h for h in hosts if h}


def clean_domain(raw):
    """Domänen för ett skript, i gemener och IDNA, utan www. och utan
    schema, sökväg och port. DomainRefused med texten för kunden när den
    inte går: en IP-adress, ett namn utan punkt, ett offentligt suffix eller
    en delad värd (links.is_shared_host), länkvärdarna eller adx.se."""
    text = str(raw or "").strip()
    if not text:
        raise DomainRefused(EMPTY_TEXT)
    if len(text) > 300 or any(ch.isspace() for ch in text):
        raise DomainRefused(INVALID_TEXT)
    if "//" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        hostname = parts.hostname or ""
    except ValueError:
        raise DomainRefused(INVALID_TEXT) from None
    if parts.scheme.lower() not in ("http", "https") or "@" in parts.netloc:
        raise DomainRefused(INVALID_TEXT)
    if hostname.startswith("[") or ":" in hostname or links._is_ip(hostname):
        raise DomainRefused(IP_TEXT)
    host = links.normalize_host(hostname).removeprefix("www.")
    if not host or "." not in host or len(host) > 253:
        raise DomainRefused(INVALID_TEXT)
    if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in host.split(".")):
        raise DomainRefused(INVALID_TEXT)
    if links._is_ip(host):
        raise DomainRefused(IP_TEXT)
    if any(links._under(host, h) for h in links.link_hosts()):
        raise DomainRefused(LINK_HOST_TEXT)
    if any(links._under(host, h) for h in _adx_hosts()):
        raise DomainRefused(ADX_TEXT)
    if links.is_shared_host(host):
        raise DomainRefused(SHARED_TEXT)
    return host


def origin_host(request):
    """Värden i besöksanropets Origin (http eller https, utan port), eller
    "" när huvudet saknas, är "null" eller inte går att läsa."""
    raw = str(request.headers.get("Origin", "") or "").strip()
    if not raw or raw == "null" or len(raw) > 300:
        return ""
    try:
        parts = urlsplit(raw)
        hostname = parts.hostname or ""
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    return links.normalize_host(hostname)


def origin_ok(site, host):
    """Kommer anropet från skriptets domän eller en underdomän till den
    (www.exempelror.example för exempelror.example)?"""
    domain = links.normalize_host(site.domain)
    return bool(host and domain) and links._under(host, domain)


def clean_path(raw):
    """Sökvägen som händelsen site_visit sparar: utan fråga och fragment,
    avkodad, utan styrtecken, högst PATH_MAX tecken, alltid med / först."""
    text = str(raw or "")[:400].split("?", 1)[0].split("#", 1)[0]
    try:
        text = unquote(text, errors="replace")
    except (TypeError, ValueError):
        text = ""
    text = "".join(ch for ch in text if ch.isprintable() and not ch.isspace())
    if not text.startswith("/"):
        text = "/" + text
    return text[:PATH_MAX]


def clean_goal(raw):
    """Namnet i adxFlamingo.track(namn), i gemener, eller ""."""
    text = str(raw or "").strip().lower()
    return text if GOAL_RE.match(text) else ""


def install_token(site):
    """Provtoken för ?adx= (samma form som ut, en egen signatur)."""
    body = tokens._b62(site.pk)
    return f"{body}.{tokens._sig62(INSTALL_PURPOSE, body)}"


def read_install_token(token):
    """Skriptets pk för en äkta provtoken, annars None."""
    parts = str(token or "").split(".")
    if len(parts) != 2:
        return None
    body, sig = parts
    try:
        site_id = tokens._from_b62(body)
    except ValueError:
        return None
    if len(sig) != tokens._UT_SIG_LEN:
        return None
    if not tokens._same(sig, tokens._sig62(INSTALL_PURPOSE, body)):
        return None
    return site_id or None


def install_url(site):
    """Provlänken på sidan Spårningsskript: kundens startsida med adx."""
    return f"https://{site.domain}/?adx={install_token(site)}"


def seen_text(site, now=None):
    """ "senast sett i dag", "senast sett i går", "senast sett 2 okt", eller ""."""
    if site.last_seen_at is None:
        return ""
    from .app_views.contacts import day_text

    return f"senast sett {day_text(site.last_seen_at, now)}"


def status(site, now=None):
    """(rubrik, ton) för läget: Installerat (ok) eller Inte sett än (muted)."""
    if site.last_seen_at is not None:
        return INSTALLED_LABEL, "ok"
    return NOT_SEEN_LABEL, "muted"


def summary(account, now=None):
    """Raden Spårningsskript under Inställningar för utskick (I.9):
    {"sites": [{site, text, label, tone}], "text": "exempelror.example · senast
    sett i går", "label", "tone"} eller sites tom (inget skript än)."""
    from .models import SiteSnippet

    now = now or timezone.now()
    rows = []
    for site in SiteSnippet.objects.filter(account=account).order_by("domain", "pk"):
        label, tone = status(site, now)
        seen = seen_text(site, now)
        rows.append(
            {
                "site": site,
                "text": f"{site.domain} · {seen}" if seen else site.domain,
                "label": label,
                "tone": tone,
            }
        )
    if not rows:
        return {"sites": [], "text": "", "label": "", "tone": ""}
    installed = any(row["site"].last_seen_at for row in rows)
    return {
        "sites": rows,
        "text": ", ".join(row["text"] for row in rows),
        "label": INSTALLED_LABEL if installed else NOT_SEEN_LABEL,
        "tone": "ok" if installed else "muted",
    }


def privacy_domains(account):
    """Domänerna med skriptet, för den genererade integritetstexten (H.5).
    Kroken som public_views.privacy använder; byråns egen integritetspolicy
    (J S4, kryssa i checklistan) är ledarens."""
    from .models import SiteSnippet

    return list(
        SiteSnippet.objects.filter(account=account)
        .order_by("domain")
        .values_list("domain", flat=True)
    )
