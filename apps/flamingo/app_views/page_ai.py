"""
AI och Konverteringskollen i sidbyggaren (/flamingo/app/sidor/<pk>/...),
som JSON till redigeraren (static/js/flamingo-pb-ai.js).

    page_ai_build     GET:  formulärets läge (tjänsterna, vad AI använder,
                            vad som saknas, om AI är på)
                      POST: {goal, service_id, tone} ger ett förslag på hela
                            sidan: {blocks, explanations, used_facts, missing,
                            source, note, problems}. Sparar ingenting.
    page_ai_rewrite   POST: {block_id, field, goal?, tone?, fields?, type?,
                            variant?} ger {suggestions: [{text, principle_key,
                            principle_label, why}], ...}. Sparar ingenting.
    page_koll         GET ?which=draft|published: Konverteringskollen
                      {score, total, summary, items}.

Allt hämtas via kundens konto (account=account): en sida eller tjänst från
ett annat konto ger 404. Byrån i kundvyn gör samma sak som kunden. AI-reglerna
(bara bekräftade uppgifter, vakten, dagsgränsen, mallarna) står i
pagebuilder/ai.py. Kunden mejlas aldrig härifrån.
"""

import json

from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from ..models import LandingPage, Service
from ..pagebuilder import ai, koll
from . import app_view


def _error(message, status=400):
    return JsonResponse({"error": message}, status=status)


def _body(request):
    """JSON-kroppen som dict, eller None (för stor, inte JSON, eller så djupt
    nästlad att den inte går att läsa: 400, aldrig 500)."""
    if len(request.body or b"") > ai.MAX_BODY:
        return None
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _service(account, value):
    """Kontots tjänst för id:t. Ett id från ett annat konto ger 404, som
    allt annat ur en adress eller ett formulär."""
    if value in (None, ""):
        return None
    try:
        pk = int(value)
    except (TypeError, ValueError):
        raise Http404 from None
    return get_object_or_404(Service, pk=pk, account=account)


def _choice(value, allowed):
    return value if isinstance(value, str) and value in allowed else ""


@app_view
@require_http_methods(["GET", "POST"])
def page_ai_build(request, account, pk):
    page = get_object_or_404(LandingPage, pk=pk, account=account)
    if request.method == "GET":
        service = _service(account, request.GET.get("service_id"))
        goal = _choice(request.GET.get("goal"), ai.GOALS)
        return JsonResponse(ai.form_state(page, account, service=service, goal=goal))
    data = _body(request)
    if data is None:
        return _error("Förfrågan gick inte att läsa. Ladda om sidan och försök igen.")
    service = _service(account, data.get("service_id"))
    if service is None and account.services.filter(is_active=True).exists():
        return _error("Välj en tjänst.")
    result = ai.build(
        page,
        account,
        goal=_choice(data.get("goal"), ai.GOALS),
        service=service,
        tone=_choice(data.get("tone"), ai.TONES),
        user=request.user,
    )
    return JsonResponse(result)


@app_view
@require_POST
def page_ai_rewrite(request, account, pk):
    page = get_object_or_404(LandingPage, pk=pk, account=account)
    data = _body(request)
    if data is None:
        return _error("Förfrågan gick inte att läsa. Ladda om sidan och försök igen.")
    block_id, field = data.get("block_id"), data.get("field")
    if not isinstance(block_id, str) or not isinstance(field, str):
        return _error("Välj ett block och ett fält att skriva om.")
    fields = data.get("fields")
    try:
        result = ai.rewrite(
            page,
            account,
            block_id=block_id[:20],
            field=field[:40],
            goal=_choice(data.get("goal"), ai.GOALS),
            tone=_choice(data.get("tone"), ai.TONES),
            fields=fields if isinstance(fields, dict) else None,
            block_type=_choice(data.get("type"), ai.TYPES),
            variant=data.get("variant") if isinstance(data.get("variant"), str) else "",
            service=_service(account, data.get("service_id")),
            user=request.user,
        )
    except ai.AIError as exc:
        return _error(exc.message)
    return JsonResponse(result)


@app_view
@require_GET
def page_koll(request, account, pk):
    page = get_object_or_404(LandingPage, pk=pk, account=account)
    which = "published" if request.GET.get("which") == "published" else "draft"
    result = koll.koll(page, account, which=which)
    result["which"] = which
    return JsonResponse(result)
