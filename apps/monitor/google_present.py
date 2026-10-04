"""
Googles data i klartext: rader och tal som mallarna kan rita direkt.
Används av byråns detaljsida (manage_views.domain_google) och kundens
statussida (status_areas.py). Läser bara Check.data, anropar aldrig Google.
"""

from datetime import date, datetime

from django.utils import timezone
from django.utils.formats import date_format

from .google_checks import (
    FIELD_METRIC_ORDER,
    GBP_METRIC_LABELS,
    GOOD,
    METRIC_LABELS,
    NEEDS,
    OPEN_LABELS,
    POOR,
    RATING_LABELS,
    change_pct,
    crux_latest,
    format_metric,
    home_inspection,
    rating,
)

#: PSI:s fältkategorier i klartext.
FIELD_CATEGORY = {"FAST": GOOD, "AVERAGE": NEEDS, "SLOW": POOR}
PSI_CATEGORY_LABELS = (
    ("performance", "Prestanda"),
    ("accessibility", "Tillgänglighet"),
    ("best-practices", "Bästa praxis"),
    ("seo", "Sökmotoroptimering (SEO)"),
)
STRATEGIES = (("mobile", "Mobil"), ("desktop", "Dator"))
FORM_FACTORS = (("phone", "Telefon"), ("desktop", "Dator"))
NO_TRAFFIC = "För lite trafik för Googles mätning"
VERDICT_LABELS = {
    "PASS": "Indexerad",
    "PARTIAL": "Indexerad, med anmärkning",
    "NEUTRAL": "Inte indexerad",
    "FAIL": "Inte indexerad (fel)",
}
ROBOTS_LABELS = {"ALLOWED": "Tillåten", "DISALLOWED": "Blockerad i robots.txt"}
INDEXING_LABELS = {
    "INDEXING_ALLOWED": "Tillåten",
    "BLOCKED_BY_META_TAG": "Blockerad (noindex i sidan)",
    "BLOCKED_BY_HTTP_HEADER": "Blockerad (noindex i headern)",
    "BLOCKED_BY_ROBOTS_TXT": "Blockerad i robots.txt",
}


def nice_date(value):
    """ISO-datum eller tidpunkt som "4 okt 2026", annars ""."""
    text = str(value or "")
    if not text:
        return ""
    try:
        if "T" in text:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return date_format(timezone.localtime(moment), "j M Y")
        return date_format(date.fromisoformat(text[:10]), "j M Y")
    except ValueError:
        return text[:10]


def score_state(score):
    if not isinstance(score, int):
        return ""
    return GOOD if score >= 90 else NEEDS if score >= 50 else POOR


def psi_categories(data):
    """[(etikett, mobil, dator)] med poäng 0-100 eller None."""
    strategies = (data or {}).get("strategies") or {}
    rows = []
    for key, label in PSI_CATEGORY_LABELS:
        values = []
        for name, _ in STRATEGIES:
            s = strategies.get(name) or {}
            value = (s.get("categories") or {}).get(key)
            if value is None and key == "performance":
                value = s.get("score")
            values.append({"value": value, "state": score_state(value)})
        if any(v["value"] is not None for v in values):
            rows.append({"label": label, "values": values})
    return rows


def psi_lab(data):
    strategies = (data or {}).get("strategies") or {}
    rows = []
    for key, label in (
        ("lcp", "Största elementet (LCP)"),
        ("fcp", "Första innehållet (FCP)"),
        ("tbt", "Blockerad tid (TBT)"),
        ("cls", "Layoutskift (CLS)"),
        ("si", "Hastighetsindex"),
    ):
        values = [(strategies.get(name) or {}).get(key) or "-" for name, _ in STRATEGIES]
        if any(v != "-" for v in values):
            rows.append({"label": label, "values": values})
    return rows


def field_rows(field):
    """PSI:s fältdata (parse_field_data) som rader med betyg i klartext."""
    if not field:
        return []
    rows = []
    for metric in FIELD_METRIC_ORDER:
        item = (field.get("metrics") or {}).get(metric)
        if not item:
            continue
        state = FIELD_CATEGORY.get(item.get("category")) or rating(metric, item.get("p75"))
        rows.append(
            {
                "label": METRIC_LABELS[metric],
                "value": format_metric(metric, item.get("p75")),
                "state": state,
                "state_label": RATING_LABELS.get(state, ""),
            }
        )
    return rows


def psi_field(data):
    """Fältdatan per strategi: sidan och hela sajten (origin)."""
    strategies = (data or {}).get("strategies") or {}
    out = []
    for name, label in STRATEGIES:
        s = strategies.get(name) or {}
        page, origin = s.get("field"), s.get("origin_field")
        overall = (page or origin or {}).get("overall", "")
        out.append(
            {
                "label": label,
                "page": field_rows(page) if page and not page.get("origin_fallback") else [],
                "origin": field_rows(origin),
                "overall": FIELD_CATEGORY.get(overall, ""),
                "overall_label": RATING_LABELS.get(FIELD_CATEGORY.get(overall, ""), ""),
                "error": s.get("error", ""),
            }
        )
    return out


def crux_trends(data):
    """Per formfaktor: [{metric, label, values, latest, state}] eller no_data."""
    out = []
    for short, label in FORM_FACTORS:
        ff = ((data or {}).get("form_factors") or {}).get(short)
        if ff is None:
            continue
        item = {"label": label, "no_data": bool(ff.get("no_data")), "error": ff.get("error", "")}
        periods = ff.get("periods") or []
        item["first"] = nice_date(periods[0]) if periods else ""
        item["last"] = nice_date(periods[-1]) if periods else ""
        item["metrics"] = []
        for metric in ("lcp", "inp", "cls"):
            values = (ff.get("metrics") or {}).get(metric)
            if not values:
                continue
            latest, state = crux_latest(ff, metric)
            item["metrics"].append(
                {
                    "label": METRIC_LABELS[metric],
                    "values": [v for v in values if v is not None],
                    "latest": format_metric(metric, latest),
                    "state": state,
                    "state_label": RATING_LABELS.get(state, ""),
                }
            )
        out.append(item)
    return out


def crux_summary(data):
    """(betyg, text) för telefon, för kundens rad. None utan mätning."""
    ff = ((data or {}).get("form_factors") or {}).get("phone") or {}
    if not data or not data.get("ok") or not ff:
        return None
    if ff.get("no_data"):
        return {"state": "", "text": NO_TRAFFIC, "rows": []}
    rows, worst = [], GOOD
    order = [GOOD, NEEDS, POOR]
    for metric in ("lcp", "inp", "cls"):
        latest, state = crux_latest(ff, metric)
        if latest is None:
            continue
        rows.append((METRIC_LABELS[metric], format_metric(metric, latest), state))
        worst = max(worst, state, key=order.index)
    if not rows:
        return {"state": "", "text": NO_TRAFFIC, "rows": []}
    return {"state": worst, "text": RATING_LABELS[worst], "rows": rows}


def search_view(data):
    """Search Console i klartext för mallarna."""
    if not data:
        return None
    cur, prev = data.get("current") or {}, data.get("previous") or {}
    totals = []
    for key, label, fmt in (
        ("clicks", "Klick", "{}"),
        ("impressions", "Visningar", "{}"),
        ("ctr", "Klickfrekvens (CTR)", "{} %"),
        ("position", "Snittposition", "{}"),
    ):
        if key not in cur:
            continue
        c, p = cur.get(key), prev.get(key)
        change = change_pct(c, p) if key in ("clicks", "impressions") and c is not None else None
        totals.append(
            {
                "label": label,
                "current": fmt.format(str(c).replace(".", ",")) if c is not None else "-",
                "previous": fmt.format(str(p).replace(".", ",")) if p is not None else "-",
                "change": change,
            }
        )
    home = home_inspection(data)
    inspections = []
    for item in data.get("inspections") or []:
        inspections.append(
            {
                **item,
                "verdict_label": VERDICT_LABELS.get(item.get("verdict"), item.get("verdict", "-")),
                "robots_label": ROBOTS_LABELS.get(item.get("robots"), item.get("robots") or "-"),
                "indexing_label": INDEXING_LABELS.get(
                    item.get("indexing"), item.get("indexing") or "-"
                ),
                "crawl": nice_date(item.get("last_crawl")) or "-",
                "indexed": item.get("verdict") in ("PASS", "PARTIAL"),
            }
        )
    sitemaps = [
        {
            **s,
            "downloaded": nice_date(s.get("last_downloaded")) or "-",
            "submitted": nice_date(s.get("last_submitted")) or "-",
        }
        for s in data.get("sitemaps") or []
    ]
    window = data.get("window") or {}
    return {
        **data,
        "totals": totals,
        "clicks_change": change_pct(cur.get("clicks"), prev.get("clicks"))
        if cur.get("clicks") is not None
        else None,
        "spark": ((data.get("daily") or {}).get("clicks")) or [],
        "home": home,
        "home_indexed": bool(home and home.get("verdict") in ("PASS", "PARTIAL")),
        "inspections_view": inspections,
        "sitemaps_view": sitemaps,
        "period": f"{nice_date(window.get('start'))} till {nice_date(window.get('end'))}"
        if window
        else "",
    }


def gbp_view(data):
    if not data:
        return None
    metrics = []
    for key, label in GBP_METRIC_LABELS.items():
        m = (data.get("metrics") or {}).get(key)
        if not m:
            continue
        metrics.append(
            {
                "label": label,
                "current": m.get("current", 0),
                "previous": m.get("previous", 0),
                "change": change_pct(m.get("current", 0), m.get("previous", 0)),
            }
        )
    return {
        **data,
        "open_label": OPEN_LABELS.get(data.get("open_status"), "Okänt"),
        "metrics_view": metrics,
        "rating_text": (
            f"{str(data['rating']).replace('.', ',')} av 5 ({data.get('review_count', 0)} omdömen)"
            if data.get("rating") is not None
            else ""
        ),
    }
