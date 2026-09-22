"""
Statussidans innehåll i kundens språk (templates/portal/status.html).

Varje kontroll blir EN rad: prick, namn, en mening och ett nyckeltal, med
detaljerna bakom ett klick. Kontroller som är påslagna men saknar mätning
blir inga tomma kort - de räknas upp på en rad längst ned. Interna fel
(saknad nyckel, timeout mot statusendpointet) visas aldrig för kunden:
det är byråns sak, och kunden kan inte göra något åt dem.
"""

from datetime import date, timedelta

from django.utils import timezone
from django.utils.formats import date_format

OK, WARN, BAD = "ok", "warn", "bad"


def _nice_date(iso):
    try:
        return date_format(date.fromisoformat(str(iso)[:10]), "j M Y")
    except ValueError:
        return str(iso)


def _rows_status(rows):
    statuses = {r.get("status") for r in rows}
    if "fel" in statuses:
        return BAD
    if "varning" in statuses:
        return WARN
    return OK


def _area(key, title, status, headline, value="", details=None, note=""):
    return {
        "key": key,
        "title": title,
        "status": status,
        "headline": headline,
        "value": value,
        "details": details or [],
        "note": note,
    }


def _ssl(check):
    if not check:
        return None
    data = check.data or {}
    if not check.ok:
        return _area("ssl", "HTTPS-certifikat", BAD, "Certifikatet kunde inte kontrolleras.")
    days = data.get("days_left")
    if days is None:
        return None
    status = BAD if days < 7 else WARN if days < 21 else OK
    return _area(
        "ssl",
        "HTTPS-certifikat",
        status,
        f"Giltigt till {_nice_date(data.get('not_after'))}"
        + (f", utfärdat av {data['issuer']}" if data.get("issuer") else ""),
        f"{days} dagar",
        [("Giltigt till", _nice_date(data.get("not_after")), None)]
        + ([("Utfärdare", data["issuer"], None)] if data.get("issuer") else []),
        "Förnyas automatiskt. Vi larmas om det inte sker.",
    )


def _domain(check):
    if not check or not check.ok or (check.data or {}).get("error"):
        return None
    data = check.data
    days = data.get("days_left")
    status = WARN if days is not None and days < 30 else OK
    headline = "Registrerad"
    if data.get("expires"):
        headline = f"Förnyas senast {_nice_date(data['expires'])}"
    if data.get("registrar"):
        headline += f" hos {data['registrar']}"
    details = [("Domän", data.get("apex", ""), None)]
    if data.get("registrar"):
        details.append(("Registrar", data["registrar"], None))
    if data.get("expires"):
        details.append(("Förnyas senast", _nice_date(data["expires"]), None))
    if data.get("nameservers"):
        details.append(("Namnservrar", ", ".join(data["nameservers"]), None))
    details.append(("Överlåtelselås", "På" if data.get("locked") else "Av", None))
    return _area(
        "domain",
        "Domän",
        status,
        headline,
        f"{days} dagar" if days is not None else "",
        details,
    )


def _rows_area(key, title, check, good_text, count_text):
    if not check:
        return None
    rows = (check.data or {}).get("rows") or []
    if not rows:
        return None
    status = _rows_status(rows)
    fine = sum(1 for r in rows if r.get("status") == "ok")
    headline = good_text if status == OK else count_text(len(rows) - fine)
    return _area(
        key,
        title,
        status,
        headline,
        f"{fine} av {len(rows)}",
        [(r.get("titel", ""), r.get("detalj", ""), r.get("status")) for r in rows],
    )


def _email(check):
    return _rows_area(
        "email",
        "E-post",
        check,
        "Mejl tas emot, och avsändaren går att verifiera (SPF, DKIM, DMARC).",
        lambda n: f"{n} inställning{'ar' if n != 1 else ''} att se över.",
    )


def _security(check):
    return _rows_area(
        "security",
        "Säkerhet",
        check,
        "Alla skydd vi kontrollerar är på plats.",
        lambda n: f"{n} skydd saknas eller är ofullständig{'a' if n != 1 else 't'}.",
    )


def _performance(check):
    if not check:
        return None
    strategies = (check.data or {}).get("strategies") or {}
    scores = {name: (s or {}).get("score") for name, s in strategies.items()}
    if not any(v is not None for v in scores.values()):
        return None
    mobile, desktop = scores.get("mobile"), scores.get("desktop")
    worst = min(v for v in scores.values() if v is not None)
    status = BAD if worst < 50 else WARN if worst < 90 else OK
    value = " · ".join(
        f"{label} {v}" for label, v in (("Mobil", mobile), ("Dator", desktop)) if v is not None
    )
    details = []
    for name, label in (("mobile", "Mobil"), ("desktop", "Dator")):
        s = strategies.get(name) or {}
        if s.get("score") is not None:
            extra = f", största element {s['lcp']}" if s.get("lcp") else ""
            details.append((label, f"{s['score']} av 100{extra}", None))
    return _area(
        "performance",
        "Prestanda",
        status,
        "Googles mätning av hur snabbt sidan laddar.",
        value,
        details,
        f"Google PageSpeed, mätt {date_format(timezone.localtime(check.checked_at), 'j M')}.",
    )


def _visits(snapshot):
    visits = (snapshot.data or {}).get("visits") if snapshot else None
    if not visits:
        return None
    details = [(p["path"], f"{p['n']} visningar", None) for p in visits.get("top_pages_7d", [])]
    return _area(
        "visits",
        "Besök",
        OK,
        f"{visits.get('sessions_7d', 0)} besök senaste veckan, "
        f"{visits.get('pageviews_7d', 0)} sidvisningar.",
        f"{visits.get('sessions_30d', 0)} på 30 dagar",
        details,
        "Mest besökta sidorna senaste veckan." if details else "",
    )


def _server(snapshot):
    if not snapshot:
        return None
    data = snapshot.data or {}
    server = data.get("server") or {}
    disk = (server.get("disk") or {}).get("used_pct")
    db_ok = data.get("db") == "ok"
    status = BAD if not db_ok else WARN if isinstance(disk, (int, float)) and disk >= 85 else OK
    headline = "Webbserver och databas svarar." if db_ok else "Databasen svarar inte."
    backup = (data.get("backup") or {}).get("latest_at")
    if backup:
        headline += f" Senaste backup {backup[:16].replace('T', ' ')}."
    details = [
        ("Webbserver", f"Svarar ({snapshot.ms} ms)" if snapshot.ms else "Svarar", None),
        ("Databas", "Svarar" if db_ok else "Fel", None if db_ok else "fel"),
    ]
    if server.get("mem"):
        details.append(
            (
                "Minne",
                f"{server['mem']['used_pct']} % använt av {server['mem']['total_mb']} MB",
                None,
            )
        )
    if server.get("disk"):
        details.append(("Disk", f"{disk} % använt av {server['disk']['total_gb']} GB", None))
    if backup:
        size = (data.get("backup") or {}).get("size_mb")
        details.append(
            (
                "Senaste backup",
                backup[:16].replace("T", " ") + (f" ({size} MB)" if size else ""),
                None,
            )
        )
    deploy = data.get("deploy") or {}
    if deploy.get("at"):
        details.append(("Senast uppdaterad", _nice_date(deploy["at"]), None))
    return _area(
        "server",
        "Server",
        status,
        headline,
        f"disk {disk:.0f} %" if isinstance(disk, (int, float)) else "",
        details,
    )


def _errors(check):
    if not check or not check.ok:
        return None
    data = check.data or {}
    week, month = data.get("total_7d", 0), data.get("total_30d", 0)
    return _area(
        "errors",
        "Fel i sajten",
        OK,
        "Inga fel rapporterade."
        if not month
        else f"{week} senaste veckan. Vi ser dem och åtgärdar.",
        f"{month} på 30 dagar",
    )


#: (fält på MonitorSettings, namn i "kommer snart"-raden, byggare)
_AREAS = (
    ("show_ssl", "HTTPS-certifikat", lambda b: _ssl(b["ssl"])),
    ("show_domain", "Domän", lambda b: _domain(b["reg"])),
    ("show_email", "E-post", lambda b: _email(b["email"])),
    ("show_security", "Säkerhet", lambda b: _security(b["security"])),
    ("show_performance", "Prestanda", lambda b: _performance(b["performance"])),
    ("show_server", "Server", lambda b: _server(b["snapshot"])),
    ("show_visits", "Besök", lambda b: _visits(b["snapshot"])),
    ("show_errors", "Fel i sajten", lambda b: _errors(b["errors"])),
    ("show_gbp", "Google Business Profile", lambda b: None),
    ("show_search", "Sökpositioner", lambda b: None),
)


def areas_for(bundle, monitor):
    """(rader med data, namn på påslagna kontroller som ännu saknar data)."""
    rows, pending = [], []
    for field, name, build in _AREAS:
        if not getattr(monitor, field, False):
            continue
        area = build(bundle)
        if area:
            rows.append(area)
        else:
            pending.append(name)
    return rows, pending


def daily_uptime(rows, days=30):
    """En post per dag, äldst först: {date, pct} där pct är None om ingen mätning finns."""
    today = timezone.localdate()
    buckets = {}
    for t, ok, _ms in rows:
        buckets.setdefault(timezone.localtime(t).date(), []).append(bool(ok))
    out = []
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        checks = buckets.get(day)
        pct = round(100 * sum(checks) / len(checks), 2) if checks else None
        state = "none" if pct is None else OK if pct >= 100 else WARN if pct >= 98 else BAD
        out.append({"date": day, "pct": pct, "state": state})
    return out
