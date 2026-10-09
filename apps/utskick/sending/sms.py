"""
Sms-slingan (README D.4), tickens fas 5.

    send_due(now, deadline, only=None) -> dict   en omgång tills deadline (time.monotonic())
    accounts_with_due_sms(now, only=None, demo=False) -> [FlamingoAccount]
    budget_for(sms_account, now=None) -> int     sms kontot får skicka nu (minutfönstret)
    claim(account, n, now, only=None) -> [Recipient]
    requeue(recipients, not_before=None)         tillbaka i kön, försöket räknas inte
    simulate(account, now, only=None) -> int     demokontot: levererat utan sms (D12)
    process(recipient, account, now, ctx) -> str ett sms: kontroller, text, sändning, utfall

Gången:

    demokonton: simulate (aldrig 46elks, aldrig en SmsMessage)
    Switchboard.sms_enabled av eller nödbromsen på: inget mer
    så länge tid finns:
      konton med köade sms (konton som får skicka, not_before passerat),
      äldsta utskicket först, ett konto i taget (round robin):
        fönstret stängt: kontots köade sms får not_before = nästa start
        budget = min(45 - kontots sms senaste 60 s,
                     60 - alla sms senaste 60 s - Flamingos egna sms (SmsLog) senaste 60 s)
        ta högst min(budget, 10) med select_for_update(skip_locked) -> sending
        för varje: send_time_checks (uppskjuten: tillbaka i kön; per person:
        hoppas över), avsändaren (svarsnumret eller namnet vid kollision),
        composer.render_sms, sms_wrapper.send med reference
        "u<utskick>:<mottagare>" (raden får "~u..."), utfallet enligt tabellen nedan
      inget hände i en hel runda: klart

Utfallen (D.4):

    ok, duplicate          sent (bara från sending/unknown), sms_message, delar, avsändare
    reference_conflict     samma som ok, men bara när det befintliga sms:et har källan
                           utskick och går till mottagarens nummer; annars failed
    unknown (oklart svar)  unknown med sms_message; räknas mot nödbromsen; skickas aldrig igen
    rate_limited           tillbaka i kön med not_before = nu + retry_after; nästa konto
    monthly_cap_reached    tillbaka i kön; alla kontots pågående utskick paused_cap
                           (sms_cost_cap), så högst en blocked_cap-rad per konto och tick
    sms_not_enabled        tillbaka i kön; kontots pågående utskick paused (sms_disabled)
    sender_not_allowed,
    message_too_long       failed; utskicket paused (provider) direkt, byrån larmas
    invalid_number,
    country_not_allowed    failed med orsaken (skip_reason invalid_number eller country)
    provider_error         failed; räknas mot nödbromsen; fem i rad i utskicket: paused
                           (provider), byrån larmas

Statusen skrivs villkorligt (D.5): sms_message och sent_at alltid, status
"sent" bara från sending eller unknown, och en leveransrapport som hunnit
före (smsbridge.sync_from_message) flyttas framåt efteråt. Inga adresser i
loggen, bara pk.
"""

import logging
import time
from collections import Counter as Tally
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F, Min, Q
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from apps.sms import encoding, numbers, pricing
from apps.sms.models import SmsMessage

from .. import alerts, keys, smsbridge, timing
from ..models import CHANNEL_SMS, Recipient, Utskick, UtskickSettings
from . import checks, freeze, sms_wrapper, state

logger = logging.getLogger(__name__)

RS = Recipient.Status
#: Så här många sekunder måste finnas kvar för att ta fler mottagare.
SAFETY_SECONDS = 2
#: Högst så här många mottagare per hämtning (D.4).
CLAIM_MAX = 10
#: En mottagare som inte gått att skicka så här många gånger (texten gick
#: inte att bygga, processen dog) blir failed.
MAX_ATTEMPTS = 5
#: Så här många provider_error i rad i ett utskick pausar det (provider).
PROVIDER_STREAK = 5
#: När varje konto nått minutgränsen väntar slingan så här länge innan den
#: prövar igen (sms:en från förra minuten faller ur fönstret ett i taget).
PACE_SLEEP = 1.0

PROVIDER_TEXT = "Sms-leverantören tog inte emot sms:et."
NOT_BUILT_TEXT = "Sms:et kunde inte skapas."
REJECTED_TEXT = "Sms:et stoppades före sändningen."
SKIP_ERRORS = {
    "invalid_number": Recipient.SkipReason.INVALID_NUMBER,
    "country_not_allowed": Recipient.SkipReason.COUNTRY,
}


@dataclass
class Context:
    """Det en omgång minns: pris per del och land ur första sms:et dit när
    priset kom ur en provkörning (part_cost_hint, C.1, så att 2 000
    mottagare inte ger 2 000 provkörningar; ett land med historik behöver
    inget), och konton som vilar resten av ticken (minutgränsen hos 46elks,
    taket)."""

    hints: dict = field(default_factory=dict)
    history: dict = field(default_factory=dict)
    resting: set = field(default_factory=set)
    counts: Tally = field(default_factory=Tally)


def _country(address):
    try:
        return numbers.parse(address).country
    except numbers.InvalidNumber:
        return ""


def _remember_price(ctx, message):
    """Priset per del till sms:ets land, för nästa sms dit i omgången. Bara
    när landet saknar historik (priset kom då ur en provkörning); med
    historik använder apps/sms den i stället."""
    country = getattr(message, "country", "") or ""
    if not country or country in ctx.hints or not message.parts or not message.estimated_cost:
        return
    if country not in ctx.history:
        ctx.history[country] = pricing.recent_part_cost(country) is not None
    if not ctx.history[country]:
        ctx.hints[country] = max(1, message.estimated_cost // message.parts)


# ---------------------------------------------------------------------------
# Kön
# ---------------------------------------------------------------------------


def _due(now):
    return Q(not_before__isnull=True) | Q(not_before__lte=now)


def accounts_with_due_sms(now, only=None, demo=False):
    """Konton som får skicka och har köade sms i pågående utskick vars
    not_before passerat, äldsta utskicket först. demo väljer demokontona
    (simuleras) eller alla andra."""
    rows = (
        Recipient.objects.filter(
            status=RS.QUEUED, channel=CHANNEL_SMS, utskick__status=Utskick.Status.SENDING
        )
        .filter(_due(now))
        .filter(checks.sendable_q("utskick__account__"))
        .filter(utskick__account__is_demo=demo)
    )
    if only:
        rows = rows.filter(utskick_id=only)
    order = list(
        rows.values("utskick__account_id")
        .annotate(first=Min("utskick__started_at"))
        .order_by("first", "utskick__account_id")
        .values_list("utskick__account_id", flat=True)[:500]
    )
    if not order:
        return []
    from apps.flamingo.models import FlamingoAccount

    accounts = FlamingoAccount.objects.select_related("customer").in_bulk(order)
    return [accounts[pk] for pk in order if pk in accounts]


def budget_for(sms_account, now=None):
    """Hur många sms utskicken får skicka för kontot just nu (D.4): kontots
    del (UTSKICK_SMS_ACCOUNT_PER_MINUTE minus kontots alla sms de senaste 60
    sekunderna, API:t inräknat) och byråns (UTSKICK_SMS_GLOBAL_PER_MINUTE
    minus alla sms till 46elks och Flamingos egna sms). Klockans tid: sms:ens
    created_at är verklig."""
    from apps.flamingo.models import SmsLog

    now = now or timezone.now()
    since = now - timedelta(seconds=60)
    recent = SmsMessage.objects.filter(created_at__gt=since)
    account_used = recent.filter(account=sms_account).exclude(status=SmsMessage.Status.REJECTED)
    global_used = recent.exclude(status__in=SmsMessage.STOPPED).count()
    flamingo_used = SmsLog.objects.filter(
        created_at__gt=since,
        status__in=(SmsLog.STATUS_SENDING, SmsLog.STATUS_SENT, SmsLog.STATUS_FAILED),
    ).count()
    account_limit = int(getattr(settings, "UTSKICK_SMS_ACCOUNT_PER_MINUTE", 45))
    global_limit = int(getattr(settings, "UTSKICK_SMS_GLOBAL_PER_MINUTE", 60))
    return max(
        0, min(account_limit - account_used.count(), global_limit - global_used - flamingo_used)
    )


def claim(account, n, now, only=None):
    """Ta högst n köade sms-mottagare för kontot (äldsta utskicket, sedan
    lägsta pk) med select_for_update(skip_locked): status sending,
    claimed_at (klockans tid) och ett försök till. En manuell körning
    (--only) och dygnskommandot krockar därför aldrig med ticken."""
    if n <= 0:
        return []
    real_now = timezone.now()
    with transaction.atomic():
        rows = (
            Recipient.objects.select_for_update(skip_locked=True, of=("self",))
            .filter(
                utskick__account=account,
                utskick__status=Utskick.Status.SENDING,
                channel=CHANNEL_SMS,
                status=RS.QUEUED,
            )
            .filter(_due(now))
        )
        if only:
            rows = rows.filter(utskick_id=only)
        ids = list(
            rows.order_by("utskick__started_at", "utskick_id", "pk").values_list("pk", flat=True)[
                :n
            ]
        )
        if not ids:
            return []
        Recipient.objects.filter(pk__in=ids).update(
            status=RS.SENDING, claimed_at=real_now, attempts=F("attempts") + 1
        )
    claimed = Recipient.objects.filter(pk__in=ids).select_related("utskick", "utskick__account")
    by_pk = {r.pk: r for r in claimed}
    return [by_pk[pk] for pk in ids if pk in by_pk]


def requeue(recipients, not_before=None):
    """Tillbaka i kön utan att försöket räknas (uppskjutande kontroller,
    minutgränsen, taket, slut på tid). I ett avbrutet utskick blir de
    cancelled i stället. Antalet."""
    ids = [r.pk for r in recipients]
    if not ids:
        return 0
    moved = Recipient.objects.filter(pk__in=ids, status=RS.SENDING).update(
        status=RS.QUEUED,
        not_before=not_before,
        claimed_at=None,
        attempts=Greatest(F("attempts") - 1, 0),
    )
    Recipient.objects.filter(
        pk__in=ids, status=RS.QUEUED, utskick__status=Utskick.Status.CANCELLED
    ).update(status=RS.CANCELLED, not_before=None)
    return moved


def defer_window(account, settings_row, now, only=None):
    """Fönstret är stängt för kontot: dess köade sms väntar till nästa start
    (en UPDATE, så att kontot inte tas upp igen förrän då)."""
    start = timing.next_window_start(settings_row, now)
    rows = Recipient.objects.filter(
        utskick__account=account,
        utskick__status=Utskick.Status.SENDING,
        channel=CHANNEL_SMS,
        status=RS.QUEUED,
    ).filter(Q(not_before__isnull=True) | Q(not_before__lt=start))
    if only:
        rows = rows.filter(utskick_id=only)
    return rows.update(not_before=start)


# ---------------------------------------------------------------------------
# Demokontot (D12)
# ---------------------------------------------------------------------------


def simulate(account, now, only=None):
    """Demokontot skickar aldrig: köade sms-mottagare blir levererade med
    simulated=True, utan anrop till 46elks och utan SmsMessage."""
    if not account.is_demo:
        raise ValueError("Bara demokontot simuleras.")
    total = 0
    sending = Utskick.objects.filter(account=account, status=Utskick.Status.SENDING)
    if only:
        sending = sending.filter(pk=only)
    for utskick in sending:
        sender = (
            utskick.sms_sender_name
            if utskick.sms_sender_kind == Utskick.SenderKind.NAME
            else checks.reply_number()
        )
        parts = max(1, encoding.analyse(utskick.sms_body or " ").parts)
        total += Recipient.objects.filter(
            utskick=utskick, channel=CHANNEL_SMS, status=RS.QUEUED
        ).update(
            status=RS.DELIVERED,
            simulated=True,
            sent_at=now,
            delivered_at=now,
            sms_sender=(sender or "")[:16],
            parts=parts,
            not_before=None,
        )
    return total


# ---------------------------------------------------------------------------
# En mottagare
# ---------------------------------------------------------------------------


def _skip(recipient, reason):
    Recipient.objects.filter(pk=recipient.pk, status=RS.SENDING).update(
        status=RS.SKIPPED, skip_reason=reason, claimed_at=None, not_before=None
    )


def _fail(recipient, error, *, message=None, skip_reason=""):
    values = {"status": RS.FAILED, "error": error[:200]}
    if message is not None:
        values["sms_message"] = message
    if skip_reason:
        values["skip_reason"] = skip_reason
    Recipient.objects.filter(pk=recipient.pk, status=RS.SENDING).update(**values)


def adopt(recipient, message, sender="", now=None):
    """Sms:et finns (skickat, eller oklart): koppla det till mottagaren.
    sms_message, delarna och sent_at skrivs alltid; status sent bara från
    sending eller unknown, unknown bara från sending (D.5). En
    leveransrapport som redan kommit flyttar mottagaren framåt."""
    now = now or timezone.now()
    values = {"sms_message": message, "parts": message.parts or 0, "error": ""}
    if sender:
        values["sms_sender"] = sender[:16]
    values["sent_at"] = Coalesce("sent_at", message.sent_at or message.created_at or now)
    Recipient.objects.filter(pk=recipient.pk).update(**values)
    # En leveransrapport som kom mellan 46elks svar och raden ovan hittade
    # ingen mottagare (sms_message var inte satt): läget läses därför om.
    fresh = SmsMessage.objects.filter(pk=message.pk).first() or message
    if fresh.status == SmsMessage.Status.RESERVED:
        Recipient.objects.filter(pk=recipient.pk, status=RS.SENDING).update(status=RS.UNKNOWN)
        return RS.UNKNOWN
    Recipient.objects.filter(pk=recipient.pk, status__in=(RS.SENDING, RS.UNKNOWN)).update(
        status=RS.SENT
    )
    if fresh.status in (SmsMessage.Status.DELIVERED, SmsMessage.Status.FAILED):
        smsbridge.sync_from_message(fresh)
    return RS.SENT


def _provider_streak(utskick):
    """De senaste PROVIDER_STREAK mottagarna med ett utfall (i den ordning
    slingan tog dem) misslyckades alla hos 46elks."""
    last = list(
        Recipient.objects.filter(utskick=utskick, claimed_at__isnull=False)
        .exclude(status__in=(RS.QUEUED, RS.SENDING))
        .order_by("-claimed_at", "-pk")
        .values_list("status", "error")[:PROVIDER_STREAK]
    )
    return len(last) == PROVIDER_STREAK and all(
        status == RS.FAILED and error == PROVIDER_TEXT for status, error in last
    )


def _pause_provider(utskick, detail, now):
    if state.pause(utskick, Utskick.PauseReason.PROVIDER, now=now):
        alerts.utskick_paused(utskick, Utskick.PauseReason.PROVIDER, detail, now=now)


def _body(utskick, recipient, sender, collided, now):
    from .. import composer

    if collided:
        freeze.ensure_person_code(recipient, now)
    return composer.render_sms(utskick, recipient, sender)


def process(recipient, account, now, ctx):
    """Ett sms (D.4). Returnerar utfallet: sent, unknown, deferred, skipped,
    failed, rate_limited, cap, disabled eller error."""
    check = checks.send_time_checks(recipient, now)
    if check.defer:
        requeue([recipient], check.not_before)
        return "deferred"
    if check.skip:
        _skip(recipient, check.reason)
        return "skipped"
    utskick = recipient.utskick
    sender = check.sender
    try:
        body = _body(utskick, recipient, sender, check.collided, now)
    except keys.KeyMismatch:
        raise
    except Exception:
        logger.exception("Utskick %s: texten för mottagare %s gick inte", utskick.pk, recipient.pk)
        if recipient.attempts >= MAX_ATTEMPTS:
            _fail(recipient, NOT_BUILT_TEXT)
            return "failed"
        Recipient.objects.filter(pk=recipient.pk, status=RS.SENDING).update(
            status=RS.QUEUED, claimed_at=None, not_before=timezone.now() + timedelta(minutes=5)
        )
        return "error"
    source = SmsMessage.Source.UTSKICK
    out = sms_wrapper.send(
        account,
        to=recipient.address,
        body=body,
        sender=sender,
        source=source,
        reference=f"u{utskick.pk}:{recipient.pk}",
        part_cost_hint=ctx.hints.get(_country(recipient.address)),
    )
    return apply(recipient, out, sender, account, now, ctx)


def _own_message(recipient, message):
    """Är det befintliga sms:et (reference_conflict) mottagarens eget: från
    ett utskick och till mottagarens nummer?"""
    return (
        message is not None
        and message.source == SmsMessage.Source.UTSKICK
        and keys.clean_value(CHANNEL_SMS, message.to)
        == keys.clean_value(CHANNEL_SMS, recipient.address)
    )


def apply(recipient, out, sender, account, now, ctx):
    """Utfallet från sms_wrapper.send på mottagaren, utskicket och kontot."""
    utskick = recipient.utskick
    message = out.message
    real_now = timezone.now()
    if message is not None:
        _remember_price(ctx, message)
    if out.error == "reference_conflict" and not _own_message(recipient, message):
        logger.error(
            "Utskick %s: mottagare %s, referensen hålls av ett annat sms", utskick.pk, recipient.pk
        )
        _fail(recipient, NOT_BUILT_TEXT)
        return "failed"
    if out.ok or out.error == "reference_conflict":
        if out.error == "reference_conflict":
            logger.warning("Utskick %s: mottagare %s hade redan ett sms", utskick.pk, recipient.pk)
        result = adopt(recipient, message, sender, real_now)
        if out.unknown or result == RS.UNKNOWN:
            checks.provider_trouble(real_now)
            return "unknown"
        return "sent"
    error = out.error
    if error == "rate_limited":
        retry = int(out.retry_after or 60)
        requeue([recipient], real_now + timedelta(seconds=retry))
        ctx.resting.add(account.pk)
        return "rate_limited"
    if error == "monthly_cap_reached":
        requeue([recipient])
        state.pause_account(
            account,
            Utskick.PauseReason.SMS_COST_CAP,
            now,
            statuses=(Utskick.Status.SENDING,),
        )
        ctx.resting.add(account.pk)
        return "cap"
    if error == "sms_not_enabled":
        requeue([recipient])
        state.pause_account(
            account,
            Utskick.PauseReason.SMS_DISABLED,
            now,
            statuses=(Utskick.Status.SENDING,),
        )
        ctx.resting.add(account.pk)
        return "disabled"
    if error in SKIP_ERRORS:
        reason = SKIP_ERRORS[error]
        _fail(recipient, reason.label, message=message, skip_reason=reason)
        return "failed"
    if error in ("sender_not_allowed", "message_too_long"):
        _fail(recipient, REJECTED_TEXT, message=message)
        _pause_provider(utskick, f"apps/sms svarade {error}.", now)
        return "failed"
    if error == "provider_error":
        _fail(recipient, PROVIDER_TEXT, message=message)
        checks.provider_trouble(real_now)
        if _provider_streak(utskick):
            _pause_provider(utskick, "Fem sms i rad fick fel från 46elks.", now)
        return "failed"
    logger.error("Utskick %s: mottagare %s fick felet %s", utskick.pk, recipient.pk, error)
    _fail(recipient, NOT_BUILT_TEXT, message=message)
    return "failed"


# ---------------------------------------------------------------------------
# Omgången
# ---------------------------------------------------------------------------


def send_due(now=None, deadline=None, only=None):
    """Sms-slingan till deadline (time.monotonic()). Returnerar antal per
    utfall (bara antal: sammanfattningen går till backups/utskick.log)."""
    now = now or timezone.now()
    deadline = deadline if deadline is not None else time.monotonic() + 30
    ctx = Context()
    counts = ctx.counts

    def time_left():
        return deadline - time.monotonic()

    for account in accounts_with_due_sms(now, only, demo=True):
        simulated = simulate(account, now, only)
        if simulated:
            counts["simulated"] += simulated
    row = checks.switch()
    if not row.sms_enabled:
        return dict(counts)
    if checks.breaker_active(row=row):
        counts["breaker"] += 1
        return dict(counts)
    while time_left() > SAFETY_SECONDS:
        accounts = [a for a in accounts_with_due_sms(now, only) if a.pk not in ctx.resting]
        if not accounts:
            break
        progressed = False
        paced = False
        for account in accounts:
            if time_left() <= SAFETY_SECONDS:
                break
            if checks.breaker_active():
                counts["breaker"] += 1
                return dict(counts)
            settings_row = UtskickSettings.objects.filter(account=account).first()
            if not timing.sms_window_open(settings_row, now):
                counts["window"] += defer_window(account, settings_row, now, only)
                continue
            sms_account = sms_wrapper.sms_account_for(account)
            if sms_account is None or not sms_account.is_enabled:
                state.pause_account(
                    account,
                    Utskick.PauseReason.SMS_DISABLED,
                    now,
                    statuses=(Utskick.Status.SENDING,),
                )
                counts["disabled"] += 1
                continue
            budget = budget_for(sms_account)
            if budget <= 0:
                paced = True
                continue
            claimed = claim(account, min(budget, CLAIM_MAX), now, only)
            for index, recipient in enumerate(claimed):
                if time_left() <= SAFETY_SECONDS:
                    requeue(claimed[index:])
                    return dict(counts)
                try:
                    result = process(recipient, account, now, ctx)
                except keys.KeyMismatch:
                    requeue(claimed[index:])
                    raise
                except Exception:
                    # De som inte hann prövas går tillbaka i kön; den här kan
                    # ha nått apps/sms och lämnas åt återhämtningen (D.5).
                    requeue(claimed[index + 1 :])
                    raise
                counts[result] += 1
                if result not in ("deferred", "error"):
                    progressed = True
                if account.pk in ctx.resting or checks.breaker_active():
                    requeue(claimed[index + 1 :])
                    break
        if progressed:
            continue
        if not paced or time_left() <= SAFETY_SECONDS + PACE_SLEEP:
            break
        # Varje konto har nått minutgränsen: vänta tills en plats blir ledig
        # (UTSKICK_SMS_ACCOUNT_PER_MINUTE räknas över de senaste 60 sekunderna).
        counts["paced"] += 1
        time.sleep(PACE_SLEEP)
    return dict(counts)


# ---------------------------------------------------------------------------
# Avregistreringarna (D.9)
# ---------------------------------------------------------------------------

#: STOPP-svar och /s/-avregistreringar på minst så här stor andel av de
#: levererade (och minst STOPS_MIN_DELIVERED levererade) pausar utskicket:
#: operatörerna kan spärra det delade numret för alla kunder.
STOPS_SHARE = 0.02
STOPS_MIN_DELIVERED = 100


def stops_check(now=None, only=None):
    """Tickens fas 9: pågående sms-utskick med för många avregistreringar
    pausas (paused_health, stops) och byrån larmas. {"paused": n}."""
    from ..models import Suppression

    now = now or timezone.now()
    paused = 0
    sending = Utskick.objects.filter(status=Utskick.Status.SENDING, account__is_demo=False)
    if only:
        sending = sending.filter(pk=only)
    for utskick in sending.select_related("account"):
        delivered = Recipient.objects.filter(
            utskick=utskick, channel=CHANNEL_SMS, status=RS.DELIVERED
        ).count()
        if delivered < STOPS_MIN_DELIVERED:
            continue
        stopped = max(
            Recipient.objects.filter(
                utskick=utskick, channel=CHANNEL_SMS, stopped_at__isnull=False
            ).count(),
            Suppression.objects.filter(
                utskick=utskick,
                channel=CHANNEL_SMS,
                reason__in=(Suppression.Reason.STOP, Suppression.Reason.LINK),
            ).count(),
        )
        if stopped < delivered * STOPS_SHARE:
            continue
        note = f"{stopped} av {delivered} mottagare har avregistrerat sig."
        if state.pause(utskick, Utskick.PauseReason.STOPS, note=note, now=now):
            alerts.utskick_paused(utskick, Utskick.PauseReason.STOPS, note, now=now)
            paused += 1
    return {"paused": paused} if paused else {}
