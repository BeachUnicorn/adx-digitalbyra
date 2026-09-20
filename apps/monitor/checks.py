"""
Själva kontrollerna. Rena funktioner: värdnamn in, dict ut, aldrig ett
undantag ut - ett nätverksfel ÄR resultatet och sparas som ok=False.

SSRF-skyddet ärvs från Hemsidekollen (apps/tools/analyzer): domäner
läggs in av byrån, men kontrollerna får ändå aldrig hämta privata adresser.
"""

import json
import re
import socket
import ssl
import time
from datetime import UTC, datetime
from urllib.parse import quote
from urllib.request import Request, urlopen

from django.conf import settings

from apps.tools.analyzer import TIMEOUT, AnalysError, _assert_public, check_epost_doman

USER_AGENT = "ADX Monitor (+https://adx.se/)"


def _apex(host):
    return re.sub(r"^www\.", "", host.strip().lower())


def _get(url, headers=None, timeout=TIMEOUT, max_bytes=512 * 1024):
    """GET med SSRF-kontroll. Returnerar (status, headers, body, ms, slutlig url)."""
    from urllib.parse import urlsplit

    _assert_public(urlsplit(url).hostname)
    request = Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})  # noqa: S310
    start = time.monotonic()
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = response.read(max_bytes)
        ms = int((time.monotonic() - start) * 1000)
        return (
            response.status,
            {k.lower(): v for k, v in response.headers.items()},
            body,
            ms,
            response.url,
        )


# --- Snabbkontrollen -----------------------------------------------------------


def check_uptime(host):
    """Är sajten uppe, och hur snabbt svarar den?"""
    url = f"https://{host}/"
    try:
        status, headers, body, ms, final = _get(url, max_bytes=64 * 1024)
    except AnalysError as exc:
        return {"ok": False, "ms": None, "status": 0, "error": str(exc)[:300]}
    except Exception as exc:  # noqa: BLE001 - nätverksfel är resultatet
        return {"ok": False, "ms": None, "status": 0, "error": f"{type(exc).__name__}: {exc}"[:300]}
    ok = 200 <= status < 400
    return {
        "ok": ok,
        "ms": ms,
        "status": status,
        "final_url": final,
        "error": "" if ok else f"HTTP {status}",
    }


def fetch_status_endpoint(url):
    """Pluskundens /status/adx/ - vår egen plattform som rapporterar om sig själv."""
    key = getattr(settings, "ADX_STATUS_KEY", "")
    if not key:
        return {"ok": False, "error": "ADX_STATUS_KEY saknas i env."}
    try:
        status, _headers, body, ms, _final = _get(url, headers={"X-ADX-Key": key}, timeout=15)
        payload = json.loads(body.decode("utf-8", "replace"))
    except AnalysError as exc:
        return {"ok": False, "error": str(exc)[:300]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    if status != 200 or not isinstance(payload, dict):
        return {"ok": False, "error": f"HTTP {status}"}
    payload["ok"] = payload.get("db") == "ok"
    payload["ms"] = ms
    return payload


# --- Dygnskontrollerna -----------------------------------------------------------


def check_ssl(host):
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "days_left": None, "error": f"{type(exc).__name__}: {exc}"[:300]}
    expires = ssl.cert_time_to_seconds(cert["notAfter"])
    days = int((expires - time.time()) // 86400)
    issuer = dict(x[0] for x in cert.get("issuer", ())).get("organizationName", "")
    return {
        "ok": days > 7,
        "days_left": days,
        "not_after": datetime.fromtimestamp(expires, tz=UTC).date().isoformat(),
        "issuer": issuer,
        "warning": days <= 14,
    }


#: Toppdomäner utan RDAP i IANA:s bootstrap - Internetstiftelsen svarar på WHOIS.
WHOIS_SERVERS = {"se": "whois.iis.se", "nu": "whois.iis.se"}


def _whois(server, query):
    with socket.create_connection((server, 43), timeout=TIMEOUT) as sock:
        sock.sendall((query + "\r\n").encode())
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", "replace")


def _check_domain_whois(apex, server):
    """Internetstiftelsens WHOIS: 'registrar:', 'expires:', 'nserver:', 'status:'."""
    try:
        text = _whois(server, apex)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300], "apex": apex}
    fields = {}
    nameservers = []
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "nserver":
            nameservers.append(value.split()[0].lower())
        elif key in ("registrar", "expires", "created", "status", "domain"):
            fields.setdefault(key, value)
    expires = fields.get("expires", "")[:10]
    days_left = None
    if expires:
        try:
            days_left = (datetime.fromisoformat(expires).date() - datetime.now(UTC).date()).days
        except ValueError:
            days_left = None
    if "domain" not in fields:
        return {"ok": False, "error": "Domänen hittades inte i registret.", "apex": apex}
    return {
        "ok": days_left is None or days_left > 30,
        "apex": apex,
        "registrar": fields.get("registrar", ""),
        "expires": expires,
        "days_left": days_left,
        "created": fields.get("created", "")[:10],
        "nameservers": nameservers,
        # .se-registret låser inte överlåtelser som gTLD:er gör; status 'ok' är normalläget.
        "locked": False,
        "status": [fields.get("status", "")],
        "source": server,
    }


def check_domain(host):
    """Registrar, utgång, namnservrar och lås: RDAP via rdap.org, WHOIS för .se/.nu."""
    apex = _apex(host)
    tld = apex.rsplit(".", 1)[-1]
    if tld in WHOIS_SERVERS:
        return _check_domain_whois(apex, WHOIS_SERVERS[tld])
    try:
        status, _h, body, _ms, _f = _get(
            f"https://rdap.org/domain/{quote(apex)}",
            headers={"Accept": "application/rdap+json"},
            timeout=15,
        )
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300], "apex": apex}
    registrar = ""
    for entity in data.get("entities", []):
        if "registrar" in entity.get("roles", []):
            for item in entity.get("vcardArray", [None, []])[1] or []:
                if item and item[0] == "fn":
                    registrar = item[3]
    events = {e.get("eventAction"): e.get("eventDate") for e in data.get("events", [])}
    expires = (events.get("expiration") or "")[:10]
    days_left = None
    if expires:
        try:
            days_left = (datetime.fromisoformat(expires).date() - datetime.now(UTC).date()).days
        except ValueError:
            days_left = None
    nameservers = [ns.get("ldhName", "").lower() for ns in data.get("nameservers", [])]
    flags = data.get("status", [])
    locked = any("transfer prohibited" in f for f in flags)
    return {
        "ok": days_left is None or days_left > 30,
        "apex": apex,
        "registrar": registrar,
        "expires": expires,
        "days_left": days_left,
        "created": (events.get("registration") or "")[:10],
        "nameservers": nameservers,
        "locked": locked,
        "status": flags,
    }


def check_email(host):
    """MX/SPF/DMARC/DKIM (+ cert) ur Hemsidekollen. ok = ingen rad är 'fel'."""
    try:
        rows = check_epost_doman(host)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "rows": [], "error": f"{type(exc).__name__}: {exc}"[:300]}
    rows = [r for r in rows if r["titel"] != "HTTPS-certifikat"]
    return {"ok": all(r["status"] != "fel" for r in rows), "rows": rows}


SECURITY_HEADERS = (
    ("strict-transport-security", "HSTS", "Tvingar HTTPS i webbläsaren."),
    ("x-content-type-options", "X-Content-Type-Options", "Stoppar MIME-gissning."),
    ("x-frame-options", "X-Frame-Options", "Skyddar mot klickkapning (eller CSP frame-ancestors)."),
    ("referrer-policy", "Referrer-Policy", "Läcker inte adresser till andra sajter."),
    ("content-security-policy", "Content-Security-Policy", "Begränsar var skript får laddas från."),
)


def check_security(host):
    rows = []
    try:
        status, headers, _b, _ms, final = _get(f"https://{host}/", max_bytes=16 * 1024)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "rows": [], "error": f"{type(exc).__name__}: {exc}"[:300]}
    rows.append(
        {
            "status": "ok" if final.startswith("https://") else "fel",
            "titel": "HTTPS",
            "detalj": final,
        }
    )
    for header, title, why in SECURITY_HEADERS:
        present = header in headers
        if header == "x-frame-options" and not present:
            present = "frame-ancestors" in headers.get("content-security-policy", "")
        rows.append(
            {
                "status": "ok" if present else "varning",
                "titel": title,
                "detalj": why if present else f"Saknas. {why}",
            }
        )
    server = headers.get("server", "")
    if server and re.search(r"\d", server):
        rows.append(
            {"status": "varning", "titel": "Server-header", "detalj": f"Avslöjar version: {server}"}
        )
    score = sum(1 for r in rows if r["status"] == "ok")
    return {
        "ok": all(r["status"] != "fel" for r in rows),
        "rows": rows,
        "score": score,
        "of": len(rows),
    }


def check_performance(host):
    """Google PageSpeed Insights, mobil och desktop. Långsamt (20-40 s) - körs en gång per dygn."""
    out = {"ok": True, "strategies": {}}
    key = getattr(settings, "PAGESPEED_API_KEY", "")
    for strategy in ("mobile", "desktop"):
        url = (
            "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
            f"?url={quote(f'https://{host}/', safe='')}&strategy={strategy}&category=performance"
            + (f"&key={key}" if key else "")
        )
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
            with urlopen(request, timeout=60) as response:  # noqa: S310
                data = json.loads(response.read(4 * 1024 * 1024).decode("utf-8", "replace"))
            lh = data["lighthouseResult"]
            audits = lh.get("audits", {})
            out["strategies"][strategy] = {
                "score": int(round(lh["categories"]["performance"]["score"] * 100)),
                "lcp": audits.get("largest-contentful-paint", {}).get("displayValue", ""),
                "cls": audits.get("cumulative-layout-shift", {}).get("displayValue", ""),
                "tbt": audits.get("total-blocking-time", {}).get("displayValue", ""),
                "bytes_kb": int(audits.get("total-byte-weight", {}).get("numericValue", 0) // 1024),
            }
        except Exception as exc:  # noqa: BLE001
            out["strategies"][strategy] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
            out["ok"] = False
    return out


def fetch_sentry_errors(project_slug, days=30):
    """
    Antal fel i kundens Sentry-projekt, per dag, via byråns organisation.
    Kräver SENTRY_ORG_SLUG och SENTRY_API_TOKEN i env - inte kundens DSN.
    """
    org = getattr(settings, "SENTRY_ORG_SLUG", "")
    token = getattr(settings, "SENTRY_API_TOKEN", "")
    if not (org and token and project_slug):
        return {"ok": False, "error": "Sentry-koppling saknas (org, token eller projekt)."}
    url = (
        f"https://sentry.io/api/0/organizations/{quote(org)}/stats_v2/"
        f"?field=sum(quantity)&category=error&outcome=accepted&interval=1d"
        f"&statsPeriod={days}d&project={quote(project_slug)}"
    )
    try:
        request = Request(
            url, headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
        )  # noqa: S310
        with urlopen(request, timeout=20) as response:  # noqa: S310
            data = json.loads(response.read(1024 * 1024).decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    intervals = data.get("intervals", [])
    series = [0] * len(intervals)
    for group in data.get("groups", []):
        for i, value in enumerate(group.get("series", {}).get("sum(quantity)", [])):
            if i < len(series):
                series[i] += int(value or 0)
    return {
        "ok": True,
        "days": [d[:10] for d in intervals],
        "series": series,
        "total_30d": sum(series),
        "total_7d": sum(series[-7:]),
    }
