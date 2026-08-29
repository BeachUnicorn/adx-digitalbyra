"""
/manage/ai/ - granska och godkänna AI:ns utkast, och hantera AI-nycklar.

Det är här kundens klick sitter. AI:n kommer aldrig längre än till en
DraftChange; ingenting i den här filen kan anropas av modellen.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from apps.website.models import SiteSettings

from . import draft
from .diffing import field_diffs
from .models import AIJob, AssistantToken, ChatMessage, DraftChange, Risk
from .oauth_models import OAuthClient
from .operations import OperationError


def _superuser_required(view):
    """
    Byråns verktyg, inte kundens: personliga API-nycklar.

    AI-delen i övrigt släpptes till kunden 2026-08-22. Nycklarna är kvar
    bakom superuser - kunden ansluter via inloggning och samtycke, aldrig
    via kod, och en nyckel i fel händer syns inte som en inloggning gör.
    """
    return user_passes_test(lambda u: u.is_superuser)(view)


def _ctx(**extra):
    ctx = {"site_settings": SiteSettings.load(), "active": "assistant"}
    ctx.update(extra)
    return ctx


def _row(change):
    after = change.payload.get("andrade_falt", change.payload)
    stale = (
        draft.stale_fields(change, change.target)
        if change.status == DraftChange.Status.PENDING
        else set()
    )
    return {
        "change": change,
        "diffs": field_diffs(change.before, after),
        "is_new": change.before is None,
        # Fält någon hunnit redigera sedan förslaget lades - ett godkännande
        # skulle skriva över den redigeringen.
        "stale": sorted(stale),
    }


#: Rubrik för grupper av NYA objekt. Nya rader har inget målobjekt att
#: gruppera på, och utan det här hamnade varje enskilt förslag i en egen
#: grupp med en egen "Godkänn markerade"-knapp - knappen upprepades en gång
#: per förslag och kryssrutorna hörde till olika formulär.
_NEW_GROUP_LABELS = {
    "skapa_faq_fraga": "Nya FAQ-frågor",
    "skapa_faq_sektion": "Ny FAQ med frågor",
    "skapa_tjanst": "Nya tjänster",
    # Blocken hänger på sidutkastet via depends_on, så _grouped lägger hela
    # sidbygget i EN grupp - etiketten säger det, precis som FAQ-sektionens.
    "skapa_sida": "Ny sida med block",
    "skapa_block": "Nya textblock",
    "skapa_omrade": "Nya områden",
}


def _target_label(change):
    """Vad ändringen gäller, för gruppering."""
    target = change.target
    if target is not None:
        return f"{change.target_ct_id}:{change.target_id}", str(target)
    # Nya objekt grupperas per operation, inte per rad.
    return (
        f"ny:{change.operation}",
        _NEW_GROUP_LABELS.get(change.operation, "Nytt innehåll"),
    )


def _grouped(changes):
    """
    Gruppera väntande utkast per objekt - en tjänst, ett område, en sida -
    och nya rader per operation.

    Kunden tänker "godkänn den här tjänsten", inte "godkänn alla
    textändringar". Grupperingen gör den enheten till den man ser.
    Markeringen är däremot GEMENSAM för hela jobbet: ett formulär, en
    verktygsrad. Ett formulär per grupp innebar att kryssrutor i olika
    grupper tillhörde olika formulär, och en klumpknapp skickade bara sin
    egen grupps rutor - det såg ut som att ingenting hände.
    """
    # Ett förslag som BEROR på ett annat hör till samma enhet: en FAQ-sektion
    # och dess frågor är EN sak att godkänna, inte sex. Kunden bad om det
    # 2026-08-23 - att godkänna fråga för fråga är meningslöst arbete när
    # frågorna ändå inte kan finnas utan sektionen.
    by_pk = {c.pk: c for c in changes}

    def unit_key(change):
        seen = set()
        node = change
        while node.depends_on_id and node.depends_on_id in by_pk:
            if node.pk in seen:  # cykel ska inte kunna hänga vyn
                break
            seen.add(node.pk)
            node = by_pk[node.depends_on_id]
        return node

    groups = {}
    for change in changes:
        root = unit_key(change)
        key, label = _target_label(root)
        group = groups.setdefault(
            key,
            {"label": label, "rows": [], "text_ids": [], "has_business": False},
        )
        group["rows"].append(_row(change))
        group["text_ids"].append(change.pk)
        if change.risk == Risk.BUSINESS:
            group["has_business"] = True

    # Grupp-id:t byggs här, inte i mallen: Djangos `add`-filter klarar inte
    # sträng + heltal och ger tyst en tom sträng, vilket tidigare tog bort
    # kryssrutorna utan att något syntes i loggen.
    ordered = list(groups.values())
    for index, group in enumerate(ordered, start=1):
        group["gid"] = f"grupp-{index}"
        group["count"] = len(group["text_ids"])
    return ordered


def _change_rows(job):
    """Väntande utkast grupperade per objekt, plus redan avgjorda."""
    pending, decided = [], []
    for change in job.changes.select_related("target_ct").all():
        if change.status == DraftChange.Status.PENDING:
            pending.append(change)
        else:
            decided.append(_row(change))
    return _grouped(pending), decided


@login_required
def job_list(request):
    jobs = (
        AIJob.objects.filter(user=request.user)
        .prefetch_related("changes")
        .order_by("-created_at")[:50]
    )
    return render(request, "manage/assistant/job_list.html", _ctx(jobs=jobs, ai_section="drafts"))


@login_required
def job_detail(request, pk):
    from . import suggestions

    job = get_object_or_404(AIJob.objects.filter(user=request.user), pk=pk)
    groups, decided = _change_rows(job)

    # Underförslag: nästa steg för objekt som fått något godkänt här.
    # Deterministiska - de speglar vad som saknas på raden, och prompten
    # bär en @-token så chatten vet exakt vilket objekt det gäller.
    followups, seen = [], set()
    for row in decided:
        change = row["change"]
        target = change.target
        if target is None or change.status != DraftChange.Status.APPLIED:
            continue
        key = (change.target_ct_id, change.target_id)
        if key in seen:
            continue
        seen.add(key)
        for f in suggestions.followups_for(target):
            followups.append({**f, "target": str(target)})

    return render(
        request,
        "manage/assistant/job_detail.html",
        _ctx(
            job=job,
            groups=groups,
            decided_rows=decided,
            can_undo=job.revisions.exists(),
            followups=followups[:4],
            # Antalet kryssbara rader styr om verktygsraden visas alls.
            selectable=sum(len(g["text_ids"]) for g in groups),
            ai_section="drafts",
        ),
    )


@require_POST
@login_required
def change_decide(request, pk):
    """Godkänn eller avslå ett enskilt utkast."""
    change = get_object_or_404(
        DraftChange.objects.select_related("job").filter(job__user=request.user), pk=pk
    )
    action = request.POST.get("action")
    if action == "approve":
        try:
            draft.approve(change, request.user, force=request.POST.get("force") == "1")
            messages.success(request, f"Genomfört: {change.summary}")
        except OperationError as exc:
            messages.error(request, str(exc))
    elif action == "reject":
        draft.reject(change, request.user)
        messages.info(request, f"Avslaget: {change.summary}")
    return redirect("manage:assistant_job", pk=change.job_id)


@require_POST
@login_required
def job_bulk(request, pk):
    """
    Klumpgodkänn markerade ändringar.

    Riskklassen begränsar INTE längre vad som får markeras (kundens beslut
    2026-08-21): den som granskar väljer själv vad hen godkänner, även
    affärsdata. Klassen finns kvar som märkning i gränssnittet, så det
    syns vad markeringen innehåller innan man trycker.
    """
    job = get_object_or_404(AIJob.objects.filter(user=request.user), pk=pk)
    ids = request.POST.getlist("change_ids")
    action = request.POST.get("action")

    changes = list(job.changes.filter(pk__in=ids, status=DraftChange.Status.PENDING))
    if not changes:
        messages.info(request, "Inga ändringar var markerade.")
        return redirect("manage:assistant_job", pk=job.pk)

    if action == "approve":
        done, failed = draft.approve_many(changes, request.user)
        if done:
            messages.success(request, f"{done} ändringar genomförda.")
        for change, error in failed:
            messages.error(request, f"{change.summary}: {error}")
    elif action == "reject":
        for change in changes:
            draft.reject(change, request.user)
        messages.info(request, f"{len(changes)} ändringar avslagna.")
    return redirect("manage:assistant_job", pk=job.pk)


@require_POST
@login_required
def job_undo(request, pk):
    """Ångra ett helt genomfört jobb."""
    job = get_object_or_404(AIJob.objects.filter(user=request.user), pk=pk)
    reverted = draft.undo_job(job, request.user)
    if reverted:
        messages.success(request, f"Ångrade {reverted} ändringar. Ångringen syns i historiken.")
    else:
        messages.info(request, "Det fanns inget att ångra i det här jobbet.")
    return redirect("manage:assistant_job", pk=job.pk)


@login_required
def change_preview(request, pk):
    """Ramen runt förhandsgranskningen: beslutsknappar + sidan i en iframe."""
    change = get_object_or_404(
        DraftChange.objects.select_related("job").filter(job__user=request.user), pk=pk
    )
    return render(
        request,
        "manage/assistant/preview.html",
        _ctx(change=change, row=_row(change), job=change.job, ai_section="drafts"),
    )


@xframe_options_sameorigin
@login_required
def change_preview_frame(request, pk):
    """
    Själva sidan, renderad med utkastet applicerat och sedan återställd.

    Egen URL i stället för inbäddad HTML: den publika sidan har egna
    stilar och skript som inte ska blandas med /manage/. En iframe håller
    dem isär utan att något behöver skrivas om.

    xframe_options_sameorigin behövs eftersom sajtens globala X-Frame-Options
    är DENY. Undantaget gäller BARA den här vyn och bara samma origin - och
    innehållet är vårt eget, bakom inloggning, i en sandlåda utan skript.
    """
    from django.http import HttpResponse

    from .preview import PreviewUnavailable, render_draft

    change = get_object_or_404(
        DraftChange.objects.select_related("job").filter(job__user=request.user), pk=pk
    )
    try:
        return HttpResponse(render_draft(change, request.user))
    except (PreviewUnavailable, OperationError) as exc:
        return render(
            request,
            "manage/assistant/preview_error.html",
            {"message": str(exc)},
            status=200,
        )


@require_POST
@login_required
def job_delete(request, pk):
    """
    Radera ett förslag/samtal med allt innehåll.

    Versionshistoriken överlever: RevisionMeta.job är SET_NULL, så
    genomförda ändringar finns kvar under /manage/historik/ även när
    jobbet är borta. Det som försvinner är utkasten, chattraderna och
    möjligheten att ångra jobbet som helhet - därför varnar gränssnittet
    när det finns något genomfört att ångra.
    """
    job = get_object_or_404(AIJob.objects.filter(user=request.user), pk=pk)
    label = job.title or f"Förslag {job.pk}"
    job.delete()
    messages.success(request, f"{label} är raderat.")
    return redirect(request.POST.get("next") or "manage:assistant_jobs")


# --- AI-koppling (nycklar) -------------------------------------------------


@login_required
def style_guide(request):
    """
    Skrivguiden: kundens tonläge, som AI:n läser först i varje session.

    Ligger under AI och inte i Inställningar - fältet fanns på SiteSettings
    men saknade gränssnitt helt fram till 2026-08-23, alltså gick den inte
    att redigera. Den hör hemma där man arbetar med AI:n.
    """
    from apps.assistant.operations.context_ops import DEFAULT_STYLE_GUIDE

    site = SiteSettings.load()
    if request.method == "POST":
        site.ai_style_guide = (request.POST.get("ai_style_guide") or "").strip()[:4000]
        site.save(update_fields=["ai_style_guide", "updated_at"])
        messages.success(
            request,
            "Skrivguiden är sparad. AI:n följer den från nästa fråga."
            if site.ai_style_guide
            else "Skrivguiden är tömd. AI:n använder standardguiden igen.",
        )
        return redirect("manage:assistant_style_guide")

    return render(
        request,
        "manage/assistant/style_guide.html",
        _ctx(
            ai_section="guide",
            value=site.ai_style_guide,
            default_guide=DEFAULT_STYLE_GUIDE,
            is_default=not site.ai_style_guide.strip(),
        ),
    )


@login_required
def connection(request):
    """Sidan där kunden kopplar in AI-appen och hanterar personliga nycklar."""
    from django.conf import settings

    # Claude-appens connector ansluter från Anthropics servrar, så adressen
    # måste vara publik. SITE_BASE_URL är sanningen i produktion; i utveckling
    # faller vi tillbaka på requestens egen adress.
    base = (getattr(settings, "SITE_BASE_URL", "") or "").rstrip("/")
    mcp_url = f"{base}/mcp/" if base else request.build_absolute_uri("/mcp/")

    return render(
        request,
        "manage/assistant/connection.html",
        _ctx(
            ai_section="connection",
            tokens=AssistantToken.objects.filter(user=request.user),
            new_key=request.session.pop("assistant_new_key", None),
            mcp_url=mcp_url,
            mcp_url_is_https=mcp_url.startswith("https://"),
            oauth_clients=OAuthClient.objects.all(),
        ),
    )


@require_POST
@_superuser_required
@login_required
def token_create(request):
    name = (request.POST.get("name") or "AI-koppling").strip()[:100]
    _token, raw = AssistantToken.issue(request.user, name=name)
    # Klartextnyckeln visas exakt en gång, via sessionen - aldrig i en URL
    # och aldrig sparad i databasen.
    request.session["assistant_new_key"] = raw
    messages.success(request, "Nyckeln är skapad. Kopiera den nu - den visas bara denna gång.")
    return redirect("manage:assistant_connection")


@require_POST
@_superuser_required
@login_required
def token_revoke(request, pk):
    token = get_object_or_404(AssistantToken, pk=pk, user=request.user)
    token.is_active = False
    token.save(update_fields=["is_active"])
    messages.info(request, f"Nyckeln {token.name} är återkallad.")
    return redirect("manage:assistant_connection")


@require_POST
@login_required
def oauth_disconnect(request, pk):
    """
    Ta bort en ansluten app HELT: klienten, dess tokens och koder (cascade).

    Tidigare återkallades bara tokens och klientraden stod kvar i listan -
    det såg ut som att knappen inte gjorde något. Dessutom registrerar
    Claude-appen en NY klient vid varje anslutningsförsök, så listan fylldes
    med döda rader. Radering är vad kunden menar med "koppla bort"; vill
    appen ansluta igen registrerar den sig på nytt och passerar samtycket.
    """
    client = get_object_or_404(OAuthClient, pk=pk)
    label = str(client)
    client.delete()
    messages.info(
        request, f"{label} är borttagen. Appen måste anslutas på nytt för att användas igen."
    )
    return redirect("manage:assistant_connection")


@require_POST
@login_required
def oauth_disconnect_all(request):
    """Ta bort ALLA anslutna appar - städknappen efter misslyckade försök."""
    count = OAuthClient.objects.count()
    OAuthClient.objects.all().delete()
    if count:
        messages.info(request, f"{count} anslutningar borttagna.")
    else:
        messages.info(request, "Det fanns inga anslutningar att ta bort.")
    return redirect("manage:assistant_connection")


@login_required
def mention_search(request):
    """Sökmenyn bakom @ i chatten. Bara läsning, bara egna förslag."""
    from . import mentions

    return JsonResponse({"results": mentions.search(request.GET.get("q", ""), request.user)})


# --- Inbyggd chatt ---------------------------------------------------------


def _labelled(message):
    """Ett meddelande med stegetiketter och @-tokens som chips."""
    from .mentions import as_html
    from .rendering import inline_text, message_html
    from .runtime import step_label

    message.labelled_steps = [
        {
            **s,
            "note": inline_text(s.get("note", "")),
            "label": step_label(s.get("op", "")) if s.get("op") else "",
            # Äldre rader saknar state; de är per definition avklarade.
            "state": s.get("state") or ("fail" if s.get("ok") is False else "done"),
        }
        for s in (message.steps or [])
    ]
    # Kunden ska aldrig se råa asterisker: assistentens svar är markdown
    # och renderas, användarens text visas som skriven (med chips).
    if message.role == "assistant":
        message.display_html = message_html(message.content)
    else:
        message.display_html = as_html(message.content)
    return message


def _chat_ctx(job=None):
    from django.conf import settings

    from .llm import is_configured, model_id, model_label, provider, spent_today

    limit = float(getattr(settings, "ASSISTANT_DAILY_BUDGET_USD", 5.0))
    spent = spent_today() / 1_000_000
    model = model_id()
    short = model_label()
    return {
        "job": job,
        # INTE "messages" - det namnet äger Djangos flash-ramverk, och
        # basmallen skulle rendera chattraderna som systemnotiser.
        "chat_messages": [_labelled(m) for m in job.messages.all()] if job else [],
        "model": model,
        "model_short": short,
        "budget_spent": round(spent, 3),
        "budget_limit": limit,
        "budget_full": spent >= limit,
        "budget_percent": min(100, round(spent / limit * 100)) if limit else 0,
        # Mac visar ⌘, allt annat Ctrl. Att fråga klienten är overkill för
        # en tangentbordsgenväg; serverns gissning räcker och kan vara fel
        # utan att något går sönder.
        "send_key": "⌘",
        "provider": provider(),
        "enabled": is_configured(),
    }


@login_required
def chat(request, pk=None):
    """Chattpanelen. En konversation = ett AIJob = en granskningssida."""
    job = None
    if pk is not None:
        job = get_object_or_404(AIJob.objects.filter(user=request.user), pk=pk)

    # Förslagen renderas direkt i chatten: kunden ska kunna godkänna där
    # hen står, inte klicka sig till granskningssidan och tillbaka. Klicket
    # är kundens, i en inloggad session med CSRF - samma gräns som förut,
    # bara på en annan plats. AI:n kan fortfarande inte godkänna något.
    groups = (
        _grouped(
            list(job.changes.select_related("target_ct").filter(status=DraftChange.Status.PENDING))
        )
        if job
        else []
    )

    recent = (
        AIJob.objects.filter(user=request.user, messages__isnull=False)
        .distinct()
        .order_by("-created_at")[:15]
    )
    return render(
        request,
        "manage/assistant/chat.html",
        _ctx(
            recent=recent,
            ai_section="chat",
            # Underförslagens chips länkar hit med ?q= - texten hamnar
            # ifylld men skickas inte förrän kunden trycker Skicka.
            prefill=request.GET.get("q", "")[:2000],
            draft_groups=groups,
            **_chat_ctx(job),
        ),
    )


@require_POST
@login_required
def chat_send(request, pk=None):
    from . import chat as chat_engine
    from .tasks import queue_turn

    job = None
    if pk is not None:
        job = get_object_or_404(AIJob.objects.filter(user=request.user), pk=pk)

    try:
        job, reply = chat_engine.start_turn(request.user, request.POST.get("question"), job)
    except OperationError as exc:
        messages.error(request, str(exc))
        return (
            redirect("manage:assistant_chat_job", pk=job.pk)
            if job
            else redirect("manage:assistant_chat")
        )

    queue_turn(reply.pk)
    return redirect("manage:assistant_chat_job", pk=job.pk)


@login_required
def chat_poll(request, pk):
    """
    Läge för den öppna turen. Gränssnittet frågar tills den är klar.

    Poll i stället för strömning: en tur kan ta minuter och köras i en tråd,
    och en avbruten strömning ska inte kunna se ut som ett tappat svar.
    """
    from .rendering import inline_text
    from .runtime import step_label
    from .tasks import sweep_stale

    job = get_object_or_404(AIJob.objects.filter(user=request.user), pk=pk)
    last = job.messages.last()
    # Sveparen kördes tidigare bara när en NY tur startades. Satt man och
    # tittade på en död tur blev den aldrig utdömd - "Arbetar" stod kvar
    # tills man råkade skicka något annat.
    if last is not None and last.status == ChatMessage.Status.PENDING:
        if sweep_stale():
            last.refresh_from_db()
    # Etiketten sätts här och inte i mallen: samma steg ska läsa likadant
    # oavsett om sidan renderas om eller uppdateras via polling.
    steps = [
        {
            **s,
            "note": inline_text(s.get("note", "")),
            "label": step_label(s.get("op", "")) if s.get("op") else "",
            "state": s.get("state") or ("fail" if s.get("ok") is False else "done"),
        }
        for s in (last.steps if last else [])
    ]
    return JsonResponse(
        {
            "status": last.status if last else "done",
            "role": last.role if last else "",
            "content": last.content if last else "",
            "error": last.error if last else "",
            "steps": steps,
            "pending_drafts": job.pending_count,
            "review_url": job.get_absolute_url(),
        }
    )
