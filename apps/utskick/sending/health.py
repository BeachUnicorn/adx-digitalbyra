"""
Leveranshälsan, provet och dygnstaket för e-post (README D.9, I.5, I.11).
Alla kunder delar ADX:s rykte hos SES i eu-west-1: en kund med en gammal
lista kan skada alla andra, så ett utskick pausas före AWS gränser (5 %
studsar, 0,1 % klagomål) och ett konto spärras när de senaste 30 dagarna
ser dåliga ut.

    Gränserna (vi pausar före AWS):
    BOUNCE_LIMIT = 0.04, BOUNCE_MIN_OUTCOMES = 200
    COMPLAINT_MIN = 2, COMPLAINT_LIMIT = 0.0008
    ACCOUNT_MIN_SENT = 500 (rullande 30 dagar), ACCOUNT_DAYS = 30
    PROBE_SIZE = 200, PROBE_HOLD_MINUTES = 60
    RAMP_DAILY = 2000, RAMP_DAYS = 14
    ADX_ALERT_BOUNCE = 0.025, ADX_ALERT_COMPLAINT = 0.0005

    @dataclass Verdict(ok, reason, bounced, complained, delivered, outcomes, sent, text)
    numbers(rows) -> Verdict        talen ur mottagarnas lägen (utan gränserna)
    utskick_health(utskick, *, probe=False) -> Verdict
                         utskickets mejl; efter byråns "Fortsätt" bara de som skickats
                         sedan dess (stats["resumed"]), så att en fortsättning inte
                         pausas av samma studsar igen
    check_utskick(utskick, now=None) -> str
                         var 50:e sändning och vid varje studs och klagomål:
                         paused_health med bounces eller complaints och ett larm till
                         byrån; "" när allt är bra
    account_health(account, now=None) -> Verdict
                         30 dagar, bara utfall efter UtskickSettings.email_released_at
    check_account(account, now=None) -> bool
                         spärrar kontot (email_blocked_at, email_blocked_reason),
                         pausar dess e-postutskick (account_health) och larmar byrån
    release(account, *, actor, now=None) -> bool
                         "Släpp spärren" (byrån): email_released_at, email_released_by;
                         de pausade utskicken fortsätter byrån själv
    is_blocked(account) -> bool
    probe_needed(utskick, settings_row=None) -> bool
    probe_state(utskick, now=None) -> str
                         "" (inget prov), "sending" (de första PROBE_SIZE), "hold"
                         (hold_until satt), "passed", "failed"; sätter
                         email_probe_passed_at och SenderDomain.probe_passed_at
    daily_cap(account, now=None) -> int | None   dagens tak, None utan tak
    daily_cap_left(account, now=None) -> int | None
                         None utan tak; annars kvar i dag (svensk dag). Över taket
                         väntar utskicket till nästa dag (ingen paus, I.5)
    next_day(now) -> datetime        midnatt i Stockholm efter now
    adx_wide(now=None) -> dict
                         utskick_daily: GetAccount i eu-west-1 till
                         Switchboard.ses_account, ses_max_rate och ses_daily_quota,
                         våra egna 30-dagarstal, larm vid ADX_ALERT_* och när SES
                         inte är friskt
    BLOCKED_TEXT, WAIT_DAILY_TEXT, wait_probe_text(hold_until)

Mottagare med skip_reason ses_suppressed (adressen spärrades hos SES för en
annan avsändare) räknas aldrig. Inga adresser i loggen eller larmen.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.sms.pricing import STOCKHOLM

from .. import alerts
from ..models import (
    CHANNEL_EMAIL,
    Recipient,
    SenderDomain,
    Switchboard,
    Utskick,
    UtskickSettings,
)

logger = logging.getLogger(__name__)

BOUNCE_LIMIT = 0.04
BOUNCE_MIN_OUTCOMES = 200
COMPLAINT_MIN = 2
COMPLAINT_LIMIT = 0.0008
ACCOUNT_MIN_SENT = 500
ACCOUNT_DAYS = 30
PROBE_SIZE = 200
PROBE_HOLD_MINUTES = 60
RAMP_DAILY = 2000
RAMP_DAYS = 14
ADX_ALERT_BOUNCE = 0.025
ADX_ALERT_COMPLAINT = 0.0005
#: Kontrollen per utskick görs efter så här många sändningar (D.9).
CHECK_EVERY = 50
BLOCKED_TEXT = "E-postutskick är spärrade tills ADX har gått igenom studsarna."
WAIT_DAILY_TEXT = (
    "Fortsätter i morgon: nya konton skickar högst 2 000 mejl per dag de första två veckorna"
)
#: Taket som byrån satt (email_daily_cap) är inget nytt-konto-tak.
WAIT_CAP_TEXT = "Fortsätter i morgon: dagens tak för mejl är nått"

RS = Recipient.Status
R = Utskick.PauseReason

#: Lägen som räknas som skickade mot taken: SENT_LIKE och de som slingan
#: reserverat (sending med sent_at, sending/email.py). counted_q() lägger
#: till failed med sent_at.
COUNTED = (*Recipient.SENT_LIKE, RS.SENDING)


def counted_q():
    """Mottagarna som räknas mot ADX-taket, dygnstaket och provet: COUNTED,
    och failed med sent_at (SES tog emot mejlet, men en tillfällig studs,
    SES egen spärrlista eller 24 timmar som oklar gjorde det till failed
    efteråt). Slingans egna fel (ett mejl SES aldrig tog emot) tömmer
    sent_at och räknas inte."""
    return Q(status__in=COUNTED) | Q(status=RS.FAILED, sent_at__isnull=False)


def _pct(fraction):
    from ..templatetags.utskick_tags import procent

    return procent(round(fraction * 100, 2))


@dataclass
class Verdict:
    """Hälsans utfall. ok False betyder att en gräns är passerad (reason
    bounces eller complaints, text är meningen för bannern och larmet)."""

    ok: bool = True
    reason: str = ""
    bounced: int = 0
    complained: int = 0
    delivered: int = 0
    outcomes: int = 0
    sent: int = 0
    text: str = ""

    @property
    def bounce_rate(self):
        return self.bounced / self.outcomes if self.outcomes else 0.0

    @property
    def complaint_rate(self):
        return self.complained / self.delivered if self.delivered else 0.0


def numbers(rows):
    """Verdict med talen (utan bedömning) ur en Recipient-queryset:
    studsade, klagomål, levererade (klagomål räknas som levererade), utfall
    (levererade plus studsade) och skickade (SENT_LIKE)."""
    counts = dict(
        rows.exclude(skip_reason=Recipient.SkipReason.SES_SUPPRESSED)
        .values("status")
        .annotate(n=Count("pk"))
        .values_list("status", "n")
    )
    bounced = int(counts.get(RS.BOUNCED, 0))
    complained = int(counts.get(RS.COMPLAINED, 0))
    delivered = int(counts.get(RS.DELIVERED, 0)) + complained
    sent = sum(int(counts.get(status, 0)) for status in Recipient.SENT_LIKE)
    return Verdict(
        bounced=bounced,
        complained=complained,
        delivered=delivered,
        outcomes=delivered + bounced,
        sent=sent,
    )


def judge(verdict, *, min_outcomes=BOUNCE_MIN_OUTCOMES, scope="utskick"):
    """Gränserna på talen i verdict (D.9). min_outcomes 0 för provet."""
    if verdict.outcomes and verdict.outcomes >= max(1, min_outcomes):
        if verdict.bounce_rate >= BOUNCE_LIMIT:
            verdict.ok = False
            verdict.reason = R.BOUNCES
            first = "av de första" if scope == "utskick" else "av de senaste"
            verdict.text = (
                f"{_pct(verdict.bounce_rate)} {first} {verdict.outcomes} mejlen studsade. "
                "Vi pausar vid 4 %, före AWS gräns på 5 %. ADX har fått ett larm. "
                "De studsade adresserna är redan markerade."
            )
            return verdict
    if (
        verdict.complained >= COMPLAINT_MIN
        and verdict.delivered
        and verdict.complaint_rate >= COMPLAINT_LIMIT
    ):
        verdict.ok = False
        verdict.reason = R.COMPLAINTS
        verdict.text = (
            f"{verdict.complained} mottagare har markerat mejlet som skräppost. "
            "ADX går igenom det innan utskicket kan fortsätta."
        )
    return verdict


# ---------------------------------------------------------------------------
# Per utskick
# ---------------------------------------------------------------------------


def _resumed_at(utskick):
    resumed = (utskick.stats or {}).get("resumed") if isinstance(utskick.stats, dict) else None
    if not isinstance(resumed, dict) or not resumed.get("at"):
        return None
    try:
        return datetime.fromisoformat(str(resumed["at"]))
    except ValueError:
        return None


def utskick_health(utskick, *, probe=False):
    """Utskickets e-posthälsa (D.9). probe=True: provets bedömning, utan
    kravet på 200 utfall (de första 200 mejlen och en timmes väntan)."""
    rows = Recipient.objects.filter(utskick_id=utskick.pk, channel=CHANNEL_EMAIL)
    since = _resumed_at(utskick)
    if since is not None and not probe:
        rows = rows.filter(sent_at__gte=since)
    verdict = numbers(rows)
    return judge(verdict, min_outcomes=0 if probe else BOUNCE_MIN_OUTCOMES)


def check_utskick(utskick, now=None):
    """Pausa utskicket (paused_health) när en gräns är passerad, och larma
    byrån. Returnerar orsaken, eller "" när allt är bra eller utskicket inte
    går att pausa (klart, avbrutet, redan pausat av samma skäl)."""
    from . import state

    now = now or timezone.now()
    verdict = utskick_health(utskick)
    if verdict.ok:
        return ""
    fresh = Utskick.objects.filter(pk=utskick.pk).values("status", "pause_reason").first()
    if fresh is None:
        return ""
    active = (*Utskick.ACTIVE, *Utskick.PAUSED_STATES)
    if fresh["status"] not in active:
        return ""
    if fresh["status"] == Utskick.Status.PAUSED_HEALTH and fresh["pause_reason"] == verdict.reason:
        return verdict.reason
    if state.pause(utskick, verdict.reason, note=verdict.text, now=now):
        logger.warning(
            "Utskick %s: pausat för hälsan (%s, %s studsar, %s klagomål av %s utfall)",
            utskick.pk,
            verdict.reason,
            verdict.bounced,
            verdict.complained,
            verdict.outcomes,
        )
        # Efter commit: händelserna prövas inne i köns transaktion, och inget
        # larm går medan utskickets rad är låst.
        transaction.on_commit(
            lambda: alerts.utskick_paused(utskick, verdict.reason, verdict.text, now=now)
        )
    return verdict.reason


# ---------------------------------------------------------------------------
# Per konto
# ---------------------------------------------------------------------------


def _settings(account):
    return UtskickSettings.objects.filter(account_id=account.pk).first()


def account_health(account, now=None):
    """Kontots e-posthälsa de senaste 30 dagarna (D.9): minst 500 skickade,
    samma gränser som per utskick. Bara mejl skickade efter byråns senaste
    "Släpp spärren" räknas."""
    now = now or timezone.now()
    since = now - timedelta(days=ACCOUNT_DAYS)
    row = _settings(account)
    if row is not None and row.email_released_at and row.email_released_at > since:
        since = row.email_released_at
    rows = Recipient.objects.filter(
        utskick__account_id=account.pk, channel=CHANNEL_EMAIL, sent_at__gte=since
    )
    verdict = numbers(rows)
    if verdict.sent < ACCOUNT_MIN_SENT:
        return verdict
    return judge(verdict, min_outcomes=0, scope="account")


def is_blocked(account):
    """Har kontots e-post spärrats av hälsan (tills byrån släpper den)?"""
    return UtskickSettings.objects.filter(
        account_id=account.pk, email_blocked_at__isnull=False
    ).exists()


def _pause_email_utskick(account, now):
    """Kontots schemalagda, frysande, pågående och pausade e-postutskick
    pausas med account_health. Antalet."""
    from . import state

    paused = 0
    rows = Utskick.objects.filter(
        account_id=account.pk, status__in=(*Utskick.ACTIVE, *Utskick.PAUSED_STATES)
    ).exclude(channel_mode=Utskick.ChannelMode.SMS_ONLY)
    for utskick in rows.select_related("account"):
        if utskick.status == Utskick.Status.PAUSED_HEALTH and utskick.pause_reason in (
            R.ACCOUNT_HEALTH,
            R.COMPLAINTS,
        ):
            continue
        if state.pause(utskick, R.ACCOUNT_HEALTH, note=BLOCKED_TEXT, now=now):
            paused += 1
    return paused


def check_account(account, now=None):
    """Spärra kontots e-post när de senaste 30 dagarna passerar en gräns
    (D.9). True när kontot är spärrat (nu eller sedan tidigare)."""
    now = now or timezone.now()
    if is_blocked(account):
        return True
    verdict = account_health(account, now)
    if verdict.ok:
        return False
    with transaction.atomic():
        blocked = UtskickSettings.objects.filter(
            account_id=account.pk, email_blocked_at__isnull=True
        ).update(
            email_blocked_at=now,
            email_blocked_reason=verdict.reason,
            updated_at=now,
        )
    if not blocked:
        return is_blocked(account)
    paused = _pause_email_utskick(account, now)
    logger.error(
        "Konto %s: e-posten spärrad av hälsan (%s), %s utskick pausade",
        account.pk,
        verdict.reason,
        paused,
    )
    lines = [
        f"Konto {account.pk}: de senaste 30 dagarna passerar gränsen ({verdict.reason}).",
        f"{verdict.bounced} studsar och {verdict.complained} klagomål av "
        f"{verdict.sent} skickade mejl.",
        f"{paused} utskick pausades. Gå igenom listan med kunden och släpp spärren "
        "på /manage/utskick/ (Kunder med e-postspärr) när det är åtgärdat.",
    ]
    transaction.on_commit(
        lambda: alerts.agency(
            "Utskick: e-posten spärrad för en kund",
            lines,
            once=f"email_blocked:{account.pk}",
            window="day",
            now=now,
        )
    )
    return True


def release(account, *, actor, now=None):
    """Byråns "Släpp spärren" (manage:utskick_health_release). Bara utfall
    efter släppet räknas i kontots hälsa. De pausade utskicken står kvar;
    byrån fortsätter dem ett i taget. True om en spärr fanns."""
    now = now or timezone.now()
    user = getattr(actor, "user", actor)
    released = UtskickSettings.objects.filter(
        account_id=account.pk, email_blocked_at__isnull=False
    ).update(
        email_blocked_at=None,
        email_blocked_reason="",
        email_released_at=now,
        email_released_by=user if getattr(user, "pk", None) else None,
        updated_at=now,
    )
    if released:
        logger.warning(
            "Konto %s: e-postspärren släppt av användare %s",
            account.pk,
            getattr(user, "pk", None),
        )
    return bool(released)


# ---------------------------------------------------------------------------
# Provet (D.9)
# ---------------------------------------------------------------------------


def probe_needed(utskick, settings_row=None):
    """Behöver utskicket provet? Ett nytt konto (email_probe_passed_at
    saknas) eller en ny egen domän (SenderDomain.probe_passed_at saknas)."""
    row = settings_row or _settings(utskick.account)
    if row is None or row.email_probe_passed_at is None:
        return True
    if utskick.sender_domain_id:
        passed = (
            SenderDomain.objects.filter(pk=utskick.sender_domain_id)
            .values_list("probe_passed_at", flat=True)
            .first()
        )
        return passed is None
    return False


def probe_count(utskick):
    """Utskickets mejl som räknas mot provet (skickade och reserverade)."""
    return (
        Recipient.objects.filter(utskick_id=utskick.pk, channel=CHANNEL_EMAIL)
        .filter(counted_q(), sent_at__isnull=False)
        .count()
    )


def _pass_probe(utskick, now):
    UtskickSettings.objects.filter(
        account_id=utskick.account_id, email_probe_passed_at__isnull=True
    ).update(email_probe_passed_at=now)
    if utskick.sender_domain_id:
        SenderDomain.objects.filter(
            pk=utskick.sender_domain_id, probe_passed_at__isnull=True
        ).update(probe_passed_at=now)
    Utskick.objects.filter(pk=utskick.pk).update(hold_until=None)
    utskick.hold_until = None
    logger.info("Utskick %s: provet godkänt", utskick.pk)


def probe_state(utskick, now=None):
    """Provet (D.9): ett nytt konto eller en ny domän skickar de första 200
    mejlen, väntar en timme och fortsätter bara om utfallen är under
    gränserna. "" när inget prov behövs, annars sending, hold, passed eller
    failed. Sätter hold_until när de 200 är skickade, och provets
    godkännande på kontot och domänen."""
    now = now or timezone.now()
    if not probe_needed(utskick):
        return ""
    hold = Utskick.objects.filter(pk=utskick.pk).values_list("hold_until", flat=True).first()
    utskick.hold_until = hold
    if hold is None:
        if probe_count(utskick) < PROBE_SIZE:
            return "sending"
        until = now + timedelta(minutes=PROBE_HOLD_MINUTES)
        Utskick.objects.filter(pk=utskick.pk, hold_until__isnull=True).update(hold_until=until)
        utskick.hold_until = (
            Utskick.objects.filter(pk=utskick.pk).values_list("hold_until", flat=True).first()
        )
        logger.info("Utskick %s: provet väntar till %s", utskick.pk, utskick.hold_until)
        return "hold"
    if hold > now:
        return "hold"
    resumed = _resumed_at(utskick_row(utskick))
    if resumed is not None and resumed >= hold and _resumed_by_staff(utskick):
        # Byrån gick igenom provet som inte gick och fortsatte utskicket.
        # Kundens egen Fortsätt räknas aldrig (state.resume nekar den).
        _pass_probe(utskick, now)
        return "passed"
    verdict = utskick_health(utskick, probe=True)
    if not verdict.ok:
        return "failed"
    _pass_probe(utskick, now)
    return "passed"


def _resumed_by_staff(utskick):
    resumed = (utskick.stats or {}).get("resumed") if isinstance(utskick.stats, dict) else None
    return isinstance(resumed, dict) and bool(resumed.get("staff"))


def probe_failed(utskick):
    """Är utskicket pausat för att provet inte gick (studsar eller klagomål
    under provets timme)? Då släpper bara byrån det (D.9)."""
    if utskick.pause_reason not in (R.BOUNCES, R.COMPLAINTS):
        return False
    hold = Utskick.objects.filter(pk=utskick.pk).values_list("hold_until", flat=True).first()
    return hold is not None and probe_needed(utskick)


def utskick_row(utskick):
    """Utskicket med färska stats (byrån kan ha fortsatt det nyss)."""
    stats = Utskick.objects.filter(pk=utskick.pk).values_list("stats", flat=True).first()
    if stats is not None:
        utskick.stats = stats
    return utskick


def wait_probe_text(hold_until):
    """Väntetexten under provet (I.5)."""
    stamp = timezone.localtime(hold_until, STOCKHOLM).strftime("%H.%M")
    return f"Väntar på de första svaren från mottagarnas e-postservrar: fortsätter {stamp}"


# ---------------------------------------------------------------------------
# Dygnstaket (D.9)
# ---------------------------------------------------------------------------


def day_start(now):
    local = timezone.localtime(now, STOCKHOLM)
    return timezone.make_aware(datetime.combine(local.date(), time.min), STOCKHOLM)


def next_day(now):
    """Midnatt i Stockholm efter now (rätt också när sommartiden byts)."""
    local = timezone.localtime(now, STOCKHOLM)
    return timezone.make_aware(
        datetime.combine(local.date() + timedelta(days=1), time.min), STOCKHOLM
    )


def daily_cap(account, now=None, settings_row=None):
    """Dagens tak för kontots e-postutskick: byråns email_daily_cap när den
    är satt, annars RAMP_DAILY de första RAMP_DAYS dagarna efter första
    mejlet (och innan något skickats), annars None."""
    now = now or timezone.now()
    row = settings_row or _settings(account)
    if row is None:
        return RAMP_DAILY
    if row.email_daily_cap:
        return int(row.email_daily_cap)
    first = row.email_first_sent_at
    if first is None or now < first + timedelta(days=RAMP_DAYS):
        return RAMP_DAILY
    return None


def day_count(account, now=None):
    """Kontots mejl från utskick som skickats (eller reserverats) i dag."""
    now = now or timezone.now()
    start = day_start(now)
    return (
        Recipient.objects.filter(
            utskick__account_id=account.pk,
            channel=CHANNEL_EMAIL,
            sent_at__gte=start,
            sent_at__lt=next_day(now),
        )
        .filter(counted_q())
        .count()
    )


def daily_cap_left(account, now=None, settings_row=None):
    """None utan tak, annars hur många mejl kontot får skicka till i dag."""
    now = now or timezone.now()
    cap = daily_cap(account, now, settings_row)
    if cap is None:
        return None
    return max(0, cap - day_count(account, now))


def daily_wait_text(account, now=None):
    row = _settings(account)
    if row is not None and row.email_daily_cap:
        return WAIT_CAP_TEXT
    return WAIT_DAILY_TEXT


# ---------------------------------------------------------------------------
# Hela ADX (utskick_daily)
# ---------------------------------------------------------------------------

#: Delarna av GetAccount som sparas på Switchboard.ses_account (inga hemligheter).
_ACCOUNT_KEYS = (
    "ProductionAccessEnabled",
    "SendingEnabled",
    "EnforcementStatus",
    "DedicatedIpAutoWarmupEnabled",
)


def _account_snapshot(answer, now):
    quota = answer.get("SendQuota") or {}
    snapshot = {key: answer.get(key) for key in _ACCOUNT_KEYS if key in answer}
    snapshot["Max24HourSend"] = quota.get("Max24HourSend")
    snapshot["MaxSendRate"] = quota.get("MaxSendRate")
    snapshot["SentLast24Hours"] = quota.get("SentLast24Hours")
    suppression = (answer.get("SuppressionAttributes") or {}).get("SuppressedReasons")
    if suppression is not None:
        snapshot["SuppressedReasons"] = list(suppression)
    snapshot["checked_at"] = now.isoformat()
    return snapshot


def read_ses_account(now=None):
    """GetAccount i UTSKICK_SES_REGION till Switchboard. Returnerar
    ögonblicksbilden, eller {} när AWS inte är inkopplat här."""
    from botocore.exceptions import BotoCoreError, ClientError

    from .. import aws

    now = now or timezone.now()
    try:
        answer = aws.client("sesv2").get_account()
    except aws.AwsNotConfigured:
        return {}
    except (BotoCoreError, ClientError) as exc:
        logger.error("Utskick: GetAccount i SES gick inte (%s)", type(exc).__name__)
        return {"error": type(exc).__name__}
    snapshot = _account_snapshot(answer, now)
    rate = snapshot.get("MaxSendRate") or 0
    quota = snapshot.get("Max24HourSend") or 0
    Switchboard.get_solo()
    Switchboard.objects.filter(pk=Switchboard.SOLO_PK).update(
        ses_account=snapshot,
        ses_checked_at=now,
        ses_max_rate=max(0, min(32767, int(float(rate)))),
        ses_daily_quota=max(0, int(float(quota))),
    )
    return snapshot


def adx_numbers(now=None):
    """Våra egna 30-dagarstal för hela ADX (alla kunders utskicksmejl)."""
    now = now or timezone.now()
    rows = Recipient.objects.filter(
        channel=CHANNEL_EMAIL, sent_at__gte=now - timedelta(days=ACCOUNT_DAYS)
    ).exclude(utskick__account__is_demo=True)
    return numbers(rows)


def adx_wide(now=None):
    """utskick_daily (D.9): GetAccount och våra egna 30-dagarstal. Larmar
    byrån när SES inte är friskt eller när studsar eller klagomål närmar sig
    AWS gränser. Returnerar antal och lägen (inga adresser)."""
    now = now or timezone.now()
    summary = {}
    snapshot = read_ses_account(now)
    if snapshot:
        summary["ses"] = {
            key: snapshot.get(key)
            for key in ("EnforcementStatus", "SendingEnabled", "MaxSendRate", "Max24HourSend")
            if key in snapshot
        }
        if "error" in snapshot:
            summary["ses"] = {"error": snapshot["error"]}
    else:
        summary["ses"] = "off"
    own = adx_numbers(now)
    summary.update(
        sent=own.sent,
        bounced=own.bounced,
        complained=own.complained,
    )
    lines = []
    if snapshot and "error" not in snapshot:
        status = str(snapshot.get("EnforcementStatus") or "").upper()
        if status and status != "HEALTHY":
            lines.append(f"SES i eu-west-1 har läget {status}.")
        if snapshot.get("SendingEnabled") is False:
            lines.append("SES har stängt av sändningen för kontot i eu-west-1.")
    if own.outcomes and own.bounce_rate >= ADX_ALERT_BOUNCE:
        lines.append(
            f"Studsar de senaste 30 dagarna: {_pct(own.bounce_rate)} av {own.outcomes} "
            "(AWS pausar vid 5 %)."
        )
    if own.complained and own.complaint_rate >= ADX_ALERT_COMPLAINT:
        lines.append(
            f"Klagomål de senaste 30 dagarna: {_pct(own.complaint_rate)} av {own.delivered} "
            "(AWS pausar vid 0,1 %)."
        )
    if lines:
        logger.error("Utskick: e-posthälsan för hela ADX behöver ses över")
        alerts.agency(
            "Utskick: e-posthälsan för hela ADX",
            [*lines, "Se /manage/utskick/ (e-post) och Leveranshälsa per kund."],
            once="adx_wide_health",
            window="day",
            now=now,
        )
        summary["alert"] = True
    return summary
