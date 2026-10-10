"""
E-postslingan (README D.6, D.5, D.8, D.9, F.8, G.2), tickens fas 6, och
varje annat mejl från verktyget: redigeraren och Granska (testmejlet),
Inkorgen (svar på mejl) och byrån (provmejlet) går hit, aldrig direkt till
transporten.

    work_exists(now) -> bool          köade e-postmottagare hos konton som får skicka
    send_due(now, deadline, only=None) -> dict
                                      konton i tur och ordning, högst BATCH åt gången
                                      med select_for_update(skip_locked), i takten
                                      rate_for(); demokontot simuleras
    accounts_with_due_email(now, only=None, demo=False) -> [FlamingoAccount]
    claim(account, n, now, only=None) -> [Recipient]
    requeue(recipients, not_before=None)
    process(recipient, account, now, ctx) -> str
                                      ett mejl: kontrollerna, mejlet, taken, SES, utfallet
    email_checks(recipient, now) -> Gate
    compose(utskick, recipient, *, account=None) -> transport.OutgoingMail
    adx_cap_left(account, now=None) -> int
    adx_month_count(account, now=None) -> int
    adx_cap_text(account, utskick=None, need=0, now=None) -> str
    from_for(account, utskick=None, sender_domain=None) -> (namn, adress)
    reply_to_for(account, recipient=None, thread=None) -> str
    deliver(account, mail, *, kind, utskick=None, recipient=None, now=None) -> transport.Sent
    send_test(utskick, *, address, contact=None, actor, now=None) -> transport.Sent
    error_text(sent) -> str           svensk text för vyn
    simulate(account, now, only=None) -> int
    stale_unknown(now=None) -> int
    freeze_email(utskick, now=None) -> str      slutet av frysningen (freeze.py)
    email_link_problems(utskick) -> list[str]   state.content_problems före frysningen

Gången (D.6):

    demokonton: simulate (aldrig SES)
    e-posten inte påslagen (state.email_live, D.8): inget mer, utskicken väntar
    händelsekön saknas i drift: inget mer, byrån larmas (studsar och klagomål
    skulle annars aldrig nå hälsan)
    så länge tid finns:
      konton med köade mejl, äldsta utskicket först, ett konto i taget
      (round robin), högst BATCH åt gången:
        email_checks (uppskjuten: tillbaka i kön; per person: hoppas över;
        utskicket pausas: spärren, domänen eller provet)
        compose (render, text, MIME-huvudena)
        taken under kontots lås (limits.ADX_MAIL_LOCK + konto): ADX-domänens
        månadstak (paused_cap adx_mail_cap), dygnstaket (väntan till nästa dag),
        provet (de första 200, sedan en timmes väntan); mottagaren får sent_at
        (reserverad) i samma transaktion, så att två processer aldrig räknar
        förbi taket
        takten (Context.pace), transport.send med konfigurationssetet och
        taggarna k, a, u, r, och utfallet:

    lyckat                     ses_message_id, sent (bara från sending och unknown, D.5)
    Throttling, 429            tillbaka i kön, en sekunds paus, halva takten resten
                               av ticken
    anslutningen kom aldrig fram   tillbaka i kön, inget mer den här ticken
    läs-timeout, bruten anslutning, 5xx
                               unknown, skickas aldrig igen (Send-händelsen adopterar,
                               stale_unknown gör den till failed efter 24 timmar)
    SendingPaused, AccountSendingPaused
                               tillbaka i kön, Switchboard.email_enabled av, larm
    MailFromDomainNotVerified  tillbaka i kön, utskicket paused provider, larm
    AWS saknas, åtkomst nekad  tillbaka i kön, inget mer den här ticken (transporten larmar)
    MessageRejected, andra 4xx failed med felet; fem i rad: paused provider, larm

Hälsan (D.9) prövas var CHECK_EVERY:e sändning per utskick (sending/health.py)
och vid varje studs och klagomål (inbound/events.py).

ADX-taket räknar kontots mejl från ADX-domänen (utskick.adx.se) i den
svenska kalendermånaden: utskickens mottagare (skickade, reserverade och
oklara) plus testmejl och svar från Inkorgen, som räknas i en Counter vars
fönster är nästa månads början (limits.purge tar då inte raden förrän
månaden är slut).

Testmejlets och provmejlets svarsadress (och provmejlets
mailto-avregistrering) bär mottagaren NO_RECIPIENT, som aldrig finns:
Inkorgen faller då tillbaka på kontot i token och avsändaren (G.3), precis
som när en mottagare tagits bort. Testmejlet har inget List-Unsubscribe
(compose_test).

Inga adresser i loggen, bara pk och antal.
"""

import logging
import time
from collections import Counter as Tally
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db import connection, transaction
from django.db.models import F, Min, Q
from django.db.models.functions import Greatest
from django.utils import timezone

from apps.sms import pricing

from .. import alerts, keys, limits, links, tokens
from ..email import mime, transport
from ..models import (
    CHANNEL_EMAIL,
    INFORMATION,
    REKLAM,
    Contact,
    Recipient,
    SenderDomain,
    Suppression,
    Switchboard,
    TrackedLink,
    Utskick,
    UtskickSettings,
)
from . import checks, health, state

logger = logging.getLogger(__name__)

RS = Recipient.Status
R = Utskick.PauseReason

#: Mottagare per anspråk (D.6).
BATCH = 25
#: Andelen av SES MaxSendRate vi använder (D.6).
SES_RATE_SHARE = 0.8
#: Så många fel i rad pausar utskicket (provider, D.6).
FAILURES_IN_ROW = 5
#: unknown blir failed efter så här lång tid (D.5).
UNKNOWN_AFTER_HOURS = 24
UNKNOWN_TEXT = "Oklart om mejlet skickades"
TEST_PREFIX = "Test: "
#: Så här många sekunder måste finnas kvar för att ta fler mottagare.
SAFETY_SECONDS = 2
#: Ett mejl som inte gått att bygga så här många gånger blir failed.
MAX_ATTEMPTS = 5
#: Takten går aldrig under så här många mejl i sekunden.
MIN_RATE = 0.5
#: Pausen efter en broms från SES (D.6).
THROTTLE_SLEEP = 1.0
#: Testsändningar per konto och svenskt dygn, sms och mejl tillsammans (F.8).
TEST_SENDS_PER_DAY = 10
#: Counter-scopet för testmejl och svar från ADX-domänen (ADX-taket).
ADX_SCOPE = "adx_mail"
#: Mejlen utanför utskicken som räknas mot ADX-taket (F.8, G.2).
ADX_COUNTED_KINDS = (transport.TEST, transport.REPLY)
#: Mottagaren i testmejlets och provmejlets svarsadress: finns aldrig
#: ("zzzzzzzz" i bas 36), så Inkorgen routar på kontot och avsändaren.
NO_RECIPIENT = 36**8 - 1

NOT_BUILT_TEXT = "Mejlet kunde inte skapas."
#: Utskicket skickas aldrig utan sin frysta ögonblicksbild (länkarna som
#: TrackedLink): mejlet byggs då om vid Fortsätt (state.prechecks).
NOT_FROZEN_TEXT = "Mejlet kunde inte göras klart för utskick. Granska och bekräfta igen."
UNKNOWN_ALERT = (
    "SES svarade inte säkert på ett utskicksmejl (timeout, bruten anslutning eller 5xx). "
    "Mejlet är oklart och skickas aldrig igen; resten väntar till nästa tick."
)
REJECTED_PREFIX = "E-posttjänsten tog inte emot mejlet"
DOMAIN_TEXT = "Avsändardomänen är inte verifierad längre. Välj en annan avsändare."
MAIL_FROM_TEXT = "Avsändardomänens studsadress (MAIL FROM) är inte verifierad hos e-posttjänsten."
NO_EVENTS_TEXT = (
    "UTSKICK_SQS_EVENTS_URL saknas: studsar och klagomål skulle inte läsas. "
    "E-postutskicken väntar tills kön är satt (server/aws-utskick-s3.sh, J S3 steg 4)."
)

ERROR_TEXTS = {
    "demo": "Demokontot skickar aldrig.",
    "email_off": "E-post är inte påslaget än.",
    "blocked": checks.BLOCKED_TEXT,
    "disabled": checks.DISABLED_TEXT,
    "address": "Skriv en giltig e-postadress.",
    "suppressed": "Adressen har avregistrerat sig från dina mejl.",
    "test_limit": f"Du har skickat {TEST_SENDS_PER_DAY} test i dag. Försök igen i morgon.",
    "adx_cap": (
        "Taket för ADX-domänen är nått den här månaden. "
        "Verifiera din egen domän under Inställningar."
    ),
    "render": "Mejlet kunde inte skapas. Kontrollera innehållet och försök igen.",
    "keys": "Något är fel med nycklarna för utskick. ADX har fått ett larm.",
    "unknown": "Det är oklart om mejlet gick i väg. Vänta en stund innan du försöker igen.",
    "retry": "E-posttjänsten hann inte med. Försök igen om en stund.",
}
DEFAULT_ERROR_TEXT = "Mejlet gick inte att skicka."


# ---------------------------------------------------------------------------
# Kön
# ---------------------------------------------------------------------------


def _due(now):
    return Q(not_before__isnull=True) | Q(not_before__lte=now)


def email_due(now, demo=False):
    """Köade e-postmottagare i pågående utskick hos konton som får skicka,
    vars not_before passerat."""
    return (
        Recipient.objects.filter(
            status=RS.QUEUED, channel=CHANNEL_EMAIL, utskick__status=Utskick.Status.SENDING
        )
        .filter(_due(now))
        .filter(checks.sendable_q("utskick__account__"))
        .filter(utskick__account__is_demo=demo)
    )


def work_exists(now=None):
    """Finns något för fas 6? En EXISTS per sort (demot och resten)."""
    now = now or timezone.now()
    return email_due(now).exists() or email_due(now, demo=True).exists()


def accounts_with_due_email(now, only=None, demo=False):
    """Konton som får skicka och har köade mejl, äldsta utskicket först."""
    rows = email_due(now, demo=demo)
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


def claim(account, n, now, only=None):
    """Ta högst n köade e-postmottagare för kontot (äldsta utskicket, sedan
    lägsta pk) med select_for_update(skip_locked): status sending,
    claimed_at (klockans tid) och ett försök till. Anspråket sparas före
    anropet till SES (D.5)."""
    if n <= 0:
        return []
    real_now = timezone.now()
    with transaction.atomic():
        rows = (
            Recipient.objects.select_for_update(skip_locked=True, of=("self",))
            .filter(
                utskick__account=account,
                utskick__status=Utskick.Status.SENDING,
                channel=CHANNEL_EMAIL,
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
    """Tillbaka i kön utan att försöket räknas, och utan reservationen
    (sent_at). I ett avbrutet utskick blir de cancelled. Antalet."""
    ids = [r.pk for r in recipients]
    if not ids:
        return 0
    moved = Recipient.objects.filter(pk__in=ids, status=RS.SENDING).update(
        status=RS.QUEUED,
        not_before=not_before,
        claimed_at=None,
        sent_at=None,
        attempts=Greatest(F("attempts") - 1, 0),
    )
    Recipient.objects.filter(
        pk__in=ids, status=RS.QUEUED, utskick__status=Utskick.Status.CANCELLED
    ).update(status=RS.CANCELLED, not_before=None)
    return moved


def _queued(utskick_id=None, account_id=None):
    rows = Recipient.objects.filter(channel=CHANNEL_EMAIL, status=RS.QUEUED)
    if utskick_id:
        rows = rows.filter(utskick_id=utskick_id)
    if account_id:
        rows = rows.filter(utskick__account_id=account_id, utskick__status=Utskick.Status.SENDING)
    return rows


def defer_utskick(utskick, not_before):
    """Utskickets köade mejl väntar till not_before (provet), i en UPDATE."""
    return (
        _queued(utskick_id=utskick.pk)
        .filter(Q(not_before__isnull=True) | Q(not_before__lt=not_before))
        .update(not_before=not_before)
    )


def defer_account(account, not_before):
    """Kontots köade mejl väntar till not_before (dygnstaket)."""
    return (
        _queued(account_id=account.pk)
        .filter(Q(not_before__isnull=True) | Q(not_before__lt=not_before))
        .update(not_before=not_before)
    )


# ---------------------------------------------------------------------------
# Takten
# ---------------------------------------------------------------------------


def rate_for(switch=None):
    """Mejl per sekund (D.6): UTSKICK_EMAIL_PER_SECOND, högst 80 % av SES
    MaxSendRate när den är känd (Switchboard.ses_max_rate, utskick_daily)."""
    rate = float(getattr(settings, "UTSKICK_EMAIL_PER_SECOND", 10) or 10)
    switch = switch or Switchboard.get_solo()
    if switch.ses_max_rate:
        rate = min(rate, SES_RATE_SHARE * float(switch.ses_max_rate))
    return max(MIN_RATE, rate)


@dataclass
class Context:
    """Det en omgång minns: takten, när nästa mejl får gå, konton som vilar
    resten av ticken, sändningar per utskick (hälsokontrollen) och om
    slingan ska sluta (SES stoppade)."""

    rate: float = 10.0
    next_at: float = 0.0
    resting: set = field(default_factory=set)
    sent_by_utskick: Tally = field(default_factory=Tally)
    stopped: bool = False
    counts: Tally = field(default_factory=Tally)

    def pace(self):
        """Vänta tills nästa mejl får gå i takten."""
        now = time.monotonic()
        if self.next_at > now:
            time.sleep(self.next_at - now)
            now = self.next_at
        self.next_at = now + 1.0 / max(MIN_RATE, self.rate)

    def throttled(self):
        """SES bromsade: en sekunds paus och halva takten resten av ticken."""
        self.rate = max(MIN_RATE, self.rate / 2)
        time.sleep(THROTTLE_SLEEP)
        self.next_at = time.monotonic() + 1.0 / self.rate


# ---------------------------------------------------------------------------
# Avsändaren och svarsadressen
# ---------------------------------------------------------------------------


def _settings(account):
    from ..access import settings_for

    return settings_for(account)


def adx_domain():
    return (getattr(settings, "UTSKICK_ADX_MAIL_DOMAIN", "") or "utskick.adx.se").lower()


def is_adx_address(address):
    return str(address or "").strip().lower().endswith("@" + adx_domain())


def from_for(account, utskick=None, sender_domain=None):
    """(namn, adress) för mejlets From (D.6). En verifierad egen domän som
    är kontots: from_name <hej@exempelror.example> (utskickets avsändarnamn,
    annars domänens, annars display_name). Annars ADX-domänen:
    display_name <public_slug@utskick.adx.se>."""
    row = _settings(account)
    domain = sender_domain
    if domain is None and utskick is not None and utskick.sender_domain_id:
        domain = utskick.sender_domain
    if (
        domain is not None
        and domain.account_id == account.pk
        and domain.status == SenderDomain.Status.VERIFIED
    ):
        name = (getattr(utskick, "from_name", "") or "").strip() or domain.from_name
        return (name or row.display_name), domain.from_address
    slug = row.public_slug or f"konto{account.pk}"
    return row.display_name, f"{slug}@{adx_domain()}"


def own_reply_ok(account, row=None):
    """Får kontots egen svarsadress användas? Bekräftad med engångslänken,
    eller på en av kontots verifierade domäner (I.9)."""
    row = row or _settings(account)
    address = str(row.own_reply_to or "").strip().lower()
    if not address or "@" not in address:
        return False
    if row.own_reply_to_confirmed_at:
        return True
    domain = address.rsplit("@", 1)[1]
    return SenderDomain.objects.filter(
        account_id=account.pk, domain=domain, status=SenderDomain.Status.VERIFIED
    ).exists()


def reply_to_for(account, recipient=None, thread=None):
    """Reply-To (D.6, G.2): ett svar i en tråd i Inkorgen går alltid till
    trådens adress; ett utskick till kundens egen adress när
    email_reply_mode är own och adressen är godkänd (own_reply_ok), annars
    till Inkorgen med mottagarens token. "" utan mottagare och tråd."""
    if thread is not None:
        return tokens.reply_address(tokens.THREAD, account.pk, thread.pk)
    row = _settings(account)
    if row.email_reply_mode == UtskickSettings.REPLY_OWN and own_reply_ok(account, row):
        return str(row.own_reply_to).strip().lower()
    if recipient is not None:
        return tokens.reply_address(tokens.REPLY, account.pk, recipient.pk)
    return ""


# ---------------------------------------------------------------------------
# ADX-taket (D.6, F.8)
# ---------------------------------------------------------------------------


def month_bounds(now):
    return pricing.month_bounds(pricing.current_period(now))


def _counter_window(now):
    """Counter-fönstret för månadens testmejl och svar: nästa månads början
    (modulens text)."""
    return month_bounds(now)[1]


def adx_month_count(account, now=None):
    """Kontots mejl från ADX-domänen den här svenska månaden: utskickens
    mottagare (skickade, reserverade, oklara) plus testmejl och svar."""
    now = now or timezone.now()
    start, end = month_bounds(now)
    recipients = (
        Recipient.objects.filter(
            utskick__account_id=account.pk,
            utskick__sender_domain__isnull=True,
            channel=CHANNEL_EMAIL,
            sent_at__gte=start,
            sent_at__lt=end,
        )
        .filter(health.counted_q())
        .count()
    )
    return recipients + limits.count(ADX_SCOPE, str(account.pk), _counter_window(now))


def adx_cap():
    return int(getattr(settings, "UTSKICK_ADX_MONTHLY_MAIL_CAP", 2000) or 0)


def adx_cap_left(account, now=None):
    """Hur många mejl kontot får skicka från ADX-domänen till den här
    månaden (Granska, I.6, och förkontrollerna)."""
    return max(0, adx_cap() - adx_month_count(account, now))


_MONTHS = (
    "januari",
    "februari",
    "mars",
    "april",
    "maj",
    "juni",
    "juli",
    "augusti",
    "september",
    "oktober",
    "november",
    "december",
)


def _tal(number):
    from apps.flamingo.templatetags.flamingo_app import tal

    return tal(number)


def adx_cap_text(account, utskick=None, need=0, now=None):
    """Bannern för pausen adx_mail_cap (I.5)."""
    now = now or timezone.now()
    used = adx_month_count(account, now)
    month = _MONTHS[timezone.localtime(now, pricing.STOCKHOLM).month - 1]
    if need <= 0 and utskick is not None:
        need = _queued(utskick_id=utskick.pk).count()
    return (
        f"Pausat: {_tal(used)} av {_tal(adx_cap())} mejl från ADX-domänen är skickade i "
        f"{month} och utskicket behöver {_tal(need)} till. Verifiera din egen domän under "
        "Inställningar, eller skicka till färre mottagare."
    )


def _lock(account):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [limits.ADX_MAIL_LOCK + int(account.pk)])


# ---------------------------------------------------------------------------
# Kontrollerna före varje mejl
# ---------------------------------------------------------------------------


@dataclass
class Gate:
    """Kontrollernas utfall för en mottagare. defer: tillbaka i kön
    (not_before när den får försökas igen). skip: personen hoppas över
    (reason är Recipient.SkipReason). pause: utskicket pausas (reason är
    Utskick.PauseReason, note bannerns text). Annars: skicka, med
    utskicket, inställningarna, kontakten och om provet gäller."""

    defer: bool = False
    skip: bool = False
    pause: bool = False
    reason: str = ""
    note: str = ""
    not_before: object = None
    utskick: object = None
    settings_row: object = None
    contact: object = None
    probe: bool = False

    @property
    def ok(self):
        return not (self.defer or self.skip or self.pause)


def _week_count(recipient, utskick, now):
    from .. import audience

    counts = audience.week_counts(
        utskick.account_id, [recipient.contact_id], CHANNEL_EMAIL, now, exclude_pk=recipient.pk
    )
    return counts.get(recipient.contact_id, 0)


def email_checks(recipient, now=None):
    """Kontrollerna för en e-postmottagare precis före sändningen (D.6,
    D.8, D.9), med färska läsningar. Uppskjutande: utskicket är inte
    sending, provets väntan, kontot får inte skicka, e-posten är av.
    Pausande: kontots hälsospärr (account_health), en avsändardomän som
    inte är kontots verifierade (provider), ett prov som inte gick
    (bounces eller complaints). Per person: borttagen kontakt, ingen adress,
    spärrad, ny adress, studsad eller samtycket (reklam), veckotaket."""
    from .. import consent as consents

    now = now or timezone.now()
    utskick = (
        Utskick.objects.select_related("account", "account__customer", "sender_domain")
        .filter(pk=recipient.utskick_id)
        .first()
    )
    if utskick is None or utskick.status != Utskick.Status.SENDING:
        return Gate(defer=True, reason="not_sending")
    if utskick.hold_until and utskick.hold_until > now:
        return Gate(defer=True, reason="hold", not_before=utskick.hold_until)
    account = utskick.account
    row = UtskickSettings.objects.filter(account_id=account.pk).first()
    why = checks.sendable(account, row)
    if why:
        return Gate(defer=True, reason=why)
    if not state.email_live():
        return Gate(defer=True, reason="email_off")
    if row.email_blocked_at:
        return Gate(pause=True, reason=R.ACCOUNT_HEALTH, note=health.BLOCKED_TEXT, utskick=utskick)
    domain = utskick.sender_domain if utskick.sender_domain_id else None
    if domain is not None and (
        domain.account_id != account.pk or domain.status != SenderDomain.Status.VERIFIED
    ):
        return Gate(pause=True, reason=R.PROVIDER, note=DOMAIN_TEXT, utskick=utskick)
    probe = health.probe_state(utskick, now)
    if probe == "hold":
        return Gate(defer=True, reason="hold", not_before=utskick.hold_until, utskick=utskick)
    if probe == "failed":
        verdict = health.utskick_health(utskick, probe=True)
        return Gate(
            pause=True,
            reason=verdict.reason or R.BOUNCES,
            note=verdict.text,
            utskick=utskick,
        )

    # Per person.
    if recipient.contact_id is None:
        return Gate(skip=True, reason=Recipient.SkipReason.DELETED)
    contact = Contact.objects.filter(pk=recipient.contact_id, account_id=account.pk).first()
    if contact is None:
        return Gate(skip=True, reason=Recipient.SkipReason.DELETED)
    address = recipient.address
    if not address:
        return Gate(skip=True, reason=Recipient.SkipReason.NO_ADDRESS)
    value_hash = keys.value_hash(CHANNEL_EMAIL, address)
    if Suppression.objects.filter(
        account_id=account.pk, channel=CHANNEL_EMAIL, value_hash=value_hash
    ).exists():
        return Gate(skip=True, reason=Recipient.SkipReason.SUPPRESSED)
    if keys.clean_value(CHANNEL_EMAIL, contact.email) != keys.clean_value(CHANNEL_EMAIL, address):
        return Gate(skip=True, reason=Recipient.SkipReason.ADDRESS_CHANGED)
    purpose = utskick.purpose if utskick.purpose in (REKLAM, INFORMATION) else REKLAM
    reason = consents.ineligible_reason(contact, CHANNEL_EMAIL, purpose, suppressed=False)
    if reason:
        return Gate(skip=True, reason=reason)
    if purpose == REKLAM:
        from .. import audience

        if _week_count(recipient, utskick, now) >= audience.weekly_cap(row, CHANNEL_EMAIL):
            return Gate(skip=True, reason=Recipient.SkipReason.WEEKLY_CAP)
    return Gate(
        utskick=utskick,
        settings_row=row,
        contact=contact,
        probe=probe == "sending",
    )


# ---------------------------------------------------------------------------
# Mejlet
# ---------------------------------------------------------------------------


class NotFrozen(Exception):
    """Utskicket saknar sin frysta ögonblicksbild (freeze_email gick inte)."""


def is_frozen(utskick):
    """Har mejlet frysts (freeze_email gick hela vägen)? Ögonblicksbilden
    får sin länktabell ("links") sist."""
    snapshot = utskick.email_snapshot
    return isinstance(snapshot, dict) and isinstance(snapshot.get("links"), dict)


def compose(utskick, recipient, *, account=None):
    """Utskicksmejlet till en mottagare (F.4): HTML och text från
    renderaren med den frysta ögonblicksbilden, ämnesraden, From, Reply-To
    och List-Unsubscribe med ettklicket (D.6). NotFrozen utan
    ögonblicksbilden: mejlet byggs aldrig ur det levande utkastet (då vore
    länkarna ospårade och mejlet ett annat än det som granskades)."""
    from ..email import render
    from ..email import text as email_text

    account = account or utskick.account
    if not is_frozen(utskick):
        raise NotFrozen
    ctx = render.context_for(
        utskick,
        mode=render.SEND,
        recipient=recipient,
        snapshot=utskick.email_snapshot,
    )
    html = render.render_html(utskick, ctx, render.SEND)
    body = email_text.render_text(utskick, ctx)
    subject = render.subject_for(utskick, ctx)
    name, address = from_for(account, utskick)
    value_hash = keys.value_hash(CHANNEL_EMAIL, recipient.address)
    headers = mime.unsubscribe_headers(
        links.unsubscribe_url(account.pk, value_hash),
        links.mailto_unsubscribe(account.pk, recipient.pk),
    )
    reply_to = reply_to_for(account, recipient=recipient)
    if reply_to:
        headers["Reply-To"] = reply_to
    return transport.OutgoingMail(
        to=recipient.address,
        from_name=name,
        from_addr=address,
        subject=subject,
        text=body,
        html=html,
        headers=headers,
    )


def compose_test(utskick, address, contact=None):
    """Testmejlet (F.8): som utskicksmejlet med kontakten som visas (eller
    utan), ämnesraden med "Test: " först. Svarsadressen bär NO_RECIPIENT
    (ett svar på testet hamnar i Inkorgen). Inget List-Unsubscribe: ett
    tryck på Avsluta prenumerationen i testet skulle annars avregistrera
    testadressen eller den visade kontakten från kundens egna mejl, och
    sidfotens länkar går till klick.adx.se/ i ett test (renderaren)."""
    from ..email import render
    from ..email import text as email_text

    account = utskick.account
    ctx = render.context_for(utskick, mode=render.PREVIEW, contact=contact, test=True)
    html = render.render_html(utskick, ctx, render.PREVIEW)
    body = email_text.render_text(utskick, ctx)
    subject = TEST_PREFIX + render.subject_for(utskick, ctx)
    name, from_addr = from_for(account, utskick)
    headers = {
        "Reply-To": reply_to_for(account)
        or tokens.reply_address(tokens.REPLY, account.pk, NO_RECIPIENT)
    }
    return transport.OutgoingMail(
        to=address,
        from_name=name,
        from_addr=from_addr,
        subject=subject,
        text=body,
        html=html,
        headers=headers,
    )


# ---------------------------------------------------------------------------
# En mottagare
# ---------------------------------------------------------------------------


def _skip(recipient, reason):
    Recipient.objects.filter(pk=recipient.pk, status=RS.SENDING).update(
        status=RS.SKIPPED, skip_reason=reason, claimed_at=None, not_before=None, sent_at=None
    )


def _fail(recipient, error):
    Recipient.objects.filter(pk=recipient.pk, status=RS.SENDING).update(
        status=RS.FAILED, error=str(error)[:200], sent_at=None
    )


def _pause(utskick, reason, note, now):
    """Pausa utskicket av slingan (provider, hälsan, taket). Byrån larmas
    för provider och hälsan."""
    if state.pause(utskick, reason, note=note or "", now=now):
        logger.warning("Utskick %s: pausat av e-postslingan (%s)", utskick.pk, reason)
        if reason not in (R.ADX_MAIL_CAP,):
            alerts.utskick_paused(utskick, reason, note, now=now)
        return True
    return False


def reserve(recipient, utskick, account, now=None, *, probe=False, settings_row=None):
    """Taken under kontots lås (D.6, D.9): "" när mottagaren får sent_at
    (reserverad), annars "adx_cap", "daily_cap" eller "probe"."""
    real_now = timezone.now()
    with transaction.atomic():
        _lock(account)
        if utskick.sender_domain_id is None and adx_month_count(account, real_now) >= adx_cap():
            return "adx_cap"
        left = health.daily_cap_left(account, real_now, settings_row)
        if left is not None and left <= 0:
            return "daily_cap"
        if probe and health.probe_count(utskick) >= health.PROBE_SIZE:
            return "probe"
        Recipient.objects.filter(pk=recipient.pk, status=RS.SENDING).update(sent_at=real_now)
    return ""


def _failure_streak(utskick):
    """De senaste FAILURES_IN_ROW mottagarna med ett utfall nekades alla av SES."""
    last = list(
        Recipient.objects.filter(utskick_id=utskick.pk, channel=CHANNEL_EMAIL)
        .filter(claimed_at__isnull=False)
        .exclude(status__in=(RS.QUEUED, RS.SENDING, RS.SKIPPED))
        .order_by("-claimed_at", "-pk")
        .values_list("status", "error")[:FAILURES_IN_ROW]
    )
    return len(last) == FAILURES_IN_ROW and all(
        status == RS.FAILED and str(error).startswith(REJECTED_PREFIX) for status, error in last
    )


def _email_off(code, now):
    """SES har pausat kontot: e-posten stängs av för hela ADX (D.6)."""
    Switchboard.get_solo()
    changed = Switchboard.objects.filter(pk=Switchboard.SOLO_PK, email_enabled=True).update(
        email_enabled=False, changed_at=now, note=f"SES svarade {code}"[:200]
    )
    logger.error("Utskick: SES har pausat e-posten (%s); email_enabled av", code)
    alerts.agency(
        "Utskick: SES har pausat e-posten",
        [
            f"SES svarade {code}. E-postutskicken är avstängda (email_enabled av).",
            "Se kontots läge i SES i eu-west-1 (aws sesv2 get-account) och "
            "Leveranshälsa per kund. Slå på e-posten igen på /manage/utskick/ när det "
            "är åtgärdat.",
        ],
        once=f"ses_paused:{code}"[:80],
        now=now,
    )
    return bool(changed)


def process(recipient, account, now, ctx):
    """Ett mejl (D.6). Returnerar utfallet: sent, unknown, deferred,
    skipped, paused, failed, throttled, adx_cap, daily_cap, hold, stopped
    eller error."""
    gate = email_checks(recipient, now)
    if gate.defer:
        requeue([recipient], gate.not_before)
        if gate.reason == "hold" and gate.utskick is not None and gate.not_before:
            defer_utskick(gate.utskick, gate.not_before)
        return "deferred"
    if gate.skip:
        _skip(recipient, gate.reason)
        return "skipped"
    if gate.pause:
        requeue([recipient])
        _pause(gate.utskick, gate.reason, gate.note, now)
        return "paused"
    utskick = gate.utskick
    try:
        mail = compose(utskick, recipient, account=account)
    except keys.KeyMismatch:
        raise
    except NotFrozen:
        logger.error("Utskick %s: mejlet saknar sin frysta ögonblicksbild", utskick.pk)
        requeue([recipient])
        _pause(utskick, R.CONTENT, NOT_FROZEN_TEXT, now)
        return "paused"
    except Exception:
        logger.exception("Utskick %s: mejlet för mottagare %s gick inte", utskick.pk, recipient.pk)
        if recipient.attempts >= MAX_ATTEMPTS:
            _fail(recipient, NOT_BUILT_TEXT)
            return "failed"
        Recipient.objects.filter(pk=recipient.pk, status=RS.SENDING).update(
            status=RS.QUEUED, claimed_at=None, not_before=timezone.now() + timedelta(minutes=5)
        )
        return "error"
    held = reserve(
        recipient, utskick, account, now, probe=gate.probe, settings_row=gate.settings_row
    )
    if held == "adx_cap":
        requeue([recipient])
        _pause(utskick, R.ADX_MAIL_CAP, adx_cap_text(account, utskick, now=now), now)
        return "adx_cap"
    if held == "daily_cap":
        until = health.next_day(timezone.now())
        requeue([recipient], until)
        defer_account(account, until)
        ctx.resting.add(account.pk)
        return "daily_cap"
    if held == "probe":
        health.probe_state(utskick, now)
        requeue([recipient], utskick.hold_until)
        if utskick.hold_until:
            defer_utskick(utskick, utskick.hold_until)
        return "hold"
    ctx.pace()
    sent = transport.send(
        mail,
        kind=transport.UTSKICK,
        account_id=account.pk,
        utskick_id=utskick.pk,
        recipient_id=recipient.pk,
    )
    return apply(recipient, sent, utskick, account, now, ctx, probe=gate.probe)


def apply(recipient, sent, utskick, account, now, ctx, *, probe=False):
    """Utfallet från transport.send på mottagaren, utskicket och slingan."""
    real_now = timezone.now()
    if sent.ok:
        Recipient.objects.filter(pk=recipient.pk).update(
            ses_message_id=str(sent.message_id or "")[:100], error=""
        )
        Recipient.objects.filter(pk=recipient.pk, status__in=(RS.SENDING, RS.UNKNOWN)).update(
            status=RS.SENT
        )
        UtskickSettings.objects.filter(
            account_id=account.pk, email_first_sent_at__isnull=True
        ).update(email_first_sent_at=real_now)
        ctx.sent_by_utskick[utskick.pk] += 1
        if ctx.sent_by_utskick[utskick.pk] % health.CHECK_EVERY == 0:
            health.check_utskick(utskick, now)
        if probe and health.probe_count(utskick) >= health.PROBE_SIZE:
            if health.probe_state(utskick, now) == "hold" and utskick.hold_until:
                defer_utskick(utskick, utskick.hold_until)
        return "sent"
    if sent.unknown:
        Recipient.objects.filter(pk=recipient.pk, status=RS.SENDING).update(status=RS.UNKNOWN)
        logger.warning(
            "Utskick %s: mottagare %s oklar efter SES (%s)", utskick.pk, recipient.pk, sent.error
        )
        # Ett oklart svar (timeout, 5xx) kommer sällan ensamt: slingan slutar
        # för den här ticken, så att några minuters fel hos SES inte gör
        # tusentals mottagare oklara (de skickas aldrig igen). Resten tas
        # nästa tick.
        ctx.stopped = True
        alerts.agency(
            "Utskick: SES svarar inte säkert",
            [UNKNOWN_ALERT, f"Senaste felet: {sent.error or 'okänt'}."],
            once="ses_unknown",
            now=now,
        )
        return "unknown"
    if sent.retry and not sent.stop:
        requeue([recipient])
        if sent.error == "connect":
            ctx.stopped = True
            return "stopped"
        ctx.throttled()
        return "throttled"
    if sent.stop:
        requeue([recipient])
        if sent.error in transport.PAUSED_CODES:
            _email_off(sent.error, now)
            ctx.stopped = True
            return "ses_paused"
        if sent.error in transport.MAIL_FROM_CODES:
            _pause(utskick, R.PROVIDER, MAIL_FROM_TEXT, now)
            return "paused"
        ctx.stopped = True
        return "stopped"
    _fail(recipient, f"{REJECTED_PREFIX} ({sent.error or 'okänt fel'}).")
    if _failure_streak(utskick):
        _pause(utskick, R.PROVIDER, "Fem mejl i rad nekades av e-posttjänsten.", now)
    return "failed"


# ---------------------------------------------------------------------------
# Omgången
# ---------------------------------------------------------------------------

#: Utfall som betyder att slingan kom någonstans.
PROGRESS = frozenset(
    {"sent", "unknown", "skipped", "failed", "paused", "adx_cap", "daily_cap", "hold"}
)


def _no_events_alert(now):
    logger.error("Utskick: e-postutskicken väntar, händelsekön saknas")
    alerts.agency(
        "Utskick: e-postutskicken väntar på händelsekön",
        [NO_EVENTS_TEXT],
        once="email_no_events",
        now=now,
    )


def send_due(now=None, deadline=None, only=None):
    """E-postslingan till deadline (time.monotonic()). Returnerar antal per
    utfall (bara antal: sammanfattningen går till backups/utskick.log)."""
    now = now or timezone.now()
    deadline = deadline if deadline is not None else time.monotonic() + 30
    switch = Switchboard.get_solo()
    ctx = Context(rate=rate_for(switch))
    counts = ctx.counts

    def time_left():
        return deadline - time.monotonic()

    for account in accounts_with_due_email(now, only, demo=True):
        simulated = simulate(account, now, only)
        if simulated:
            counts["simulated"] += simulated
    if not state.email_live():
        # D.8: utskicken väntar ("Väntar: sändningen är tillfälligt stoppad av ADX").
        if email_due(now).exists():
            counts["email_off"] += 1
        return dict(counts)
    if not settings.DEBUG and not transport.configuration_set():
        _no_events_alert(now)
        counts["no_events"] += 1
        return dict(counts)
    while time_left() > SAFETY_SECONDS and not ctx.stopped:
        accounts = [a for a in accounts_with_due_email(now, only) if a.pk not in ctx.resting]
        if not accounts:
            break
        progressed = False
        for account in accounts:
            if time_left() <= SAFETY_SECONDS or ctx.stopped:
                break
            claimed = claim(account, BATCH, now, only)
            for index, recipient in enumerate(claimed):
                if time_left() <= SAFETY_SECONDS or ctx.stopped:
                    requeue(claimed[index:])
                    break
                try:
                    result = process(recipient, account, now, ctx)
                except keys.KeyMismatch:
                    requeue(claimed[index:])
                    raise
                except Exception:
                    # De som inte hann prövas går tillbaka i kön; den här kan
                    # ha nått SES och lämnas åt återhämtningen (unknown, D.5).
                    requeue(claimed[index + 1 :])
                    raise
                counts[result] += 1
                if result in PROGRESS:
                    progressed = True
                if account.pk in ctx.resting:
                    requeue(claimed[index + 1 :])
                    break
        if not progressed:
            break
    return dict(counts)


# ---------------------------------------------------------------------------
# Enstaka mejl: test, svar, prov och bekräftelselänken
# ---------------------------------------------------------------------------


def _unhit(account, now):
    from ..models import Counter

    Counter.objects.filter(
        scope=ADX_SCOPE, key=str(account.pk), window=_counter_window(now), count__gt=0
    ).update(count=F("count") - 1)


def deliver(account, mail, *, kind, utskick=None, recipient=None, now=None):
    """Ett enstaka mejl (testmejl, svar från Inkorgen, byråns provmejl,
    länken för en egen svarsadress) genom transporten, med konfigurationssetet
    och taggarna. Demokontot nekas. Testmejl och svar kräver att e-posten är
    påslagen (D.8) och att kontot får skicka; från ADX-domänen räknas de mot
    ADX-taket (under kontots lås). Provmejlet och bekräftelselänken går före
    email_enabled (J S3 steg 7, I.9). Returnerar alltid ett transport.Sent;
    error_text(sent) ger texten för vyn. KeyMismatch när processens nycklar
    inte stämmer (H.7)."""
    from .sms_wrapper import DemoRefused

    now = now or timezone.now()
    if account is None or getattr(account, "is_demo", False):
        return transport.Sent(ok=False, error="demo", stop=True)
    if kind not in transport.KINDS or kind == transport.DOI:
        raise ValueError(f"deliver skickar inte slaget {kind!r}.")
    if kind in (transport.UTSKICK, transport.TEST, transport.REPLY):
        if not state.email_live():
            return transport.Sent(ok=False, error="email_off", stop=True)
        why = checks.sendable(account)
        if why == R.BLOCKED:
            return transport.Sent(ok=False, error="blocked", stop=True)
        if why:
            return transport.Sent(ok=False, error="disabled", stop=True)
    keys.require_fingerprints()
    counted = kind in ADX_COUNTED_KINDS and is_adx_address(mail.from_addr)
    if counted:
        with transaction.atomic():
            _lock(account)
            if adx_month_count(account, now) >= adx_cap():
                return transport.Sent(ok=False, error="adx_cap", stop=True)
            limits.hit(ADX_SCOPE, str(account.pk), _counter_window(now), 10**9)
    try:
        sent = transport.send(
            mail,
            kind=kind,
            account_id=account.pk,
            utskick_id=getattr(utskick, "pk", None),
            recipient_id=getattr(recipient, "pk", None),
        )
    except DemoRefused:
        sent = transport.Sent(ok=False, error="demo", stop=True)
    if counted and not (sent.ok or sent.unknown):
        _unhit(account, now)
    logger.info(
        "Utskick: %s för konto %s (utskick %s): %s",
        kind,
        account.pk,
        getattr(utskick, "pk", None),
        "skickat" if sent.ok else ("oklart" if sent.unknown else sent.error),
    )
    return sent


def error_text(sent):
    """Texten för vyn när ett enstaka mejl inte gick (deliver, send_test)."""
    if sent is None or sent.ok:
        return ""
    if sent.unknown:
        return ERROR_TEXTS["unknown"]
    if sent.error in ERROR_TEXTS:
        return ERROR_TEXTS[sent.error]
    if sent.retry:
        return ERROR_TEXTS["retry"]
    return DEFAULT_ERROR_TEXT


def send_test(utskick, *, address, contact=None, actor, now=None):
    """Testmejlet (F.8), anropat av redigeraren och utskick_test (byggare
    B; vyn sköter vem som får testet, byråns kryssruta och innehållets
    kontroller). Här: aldrig demot, e-posten påslagen, kontot får skicka,
    adressen giltig och inte avregistrerad, högst TEST_SENDS_PER_DAY test per
    konto och dygn (Counter "test_send", samma som testsms:en; vyn räknar
    inte själv), ämnesraden med "Test: ", ADX-taket (deliver) och
    Event(kind="test_send") när testet gick till en kontakt. Varje försök
    loggas med användaren och om det var byrån (I.4)."""
    from .. import contacts as register
    from .. import normalize
    from .. import suppression as suppressions

    now = now or timezone.now()
    account = utskick.account
    user = getattr(actor, "user", None)
    staff = bool(getattr(actor, "staff", False))

    def done(sent):
        logger.info(
            "Utskick %s: testmejl av användare %s (byrån: %s): %s",
            utskick.pk,
            getattr(user, "pk", None),
            staff,
            "skickat" if sent.ok else ("oklart" if sent.unknown else sent.error),
        )
        return sent

    if account.is_demo:
        return done(transport.Sent(ok=False, error="demo", stop=True))
    try:
        address = normalize.email(address)
    except normalize.InvalidValue:
        address = ""
    if not address:
        return done(transport.Sent(ok=False, error="address"))
    if contact is not None and contact.account_id != account.pk:
        return done(transport.Sent(ok=False, error="address"))
    if not state.email_live():
        return done(transport.Sent(ok=False, error="email_off", stop=True))
    why = checks.sendable(account)
    if why:
        error = "blocked" if why == R.BLOCKED else "disabled"
        return done(transport.Sent(ok=False, error=error, stop=True))
    if suppressions.is_suppressed(account, CHANNEL_EMAIL, value=address):
        return done(transport.Sent(ok=False, error="suppressed"))
    if limits.hit("test_send", str(account.pk), limits.day_window(now), TEST_SENDS_PER_DAY):
        return done(transport.Sent(ok=False, error="test_limit"))
    try:
        mail = compose_test(utskick, address, contact)
    except keys.KeyMismatch:
        return done(transport.Sent(ok=False, error="keys", stop=True))
    except Exception:
        logger.exception("Utskick %s: testmejlet gick inte att skapa", utskick.pk)
        return done(transport.Sent(ok=False, error="render"))
    try:
        sent = deliver(account, mail, kind=transport.TEST, utskick=utskick, now=now)
    except keys.KeyMismatch:
        return done(transport.Sent(ok=False, error="keys", stop=True))
    if sent.ok and contact is not None:
        register.record_event(
            contact,
            "test_send",
            data={
                "utskick": utskick.pk,
                "channel": CHANNEL_EMAIL,
                "user": getattr(user, "pk", None),
                "staff": staff,
            },
            activity=False,
        )
    return done(sent)


def probe_mail(account, address, *, staff_name=""):
    """Byråns provmejl (J S3 steg 7, manage_sending.probe): från kontots
    avsändare på ADX-domänen, med List-Unsubscribe och ettklicket som i ett
    utskick, Reply-To till Inkorgen (NO_RECIPIENT) och en kort text."""
    row = _settings(account)
    name, from_addr = from_for(account)
    value_hash = keys.value_hash(CHANNEL_EMAIL, address)
    headers = mime.unsubscribe_headers(
        links.unsubscribe_url(account.pk, value_hash),
        links.mailto_unsubscribe(account.pk, NO_RECIPIENT),
    )
    headers["Reply-To"] = tokens.reply_address(tokens.REPLY, account.pk, NO_RECIPIENT)
    text = (
        f"Provmejl från ADX Flamingo för {row.display_name}.\n\n"
        "Svara på mejlet för att prova Inkorgen. Avregistreringen i mejlprogrammet "
        "och länken nedan ska fungera.\n\n"
        f"Avregistrera dig: {links.unsubscribe_url(account.pk, value_hash)}\n"
    )
    if staff_name:
        text += f"\nSkickat av {staff_name}.\n"
    return transport.OutgoingMail(
        to=address,
        from_name=name,
        from_addr=from_addr,
        subject=f"Provmejl från {row.display_name}",
        text=text,
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Demot och återhämtningen
# ---------------------------------------------------------------------------


def simulate(account, now, only=None):
    """Demokontot skickar aldrig: köade e-postmottagare blir levererade med
    simulated=True, utan anrop till SES (D12)."""
    if not account.is_demo:
        raise ValueError("Bara demokontot simuleras.")
    sending = Utskick.objects.filter(account=account, status=Utskick.Status.SENDING)
    if only:
        sending = sending.filter(pk=only)
    total = 0
    for utskick in sending:
        total += Recipient.objects.filter(
            utskick=utskick, channel=CHANNEL_EMAIL, status=RS.QUEUED
        ).update(
            status=RS.DELIVERED,
            simulated=True,
            sent_at=now,
            delivered_at=now,
            not_before=None,
        )
    return total


def stale_unknown(now=None):
    """D.5: en e-postmottagare som stått som unknown i 24 timmar utan att
    någon SES-händelse adopterat den blir failed ("Oklart om mejlet
    skickades"). Den skickas aldrig igen. Antalet."""
    now = now or timezone.now()
    cutoff = now - timedelta(hours=UNKNOWN_AFTER_HOURS)
    rows = Recipient.objects.filter(channel=CHANNEL_EMAIL, status=RS.UNKNOWN).filter(
        Q(sent_at__lt=cutoff) | Q(sent_at__isnull=True, claimed_at__lt=cutoff)
    )
    changed = rows.update(status=RS.FAILED, error=UNKNOWN_TEXT)
    if changed:
        logger.warning("Utskick: %s oklara mejl blev failed efter 24 timmar", changed)
    return changed


# ---------------------------------------------------------------------------
# Frysningen och länkarna (D.3, E.8, F.4)
# ---------------------------------------------------------------------------


def own_pages(account):
    """{adress: kampanj} för kontots Flamingo-sidor (också
    email.blocks.own_page)."""
    from apps.flamingo.exports import landing_page_url
    from apps.flamingo.models import Campaign

    pages = {}
    for campaign in Campaign.objects.filter(account=account):
        try:
            pages[landing_page_url(campaign).rstrip("/")] = campaign
        except Exception:  # noqa: BLE001 - en kampanj utan sida är ingen länk
            continue
    return pages


def _is_web(url):
    return urlsplit(str(url or "")).scheme.lower() in ("http", "https")


def email_link_problems(utskick):
    """Mejlets länkar till webbplatser som väntar på ADX eller som ADX
    nekat (E.8), som state.content_problems ser före frysningen; efter
    frysningen är de TrackedLink och prövas av links.link_problems. Kontots
    egna Flamingo-sidor är alltid fria."""
    if not utskick.has_email:
        return []
    doc = utskick.email_doc if isinstance(utskick.email_doc, dict) else {}
    if not doc.get("blocks"):
        return []
    from ..email import blocks

    urls = [u for u in blocks.urls(blocks.active_blocks(utskick, doc)) if _is_web(u)]
    if not urls:
        return []
    pages = own_pages(utskick.account)
    problems = []
    pending = False
    for url in urls:
        if blocks.own_page(utskick.account, url, pages=pages) is not None:
            continue
        try:
            links.clean_external(utskick.account, url)
        except links.HostPending:
            pending = True
        except links.LinkRefused as exc:
            text = str(exc)
            if text not in problems:
                problems.append(text)
    if pending:
        problems.insert(0, links.PENDING_TEXT)
    return problems


def _tracked_link(utskick, spot, pages):
    """TrackedLink för en plats i mejlet: kind lp för kontots Flamingo-sida,
    annars external med den rensade adressen. LinkRefused för en adress som
    inte får användas. Samma (block, plats) ger samma rad."""
    from ..email import blocks

    url = str(spot.url or "")
    # Samma tolkning som redigeraren och Granska (blocks.own_page): en sida
    # med #ankare eller ?fråga är fortfarande kontots egen.
    page = blocks.own_page(utskick.account, url, pages=pages)
    if page is not None:
        from apps.flamingo import answers as form_answers
        from apps.flamingo.exports import landing_page_url

        destination = landing_page_url(page)
        # Förvalet i sidans formulär (?val=tjanst.reparation) följer med, som
        # ankaret (links.bare_destination). Sidan prövar det själv.
        preselect = form_answers.preselect_of(url)
        if preselect:
            destination = form_answers.with_preselect(destination, preselect)
        fragment = urlsplit(url).fragment
        if fragment:
            # Ankaret följer med till sidan (links.bare_destination).
            destination = f"{destination}#{fragment}"
        values = {
            "kind": TrackedLink.Kind.LP,
            "campaign": page,
            "destination": destination[: links.URL_MAX],
        }
    else:
        values = {
            "kind": TrackedLink.Kind.EXTERNAL,
            "campaign": None,
            "destination": links.clean_external(utskick.account, url, allow_pending=True)[
                : links.URL_MAX
            ],
        }
    values["label"] = str(spot.label or "")[:120]
    values["account"] = utskick.account
    link, _created = TrackedLink.objects.update_or_create(
        utskick=utskick,
        key="",
        block_id=str(spot.block_id)[:16],
        position=int(spot.position),
        defaults=values,
    )
    return link


def freeze_email(utskick, now=None):
    """Slutet av frysningen för ett utskick med köade mejl (D.3, F.4):
    bildernas media-id prövas igen mot kontot (H.1), en TrackedLink per
    länkplats i mejlet (render.collect_links) och ögonblicksbilden
    (render.snapshot) med {"links": {"<block_id>:<plats>": link_id}} i
    Utskick.email_snapshot. Returnerar "" när det gick, annars texten som
    pausar utskicket med content."""
    from ..access import ForeignIds, owned_ids

    if not utskick.has_email:
        return ""
    if not Recipient.objects.filter(
        utskick_id=utskick.pk, channel=CHANNEL_EMAIL, status=RS.QUEUED
    ).exists():
        return ""
    from apps.flamingo.models import MediaAsset

    from ..email import blocks, render

    now = now or timezone.now()
    doc = utskick.email_doc if isinstance(utskick.email_doc, dict) else {}
    try:
        owned_ids(MediaAsset, utskick.account, blocks.media_ids(blocks.active_blocks(utskick, doc)))
    except ForeignIds:
        logger.error("Utskick %s: mejlet har bilder som inte är kontots", utskick.pk)
        return "Mejlet innehåller bilder som inte finns hos dig. Byt bilderna och bekräfta igen."
    # Ögonblicksbilden först: länkplatserna räknas på exakt det som skickas.
    snapshot = dict(render.snapshot(utskick, now=now) or {})
    pages = own_pages(utskick.account)
    table = {}
    try:
        for spot in render.collect_links(utskick, doc, data=snapshot):
            if not _is_web(spot.url):
                continue
            link = _tracked_link(utskick, spot, pages)
            table[f"{spot.block_id}:{spot.position}"] = link.pk
    except links.LinkRefused as exc:
        return str(exc)
    snapshot["links"] = table
    Utskick.objects.filter(pk=utskick.pk).update(email_snapshot=snapshot)
    utskick.email_snapshot = snapshot
    return ""
