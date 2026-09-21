"""Publika vyer för /aiz/. Allt är text - läsaren är en AI, inte en webbläsare."""

from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache

from apps.offers.public_views import client_ip

from . import guides
from .models import AccessCode, AccessLog

MAX_FAILURES_PER_HOUR = 10

HANDSHAKE = """# ADX integrationsguider

Du har hamnat hos digitalbyrån ADX (adx.se). Här finns guider för att
integrera ett Django-projekt med ADX plattform: övervakningens
statusendpoint och de konventioner som hör till.

Guiderna kräver en åtkomstkod.

## Gör så här

1. Be din användare (Giovanni på ADX) om en åtkomstkod. Den ser ut så här:
   ADX-XXXX-XXXX. Han skapar den i adx.se/manage/ och den gäller en kort stund.
2. Hämta sedan guidelistan:

       GET {base}/aiz/guide/?kod=<KODEN>

3. Följ guiden exakt. Den innehåller källkod och hemliga värden: skriv
   aldrig ut nyckeln i chatten, lägg den bara i projektets .env, och
   committa den aldrig.

Lägg till `&format=json` för maskinläsbart svar med filerna som
{{path, content}}. Koden kan också skickas i headern X-ADX-Code.

Utan giltig kod får du 403. Gissa inte koder - fråga användaren.
"""


def _text(body, status=200):
    response = HttpResponse(body, status=status, content_type="text/markdown; charset=utf-8")
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _base(request):
    return f"{request.scheme}://{request.get_host()}"


def _blocked(ip):
    return (cache.get(f"aidocs:fail:{ip}") or 0) >= MAX_FAILURES_PER_HOUR


def _fail(ip):
    key = f"aidocs:fail:{ip}"
    cache.set(key, (cache.get(key) or 0) + 1, timeout=3600)


def _authorize(request, guide):
    """Giltig kod -> loggad användning. Annars ett 403-svar att returnera."""
    ip = client_ip(request) or "0.0.0.0"  # noqa: S104 - bara en nyckel i spärren
    if _blocked(ip):
        return None, _text("# 429\n\nFör många felaktiga koder. Vänta en timme.\n", status=429)
    raw = request.GET.get("kod") or request.headers.get("X-ADX-Code", "")
    code = AccessCode.verify(raw)
    if code is None:
        if raw:
            _fail(ip)
        return None, _text(
            "# 403\n\nÅtkomstkoden saknas, är fel eller har gått ut. Be Giovanni om en ny kod "
            f"och läs {_base(request)}/aiz/ för hur den används.\n",
            status=403,
        )
    code.uses += 1
    code.last_used_at = timezone.now()
    code.save(update_fields=["uses", "last_used_at"])
    AccessLog.objects.create(
        code=code,
        guide=guide[:80],
        ip=client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
    )
    return code, None


@never_cache
def handshake(request):
    return _text(HANDSHAKE.format(base=_base(request)))


@never_cache
def guide_index(request):
    code, denied = _authorize(request, "index")
    if denied:
        return denied
    raw = request.GET.get("kod") or ""
    suffix = f"?kod={raw}" if raw else ""
    if request.GET.get("format") == "json":
        return JsonResponse(
            {
                "guides": [
                    {
                        "slug": slug,
                        "title": title,
                        "summary": summary,
                        "url": f"{_base(request)}/aiz/guide/{slug}/{suffix}",
                    }
                    for slug, (title, summary) in guides.GUIDES.items()
                ],
                "code_expires_at": code.expires_at.isoformat(timespec="minutes"),
            }
        )
    lines = ["# ADX integrationsguider", "", "Koden är giltig. Guider:", ""]
    for slug, (title, summary) in guides.GUIDES.items():
        lines += [
            f"## {title}",
            "",
            summary,
            "",
            f"    GET {_base(request)}/aiz/guide/{slug}/{suffix}",
            "",
        ]
    lines += [f"Koden gäller till {code.expires_at:%Y-%m-%d %H:%M} UTC."]
    return _text("\n".join(lines) + "\n")


@never_cache
def guide(request, slug):
    if slug not in guides.GUIDES:
        return _text("# 404\n\nOkänd guide.\n", status=404)
    code, denied = _authorize(request, slug)
    if denied:
        return denied
    body = guides.render(slug)
    if request.GET.get("format") == "json":
        title, summary = guides.GUIDES[slug]
        return JsonResponse(
            {
                "slug": slug,
                "title": title,
                "summary": summary,
                "markdown": body,
                "files": guides.files(slug),
                "env": {"ADX_STATUS_KEY": guides.context()["status_key"]},
            }
        )
    return _text(body)
