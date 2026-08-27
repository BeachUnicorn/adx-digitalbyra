"""
Lightweight, dependency-free helpers for analytics.

Deliberately no external UA-parsing library - a small ruleset covers the
common OS/browser/device cases well enough for first-touch analytics. Refine
the tables as real traffic comes in.
"""

import ipaddress
import re
from urllib.parse import urlparse

from .models import DeviceType, TrafficSource

# ---------------------------------------------------------------------------
# Path normalisation
# ---------------------------------------------------------------------------

# Paths that embed a unique value would otherwise produce one row per visitor,
# burying real pages in the reports and bloating the path index. Only genuinely
# unbounded segments belong here - content slugs (/tjanster/<slug>/) are a
# bounded set and stay as they are.
_PATH_RULES = ((re.compile(r"^/forfragan/tack/[^/]+/?$"), "/forfragan/tack/:referens/"),)


def normalize_path(path: str) -> str:
    """
    Collapse high-cardinality URL segments to a stable placeholder.

    /forfragan/tack/SKV-2026-0412/ -> /forfragan/tack/:referens/

    Rewritten values keep the original prefix, so reports that match on
    "/forfragan/tack/" still pick up rows recorded before normalisation.
    """
    if not path:
        return ""
    for pattern, replacement in _PATH_RULES:
        if pattern.match(path):
            return replacement
    return path


# ---------------------------------------------------------------------------
# Bot detection
# ---------------------------------------------------------------------------

_BOT_SIGNATURES = (
    "bot",
    "crawl",
    "spider",
    "slurp",
    "facebookexternalhit",
    "embedly",
    "quora link preview",
    "showyoubot",
    "outbrain",
    "pinterest/0.",
    "developers.google.com",
    "lighthouse",
    "headlesschrome",
    "python-requests",
    "curl/",
    "wget/",
    "go-http-client",
    "axios/",
    "okhttp",
    "java/",
    "semrush",
    "ahrefs",
    "mj12bot",
    "dotbot",
    "applebot",
    "bingpreview",
    "uptimerobot",
    "pingdom",
    "monitis",
    "site24x7",
    "gtmetrix",
)


def is_bot(user_agent: str) -> bool:
    if not user_agent:
        return True  # no UA at all is almost always automated
    ua = user_agent.lower()
    return any(sig in ua for sig in _BOT_SIGNATURES)


# ---------------------------------------------------------------------------
# User-Agent parsing (OS / browser / device)
# ---------------------------------------------------------------------------

# Order matters - most specific first.
_OS_RULES = (
    ("Windows NT 10.0", "Windows 10/11"),
    ("Windows NT 6.3", "Windows 8.1"),
    ("Windows NT 6.1", "Windows 7"),
    ("Windows", "Windows"),
    ("iPhone", "iOS"),
    ("iPad", "iPadOS"),
    ("Mac OS X", "macOS"),
    ("Macintosh", "macOS"),
    ("Android", "Android"),
    ("CrOS", "ChromeOS"),
    ("Linux", "Linux"),
)

# Order matters - Edge/Chrome/Opera all contain "Safari", etc.
_BROWSER_RULES = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Opera", "Opera"),
    ("SamsungBrowser", "Samsung Internet"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("CriOS/", "Chrome"),
    ("Safari/", "Safari"),
)


def parse_user_agent(user_agent: str) -> dict:
    """Return {os, browser, device_type} from a UA string. Best-effort."""
    if not user_agent:
        return {"os": "", "browser": "", "device_type": DeviceType.UNKNOWN}

    if is_bot(user_agent):
        return {"os": "", "browser": "", "device_type": DeviceType.BOT}

    os_name = ""
    for needle, label in _OS_RULES:
        if needle in user_agent:
            os_name = label
            break

    browser = ""
    for needle, label in _BROWSER_RULES:
        if needle in user_agent:
            browser = label
            break

    device_type = _detect_device(user_agent)

    return {"os": os_name, "browser": browser, "device_type": device_type}


def _detect_device(user_agent: str) -> str:
    ua = user_agent.lower()
    is_tablet = "ipad" in ua or ("android" in ua and "mobile" not in ua)
    is_mobile = (
        "mobile" in ua or "iphone" in ua or "ipod" in ua or ("android" in ua and "mobile" in ua)
    )
    if is_tablet:
        return DeviceType.TABLET
    if is_mobile:
        return DeviceType.MOBILE
    return DeviceType.DESKTOP


# ---------------------------------------------------------------------------
# Referrer classification
# ---------------------------------------------------------------------------

_SEARCH_ENGINES = {
    "google": "google",
    "bing": "bing",
    "yahoo": "yahoo",
    "duckduckgo": "duckduckgo",
    "ecosia": "ecosia",
    "yandex": "yandex",
    "baidu": "baidu",
    "qwant": "qwant",
    "startpage": "startpage",
}

_SOCIAL = {
    "facebook": "facebook",
    "fb.com": "facebook",
    "fb.me": "facebook",
    "instagram": "instagram",
    "t.co": "twitter",
    "twitter": "twitter",
    "x.com": "twitter",
    "linkedin": "linkedin",
    "lnkd.in": "linkedin",
    "youtube": "youtube",
    "pinterest": "pinterest",
    "reddit": "reddit",
    "tiktok": "tiktok",
    "snapchat": "snapchat",
}


def classify_referrer(referrer: str, current_host: str = "") -> tuple[str, str]:
    """
    Classify a referrer URL into (source, detail).

    Returns one of TrafficSource values + a detail string (e.g. "google").
    """
    if not referrer:
        return TrafficSource.DIRECT, ""

    try:
        host = (urlparse(referrer).hostname or "").lower()
    except ValueError:
        return TrafficSource.DIRECT, ""

    if not host:
        return TrafficSource.DIRECT, ""

    # Internal navigation
    if current_host and host == current_host.lower():
        return TrafficSource.INTERNAL, host

    for needle, name in _SEARCH_ENGINES.items():
        if needle in host:
            return TrafficSource.ORGANIC, name

    for needle, name in _SOCIAL.items():
        if needle in host:
            return TrafficSource.SOCIAL, name

    return TrafficSource.REFERRAL, host


def classify_source(referrer, utm_source, utm_medium, current_host=""):
    """
    Resolve final (source, detail), giving UTM precedence over referrer.

    UTM medium maps: cpc/ppc/paid → PAID, email → EMAIL, social → SOCIAL.
    """
    medium = (utm_medium or "").lower()
    if medium:
        if medium in ("cpc", "ppc", "paid", "paidsearch"):
            return TrafficSource.PAID, utm_source or ""
        if medium == "email":
            return TrafficSource.EMAIL, utm_source or ""
        if medium == "social":
            return TrafficSource.SOCIAL, utm_source or ""
        if medium in ("referral",):
            return TrafficSource.REFERRAL, utm_source or ""
        if medium in ("organic",):
            return TrafficSource.ORGANIC, utm_source or ""
    if utm_source:
        return TrafficSource.REFERRAL, utm_source

    return classify_referrer(referrer, current_host)


# ---------------------------------------------------------------------------
# IP handling
# ---------------------------------------------------------------------------


def get_client_ip(request) -> str:
    """Extract the client IP, honouring X-Forwarded-For (first hop)."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def anonymize_ip(ip: str) -> str | None:
    """
    Mask the host portion: last octet (IPv4) or last 80 bits (IPv6).

    Returns None if the IP can't be parsed.
    """
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None

    if addr.version == 4:
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
        return str(net.network_address)
    net = ipaddress.ip_network(f"{ip}/48", strict=False)
    return str(net.network_address)
