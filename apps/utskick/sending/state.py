"""
Utskickets läge (README B.2 tillståndsmaskinen, D.3 förkontrollerna, D.8,
I.4, I.5). Kontraktets "sending.transition" bor här: ingen annan skriver
Utskick.status, och varje skrivning är en villkorlig UPDATE ... WHERE
status IN (förväntat), så två processer aldrig skriver över varandra (som
offertsystemet).

    transition(utskick, to, reason="", *, expected=None, now=None, fields=None) -> bool
    issue_nonce(utskick) -> str          engångsvärdet i Granska-formuläret
    confirm(utskick, *, actor, nonce, summary, send_now=False, now=None) -> Result
    unconfirm(utskick, now=None) -> bool en ändring gör ett schemalagt till utkast
    reopen(utskick, *, actor, now=None) -> bool
                                         ett pausat som ska bekräftas igen och inte
                                         är fryst blir ett utkast (kunden ändrar)
    pause(utskick, reason, *, actor=None, note="", now=None) -> bool
    resume(utskick, *, actor, now=None) -> Result   förkontrollerna (D.3) igen
    cancel(utskick, *, actor, now=None) -> bool     köade mottagare -> cancelled
    pause_account(account, reason, now=None, *, statuses=None) -> int
    prechecks(utskick, now=None, *, reconfirmed=False) -> Verdict
    content_problems(utskick) -> list[str]   länkarna och informationens regler igen

Tillståndsmaskinen (B.2):

    draft --confirm--> scheduled --due (ticken)--> freezing --fryst--> sending --klart--> sent
    scheduled --edit (unconfirm)--> draft
    scheduled --länkarna eller informationen stoppar (ticken)--> paused (content)
    paused (late, content, account_disabled; inget fryst) --reopen--> draft
    freezing|sending --> paused_cap | paused_health | paused --resume--> freezing|sending
    draft|scheduled|freezing|paused_* --cancel--> cancelled
    scheduled|freezing|sending --kontot av, Flamingo av, kunden inaktiv--> paused (account_disabled)
    scheduled --mer än 3 h sent eller ett senare svenskt datum--> paused (late)

En paus med late, audience_grew, account_disabled eller content kräver en
ny bekräftelse (confirm igen, med ny nonce; Granska skickar då nu om tiden
har passerat). Andra pauser fortsätter med
resume, som kör förkontrollerna igen. Byrån i kundvyn agerar på riktigt;
bekräftelsen sparar vem (confirmed_by, confirmed_as_staff) och varje
sändande åtgärd loggas med användarens pk (D12, I.4). Kunden mejlas aldrig.

Result: .ok, .error (svensk text för vyn), .utskick. Verdict: .state,
.reason, .text och .estimate (sms, delar och kostnaden mot taket).
"""

import hmac
import logging
import secrets
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.sms import pricing

from .. import audience
from ..models import CHANNEL_EMAIL, CHANNEL_SMS, Recipient, Utskick
from . import checks

logger = logging.getLogger(__name__)

S = Utskick.Status
R = Utskick.PauseReason

PAUSED_STATES = tuple(Utskick.PAUSED_STATES)
#: Varifrån varje läge får nås (B.2). Pauserna får byta orsak sinsemellan
#: (en byråpaus över en kundpaus, taket efter en fortsättning).
SOURCES = {
    S.DRAFT: (S.SCHEDULED,),
    S.SCHEDULED: (S.DRAFT, S.PAUSED),
    S.FREEZING: (S.SCHEDULED, *PAUSED_STATES),
    S.SENDING: (S.FREEZING, *PAUSED_STATES),
    S.PAUSED: (S.SCHEDULED, S.FREEZING, S.SENDING, *PAUSED_STATES),
    S.PAUSED_CAP: (S.FREEZING, S.SENDING, *PAUSED_STATES),
    S.PAUSED_HEALTH: (S.FREEZING, S.SENDING, *PAUSED_STATES),
    S.SENT: (S.SENDING,),
    S.CANCELLED: (S.DRAFT, S.SCHEDULED, S.FREEZING, *PAUSED_STATES),
}
#: Pauser som räknas mot taket eller hälsan (I.5).
CAP_REASONS = (R.SMS_COST_CAP, R.ADX_MAIL_CAP)
HEALTH_REASONS = (R.BOUNCES, R.COMPLAINTS, R.STOPS, R.ACCOUNT_HEALTH)
#: Bara byrån fortsätter efter de här (I.5): ADX går igenom utskicket först.
STAFF_ONLY = (R.COMPLAINTS, R.STOPS, R.STAFF, R.ACCOUNT_HEALTH)
#: Mottagarna har blivit fler än så här sedan bekräftelsen: ny bekräftelse (D.3).
GROWTH_LIMIT = 0.20
#: Pris per sms-del när historiken saknas: 46elks pris till Sverige som
#: provkörningen gav 2026-10-03 (52 öre). Taket i apps/sms prövar ändå varje
#: sms; det här är bara förkontrollen.
ESTIMATE_PART_COST = 5200
#: En schemalagd tid får ha passerat med så här mycket när den bekräftas.
PAST_GRACE = timedelta(minutes=5)

NOT_CONFIRMABLE_TEXT = "Utskicket kan inte bekräftas i det här läget."
STALE_TEXT = "Sidan har hunnit bli gammal. Granska utskicket och bekräfta igen."
NO_AUDIENCE_TEXT = "Välj mottagare först."
NO_BODY_TEXT = "Skriv sms:et först."
NO_TIME_TEXT = "Välj när utskicket ska skickas."
PAST_TEXT = "Tiden har redan passerat. Välj en ny tid eller Skicka nu."
EMAIL_OFF_TEXT = "E-post är inte påslaget än."
SMS_DISABLED_TEXT = "Sms är inte aktiverat för dig. Be ADX slå på det."
ACCOUNT_DISABLED_TEXT = "Utskick var avstängt för kontot. Granska och bekräfta igen."
RECONFIRM_TEXT = "Granska och bekräfta utskicket igen."
STAFF_RESUMES_TEXT = "ADX går igenom utskicket innan det kan fortsätta."
NOT_PAUSED_TEXT = "Utskicket är inte pausat."


@dataclass
class Result:
    ok: bool = True
    error: str = ""
    utskick: Utskick | None = None


@dataclass
class Verdict:
    """Förkontrollernas utfall: läget utskicket ska till, orsaken för en
    paus och texten för vyn. estimate: {"sms", "parts", "cost", "remaining",
    "cap"} (tiotusendels krona) när sms-kostnaden prövades."""

    state: str = S.SENDING
    reason: str = ""
    text: str = ""
    estimate: dict = field(default_factory=dict)


def _now(now):
    return now or timezone.now()


def pause_state_for(reason):
    if reason in CAP_REASONS:
        return S.PAUSED_CAP
    if reason in HEALTH_REASONS:
        return S.PAUSED_HEALTH
    return S.PAUSED


def _actor_note(actor, now, **extra):
    data = {
        "by": getattr(actor, "label", "") or "",
        "user": getattr(getattr(actor, "user", None), "pk", None),
        "staff": bool(getattr(actor, "staff", False)),
        "at": now.isoformat(),
    }
    data.update(extra)
    return data


def _log(action, utskick, actor):
    logger.info(
        "Utskick %s: %s av användare %s (byrån: %s)",
        utskick.pk,
        action,
        getattr(getattr(actor, "user", None), "pk", None),
        bool(getattr(actor, "staff", False)),
    )


# ---------------------------------------------------------------------------
# Övergången
# ---------------------------------------------------------------------------


def transition(utskick, to, reason="", *, expected=None, now=None, fields=None):
    """Flytta utskicket till läget to om det står i ett av expected (standard:
    SOURCES[to]). En villkorlig UPDATE: False när någon annan hann före.
    reason sparas för pauser (pause_reason), annars töms den. fields är
    fler kolumner att skriva i samma UPDATE. Instansen uppdateras."""
    now = _now(now)
    expected = tuple(expected) if expected else SOURCES[to]
    values = {
        "status": to,
        "pause_reason": reason if to in PAUSED_STATES else "",
        "status_changed_at": now,
        "updated_at": now,
    }
    if to == S.SENDING:
        values["started_at"] = Coalesce("started_at", now)
    if to in Utskick.FINISHED:
        values["finished_at"] = now
    values.update(fields or {})
    changed = Utskick.objects.filter(pk=utskick.pk, status__in=expected).update(**values)
    if changed:
        fresh = (
            Utskick.objects.filter(pk=utskick.pk)
            .values("status", "pause_reason", "status_changed_at", "started_at", "finished_at")
            .first()
        )
        for name, value in (fresh or {}).items():
            setattr(utskick, name, value)
        for name, value in (fields or {}).items():
            if not hasattr(value, "resolve_expression"):
                setattr(utskick, name, value)
    return bool(changed)


def _lock(utskick):
    return Utskick.objects.select_for_update().select_related("account").get(pk=utskick.pk)


def _sync(target, source):
    for name in (
        "status",
        "pause_reason",
        "status_changed_at",
        "scheduled_at",
        "send_mode",
        "confirmed_at",
        "confirmed_by_id",
        "confirmed_as_staff",
        "confirm_summary",
        "confirm_nonce",
        "started_at",
        "finished_at",
        "stats",
        "frozen_counts",
    ):
        setattr(target, name, getattr(source, name))


# ---------------------------------------------------------------------------
# Förkontrollerna (D.3 steg 4)
# ---------------------------------------------------------------------------


def _sms_parts(utskick, queued):
    """Sms-delarna för de köade mottagarna, uppskattade med composer.preview
    (den vanliga texten och de längsta namnen). Faller tillbaka på texten
    själv om förhandsvisningen inte går att göra."""
    from apps.sms import encoding

    try:
        from .. import composer

        preview = composer.preview(utskick)
        parts = int(preview.get("parts") or 1)
        longest = int(preview.get("longest_parts") or parts)
        longest_count = min(int(preview.get("longest_count") or 0), queued)
        return (queued - longest_count) * parts + longest_count * longest
    except Exception:  # noqa: BLE001 - förkontrollen får aldrig fälla frysningen
        logger.exception(
            "Utskick %s: förhandsvisningen gick inte, delarna räknas på texten", utskick.pk
        )
        return queued * max(1, encoding.analyse(utskick.sms_body or " ").parts)


def cost_estimate(utskick, queued_sms, now=None):
    """{"sms", "parts", "cost", "remaining", "cap"} för de köade sms:en, eller
    {} när kunden saknar SmsAccount."""
    from .sms_wrapper import sms_account_for

    sms_account = sms_account_for(utskick.account)
    if sms_account is None or not queued_sms:
        return {}
    parts = _sms_parts(utskick, queued_sms)
    per_part = pricing.recent_part_cost("SE", now) or ESTIMATE_PART_COST
    cost = parts * per_part + pricing.markup_for(sms_account, parts)
    usage = pricing.usage(sms_account, now)
    return {
        "sms": queued_sms,
        "parts": parts,
        "cost": int(cost),
        "remaining": int(usage["remaining"]),
        "cap": int(usage["cap"]),
    }


def cap_text(estimate):
    return (
        f"Pausat vid taket: {estimate['sms']} sms kostar cirka "
        f"{pricing.kr_text(estimate['cost'], 0)} kr och "
        f"{pricing.kr_text(estimate['remaining'], 0)} kr är kvar av taket "
        f"{pricing.kr_text(estimate['cap'], 0)} kr."
    )


def queued_counts(utskick):
    rows = (
        Recipient.objects.filter(utskick=utskick, status=Recipient.Status.QUEUED)
        .values("channel")
        .annotate(n=Count("pk"))
        .values_list("channel", "n")
    )
    counts = {CHANNEL_SMS: 0, CHANNEL_EMAIL: 0}
    for channel, n in rows:
        counts[channel] = int(n or 0)
    return counts


def content_problems(utskick):
    """Länkarna (E.8) och reglerna för information (H.5) prövade igen efter
    bekräftelsen: byrån kan ha nekat eller inte hunnit godkänna en värd,
    en webbplats kan ha slutat vara kundens, och byråns undantag gäller bara
    texten det gavs för. Samma texter som i Granska."""
    from .. import links

    return [*links.link_problems(utskick), *checks.information_problems(utskick)]


def prechecks(utskick, now=None, *, reconfirmed=False):
    """Förkontrollerna innan ett fryst utskick skickas, och när ett pausat
    fortsätter (D.3 steg 4): kontot, länkarna och informationens regler
    (content_problems: pausar med content), e-posten, att urvalet inte växt
    mer än 20 % sedan bekräftelsen (inte efter en ny bekräftelse) och
    sms-kostnaden mot taket. Demokontot prövas bara mot kontot och
    innehållet (det skickar aldrig)."""
    now = _now(now)
    account = utskick.account
    why = checks.sendable(account)
    if why == R.BLOCKED:
        return Verdict(S.PAUSED, R.BLOCKED, checks.BLOCKED_TEXT)
    if why:
        return Verdict(S.PAUSED, R.ACCOUNT_DISABLED, ACCOUNT_DISABLED_TEXT)
    problems = content_problems(utskick)
    if problems:
        logger.warning("Utskick %s: innehållet stoppar sändningen (content)", utskick.pk)
        return Verdict(S.PAUSED, R.CONTENT, problems[0])
    counts = queued_counts(utskick)
    if account.is_demo:
        return Verdict(S.SENDING)
    if counts[CHANNEL_EMAIL] and not email_live():
        return Verdict(S.PAUSED, R.EMAIL_DISABLED, EMAIL_OFF_TEXT)
    if not reconfirmed:
        confirmed = audience.confirmed_total(utskick.confirm_summary, utskick.channel_mode)
        frozen = counts[CHANNEL_SMS] + counts[CHANNEL_EMAIL]
        if confirmed is not None and frozen > confirmed * (1 + GROWTH_LIMIT):
            return Verdict(
                S.PAUSED,
                R.AUDIENCE_GREW,
                f"Mottagarna har blivit fler sedan du bekräftade: {frozen} i stället för "
                f"{confirmed}. Granska och bekräfta igen.",
            )
    if counts[CHANNEL_SMS]:
        from .sms_wrapper import sms_account_for

        sms_account = sms_account_for(account)
        if sms_account is None or not sms_account.is_enabled:
            return Verdict(S.PAUSED, R.SMS_DISABLED, SMS_DISABLED_TEXT)
        estimate = cost_estimate(utskick, counts[CHANNEL_SMS], now)
        if estimate and estimate["cost"] > estimate["remaining"]:
            return Verdict(S.PAUSED_CAP, R.SMS_COST_CAP, cap_text(estimate), estimate)
        return Verdict(S.SENDING, estimate=estimate)
    return Verdict(S.SENDING)


def email_live():
    """E-postutskick kan gå (D.8): UTSKICK_EMAIL_LIVE och byråns brytare."""
    return bool(getattr(settings, "UTSKICK_EMAIL_LIVE", False)) and bool(
        checks.switch().email_enabled
    )


def _store_estimate(utskick, verdict):
    if not verdict.estimate:
        return
    counts = dict(utskick.frozen_counts or {})
    counts["estimate"] = verdict.estimate
    Utskick.objects.filter(pk=utskick.pk).update(frozen_counts=counts)
    utskick.frozen_counts = counts


def apply_prechecks(utskick, now=None, *, expected, reconfirmed=False):
    """Kör förkontrollerna och flytta utskicket dit de säger. Returnerar
    (Verdict, flyttat)."""
    now = _now(now)
    verdict = prechecks(utskick, now, reconfirmed=reconfirmed)
    _store_estimate(utskick, verdict)
    moved = transition(utskick, verdict.state, verdict.reason, expected=expected, now=now)
    return verdict, moved


# ---------------------------------------------------------------------------
# Bekräftelsen (D12, I.4, I.6)
# ---------------------------------------------------------------------------


def issue_nonce(utskick):
    """Ett nytt engångsvärde för Granska-formuläret (sparas på utskicket).
    En bekräftelse med ett annat värde, eller samma två gånger, nekas."""
    nonce = secrets.token_hex(16)
    Utskick.objects.filter(pk=utskick.pk).update(confirm_nonce=nonce)
    utskick.confirm_nonce = nonce
    return nonce


def uses_sms(utskick):
    return utskick.channel_mode != Utskick.ChannelMode.EMAIL_ONLY


def uses_email(utskick):
    return utskick.channel_mode != Utskick.ChannelMode.SMS_ONLY


def confirm(utskick, *, actor, nonce, summary, send_now=False, now=None):
    """Granskas bekräftelse. Från utkast, eller från en paus som kräver ny
    bekräftelse (late, audience_grew, account_disabled). summary är exakt
    Granskas siffror (sparas i confirm_summary; audience.count-formen eller
    {"sms", "email", ...}). send_now: Skicka nu (scheduled_at = nu).

    Byrån i kundvyn: vyn kräver kryssrutan, här sparas confirmed_by och
    confirmed_as_staff. Ett fryst utskick (pausat efter frysningen) går
    direkt till förkontrollerna och sending; annars till scheduled."""
    now = _now(now)
    with transaction.atomic():
        row = _lock(utskick)
        reconfirm = row.status == S.PAUSED and row.pause_reason in Utskick.RECONFIRM_REASONS
        if row.status != S.DRAFT and not reconfirm:
            return Result(False, NOT_CONFIRMABLE_TEXT, row)
        if (
            not nonce
            or not row.confirm_nonce
            or not hmac.compare_digest(str(row.confirm_nonce), str(nonce))
        ):
            return Result(False, STALE_TEXT, row)
        account = row.account
        why = checks.sendable(account)
        if why == R.BLOCKED:
            return Result(False, checks.BLOCKED_TEXT, row)
        if why:
            return Result(False, checks.DISABLED_TEXT, row)
        if uses_sms(row) and not account.is_demo and not checks.sms_ready():
            return Result(False, checks.SMS_OFF_TEXT, row)
        if uses_email(row) and not account.is_demo and not email_live():
            return Result(False, EMAIL_OFF_TEXT, row)
        frozen = row.frozen_at is not None
        if not frozen and audience.is_empty(row):
            return Result(False, NO_AUDIENCE_TEXT, row)
        if uses_sms(row) and not (row.sms_body or "").strip():
            return Result(False, NO_BODY_TEXT, row)
        if send_now:
            row.send_mode = Utskick.SendMode.NOW
            row.scheduled_at = now
        elif not row.scheduled_at:
            return Result(False, NO_TIME_TEXT, row)
        elif row.scheduled_at < now - PAST_GRACE:
            return Result(False, PAST_TEXT, row)
        user = getattr(actor, "user", None)
        row.confirm_summary = summary if isinstance(summary, dict) else {}
        row.confirmed_at = now
        row.confirmed_by = user if getattr(user, "pk", None) else None
        row.confirmed_as_staff = bool(getattr(actor, "staff", False))
        row.confirm_nonce = ""
        row.save(
            update_fields=[
                "send_mode",
                "scheduled_at",
                "confirm_summary",
                "confirmed_at",
                "confirmed_by",
                "confirmed_as_staff",
                "confirm_nonce",
                "updated_at",
            ]
        )
        if frozen:
            verdict, moved = apply_prechecks(row, now, expected=(S.PAUSED,), reconfirmed=True)
            error = verdict.text if verdict.state != S.SENDING else ""
        elif row.freeze_cursor:
            moved = transition(row, S.FREEZING, expected=(S.PAUSED,), now=now)
            error = ""
        else:
            moved = transition(row, S.SCHEDULED, expected=(S.DRAFT, S.PAUSED), now=now)
            error = ""
    _sync(utskick, row)
    _log("bekräftat" + (" (skicka nu)" if send_now else ""), row, actor)
    if not moved:
        return Result(False, NOT_CONFIRMABLE_TEXT, utskick)
    return Result(not error, error, utskick)


def unconfirm(utskick, now=None):
    """En ändring av ett schemalagt utskick gör det till utkast och tömmer
    bekräftelsen (B.2). False när det inte var schemalagt."""
    now = _now(now)
    changed = transition(
        utskick,
        S.DRAFT,
        expected=(S.SCHEDULED,),
        now=now,
        fields={
            "confirmed_at": None,
            "confirmed_by": None,
            "confirmed_as_staff": False,
            "confirm_summary": {},
            "confirm_nonce": "",
        },
    )
    return changed


def can_reopen(utskick):
    """Kan kunden ändra ett pausat utskick (reopen)? Bara en paus som ändå
    kräver en ny bekräftelse, och bara innan något har frysts."""
    return (
        utskick.status == S.PAUSED
        and utskick.pause_reason in Utskick.RECONFIRM_REASONS
        and utskick.frozen_at is None
        and not utskick.freeze_cursor
    )


def reopen(utskick, *, actor, now=None):
    """Ett pausat utskick som ska bekräftas igen (sent, innehållet,
    avstängt konto) och inte har frysts blir ett utkast, så att kunden kan
    välja en ny tid eller byta en länk. Bekräftelsen töms. False när det
    inte går (fryst, en annan paus, någon hann före)."""
    now = _now(now)
    with transaction.atomic():
        row = _lock(utskick)
        if not can_reopen(row):
            return False
        moved = transition(
            row,
            S.DRAFT,
            expected=(S.PAUSED,),
            now=now,
            fields={
                "confirmed_at": None,
                "confirmed_by": None,
                "confirmed_as_staff": False,
                "confirm_summary": {},
                "confirm_nonce": "",
            },
        )
    _sync(utskick, row)
    if moved:
        _log("öppnat för ändring", row, actor)
    return moved


# ---------------------------------------------------------------------------
# Paus, fortsätt och avbryt (I.5)
# ---------------------------------------------------------------------------


def _note_stats(utskick, key, value):
    stats = dict(utskick.stats or {})
    stats[key] = value
    Utskick.objects.filter(pk=utskick.pk).update(stats=stats)
    utskick.stats = stats


def pause(utskick, reason, *, actor=None, note="", now=None):
    """Pausa ett utskick. reason är Utskick.PauseReason (customer från
    kunden, staff från byrån, eller motorns orsaker). En kund pausar bara
    schemalagda och pågående; byrån och motorn även ett pausat (ny orsak).
    Byråns anteckning sparas i stats["pause"]. Mottagare som slingan redan
    tagit går tillbaka i kön (sending/checks)."""
    now = _now(now)
    if reason not in R.values:
        raise ValueError(f"Okänd orsak: {reason!r}")
    expected = Utskick.ACTIVE
    if reason != R.CUSTOMER:
        expected = (*Utskick.ACTIVE, *PAUSED_STATES)
    with transaction.atomic():
        row = _lock(utskick)
        moved = transition(row, pause_state_for(reason), reason, expected=expected, now=now)
        if moved and (actor is not None or note):
            _note_stats(row, "pause", _actor_note(actor, now, reason=reason, note=note[:200]))
    _sync(utskick, row)
    if moved and actor is not None:
        _log(f"pausat ({reason})", row, actor)
    return moved


def resume(utskick, *, actor, now=None):
    """Fortsätt ett pausat utskick: förkontrollerna körs igen (I.5). En
    paus som kräver ny bekräftelse ger RECONFIRM_TEXT; klagomål,
    avregistreringar och byråns paus släpper bara byrån."""
    now = _now(now)
    with transaction.atomic():
        row = _lock(utskick)
        if row.status not in PAUSED_STATES:
            return Result(False, NOT_PAUSED_TEXT, row)
        if row.pause_reason in Utskick.RECONFIRM_REASONS:
            return Result(False, RECONFIRM_TEXT, row)
        if row.pause_reason in STAFF_ONLY and not getattr(actor, "staff", False):
            return Result(False, STAFF_RESUMES_TEXT, row)
        if row.frozen_at is not None:
            verdict, moved = apply_prechecks(row, now, expected=(row.status,))
            error = "" if verdict.state == S.SENDING else verdict.text
        else:
            why = checks.sendable(row.account)
            if why:
                text = checks.BLOCKED_TEXT if why == R.BLOCKED else ACCOUNT_DISABLED_TEXT
                return Result(False, text, row)
            target = S.FREEZING if row.freeze_cursor else S.SCHEDULED
            moved = transition(row, target, expected=(row.status,), now=now)
            error = ""
        if moved:
            _note_stats(row, "resumed", _actor_note(actor, now))
    _sync(utskick, row)
    _log("fortsatt", row, actor)
    if not moved:
        return Result(False, NOT_PAUSED_TEXT, utskick)
    return Result(not error, error, utskick)


def cancel(utskick, *, actor, now=None):
    """Avbryt: köade mottagare blir cancelled. Ett pågående utskick pausas
    först (slingan tar då inga fler)."""
    now = _now(now)
    with transaction.atomic():
        row = _lock(utskick)
        moved = transition(row, S.CANCELLED, now=now)
        if moved:
            Recipient.objects.filter(utskick=row, status=Recipient.Status.QUEUED).update(
                status=Recipient.Status.CANCELLED, not_before=None
            )
            _note_stats(row, "cancelled", _actor_note(actor, now))
    _sync(utskick, row)
    if moved:
        _log("avbrutet", row, actor)
    return moved


def pause_account(account, reason, now=None, *, statuses=None):
    """D.8: utskick av, Flamingo av, kunden inaktiv (account_disabled) eller
    sending_blocked (blocked): kontots schemalagda, frysande och pågående
    utskick pausas i en UPDATE. Taket (sms_cost_cap) pausar kontots
    pågående (statuses=(sending,)). Returnerar antalet. Att slå på utskick
    igen fortsätter ingenting av sig självt."""
    now = _now(now)
    statuses = tuple(statuses) if statuses else Utskick.ACTIVE
    state = pause_state_for(reason)
    with transaction.atomic():
        changed = Utskick.objects.filter(account=account, status__in=statuses).update(
            status=state,
            pause_reason=reason,
            status_changed_at=now,
            updated_at=now,
        )
    if changed:
        logger.warning("Konto %s: %s utskick pausade (%s)", account.pk, changed, reason)
    return changed
