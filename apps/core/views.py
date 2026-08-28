from django.db import connection
from django.http import FileResponse, Http404, JsonResponse


def favicon(request):
    """Serve the favicon from SiteSettings at /favicon.ico."""
    from apps.website.models import SiteSettings

    site = SiteSettings.load()
    if not site.favicon_id or not site.favicon.file:
        raise Http404
    return FileResponse(site.favicon.file.open("rb"), content_type="image/x-icon")


def healthz(request):
    """
    Liveness + readiness check.

    Returns 200 only if the DB answers. Used by:
      - deploy.sh (post-deploy gate)
      - external uptime monitoring (UptimeRobot / Healthchecks.io)
    Keep it cheap and unauthenticated.
    """
    checks = {"app": "ok"}
    status = 200
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - report any DB failure as unhealthy
        checks["database"] = f"error: {exc}"
        status = 503

    return JsonResponse(
        {"status": "ok" if status == 200 else "unhealthy", "checks": checks}, status=status
    )
