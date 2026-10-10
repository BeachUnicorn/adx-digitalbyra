"""
Frysningen (README D.3): ett bekräftat utskick blir mottagare, i bitar om
2 000 kontakter, i tickens fas 4.

    start_due(now, only=None) -> dict       schemalagda som är dags: freezing,
                                            eller paused (late) när de är mer än
                                            3 timmar sena eller ett nytt svenskt datum,
                                            eller paused (content) när länkarna eller
                                            informationens regler stoppar dem
    freeze_due(now, deadline, only=None) -> dict
                                            bitar tills deadline (time.monotonic())
    freeze_chunk(utskick, now) -> dict | None
                                            en bit i en transaktion
    pause_unsendable(now, only=None) -> int D.8: kontots utskick pausas när
                                            kontot inte längre får skicka
    ensure_person_code(recipient, now=None) en personkod (/s/, /p/) i efterhand,
                                            när slingan byter till namnavsändaren
    link_keys(utskick), needs_person_code(utskick)

En bit, i en transaction.atomic():

1. kontakterna efter freeze_cursor (audience.contacts: alltid kontots egna,
   listor och taggar med kontot i villkoret, så ett manipulerat urval ger
   inga främmande mottagare, H.1);
2. bedömningen för hela biten (audience.Judge: samtycken, spärrar,
   veckotaket, landet) och kanalen per kontakt (audience.plan);
3. bulk_create(ignore_conflicts=True) av Recipient (queued eller skipped
   med skip_reason, frysta värden för sammanfogningen, grunden och
   tracking_ok), läsning av de köade sms-mottagarna, och deras koder:
   en klickkod per {länk:nyckel} i texten och en personkod när texten
   behöver /s/ (namnavsändaren). Koderna läggs utan
   ignore_conflicts i en savepoint; en krock drar nya, högst
   codes.MAX_TRIES gånger, sedan misslyckas biten och tas om nästa tick;
4. freeze_cursor flyttas, och biten prövas: varje köad sms-mottagare har
   sina koder.

När markören nått slutet: frozen_counts, frozen_at, byråns larm (första
stora utskicket, information) och förkontrollerna (state.prechecks), som
flyttar utskicket till sending eller en paus.

Disken (E.7): under 8 % ledigt fryses inget; utskicken står kvar som
schemalagda och byrån larmas högst en gång i timmen.
"""

import logging
import shutil
import time
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone

from apps.sms.pricing import STOCKHOLM

from .. import alerts, audience, codes, keys
from ..access import settings_for
from ..models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    INFORMATION,
    LinkCode,
    Recipient,
    TrackedLink,
    Utskick,
    UtskickSettings,
)
from . import checks, state

logger = logging.getLogger(__name__)

S = Utskick.Status
#: Ett schemalagt utskick som är så här sent pausas (late), liksom ett som
#: skulle ha gått ett tidigare svenskt datum (D.3 steg 0).
LATE_AFTER = timedelta(hours=3)
#: Under så här mycket ledigt på disken fryses inget (E.7).
MIN_FREE = 0.08
#: Byråns larm (D.9, H.5).
FIRST_BIG = 500
INFO_BIG = 200
INFO_PER_30_DAYS = 2


# ---------------------------------------------------------------------------
# Steg 0: dags att frysa
# ---------------------------------------------------------------------------


def is_late(utskick, now):
    scheduled = utskick.scheduled_at
    if scheduled is None:
        return False
    if now - scheduled > LATE_AFTER:
        return True
    day = timezone.localtime(scheduled, STOCKHOLM).date()
    return timezone.localtime(now, STOCKHOLM).date() != day


def free_disk(path=None):
    usage = shutil.disk_usage(path or settings.BASE_DIR)
    return usage.free / usage.total if usage.total else 1.0


def start_due(now=None, only=None):
    """Schemalagda utskick vars tid har kommit, hos konton som får skicka:
    freezing, eller paused (late), eller paused (content) när en länk inte
    är godkänd eller informationens regler stoppar texten
    (state.content_problems; förkontrollerna prövar igen efter frysningen).
    {"started", "late", "content", "held"}."""
    now = now or timezone.now()
    counts = {"started": 0, "late": 0, "content": 0, "held": 0}
    due = Utskick.objects.filter(status=S.SCHEDULED, scheduled_at__lte=now).filter(
        checks.sendable_q("account__")
    )
    if only:
        due = due.filter(pk=only)
    due = list(due.select_related("account__customer").order_by("scheduled_at", "pk")[:50])
    if not due:
        return counts
    free = free_disk()
    if free < MIN_FREE:
        logger.error("Utskick: disken har %.1f %% ledigt, inget fryses", free * 100)
        alerts.low_disk(free * 100, now=now)
        counts["held"] = len(due)
        return counts
    for utskick in due:
        if is_late(utskick, now):
            if state.transition(
                utskick, S.PAUSED, Utskick.PauseReason.LATE, expected=(S.SCHEDULED,), now=now
            ):
                counts["late"] += 1
            continue
        if state.content_problems(utskick):
            if state.transition(
                utskick, S.PAUSED, Utskick.PauseReason.CONTENT, expected=(S.SCHEDULED,), now=now
            ):
                logger.warning("Utskick %s: innehållet stoppar sändningen (content)", utskick.pk)
                counts["content"] += 1
            continue
        if state.transition(utskick, S.FREEZING, expected=(S.SCHEDULED,), now=now):
            counts["started"] += 1
    return counts


def pause_unsendable(now=None, only=None):
    """D.8 som skyddsnät i ticken: schemalagda, frysande och pågående
    utskick hos konton som inte längre får skicka (utskick av, Flamingo av,
    kunden inaktiv, sending_blocked) pausas, också när ändringen gjordes
    någon annanstans än på kundkortet. Antalet pausade."""
    now = now or timezone.now()
    active = Utskick.objects.filter(status__in=Utskick.ACTIVE).exclude(
        checks.sendable_q("account__")
    )
    if only:
        active = active.filter(pk=only)
    from apps.flamingo.models import FlamingoAccount

    paused = 0
    account_ids = set(active.values_list("account_id", flat=True)[:200])
    for account in FlamingoAccount.objects.filter(pk__in=account_ids):
        reason = checks.sendable(account)
        if reason:
            paused += state.pause_account(account, reason, now)
    return paused


# ---------------------------------------------------------------------------
# Koderna
# ---------------------------------------------------------------------------


def link_keys(utskick):
    """Länkarnas nycklar i sms-texten ({länk:nyckel}), som composer läser dem."""
    from .. import composer

    return list(composer.placeholders(utskick.sms_body or "").links)


def needed_links(utskick):
    keys_used = set(link_keys(utskick))
    if not keys_used:
        return []
    return list(TrackedLink.objects.filter(utskick=utskick, key__in=keys_used).order_by("pk"))


def needs_person_code(utskick):
    """Behöver texten /s/-länken? Bara med namnavsändaren (den går inte att
    svara till, D4). Byter slingan till namnavsändaren vid en kollision på
    svarsnumret läggs koden till då (ensure_person_code, composer.render_sms)."""
    return utskick.sms_sender_kind == Utskick.SenderKind.NAME


def _code_rows(utskick, recipients, links, person, now):
    """(mottagare, länk eller None, sort) som saknar en kod."""
    have = set(
        LinkCode.objects.filter(recipient__in=[r.pk for r in recipients]).values_list(
            "recipient_id", "kind", "link_id"
        )
    )
    specs = []
    for recipient in recipients:
        for link in links:
            if (recipient.pk, LinkCode.Kind.LINK, link.pk) not in have:
                specs.append((recipient, link, LinkCode.Kind.LINK))
        if person and (recipient.pk, LinkCode.Kind.PERSON, None) not in have:
            specs.append((recipient, None, LinkCode.Kind.PERSON))
    return specs


def _insert_codes(utskick, specs, now):
    """Lägg koderna med bulk_create i en savepoint, nya koder vid krock,
    högst codes.MAX_TRIES gånger (D.3 steg 3)."""
    if not specs:
        return 0
    hashes = {}
    for recipient, _link, _kind in specs:
        if recipient.pk not in hashes:
            hashes[recipient.pk] = keys.value_hash(CHANNEL_SMS, recipient.address)
    for _attempt in range(codes.MAX_TRIES):
        drawn = codes.new_codes(len(specs))
        rows = [
            LinkCode(
                code=code,
                kind=kind,
                account_id=utskick.account_id,
                channel=CHANNEL_SMS,
                value_hash=hashes[recipient.pk],
                recipient=recipient,
                link=link,
                created_at=now,
            )
            for code, (recipient, link, kind) in zip(drawn, specs, strict=True)
        ]
        try:
            with transaction.atomic():
                LinkCode.objects.bulk_create(rows, batch_size=1000)
        except IntegrityError:
            logger.warning("Utskick %s: krock bland sms-koderna, nya dras", utskick.pk)
            continue
        return len(rows)
    raise codes.CodeCollision("Ingen ledig sms-kod efter flera försök.")


def allocate_codes(utskick, recipients, now=None):
    """Klick- och personkoderna för köade sms-mottagare. Antalet nya koder."""
    now = now or timezone.now()
    links = needed_links(utskick)
    person = needs_person_code(utskick)
    if not recipients or not (links or person):
        return 0
    return _insert_codes(utskick, _code_rows(utskick, recipients, links, person, now), now)


def ensure_person_code(recipient, now=None):
    """Personkoden för /s/ och /p/ när den saknas (slingan bytte till
    namnavsändaren efter en kollision på svarsnumret). Returnerar koden."""
    now = now or timezone.now()
    existing = LinkCode.objects.filter(recipient=recipient, kind=LinkCode.Kind.PERSON).first()
    if existing is not None:
        return existing
    _insert_codes(recipient.utskick, [(recipient, None, LinkCode.Kind.PERSON)], now)
    return LinkCode.objects.get(recipient=recipient, kind=LinkCode.Kind.PERSON)


def _verify_codes(utskick, recipients, links, person):
    """Varje köad sms-mottagare i biten har sina koder (D.3 steg 3)."""
    if not recipients or not (links or person):
        return
    expected = len(recipients) * (len(links) + (1 if person else 0))
    found = LinkCode.objects.filter(
        recipient__in=[r.pk for r in recipients],
        kind__in=(LinkCode.Kind.LINK, LinkCode.Kind.PERSON),
    ).count()
    if found < expected:
        raise RuntimeError(f"Utskick {utskick.pk}: {expected - found} sms-koder saknas i biten.")


# ---------------------------------------------------------------------------
# Mottagarna
# ---------------------------------------------------------------------------


def _recipient(utskick, contact, channel, reason, judge, defs, now):
    """En mottagare med de frysta värdena för sammanfogningen
    (composer.merge_values: {förnamn}, {efternamn}, {namn}, {företag},
    {fält:nyckel}, enradiga och högst 60 tecken)."""
    from .. import composer

    consent = judge.consents.get((contact.pk, channel))
    return Recipient(
        utskick=utskick,
        contact=contact,
        channel=channel,
        address=contact.address(channel) or "",
        merge=composer.merge_values(contact, defs) if not reason else {},
        basis=consent.basis if consent is not None and not reason else "",
        tracking_ok=bool(consent and consent.tracking_ok and channel == CHANNEL_EMAIL),
        status=Recipient.Status.SKIPPED if reason else Recipient.Status.QUEUED,
        skip_reason=reason or "",
        created_at=now,
    )


def freeze_chunk(utskick, now=None):
    """En bit (D.3 steg 1 till 3) i en transaktion. Returnerar
    {"contacts", "queued", "skipped", "codes", "done"}, eller None när
    utskicket inte längre fryses (avbrutet, pausat) eller är låst av någon
    annan."""
    now = now or timezone.now()
    with transaction.atomic():
        row = (
            Utskick.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("account")
            .filter(pk=utskick.pk, status=S.FREEZING)
            .first()
        )
        if row is None:
            return None
        chunk = list(audience.contacts(row, now).filter(pk__gt=row.freeze_cursor)[: audience.CHUNK])
        if not chunk:
            finish_freeze(row, now)
            return {"contacts": 0, "queued": 0, "skipped": 0, "codes": 0, "done": True}
        settings_row = settings_for(row.account)
        mode = row.channel_mode if row.channel_mode in audience.MODES else audience.MODES[0]
        judge = audience.Judge(
            row,
            chunk,
            at=now,
            settings_row=settings_row,
            countries=audience.allowed_countries(row.account),
            channels=audience.channels_for(mode),
        )
        from .. import composer

        defs = composer.field_defs(row.account)
        rows = []
        for contact in chunk:
            for channel, reason in audience.plan(mode, judge.reasons(contact)):
                rows.append(_recipient(row, contact, channel, reason, judge, defs, now))
        Recipient.objects.bulk_create(rows, ignore_conflicts=True, batch_size=1000)
        ids = [c.pk for c in chunk]
        queued_sms = list(
            Recipient.objects.filter(
                utskick=row,
                contact_id__in=ids,
                channel=CHANNEL_SMS,
                status=Recipient.Status.QUEUED,
            ).only("pk", "address", "utskick_id")
        )
        links = needed_links(row)
        person = needs_person_code(row)
        added = 0
        if queued_sms and (links or person):
            added = _insert_codes(row, _code_rows(row, queued_sms, links, person, now), now)
        _verify_codes(row, queued_sms, links, person)
        Utskick.objects.filter(pk=row.pk).update(freeze_cursor=chunk[-1].pk, updated_at=now)
        queued = sum(1 for r in rows if r.status == Recipient.Status.QUEUED)
    utskick.freeze_cursor = chunk[-1].pk
    return {
        "contacts": len(chunk),
        "queued": queued,
        "skipped": len(rows) - queued,
        "codes": added,
        "done": False,
    }


def frozen_counts(utskick):
    """{"sms", "email", "skipped", "skipped_by_reason", "total"} ur
    mottagarna."""
    counts = {"sms": 0, "email": 0, "skipped": 0, "skipped_by_reason": {}, "total": 0}
    rows = (
        Recipient.objects.filter(utskick=utskick)
        .values("channel", "status", "skip_reason")
        .annotate(n=Count("pk"))
    )
    for row in rows:
        n = row["n"]
        counts["total"] += n
        if row["status"] == Recipient.Status.SKIPPED:
            counts["skipped"] += n
            reason = row["skip_reason"] or "okänd"
            counts["skipped_by_reason"][reason] = counts["skipped_by_reason"].get(reason, 0) + n
        elif row["channel"] in (CHANNEL_SMS, CHANNEL_EMAIL):
            counts[row["channel"]] += n
    return counts


def _alerts(utskick, counts, now):
    """Byråns larm när frysningen är klar (D.9, H.5)."""
    recipients = counts["sms"] + counts["email"]
    if recipients > FIRST_BIG:
        claimed = UtskickSettings.objects.filter(
            account_id=utskick.account_id, first_utskick_alerted=False
        ).update(first_utskick_alerted=True)
        if claimed:
            alerts.first_big_utskick(utskick, recipients, now=now)
    if utskick.purpose == INFORMATION:
        recent = (
            Utskick.objects.filter(
                account_id=utskick.account_id,
                purpose=INFORMATION,
                frozen_at__gte=now - timedelta(days=30),
            )
            .exclude(pk=utskick.pk)
            .count()
            + 1
        )
        if recipients > INFO_BIG or recent > INFO_PER_30_DAYS:
            alerts.information_utskick(utskick, recipients, recent, now=now)


def finish_freeze(utskick, now=None):
    """Markören har nått slutet (D.3 steg 4): frozen_counts, frozen_at,
    larmen och förkontrollerna. Anropas inne i bitens transaktion."""
    now = now or timezone.now()
    counts = frozen_counts(utskick)
    Utskick.objects.filter(pk=utskick.pk).update(
        frozen_counts=counts, frozen_at=now, updated_at=now
    )
    utskick.frozen_counts = counts
    utskick.frozen_at = now
    if not utskick.account.is_demo:
        _alerts(utskick, counts, now)
    # S3 (sändnings-byggaren): mejlet fryses före förkontrollerna (D.3, F.4):
    # länkarna blir TrackedLink och ögonblicksbilden hamnar i email_snapshot.
    problem = _freeze_email(utskick, now)
    if problem:
        state.pause(utskick, Utskick.PauseReason.CONTENT, note=problem, now=now)
        return state.Verdict(S.PAUSED, Utskick.PauseReason.CONTENT, problem)
    # --- slut S3
    verdict, _moved = state.apply_prechecks(utskick, now, expected=(S.FREEZING,))
    if verdict.reason == Utskick.PauseReason.SMS_COST_CAP:
        logger.info("Utskick %s pausades vid taket före första sms:et", utskick.pk)
    return verdict


# --- S3 (sändnings-byggaren) ------------------------------------------------


def _freeze_email(utskick, now):
    """sending.email.freeze_email i en egen savepoint: ett fel pausar
    utskicket (content) i stället för att fälla biten tick efter tick."""
    if not utskick.has_email:
        return ""
    from . import email as email_loop

    try:
        with transaction.atomic():
            return email_loop.freeze_email(utskick, now)
    except Exception:
        logger.exception("Utskick %s: mejlet kunde inte frysas", utskick.pk)
        return email_loop.NOT_BUILT_TEXT


# --- slut S3 ---------------------------------------------------------------------


def freeze_due(now=None, deadline=None, only=None):
    """Frys bitar tills deadline (time.monotonic()), äldsta utskicket först.
    Ett utskick vars bit misslyckas (till exempel krockar bland koderna tre
    gånger) lämnas till nästa tick. {"chunks", "recipients", "frozen", "failed"}."""
    now = now or timezone.now()
    deadline = deadline if deadline is not None else time.monotonic() + 10
    counts = {"chunks": 0, "recipients": 0, "frozen": 0, "failed": 0}
    failed = set()
    while time.monotonic() < deadline:
        candidates = (
            Utskick.objects.filter(status=S.FREEZING)
            .filter(checks.sendable_q("account__"))
            .exclude(pk__in=failed)
        )
        if only:
            candidates = candidates.filter(pk=only)
        utskick = candidates.order_by("status_changed_at", "pk").first()
        if utskick is None:
            break
        try:
            result = freeze_chunk(utskick, now)
        except Exception:
            logger.exception("Utskick %s: en bit av frysningen misslyckades", utskick.pk)
            failed.add(utskick.pk)
            counts["failed"] += 1
            continue
        if result is None:
            failed.add(utskick.pk)
            continue
        counts["chunks"] += 1
        counts["recipients"] += result["queued"] + result["skipped"]
        if result["done"]:
            counts["frozen"] += 1
    return {k: v for k, v in counts.items() if v}
