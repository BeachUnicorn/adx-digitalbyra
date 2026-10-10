"""
Leveranshälsa (README I.11, D.9, I.5).

    utskick_health   utskick/halsa/   app_utskick_health   GET

Sidan (flamingo/app/utskick/health.html, fliken "health"): kontots
hälsospärr ("E-postutskick är spärrade tills ADX har gått igenom
studsarna."), I.5-bannern för varje utskick som är pausat för hälsan,
väntan på provet och dygnstaket, rutorna Studsar 30 d, Klagomål 30 d,
Studsade adresser ("Får aldrig mejl igen") och Domän ("SPF, DKIM och DMARC
OK" eller "ADX-domänen"), och händelsetabellen Adress (maskerad), Händelse,
Vad vi gjorde, med data-label på varje cell. Siffrorna kommer från
sending.health och email.domains; inget här ändrar något. Fliken finns för
varje konto med utskick, också innan e-posten är påslagen (sidan säger
det).
"""

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from django.views.decorators.http import require_safe

from ..access import utskick_view
from ..email import domains
from ..inbound import events
from ..models import CHANNEL_EMAIL, Contact, Recipient, Utskick
from ..normalize import mask_email
from ..sending import health, state
from . import render_utskick

#: Rader i händelsetabellen.
ROWS = 50

RS = Recipient.Status
R = Utskick.PauseReason

#: (händelse, ton, vad vi gjorde) per slag av rad.
EVENT_ROWS = {
    "bounced": ("Studs: finns inte", "stop", "Markerad som studsad, inga fler mejl"),
    "soft": ("Tillfällig studs", "warn", "Kom inte fram den här gången. Efter 5 i rad: studsad"),
    "complained": (
        "Markerade som skräppost",
        "stop",
        "Avregistrerad från e-post direkt",
    ),
    "suppressed": (
        "Spärrad hos e-posttjänsten",
        "muted",
        "Inget skickades. Adressen är spärrad för en annan avsändare.",
    ),
}
HEALTH_REASONS = (R.BOUNCES, R.COMPLAINTS, R.ACCOUNT_HEALTH)


def pct_text(fraction):
    """En andel som text: "0,8 %", och små andelar med två decimaler
    ("0,02 %") så att klagomålen inte ser ut som noll."""
    from ..templatetags.utskick_tags import procent

    number = float(fraction or 0) * 100
    if 0 < number < 0.1:
        return f"{number:.2f}".replace(".", ",") + "\u00a0%"
    return procent(round(number, 1))


def _kind(recipient):
    if recipient.status == RS.BOUNCED:
        return "bounced"
    if recipient.status == RS.COMPLAINED:
        return "complained"
    if recipient.skip_reason == Recipient.SkipReason.SES_SUPPRESSED:
        return "suppressed"
    return "soft"


def event_rows(account, now):
    """Studsar, tillfälliga studsar, klagomål och SES-spärrar de senaste 30
    dagarna, senaste först (högst ROWS)."""
    since = now - timedelta(days=health.ACCOUNT_DAYS)
    rows = (
        Recipient.objects.filter(
            utskick__account=account, channel=CHANNEL_EMAIL, sent_at__gte=since
        )
        .filter(
            Q(status__in=(RS.BOUNCED, RS.COMPLAINED))
            | Q(status=RS.FAILED, error=events.SOFT_TEXT)
            | Q(status=RS.FAILED, skip_reason=Recipient.SkipReason.SES_SUPPRESSED)
        )
        .select_related("utskick")
        .order_by("-sent_at", "-pk")[:ROWS]
    )
    out = []
    for recipient in rows:
        label, tone, done = EVENT_ROWS[_kind(recipient)]
        out.append(
            {
                "address": mask_email(recipient.address) if recipient.address else "",
                "label": label,
                "tone": tone,
                "done": done,
                "utskick": recipient.utskick,
                "at": recipient.sent_at,
            }
        )
    return out


def _banners(account, now):
    """Utskick som är pausade för hälsan, med I.5-texten."""
    banners = []
    paused = Utskick.objects.listed().filter(
        account=account, status=Utskick.Status.PAUSED_HEALTH, pause_reason__in=HEALTH_REASONS
    )
    for utskick in paused.order_by("-status_changed_at")[:10]:
        note = health.shown_note(((utskick.stats or {}).get("pause") or {}).get("note", ""))
        if not note:
            note = (
                health.BLOCKED_TEXT
                if utskick.pause_reason == R.ACCOUNT_HEALTH
                else health.utskick_health(utskick).text
            )
        banners.append({"utskick": utskick, "text": note or "Utskicket är pausat."})
    return banners


def _waits(account, now):
    """Väntan som inte är en paus (I.5): provet och dygnstaket."""
    waits = []
    held = Utskick.objects.listed().filter(
        account=account, status=Utskick.Status.SENDING, hold_until__gt=now
    )
    for utskick in held.order_by("hold_until")[:10]:
        waits.append({"utskick": utskick, "text": health.wait_probe_text(utskick.hold_until)})
    if health.daily_cap_left(account, now) == 0:
        waiting = (
            Utskick.objects.listed()
            .filter(
                account=account,
                status=Utskick.Status.SENDING,
                recipients__channel=CHANNEL_EMAIL,
                recipients__status=RS.QUEUED,
            )
            .distinct()
        )
        for utskick in waiting.order_by("started_at")[:10]:
            waits.append({"utskick": utskick, "text": health.daily_wait_text(account, now)})
    return waits


@utskick_view
@require_safe
def utskick_health(request, account):
    now = timezone.now()
    verdict = health.account_health(account, now)
    blocked = health.is_blocked(account)
    summary = domains.summary(account, now)
    bounced_contacts = Contact.objects.filter(
        account=account, email_state=Contact.EmailState.BOUNCED
    ).count()
    context = {
        "email_live": state.email_live(),
        "blocked": blocked,
        "blocked_text": health.BLOCKED_TEXT,
        "banners": _banners(account, now),
        "waits": _waits(account, now),
        "verdict": verdict,
        "bounce_text": pct_text(verdict.bounce_rate),
        "complaint_text": pct_text(verdict.complaint_rate),
        "has_outcomes": bool(verdict.outcomes),
        "bounced_contacts": bounced_contacts,
        "domain": summary,
        "rows": event_rows(account, now),
    }
    return render_utskick(request, "flamingo/app/utskick/health.html", "health", context)
