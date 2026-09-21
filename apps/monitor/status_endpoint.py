"""
/status/adx/ - plattformens standardiserade statusrapport.

Samma vy i varje Django-sajt vi driftar. adx.se:s övervakning anropar den
med den delade nyckeln (header X-ADX-Key = ADX_STATUS_KEY i env) och får
det som inte går att se utifrån: databas, server, backup, deploy, besök.
Utan nyckel i env finns endpointet inte (404); fel nyckel ger 403.

Nyckeln tas BARA emot i headern: en nyckel i adressen hamnar i
webbserverns accesslogg och i felrapporteringens query_string. Den jämförs
i _authorized(), en egen liten funktion, så att den aldrig ligger som lokal
variabel i en ram som kan kasta - Sentry skickar lokala variabler ur
stackramarna. Och jämförelsen sker på bytes: hmac.compare_digest kastar
TypeError på strängar med icke-ASCII-tecken.

Kontraktet (alla fält valfria utom db):
{
  "app": "adx-platform", "endpoint_version": 2, "site": "<SITE_SLUG>", "time": "<ISO>",
  "db": "ok" | "error: ...",
  "server": {"uptime_seconds", "load": [1, 5, 15], "cpu_count",
             "mem": {"total_mb", "available_mb", "used_pct"},
             "disk": {"total_gb", "used_gb", "used_pct"}},
  "deploy": {"rev", "at"},
  "backup": {"latest_at", "size_mb", "age_hours"},
  "visits": {"sessions_7d", "sessions_30d", "pageviews_7d", "top_pages_7d": [{"path", "n"}]},
  "sentry": {"configured": bool},
  "errors": {"<del>": "<undantagets typ>"}   # bara när en del inte gick att ta fram
}
"""

import hmac
import json
import os
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache


def _db():
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"[:200]


def _server():
    out = {"cpu_count": os.cpu_count()}
    try:
        with open("/proc/uptime") as handle:
            out["uptime_seconds"] = int(float(handle.read().split()[0]))
    except OSError:
        pass
    try:
        out["load"] = [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        pass
    try:
        info = {}
        with open("/proc/meminfo") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                info[key] = int(rest.split()[0])
        total, avail = info["MemTotal"], info["MemAvailable"]
        out["mem"] = {
            "total_mb": total // 1024,
            "available_mb": avail // 1024,
            "used_pct": round(100 * (total - avail) / total, 1),
        }
    except (OSError, KeyError, ValueError):
        pass
    try:
        usage = shutil.disk_usage(str(settings.BASE_DIR))
        out["disk"] = {
            "total_gb": round(usage.total / 1e9, 1),
            "used_gb": round(usage.used / 1e9, 1),
            "used_pct": round(100 * usage.used / usage.total, 1),
        }
    except OSError:
        pass
    return out


def _deploy():
    base = Path(settings.BASE_DIR)
    stamp = base.parent / "release.json"
    try:
        return json.loads(stamp.read_text())
    except (OSError, ValueError):
        pass
    try:
        rev = subprocess.run(  # noqa: S603, S607
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=base,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        return {"rev": rev, "at": None}
    except (OSError, subprocess.SubprocessError):
        return {}


def _backup():
    folder = Path(settings.BASE_DIR).parent / "backups"
    try:
        files = sorted(folder.glob("*.sql.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return {}
    if not files:
        return {"latest_at": None}
    stat = files[0].stat()
    latest = timezone.datetime.fromtimestamp(stat.st_mtime, tz=timezone.get_current_timezone())
    return {
        "latest_at": latest.isoformat(timespec="minutes"),
        "size_mb": round(stat.st_size / 1e6, 1),
        "age_hours": round((timezone.now() - latest).total_seconds() / 3600, 1),
    }


def _visits():
    try:
        from django.db.models import Count

        from apps.analytics.models import PageView, Session
    except Exception:  # noqa: BLE001 - sajter utan analytics-appen
        return None
    now = timezone.now()
    week, month = now - timedelta(days=7), now - timedelta(days=30)
    top = (
        PageView.objects.filter(viewed_at__gte=week)
        .values("path")
        .annotate(n=Count("id"))
        .order_by("-n")[:5]
    )
    return {
        "sessions_7d": Session.objects.filter(started_at__gte=week).count(),
        "sessions_30d": Session.objects.filter(started_at__gte=month).count(),
        "pageviews_7d": PageView.objects.filter(viewed_at__gte=week).count(),
        "top_pages_7d": [{"path": row["path"], "n": row["n"]} for row in top],
    }


ENDPOINT_VERSION = 2


def _authorized(request):
    """None = ingen nyckel i miljön (endpointet finns inte), annars True/False."""
    expected = getattr(settings, "ADX_STATUS_KEY", "")
    if not expected:
        return None
    given = request.headers.get("X-ADX-Key", "")
    return hmac.compare_digest(given.encode(), expected.encode())


@never_cache
def status_view(request):
    allowed = _authorized(request)
    if allowed is None:
        raise Http404
    if not allowed:
        return JsonResponse({"error": "forbidden"}, status=403)

    # En trasig del får inte fälla hela rapporten - då syns inte ens att
    # databasen är nere. Delen blir null och felets typ hamnar i "errors".
    report, errors = {}, {}
    sections = {"server": _server, "deploy": _deploy, "backup": _backup, "visits": _visits}
    for name, build in sections.items():
        try:
            report[name] = build()
        except Exception as exc:  # noqa: BLE001
            report[name] = None
            errors[name] = type(exc).__name__

    return JsonResponse(
        {
            "app": "adx-platform",
            "endpoint_version": ENDPOINT_VERSION,
            "site": getattr(settings, "SITE_SLUG", ""),
            "time": timezone.now().isoformat(timespec="seconds"),
            "db": _db(),
            **report,
            "sentry": {"configured": bool(getattr(settings, "SENTRY_DSN", ""))},
            **({"errors": errors} if errors else {}),
        }
    )
