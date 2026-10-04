"""
Googles data om en sajt, hämtad i dygnskontrollen (runner.run_daily):

- Chrome UX Report History API (fetch_crux): riktiga Chrome-besökares p75
  för LCP, INP och CLS, vecka för vecka (varje punkt är 28 dagar), för
  telefon och dator. Små sajter saknas hos Google (404): det är inget fel,
  bara "för lite trafik".
- Search Console (fetch_search): klick, visningar, CTR och position för de
  senaste 28 dagarna mot de 28 före, toppfrågor och toppsidor, sitemaps och
  indexstatus (URL Inspection) för startsidan och några viktiga sidor, högst
  MAX_INSPECTIONS_PER_DAY per sajt och dygn.
- Google Business Profile (fetch_gbp): profilens grunduppgifter, statistik
  (28 mot 28 dagar) och betyg.

Alla funktioner returnerar ett dict att spara i Check.data och kastar
aldrig: ett fel är resultatet. Fel i inställningen (ingen inloggning, saknad
behörighet, API:t avslaget, ingen godkänd åtkomst) sparas som
{"ok": False, "setup": True, "error": ..., "error_kind": ...}.

*_alerts(data) ger [(nyckel, text)] för byråns dygnslarm; runner.py ser till
att samma nyckel inte larmar varje dag.
"""

import logging
import re
from datetime import date, timedelta
from urllib.parse import quote, urlsplit

from django.utils import timezone

from apps.flamingo import google_ads

from .google_api import (
    ACCOUNTS_HOST,
    BUSINESS_INFO_HOST,
    KIND_NOT_FOUND,
    KIND_PERMISSION,
    KIND_QUOTA,
    KIND_QUOTA_ZERO,
    PERFORMANCE_HOST,
    REVIEWS_HOST,
    SEARCHCONSOLE_HOST,
    SETUP_KINDS,
    WEBMASTERS_HOST,
    GoogleApiError,
    crux_call,
    oauth_call,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gemensamt
# ---------------------------------------------------------------------------

#: Gränserna för Core Web Vitals (web.dev): bra till och med första talet,
#: dåligt över det andra, däremellan behöver förbättras.
THRESHOLDS = {
    "lcp": (2500, 4000),
    "inp": (200, 500),
    "cls": (0.1, 0.25),
    "fcp": (1800, 3000),
    "ttfb": (800, 1800),
}
FIELD_METRIC_ORDER = ("lcp", "inp", "cls", "fcp", "ttfb")
GOOD, NEEDS, POOR = "good", "needs", "poor"
RATING_LABELS = {GOOD: "Bra", NEEDS: "Behöver förbättras", POOR: "Dålig"}
METRIC_LABELS = {
    "lcp": "Största elementet (LCP)",
    "inp": "Svar på klick (INP)",
    "cls": "Layoutskift (CLS)",
    "fcp": "Första innehållet (FCP)",
    "ttfb": "Serverns svar (TTFB)",
}

#: En kraftig nedgång: mer än 40 % färre, och tillräckligt mycket att jämföra med.
DROP_SHARE = 0.4
SEARCH_MIN_CLICKS = 50
GBP_MIN_ACTIONS = 20


def rating(metric, value):
    if not isinstance(value, (int, float)) or metric not in THRESHOLDS:
        return ""
    good, poor = THRESHOLDS[metric]
    if value <= good:
        return GOOD
    return NEEDS if value <= poor else POOR


def format_metric(metric, value):
    """1234 ms blir "1,2 s", CLS 0.05 blir "0,05"."""
    if not isinstance(value, (int, float)):
        return "-"
    if metric == "cls":
        return f"{value:.2f}".replace(".", ",")
    if value >= 1000:
        return f"{value / 1000:.1f} s".replace(".", ",")
    return f"{int(value)} ms"


def _apex(host):
    return re.sub(r"^www\.", "", (host or "").strip().lower().rstrip("."))


def _host_of(url):
    try:
        return (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def is_drop(current, previous, minimum):
    """Mer än DROP_SHARE färre än förra perioden, med minst `minimum` att jämföra med."""
    if not isinstance(current, (int, float)) or not isinstance(previous, (int, float)):
        return False
    return previous >= minimum and current < previous * (1 - DROP_SHARE)


def change_pct(current, previous):
    if not previous:
        return None
    return round(100 * (current - previous) / previous)


def _setup_failure(error):
    return {
        "ok": False,
        "setup": error.is_setup,
        "error": error.message,
        "error_kind": error.kind,
    }


def _windows(today=None, lag_days=3):
    """(start, slut) för de senaste 28 dagarna och de 28 före. Googles data
    släpar ett par dygn, så fönstret slutar lag_days före i dag."""
    end = (today or timezone.localdate()) - timedelta(days=lag_days)
    start = end - timedelta(days=27)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=27)
    return start, end, prev_start, prev_end


# ---------------------------------------------------------------------------
# Chrome UX Report History API
# ---------------------------------------------------------------------------

CRUX_METRICS = {
    "largest_contentful_paint": "lcp",
    "interaction_to_next_paint": "inp",
    "cumulative_layout_shift": "cls",
}
CRUX_FORM_FACTORS = (("PHONE", "phone"), ("DESKTOP", "desktop"))
#: 25 veckor (Googles standard), ungefär ett halvår.
CRUX_PERIODS = 25


def _crux_date(value):
    try:
        return date(int(value["year"]), int(value["month"]), int(value["day"])).isoformat()
    except (KeyError, TypeError, ValueError):
        return ""


def _number(value):
    """p75 som tal: LCP och INP är heltal, CLS en sträng ("0.05"), saknad är null."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return int(number) if number.is_integer() and abs(number) >= 1 else round(number, 3)


def parse_crux(payload):
    """Svaret från queryHistoryRecord som {"periods": [slutdatum], "metrics":
    {"lcp": [p75 eller None, ...]}}. Äldst först, som hos Google."""
    record = payload.get("record") if isinstance(payload.get("record"), dict) else {}
    periods = [_crux_date(p.get("lastDate")) for p in record.get("collectionPeriods") or []]
    metrics = {}
    for name, short in CRUX_METRICS.items():
        raw = (record.get("metrics") or {}).get(name) or {}
        p75s = ((raw.get("percentilesTimeseries") or {}).get("p75s")) or []
        series = [_number(v) for v in p75s][: len(periods) or None]
        if any(v is not None for v in series):
            metrics[short] = series
    return {"periods": periods, "metrics": metrics}


def fetch_crux(origin):
    """Riktiga besökare för originet, telefon och dator. 404 är "för lite
    trafik", inget fel: {"no_data": True} för den formfaktorn."""
    result = {"ok": True, "origin": origin, "form_factors": {}}
    for form_factor, short in CRUX_FORM_FACTORS:
        body = {
            "origin": origin,
            "formFactor": form_factor,
            "metrics": list(CRUX_METRICS),
            "collectionPeriodCount": CRUX_PERIODS,
        }
        try:
            parsed = parse_crux(crux_call(body))
        except GoogleApiError as error:
            if error.kind == KIND_NOT_FOUND:
                result["form_factors"][short] = {"no_data": True}
                continue
            if error.kind in SETUP_KINDS:
                return {**_setup_failure(error), "origin": origin}
            result["form_factors"][short] = {"error": error.message}
            result["ok"] = False
            continue
        if not parsed["metrics"]:
            parsed = {"no_data": True}
        result["form_factors"][short] = parsed
    result["no_data"] = all(f.get("no_data") for f in result["form_factors"].values())
    return result


def crux_latest(series_data, metric):
    """Senaste p75 och dess betyg för en mätning, eller (None, "")."""
    values = (series_data.get("metrics") or {}).get(metric) or []
    for value in reversed(values):
        if value is not None:
            return value, rating(metric, value)
    return None, ""


def crux_alerts(data):
    """Riktiga besökares p75 gick från bra till sämre och har legat kvar där
    två perioder i rad. Nyckeln bär perioden: larmar en gång per övergång."""
    alerts = []
    if not data or not data.get("ok"):
        return alerts
    for short, label in (("phone", "telefon"), ("desktop", "dator")):
        series_data = (data.get("form_factors") or {}).get(short) or {}
        periods = series_data.get("periods") or []
        for metric, values in (series_data.get("metrics") or {}).items():
            if len(values) < 3:
                continue
            before, *last_two = values[-3:]
            ratings = [rating(metric, v) for v in last_two]
            if rating(metric, before) == GOOD and all(r in (NEEDS, POOR) for r in ratings):
                period = periods[-1] if periods else ""
                alerts.append(
                    (
                        f"crux:{short}:{metric}:{period}",
                        f"riktiga besökare ({label}): {METRIC_LABELS[metric]} har gått från bra "
                        f"till {RATING_LABELS[ratings[-1]].lower()} "
                        f"({format_metric(metric, last_two[-1])}) två veckor i rad",
                    )
                )
    return alerts


# ---------------------------------------------------------------------------
# Search Console
# ---------------------------------------------------------------------------

#: URL Inspection har en kvot per egendom (2 000 per dygn); vi håller oss långt under.
MAX_INSPECTIONS_PER_DAY = 10
#: Startsidan plus de mest klickade sidorna, per körning.
INSPECT_PER_RUN = 5
TOP_ROWS = 10


def _search(method, path, **kwargs):
    return oauth_call(
        "search", method, WEBMASTERS_HOST, path, scope=google_ads.WEBMASTERS_SCOPE, **kwargs
    )


def fetch_sites():
    """Egendomarna inloggningen når: [{"site_url", "permission"}]. Kastar GoogleApiError."""
    payload = _search("GET", "/webmasters/v3/sites")
    sites = []
    for entry in payload.get("siteEntry") or []:
        if isinstance(entry, dict) and entry.get("siteUrl"):
            sites.append(
                {
                    "site_url": str(entry["siteUrl"])[:300],
                    "permission": str(entry.get("permissionLevel") or ""),
                }
            )
    return sites


def property_candidates(host):
    apex = _apex(host)
    return [
        f"sc-domain:{apex}",
        f"https://{apex}/",
        f"https://www.{apex}/",
        f"http://{apex}/",
        f"http://www.{apex}/",
    ]


def match_property(host, sites, chosen=""):
    """(egendom, "chosen"|"auto") eller (None, ""). Egendomar där
    inloggningen inte är verifierad användare räknas inte."""
    usable = {s["site_url"]: s for s in sites if s.get("permission") != "siteUnverifiedUser"}
    if chosen:
        return (chosen, "chosen") if chosen in usable else (None, "")
    for candidate in property_candidates(host):
        if candidate in usable:
            return candidate, "auto"
    return None, ""


def _totals(rows):
    row = rows[0] if rows else {}
    return {
        "clicks": int(row.get("clicks") or 0),
        "impressions": int(row.get("impressions") or 0),
        "ctr": round(float(row.get("ctr") or 0) * 100, 1),
        "position": round(float(row.get("position") or 0), 1) if row else None,
    }


def _rows(rows):
    out = []
    for row in rows[:TOP_ROWS]:
        keys = row.get("keys") or [""]
        out.append(
            {
                "key": str(keys[0])[:300],
                "clicks": int(row.get("clicks") or 0),
                "impressions": int(row.get("impressions") or 0),
                "ctr": round(float(row.get("ctr") or 0) * 100, 1),
                "position": round(float(row.get("position") or 0), 1),
            }
        )
    return out


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_sitemaps(payload):
    out = []
    for item in (payload.get("sitemap") or [])[:50]:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "path": str(item.get("path") or "")[:300],
                "last_submitted": str(item.get("lastSubmitted") or "")[:30],
                "last_downloaded": str(item.get("lastDownloaded") or "")[:30],
                "is_pending": bool(item.get("isPending")),
                "errors": _int(item.get("errors")),
                "warnings": _int(item.get("warnings")),
                "type": str(item.get("type") or "")[:30],
            }
        )
    return out


INDEXED_VERDICTS = ("PASS", "PARTIAL")


def parse_inspection(url, payload):
    result = (payload.get("inspectionResult") or {}) if isinstance(payload, dict) else {}
    index = result.get("indexStatusResult") or {}
    return {
        "url": url,
        "verdict": str(index.get("verdict") or ""),
        "coverage": str(index.get("coverageState") or "")[:200],
        "robots": str(index.get("robotsTxtState") or ""),
        "indexing": str(index.get("indexingState") or ""),
        "fetch": str(index.get("pageFetchState") or ""),
        "last_crawl": str(index.get("lastCrawlTime") or "")[:30],
        "google_canonical": str(index.get("googleCanonical") or "")[:300],
        "link": str(result.get("inspectionResultLink") or "")[:500],
    }


def fetch_search(host, origin, *, sites, chosen="", inspect_budget=INSPECT_PER_RUN, today=None):
    """Search Console för sajten. sites är fetch_sites() (en gång per körning)."""
    prop, how = match_property(host, sites, chosen)
    if not prop:
        return {
            "ok": True,
            "access": False,
            "chosen": chosen,
            "candidates": property_candidates(host)[:3],
        }
    site_path = f"/webmasters/v3/sites/{quote(prop, safe='')}"
    start, end, prev_start, prev_end = _windows(today)
    data = {
        "ok": True,
        "access": True,
        "property": prop,
        "matched": how,
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "prev_start": prev_start.isoformat(),
            "prev_end": prev_end.isoformat(),
        },
        "errors": {},
    }

    def query(first, last, dimensions=None, limit=None):
        body = {"startDate": first.isoformat(), "endDate": last.isoformat(), "type": "web"}
        if dimensions:
            body["dimensions"] = dimensions
        if limit:
            body["rowLimit"] = limit
        return _search("POST", f"{site_path}/searchAnalytics/query", body=body).get("rows") or []

    try:
        data["current"] = _totals(query(start, end))
        data["previous"] = _totals(query(prev_start, prev_end))
        daily = query(prev_start, end, ["date"], 100)
        by_day = {str((r.get("keys") or [""])[0]): int(r.get("clicks") or 0) for r in daily}
        days = [start + timedelta(days=i) for i in range(28)]
        data["daily"] = {
            "dates": [d.isoformat() for d in days],
            "clicks": [by_day.get(d.isoformat(), 0) for d in days],
        }
        data["top_queries"] = _rows(query(start, end, ["query"], TOP_ROWS))
        data["top_pages"] = _rows(query(start, end, ["page"], TOP_ROWS))
    except GoogleApiError as error:
        if error.kind in SETUP_KINDS:
            return {**_setup_failure(error), "property": prop}
        if error.kind == KIND_PERMISSION:
            return {"ok": True, "access": False, "chosen": chosen, "property": prop}
        data["errors"]["analytics"] = error.message
        data["ok"] = False

    try:
        data["sitemaps"] = parse_sitemaps(_search("GET", f"{site_path}/sitemaps"))
    except GoogleApiError as error:
        data["errors"]["sitemaps"] = error.message

    urls = [origin.rstrip("/") + "/"]
    for page in data.get("top_pages") or []:
        url = page["key"]
        if _apex(_host_of(url)) == _apex(host) and url not in urls:
            urls.append(url)
    inspections = []
    for url in urls[: max(0, min(inspect_budget, MAX_INSPECTIONS_PER_DAY))]:
        try:
            payload = oauth_call(
                "search",
                "POST",
                SEARCHCONSOLE_HOST,
                "/v1/urlInspection/index:inspect",
                body={"inspectionUrl": url, "siteUrl": prop, "languageCode": "sv-SE"},
                scope=google_ads.WEBMASTERS_SCOPE,
            )
        except GoogleApiError as error:
            data["errors"]["inspection"] = error.message
            if error.kind in (KIND_QUOTA, KIND_QUOTA_ZERO) or error.kind in SETUP_KINDS:
                break
            continue
        inspections.append(parse_inspection(url, payload))
    data["inspections"] = inspections
    data["inspected"] = len(inspections)
    return data


def home_inspection(data):
    for item in (data or {}).get("inspections") or []:
        if urlsplit(item.get("url", "")).path in ("", "/"):
            return item
    return None


def search_alerts(data):
    alerts = []
    if not data or not data.get("access") or data.get("setup"):
        return alerts
    cur = (data.get("current") or {}).get("clicks")
    prev = (data.get("previous") or {}).get("clicks")
    if is_drop(cur, prev, SEARCH_MIN_CLICKS):
        alerts.append(
            (
                "search:drop",
                f"klicken från Google-sök har minskat med {-change_pct(cur, prev)} % "
                f"({prev} till {cur} på 28 dagar)",
            )
        )
    for sitemap in data.get("sitemaps") or []:
        if sitemap.get("errors"):
            alerts.append(
                (
                    f"search:sitemap:{sitemap['path']}",
                    f"sitemap {sitemap['path']} har {sitemap['errors']} fel i Search Console",
                )
            )
    home = home_inspection(data)
    if home and home.get("verdict") and home["verdict"] not in INDEXED_VERDICTS:
        alerts.append(
            (
                "search:home",
                "startsidan är inte indexerad av Google "
                f"({home.get('coverage') or home['verdict']})",
            )
        )
    return alerts


# ---------------------------------------------------------------------------
# Google Business Profile
# ---------------------------------------------------------------------------

_LOCATION = re.compile(r"locations/\d{1,30}")
_ACCOUNT = re.compile(r"accounts/\d{1,30}")
MAX_PAGES = 10
INFO_MASK = (
    "name,title,storefrontAddress,phoneNumbers,websiteUri,categories,regularHours,openInfo,metadata"
)
GBP_METRICS = {
    "BUSINESS_IMPRESSIONS_DESKTOP_MAPS": "impressions",
    "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH": "impressions",
    "BUSINESS_IMPRESSIONS_MOBILE_MAPS": "impressions",
    "BUSINESS_IMPRESSIONS_MOBILE_SEARCH": "impressions",
    "CALL_CLICKS": "calls",
    "WEBSITE_CLICKS": "website_clicks",
    "BUSINESS_DIRECTION_REQUESTS": "directions",
}
GBP_METRIC_LABELS = {
    "impressions": "Visningar i Google",
    "calls": "Samtal",
    "website_clicks": "Klick till webbplatsen",
    "directions": "Vägbeskrivningar",
}
DAYS = (
    ("MONDAY", "Måndag"),
    ("TUESDAY", "Tisdag"),
    ("WEDNESDAY", "Onsdag"),
    ("THURSDAY", "Torsdag"),
    ("FRIDAY", "Fredag"),
    ("SATURDAY", "Lördag"),
    ("SUNDAY", "Söndag"),
)
OPEN_LABELS = {
    "OPEN": "Öppen",
    "CLOSED_TEMPORARILY": "Tillfälligt stängd",
    "CLOSED_PERMANENTLY": "Permanent stängd",
}


def _gbp(method, host, path, **kwargs):
    return oauth_call("gbp", method, host, path, scope=google_ads.BUSINESS_SCOPE, **kwargs)


def _address(address):
    if not isinstance(address, dict):
        return ""
    lines = [str(x) for x in address.get("addressLines") or []]
    place = " ".join(x for x in (address.get("postalCode"), address.get("locality")) if x)
    return ", ".join(x for x in [*lines, place] if x)[:300]


def fetch_locations():
    """Alla platser inloggningen når, för att välja eller matcha:
    [{"account", "name", "title", "website", "address"}]. Kastar GoogleApiError."""
    accounts, token = [], ""
    for _ in range(MAX_PAGES):
        params = {"pageSize": 20, **({"pageToken": token} if token else {})}
        payload = _gbp("GET", ACCOUNTS_HOST, "/v1/accounts", params=params)
        accounts += [
            a["name"]
            for a in payload.get("accounts") or []
            if isinstance(a, dict) and _ACCOUNT.fullmatch(str(a.get("name") or ""))
        ]
        token = payload.get("nextPageToken") or ""
        if not token:
            break
    locations = []
    for account in accounts[:20]:
        token = ""
        for _ in range(MAX_PAGES):
            params = {
                "readMask": "name,title,websiteUri,storefrontAddress",
                "pageSize": 100,
                **({"pageToken": token} if token else {}),
            }
            payload = _gbp("GET", BUSINESS_INFO_HOST, f"/v1/{account}/locations", params=params)
            for loc in payload.get("locations") or []:
                name = str((loc or {}).get("name") or "")
                if not _LOCATION.fullmatch(name):
                    continue
                locations.append(
                    {
                        "account": account,
                        "name": name,
                        "title": str(loc.get("title") or "")[:200],
                        "website": str(loc.get("websiteUri") or "")[:300],
                        "address": _address(loc.get("storefrontAddress")),
                    }
                )
            token = payload.get("nextPageToken") or ""
            if not token:
                break
    return locations


def match_location(host, locations):
    """Platsen vars webbadress är sajten, eller None. Flera träffar: den första."""
    apex = _apex(host)
    for loc in locations:
        if loc.get("website") and _apex(_host_of(loc["website"])) == apex:
            return loc
    return None


def _time(value):
    value = value if isinstance(value, dict) else {}
    return f"{int(value.get('hours') or 0):02d}:{int(value.get('minutes') or 0):02d}"


def parse_hours(regular):
    periods = (regular or {}).get("periods") or [] if isinstance(regular, dict) else []
    by_day = {}
    for period in periods:
        if not isinstance(period, dict):
            continue
        day = period.get("openDay")
        by_day.setdefault(day, []).append(
            f"{_time(period.get('openTime'))}-{_time(period.get('closeTime'))}"
        )
    if not by_day:
        return []
    return [(label, ", ".join(by_day.get(key) or ["Stängt"])) for key, label in DAYS]


def parse_location(payload):
    categories = payload.get("categories") or {}
    names = []
    primary = (categories.get("primaryCategory") or {}).get("displayName")
    if primary:
        names.append(str(primary))
    names += [
        str(c.get("displayName"))
        for c in categories.get("additionalCategories") or []
        if isinstance(c, dict) and c.get("displayName")
    ]
    metadata = payload.get("metadata") or {}
    status = str((payload.get("openInfo") or {}).get("status") or "")
    verified = metadata.get("hasVoiceOfMerchant")
    return {
        "title": str(payload.get("title") or "")[:200],
        "address": _address(payload.get("storefrontAddress")),
        "phone": str((payload.get("phoneNumbers") or {}).get("primaryPhone") or "")[:50],
        "website": str(payload.get("websiteUri") or "")[:300],
        "categories": names[:10],
        "hours": parse_hours(payload.get("regularHours")),
        "open_status": status,
        "verified": verified if isinstance(verified, bool) else None,
        "maps_uri": str(metadata.get("mapsUri") or "")[:500],
    }


def _date_params(prefix, value):
    return {
        f"{prefix}.year": value.year,
        f"{prefix}.month": value.month,
        f"{prefix}.day": value.day,
    }


def parse_performance(payload, start, end, prev_start, prev_end):
    sums = {key: {"current": 0, "previous": 0} for key in GBP_METRIC_LABELS}
    for group in payload.get("multiDailyMetricTimeSeries") or []:
        for series in (group or {}).get("dailyMetricTimeSeries") or []:
            key = GBP_METRICS.get((series or {}).get("dailyMetric"))
            if not key:
                continue
            for item in ((series.get("timeSeries") or {}).get("datedValues")) or []:
                day = _crux_date((item or {}).get("date"))
                value = _int(item.get("value"))
                if start.isoformat() <= day <= end.isoformat():
                    sums[key]["current"] += value
                elif prev_start.isoformat() <= day <= prev_end.isoformat():
                    sums[key]["previous"] += value
    return sums


def fetch_gbp(host, *, location="", account="", locations=None, today=None):
    """Profilen för sajten. location ("locations/123") väljs av byrån; tom
    matchas på webbadress bland locations (fetch_locations(), en gång per körning)."""
    how = "chosen" if location else ""
    if not location:
        if locations is None:
            return {"ok": True, "linked": False}
        match = match_location(host, locations)
        if not match:
            return {"ok": True, "linked": False, "candidates": len(locations)}
        location, account, how = match["name"], match["account"], "auto"
    if not _LOCATION.fullmatch(location):
        return {"ok": False, "error": "Platsens id ska se ut som locations/123.", "linked": False}
    account = account if _ACCOUNT.fullmatch(account or "") else ""
    try:
        info = _gbp("GET", BUSINESS_INFO_HOST, f"/v1/{location}", params={"readMask": INFO_MASK})
    except GoogleApiError as error:
        if error.kind in SETUP_KINDS:
            return {**_setup_failure(error), "location": location}
        return {"ok": False, "error": error.message, "location": location, "linked": True}
    data = {
        "ok": True,
        "linked": True,
        "location": location,
        "account": account,
        "matched": how,
        **parse_location(info),
        "errors": {},
    }
    start, end, prev_start, prev_end = _windows(today)
    data["window"] = {"start": start.isoformat(), "end": end.isoformat()}
    try:
        params = {
            "dailyMetrics": sorted(GBP_METRICS),
            **_date_params("dailyRange.startDate", prev_start),
            **_date_params("dailyRange.endDate", end),
        }
        payload = _gbp(
            "GET",
            PERFORMANCE_HOST,
            f"/v1/{location}:fetchMultiDailyMetricsTimeSeries",
            params=params,
        )
        data["metrics"] = parse_performance(payload, start, end, prev_start, prev_end)
    except GoogleApiError as error:
        data["errors"]["metrics"] = error.message
    if account:
        try:
            payload = _gbp(
                "GET",
                REVIEWS_HOST,
                f"/v4/{account}/{location}/reviews",
                params={"pageSize": 1},
            )
            average = payload.get("averageRating")
            data["rating"] = round(float(average), 1) if average is not None else None
            data["review_count"] = _int(payload.get("totalReviewCount"))
        except (GoogleApiError, TypeError, ValueError) as error:
            data["errors"]["reviews"] = getattr(error, "message", "Betyget gick inte att läsa.")
    return data


def gbp_alerts(data, host):
    alerts = []
    if not data or not data.get("ok") or not data.get("linked"):
        return alerts
    website = data.get("website") or ""
    if not website:
        alerts.append(("gbp:website", "profilen i Google Business Profile saknar webbadress"))
    elif _apex(_host_of(website)) != _apex(host):
        alerts.append(
            (
                "gbp:website",
                f"profilen i Google Business Profile länkar till {website}, inte sajten",
            )
        )
    status = data.get("open_status")
    if status in ("CLOSED_TEMPORARILY", "CLOSED_PERMANENTLY"):
        alerts.append(
            ("gbp:closed", f"profilen i Google Business Profile är {OPEN_LABELS[status].lower()}")
        )
    if data.get("verified") is False:
        alerts.append(
            (
                "gbp:unverified",
                "profilen i Google Business Profile är inte verifierad (eller avstängd)",
            )
        )
    metrics = data.get("metrics") or {}
    for key in ("calls", "website_clicks"):
        m = metrics.get(key) or {}
        if is_drop(m.get("current"), m.get("previous"), GBP_MIN_ACTIONS):
            alerts.append(
                (
                    f"gbp:drop:{key}",
                    f"{GBP_METRIC_LABELS[key].lower()} från profilen har minskat med "
                    f"{-change_pct(m['current'], m['previous'])} % ({m['previous']} till "
                    f"{m['current']} på 28 dagar)",
                )
            )
    return alerts
