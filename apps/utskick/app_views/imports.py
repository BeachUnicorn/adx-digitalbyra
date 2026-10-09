"""
Importen i fyra steg (README I.1, I.7 och J S1): Fil, Kolumner, Samtycke,
Granska. Logiken bor i apps/utskick/importer.py; här bara formulären och
vilken mall jobbets läge ritar.

    import_upload   kontakter/import/              steg 1: fil eller inklistrat,
                                                   och de senaste importerna
    import_job      kontakter/import/<pk>/         steg 2 till 4, väntan och resultatet
                                                   efter jobbets status; ?status=json
                                                   för sidan som väntar på ticken
    import_errors   kontakter/import/<pk>/fel.csv  felrapporten

Varje POST i import_job har ett action-fält (map, consent, import, back,
cancel) som går till en hanterare i _ACTIONS. Allt som tar in kontakter
kräver access.can_collect (utskick på och biträdesavtalet godkänt), också
mitt i ett jobb. Listans id går genom access.owned_ids (400 för ett annat
kontos lista). Byrån i kundvyn importerar på riktigt; samtyckesloggen visar
"ADX (Giovanni)".

Mallarna ligger i templates/flamingo/app/kontakter/import/. Inga
style-attribut och inga inline-skript: static/css/flamingo-app-import.css
och static/js/flamingo-app-import.js (väntan, inklistringens radräknare,
fältet för ny lista).
"""

from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_GET

from .. import access, contacts, importer
from ..access import actor_for, owned, owned_ids, utskick_view
from ..models import ContactList, ImportJob, Tag
from . import render_contacts

S = ImportJob.Status
FOLDER = "flamingo/app/kontakter/import"

#: Stegen överst (README I.2: under 560 px "Steg 2 av 4: Kolumner").
STEPS = (("file", "Fil"), ("columns", "Kolumner"), ("consent", "Samtycke"), ("review", "Granska"))
_STEP_OF = {
    S.UPLOADED: 0,
    S.CONVERTING: 0,
    S.MAPPING: 1,
    S.CONSENT: 2,
    S.ANALYSING: 3,
    S.REVIEW: 3,
    S.IMPORTING: 4,
    S.DONE: 4,
}
_TEMPLATES = {
    S.UPLOADED: "wait.html",
    S.CONVERTING: "wait.html",
    S.MAPPING: "map.html",
    S.CONSENT: "consent.html",
    S.ANALYSING: "wait.html",
    S.REVIEW: "review.html",
    S.IMPORTING: "wait.html",
    S.DONE: "done.html",
    S.FAILED: "done.html",
    S.CANCELLED: "done.html",
}
#: Vad sidan som väntar säger medan ticken arbetar.
_WAIT_TEXTS = {
    S.UPLOADED: "Filen läses in.",
    S.CONVERTING: "Filen läses in.",
    S.ANALYSING: "Raderna gås igenom.",
    S.IMPORTING: "Kontakterna importeras.",
}
NEW_LIST = "new"
RECENT = 10
#: Läget i Tidigare importer: stegnamnen (Kolumner, Samtycke, Granska) är
#: inga lägen, där väntar jobbet på kunden.
_STATE_LABELS = {
    S.MAPPING: "Väntar på dig",
    S.CONSENT: "Väntar på dig",
    S.REVIEW: "Väntar på dig",
}


def _job_url(job):
    return reverse("flamingo:app_import_job", args=[job.pk])


def _steps(current):
    """Stegen med current (0 till 4) som det aktuella; 4 är allt klart."""
    items = []
    for i, (key, label) in enumerate(STEPS):
        if i < current:
            state = "done"
        elif i == current:
            state = "now"
        else:
            state = "todo"
        items.append({"key": key, "label": label, "number": i + 1, "state": state})
    if current >= len(STEPS):
        return {"items": items, "line": "Klart: alla fyra steg"}
    return {"items": items, "line": f"Steg {current + 1} av {len(STEPS)}: {STEPS[current][1]}"}


def _render(request, template, context, status=200):
    return render_contacts(request, f"{FOLDER}/{template}", "import", context, status=status)


# ---------------------------------------------------------------------------
# Steg 1
# ---------------------------------------------------------------------------


@utskick_view
def import_upload(request, account):
    """Steg 1: ladda upp en fil (CSV eller Excel, högst 10 MB) eller klistra
    in rader (högst 2 000). Utan godkänt biträdesavtal visas varför i
    stället för formuläret."""
    block = access.collect_block_reason(account)
    error = ""
    paste = ""
    if request.method == "POST" and not block:
        action = request.POST.get("action", "")
        actor = actor_for(request)
        try:
            if action == "paste":
                paste = request.POST.get("text", "")
                job = importer.start_paste(account, paste, actor)
            elif action == "upload":
                job = importer.start_upload(account, request.FILES.get("file"), actor)
            else:
                return redirect("flamingo:app_import")
        except importer.ImportRefused as exc:
            error = str(exc)
        else:
            return redirect(_job_url(job))
    recent = list(
        ImportJob.objects.filter(account=account)
        .select_related("target_list")
        .order_by("-created_at", "-pk")[:RECENT]
    )
    for job in recent:
        job.result = importer.result(job)
        job.open = job.status not in importer.FINAL
        job.lage = _STATE_LABELS.get(job.status) or job.get_status_display()
    return _render(
        request,
        "upload.html",
        {
            "collect_block": block,
            "dpa_missing": bool(block) and block == access.DPA_MISSING_TEXT,
            "error": error,
            "paste": paste if error else "",
            "jobs": recent,
            "steps": _steps(0),
            "max_rows": importer.MAX_ROWS,
            "paste_max": importer.PASTE_MAX_ROWS,
        },
        status=400 if error else 200,
    )


# ---------------------------------------------------------------------------
# Steg 2 till 4
# ---------------------------------------------------------------------------


def _status_json(job):
    counts = importer.result(job) if job.status in (S.IMPORTING, S.DONE) else {}
    return {
        "status": job.status,
        "label": job.get_status_display(),
        "waiting": job.status in importer.BACKGROUND or job.status == S.UPLOADED,
        "progress": job.progress,
        "total": job.row_count,
        "text": _progress_text(job),
        "new": counts.get("new", 0),
        "updated": counts.get("updated", 0),
        "url": _job_url(job),
    }


def _group(number):
    return f"{int(number):,}".replace(",", " ")


def _progress_text(job):
    if job.status in (S.ANALYSING, S.IMPORTING) and job.row_count:
        return f"{_group(min(job.progress, job.row_count))} av {_group(job.row_count)} rader"
    return ""


@utskick_view
def import_job(request, account, pk):
    """Steg 2 till 4, väntan och resultatet, efter jobbets status."""
    job = owned(ImportJob, account, pk)
    if request.method == "GET" and request.GET.get("status") == "json":
        response = JsonResponse(_status_json(job))
        response["Cache-Control"] = "no-store"
        return response
    extra = {}
    if request.method == "POST":
        handler = _ACTIONS.get(request.POST.get("action", ""))
        if handler is None:
            return redirect(_job_url(job))
        result = handler(request, account, job)
        if isinstance(result, HttpResponse):
            return result
        extra = result or {}
    return _render_job(request, account, job, extra)


def _render_job(request, account, job, extra):
    template = _TEMPLATES[job.status]
    context = {
        "job": job,
        "steps": _steps(_STEP_OF.get(job.status, 0)),
        "collect_block": access.collect_block_reason(account),
        "job_url": _job_url(job),
        "errors_url": reverse("flamingo:app_import_errors", args=[job.pk]),
        "error_total": importer.error_total(job),
    }
    builder = _CONTEXTS.get(template)
    if builder is not None:
        context.update(builder(request, account, job))
    context.update(extra)
    return _render(request, template, context, status=400 if extra.get("errors") else 200)


def _map_context(request, account, job):
    defs = contacts.field_defs(account)
    cards = importer.column_cards(job, defs)
    return {
        "cards": cards,
        "column_count": len(cards),
        "field_limit": any(card["limit"] for card in cards),
        "field_limit_text": importer.FIELD_LIMIT_TEXT,
    }


def _consent_context(request, account, job):
    consent = job.consent or {}
    channels = importer.mapped_channels(job)
    return {
        "choices": [
            (importer.CHOICE_CONSENT, importer.CHOICE_LABELS[importer.CHOICE_CONSENT]),
            (importer.CHOICE_EXISTING, importer.CHOICE_LABELS[importer.CHOICE_EXISTING]),
            (importer.CHOICE_UNKNOWN, importer.CHOICE_LABELS[importer.CHOICE_UNKNOWN]),
        ],
        "values": {
            "choice": consent.get("choice", ""),
            "sms": bool(consent.get("sms")),
            "email": bool(consent.get("email")),
            "where": consent.get("where", ""),
        },
        "has_sms": "sms" in channels,
        "has_email": "email" in channels,
    }


def _review_context(request, account, job):
    counts = importer.preview(job)
    importable = counts["new"] + counts["updated"]
    return {
        "counts": counts,
        "importable": importable,
        "fel": counts["errors"] + counts["conflicts"],
        "varningar": counts["values"],
        "error_preview": (job.errors or [])[:10],
        "listor": ContactList.objects.filter(account=account).order_by("name"),
        "taggar": Tag.objects.filter(account=account).order_by("name"),
        "values": {"list": "", "new_list": "", "tag": importer.suggested_tag_name()},
        "consent_label": importer.CHOICE_LABELS.get((job.consent or {}).get("choice"), ""),
        "consent_channels": importer.consent_channels(job),
        "room": contacts.room_left(account),
        "new_list_value": NEW_LIST,
    }


def _wait_context(request, account, job):
    return {"wait_text": _WAIT_TEXTS.get(job.status, ""), "progress_text": _progress_text(job)}


def _done_context(request, account, job):
    counts = importer.result(job)
    fel = counts["errors"] + counts["conflicts"]
    return {
        "counts": counts,
        "fel": fel,
        "varningar": counts["values"],
        "failure": importer.failure_text(job),
        # En import som stoppades eller avbröts: visa det som hann hända.
        "handled": counts["new"] + counts["updated"] + counts["suppressed"] + fel > 0,
    }


_CONTEXTS = {
    "map.html": _map_context,
    "consent.html": _consent_context,
    "review.html": _review_context,
    "wait.html": _wait_context,
    "done.html": _done_context,
}


def _blocked(request, account, job):
    """Kontot får inte ta in kontakter just nu: meddelande och tillbaka."""
    reason = access.collect_block_reason(account)
    if not reason:
        return None
    messages.warning(request, reason)
    return redirect(_job_url(job))


def _map(request, account, job):
    if job.status != S.MAPPING:
        return redirect(_job_url(job))
    blocked = _blocked(request, account, job)
    if blocked:
        return blocked
    defs = contacts.field_defs(account)
    errors = importer.save_mapping(job, request.POST, defs)
    if errors:
        cards = importer.column_cards(job, defs)
        for card in cards:
            posted = request.POST.get(f"col-{card['index']}")
            if posted is not None and not card["pnr"]:
                card["value"] = posted
            card["error"] = errors.get(card["index"], "")
        return {"errors": errors, "cards": cards, "form_error": errors.get("__all__", "")}
    return redirect(_job_url(job))


def _consent(request, account, job):
    if job.status != S.CONSENT:
        return redirect(_job_url(job))
    blocked = _blocked(request, account, job)
    if blocked:
        return blocked
    errors = importer.save_consent(job, request.POST)
    if errors:
        context = _consent_context(request, account, job)
        context["values"] = {
            "choice": request.POST.get("choice", ""),
            "sms": bool(request.POST.get("sms")),
            "email": bool(request.POST.get("email")),
            "where": request.POST.get("where", "")[:300],
        }
        context["errors"] = errors
        return context
    if job.in_request:
        importer.run_in_request(job, importer.analyse)
    return redirect(_job_url(job))


def _import(request, account, job):
    if job.status != S.REVIEW:
        return redirect(_job_url(job))
    blocked = _blocked(request, account, job)
    if blocked:
        return blocked
    choice = request.POST.get("list", "")
    new_list = " ".join(request.POST.get("new_list", "").split())[:80]
    tag_name = " ".join(request.POST.get("tag", "").split())[:40]
    errors = {}
    target_list = None
    if choice == NEW_LIST:
        if not new_list:
            errors["list"] = "Skriv ett namn på den nya listan."
    elif choice:
        (list_pk,) = owned_ids(ContactList, account, [choice], limit=1)
        target_list = ContactList.objects.get(pk=list_pk, account=account)
    if errors:
        context = _review_context(request, account, job)
        context["values"] = {"list": choice, "new_list": new_list, "tag": tag_name}
        context["errors"] = errors
        return context
    actor = actor_for(request)
    if choice == NEW_LIST:
        target_list, _ = ContactList.objects.get_or_create(
            account=account, name=new_list, defaults={"created_by": actor.user}
        )
    target_tag = None
    if tag_name:
        target_tag, _ = Tag.objects.get_or_create(account=account, name=tag_name)
    if importer.begin_import(job, actor, target_list, target_tag) and job.in_request:
        importer.run_in_request(job, importer.run_import)
    return redirect(_job_url(job))


def _back(request, account, job):
    previous = {S.CONSENT: S.MAPPING, S.REVIEW: S.CONSENT}.get(job.status)
    if previous:
        ImportJob.objects.filter(pk=job.pk, status=job.status).update(status=previous)
    return redirect(_job_url(job))


def _cancel(request, account, job):
    if importer.cancel(job):
        messages.info(request, "Importen är avbruten. Det som redan importerats ligger kvar.")
    return redirect("flamingo:app_import")


_ACTIONS = {
    "map": _map,
    "consent": _consent,
    "import": _import,
    "back": _back,
    "cancel": _cancel,
}


# ---------------------------------------------------------------------------
# Felrapporten
# ---------------------------------------------------------------------------


@utskick_view
@require_GET
def import_errors(request, account, pk):
    """Felen som CSV: rad, kolumn och orsak, och radens värden så länge
    filen finns kvar (ett dygn efter importen)."""
    job = owned(ImportJob, account, pk)
    response = HttpResponse(importer.errors_csv(job), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="importfel-{job.pk}.csv"'
    response["Cache-Control"] = "no-store"
    return response
