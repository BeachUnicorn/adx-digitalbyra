"""
Rapportens knappar och S4-delarna av rapporten (README I.1, I.8, H.3, J S4).
Rapporten själv (utskick_report) och mottagarsidan står kvar i
app_views/utskick.py; S4:s delar av dem läggs till i markerade block med
report_context och reports.full.

    utskick_follow_up   utskick/<pk>/folj-upp/   POST: "Följ upp de som inte klickade"
    utskick_export      utskick/<pk>/export/     GET bekräftelsen, POST med bekrafta=1
                                                 CSV:n
    report_context(request, account, utskick, numbers, now) -> dict
                        det rapportens mall läser i S4: full (reports.full),
                        email_tiles med länkar, knapparna och chipsen

Följ upp: segments.create_follow_up skapar segmentet "Klickade inte: <namn>"
(fick utskicket och klickade inte i det), och ett nytt utkast med segmentet
som mottagare går till guidens första steg. Ett segment med samma regler
används igen, och ett utkast som redan följer upp med det segmentet öppnas
i stället för ett nytt (dubbeltryck, eller knappen en gång till). Utkastet
får samma början som "Nytt utskick" (utskick_new: kanalen efter vad som är
påslaget, svarsnumret, Spåra öppningar efter inställningen, en verifierad
egen domän). Inget skickas här.

Exportera (H.3): mottagarna som CSV, en rad per mottagare och kanal, också
de som hoppades över. Varje cell går genom flamingo.exports.safe_cell, en
borttagen kontakt står utan adress, varje export loggas (ExportLog kind
utskick, med vem och om det var byrån) och räknas mot samma gräns som
Kontakters export (högst contacts.EXPORTS_PER_DAY per konto och dygn).
Kunden mejlas aldrig härifrån.
"""

import csv
import io
import logging

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .. import audience, reports, segments
from .. import contacts as register
from ..access import actor_for, owned, utskick_view
from ..email import domains as email_domains
from ..models import REKLAM, ExportLog, Recipient, Segment, Utskick
from . import render_utskick

logger = logging.getLogger(__name__)

FOLLOW_UP_NAME = "Uppföljning: {name}"
FOLLOW_UP_TEXT = (
    "Segmentet {segment} är valt som mottagare i utkastet. Välj kanal och skriv utskicket."
)
FOLLOW_UP_AGAIN_TEXT = "Du har redan ett utkast som följer upp de som inte klickade. Här är det."
NOBODY_TEXT = (
    "Det finns ingen att följa upp: alla som fick utskicket har klickat, "
    "eller så har inget skickats än."
)
FOLLOW_UP_FAILED_TEXT = "Segmentet kunde inte skapas."
NO_ROWS_TEXT = (
    "Mottagarna finns inte kvar (de sparas i 13 månader), så det finns inget att exportera."
)
#: Exporten av ett utskick som inte har börjat skickas (utkast, schemalagt,
#: avbrutet före start): mottagarna har aldrig funnits, de är inte rensade.
NOT_STARTED_TEXT = "Utskicket har inte skickats än, så det finns inga mottagare att exportera."
#: Rapportens chips över mottagartabellen (vyerna på mottagarsidan).
TABLE_VIEWS = ("alla", "klickade", "klickade-inte", "svarade", "forfragan")


# ---------------------------------------------------------------------------
# Rapportens S4-delar
# ---------------------------------------------------------------------------


def email_tiles(utskick):
    """Mejlens rutor som S3 (Levererade, Klick, Öppnat (indikation) bara
    när öppningar spårades, Studsar, Avregistreringar), nu med länkar till
    mottagarna (?kanal=e-post), och Klagomål när det finns några. Tom lista
    utan e-postmottagare."""
    from .utskick import _group, _pct_text

    numbers = reports.email_numbers(utskick) if utskick.has_email else {}
    if not numbers:
        return []

    def tile(label, value, note, visa):
        return {"label": label, "value": value, "note": note, "visa": visa, "kanal": "e-post"}

    tiles = [
        tile("Levererade", numbers["delivered"], _pct_text(numbers["delivered_pct"]), "levererade"),
        tile(
            "Klick",
            numbers["clicked"],
            f"{_pct_text(numbers['click_pct'])} av levererade"
            if numbers["click_pct"] is not None
            else "",
            "klickade",
        ),
    ]
    if utskick.open_tracking:
        tiles.append(
            tile(
                "Öppnat (indikation)",
                numbers["opened"],
                "Bara hos dem som sagt ja till spårningen",
                "oppnade",
            )
        )
    tiles.append(tile("Studsar", numbers["bounced"], _pct_text(numbers["bounced_pct"]), "studsade"))
    if numbers["complained"]:
        tiles.append(
            tile("Klagomål", numbers["complained"], "Markerade mejlet som skräppost", "klagomal")
        )
    tiles.append(
        tile(
            "Avregistreringar",
            numbers["unsubscribed"],
            f"{_group(numbers['complained'])} klagomål" if numbers["complained"] else "",
            "stopp",
        )
    )
    return tiles


def _read_only(request):
    return bool(getattr(getattr(request, "flamingo", None), "read_only", False))


def report_context(request, account, utskick, numbers, now):
    """Det rapportens mall läser i S4 (bara för ett utskick som börjat gå):
    full (reports.full), email_tiles med länkar, knapparna Följ upp och
    Exportera, och chipsen över mottagartabellen."""
    if utskick.status in (Utskick.Status.DRAFT, Utskick.Status.SCHEDULED):
        return {"full": None}
    full = reports.full(utskick, now, numbers=numbers)
    context = {
        "full": full,
        # Utan länkar kan ingen klicka (full ger då follow_up 0).
        "can_follow_up": full["has_links"] and full["follow_up"] > 0 and not _read_only(request),
        "can_export": full["can_export"],
        "table_views": [(key, reports.VIEWS[key]) for key in TABLE_VIEWS],
        "show_channel": len(full["channels"]) > 1,
    }
    if utskick.has_email:
        context["email_tiles"] = email_tiles(utskick)
    return context


# ---------------------------------------------------------------------------
# Följ upp de som inte klickade
# ---------------------------------------------------------------------------


def _follow_up_segment(account, utskick, user, now):
    """Segmentet med uppföljningens regler: ett befintligt med exakt samma
    regler, annars ett nytt (segments.create_follow_up)."""
    rules = segments.follow_up_rules(utskick)
    existing = Segment.objects.filter(account=account, rules=rules).order_by("-pk").first()
    if existing is not None:
        return existing
    return segments.create_follow_up(utskick, user=user, now=now)


def _new_draft(request, account, utskick, segment, user):
    """Ett utkast med segmentet som mottagare, med samma början som Nytt
    utskick (app_views.utskick.utskick_new)."""
    from .utskick import initial_mode

    chosen = audience.empty()
    chosen["segments"] = [segment.pk]
    return Utskick.objects.create(
        account=account,
        name=FOLLOW_UP_NAME.format(name=utskick.name)[:120],
        purpose=REKLAM,
        channel_mode=initial_mode(account),
        sms_sender_kind=Utskick.SenderKind.REPLY,
        send_mode=Utskick.SendMode.NOW,
        audience=chosen,
        created_by=user,
        open_tracking=bool(getattr(request.utskick_settings, "open_tracking", False)),
        sender_domain=email_domains.verified_for(account),
    )


@utskick_view
@require_POST
def utskick_follow_up(request, account, pk):
    """ "Följ upp de som inte klickade" (I.8): segmentet och ett utkast med
    det som mottagare, sedan guidens första steg."""
    from .utskick import _step_url

    utskick = owned(Utskick, account, pk)
    back = redirect(reverse("flamingo:app_utskick", args=[utskick.pk]))
    if not reports.follow_up_count(utskick):
        messages.info(request, NOBODY_TEXT)
        return back
    now = timezone.now()
    user = request.user if request.user.is_authenticated else None
    try:
        segment = _follow_up_segment(account, utskick, user, now)
    except segments.SegmentError as exc:
        messages.error(request, exc.errors[0] if exc.errors else FOLLOW_UP_FAILED_TEXT)
        return back
    draft = (
        Utskick.objects.listed()
        .filter(account=account, status=Utskick.Status.DRAFT, audience__segments=[segment.pk])
        .order_by("-pk")
        .first()
    )
    if draft is not None:
        messages.info(request, FOLLOW_UP_AGAIN_TEXT)
        return redirect(_step_url(draft, "mottagare"))
    draft = _new_draft(request, account, utskick, segment, user)
    actor = actor_for(request)
    logger.info(
        "Utskick: uppföljning av %s, segment %s, utkast %s, användare %s%s",
        utskick.pk,
        segment.pk,
        draft.pk,
        getattr(user, "pk", None),
        " (ADX åt kunden)" if actor.staff else "",
    )
    messages.success(request, FOLLOW_UP_TEXT.format(segment=segment.name))
    return redirect(_step_url(draft, "mottagare"))


# ---------------------------------------------------------------------------
# Exportera
# ---------------------------------------------------------------------------


def _no_rows_text(utskick):
    return NO_ROWS_TEXT if utskick.started_at else NOT_STARTED_TEXT


def _render_export(request, account, utskick, count):
    from .contacts import _export_state

    context = {
        "utskick": utskick,
        "ut_nav": None,
        "count": count,
        "no_rows_text": _no_rows_text(utskick),
        "back_url": reverse("flamingo:app_utskick", args=[utskick.pk]),
    }
    context.update(_export_state(account))
    return render_utskick(request, "flamingo/app/utskick/export.html", "utskick", context)


def export_csv(utskick):
    """(CSV-text utan BOM, antal rader): rubrikraden och reports.export_rows."""
    from apps.flamingo.exports import safe_cell

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([safe_cell(cell) for cell in reports.EXPORT_HEADER])
    rows = 0
    for line in reports.export_rows(utskick):
        writer.writerow(line)
        rows += 1
    return buffer.getvalue(), rows


@utskick_view
@require_http_methods(["GET", "HEAD", "POST"])
def utskick_export(request, account, pk):
    """GET (och POST utan bekrafta=1): bekräftelsen. POST med bekrafta=1:
    filen, en rad i ExportLog, högst EXPORTS_PER_DAY per konto och dygn."""
    from .contacts import EXPORT_LIMIT_TEXT

    utskick = owned(Utskick, account, pk)
    count = Recipient.objects.filter(utskick=utskick).count()
    if request.method != "POST" or request.POST.get("bekrafta") != "1":
        return _render_export(request, account, utskick, count)
    if not count:
        messages.error(request, _no_rows_text(utskick))
        return redirect(reverse("flamingo:app_utskick", args=[utskick.pk]))
    if not register.reserve_export(account):
        messages.error(request, EXPORT_LIMIT_TEXT.format(limit=register.EXPORTS_PER_DAY))
        return _render_export(request, account, utskick, count)
    text, rows = export_csv(utskick)
    actor = actor_for(request)
    register.log_export(account, actor, ExportLog.Kind.UTSKICK, rows)
    logger.info(
        "Utskick: export av mottagarna i %s (%s rader) av användare %s%s",
        utskick.pk,
        rows,
        getattr(actor.user, "pk", None),
        " (ADX åt kunden)" if actor.staff else "",
    )
    day = timezone.localdate().isoformat()
    response = HttpResponse("﻿" + text, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="utskick-{utskick.pk}-{day}.csv"'
    response["Cache-Control"] = "no-store"
    return response
