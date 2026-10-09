"""
Bekräftelsen av e-post: dubbel opt-in (README J S1 "DOI in S1", E.2, E.5,
D.8, H.5, H.6). Bekräftelse-sms (S2) kommer hit också.

    doi_ready() -> bool                 byrån har klarmarkerat bekräftelsemejlen
    offers_email(user=None) -> bool     får anmälan och Mina utskick erbjuda e-post?
    requeue(consent, now)               personen bad igen: ett nytt mejl om gränserna tillåter
    queued(now), due(now)               kön, och det ticken får skicka av den
    work_exists(now) -> bool            finns något för ticken att skicka?
    send_due(now, deadline) -> dict     tickens fas 3: skicka mejlen som väntar
    build_doi(consent, row, now)        mejlet (transport.OutgoingMail)
    doi_consent(ref) -> Consent | None  samtycket som en äkta länk gäller
    confirm(consent, ip_hash, now)      personen klickade Ja, bekräfta (en POST)
    absolute(path)                      adressen med https://adx.se framför

Ett formulär (anmälan, landningssidan, Mina utskick) sätter e-posten till
pending genom consent.set_status; raden står då i kön (confirm_sent_at
tomt, delindexet utskick_consent_to_confirm) tills ticken skickat mejlet.
confirm_sent_at och confirm_count skrivs bara här (de ändrar ingen status):
confirm_sent_at är när ticken tog hand om raden (skickat, eller hoppat över
vid en gräns eller ett fel som inte ska göras om), confirm_count hur många
mejl som faktiskt gått.

Mejlet: från "<display_name>" <UTSKICK_DOI_FROM> (bekrafta@utskick.adx.se),
inget Reply-To, mallarna utskick/mail/doi.* utan något som personen skrev
(inget förnamn; företaget är byråns display_name), en länk till
integritetstexten och bekräftelselänken (tokens.doi_token, 14 dagar).
Länken bekräftar aldrig med ett GET: sidan har en knapp (E.5).

Gränser (Counter, fasta fönster): 1 mejl per adress och konto per svenskt
dygn, 3 per adress i hela ADX per dygn, PER_ACCOUNT_HOUR per konto och
timme (en kunds anmälningssida får inte ta hela ADX:s utrymme; byrån larmas
när gränsen nås) och 300 per timme i hela ADX. Bara konton som får skicka
(utskick, Flamingo och kunden på, sändningen inte stoppad för kunden),
aldrig demokontot, och bara med ett godkänt biträdesavtal (nya
bekräftelser kräver det).

Kön skickas bara när byrån klarmarkerat bekräftelsemejlen
(Switchboard.doi_ready_at). "Stoppa all sändning" och att ta bort
markeringen tar alltså också stopp på bekräftelsemejlen; raderna står kvar
i kön (högst QUEUE_MAX_AGE). Före markeringen går bara byråns egna
provanmälningar (samtycket ändrat av en staff-användare, checklistans steg
6) ut.

Var mejlet går ut avgör transporten: SES med UTSKICK_EMAIL_LIVE, annars en
.eml-fil lokalt med DEBUG, annars ingenting (raderna står kvar i kön).
Anmälningssidan och kryssrutan för e-post finns bara när byrån satt
Switchboard.doi_ready_at (byrån själv ser anmälningssidan ändå, för
provanmälan i checklistans steg 6).
"""

import logging
import time
from datetime import timedelta

from django.conf import settings
from django.db.models import F, Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from . import consent as consents
from . import keys, limits, tokens
from .access import PERSON, can_collect, dpa_ok, settings_for
from .email import transport
from .models import CHANNEL_EMAIL, Consent, Switchboard

logger = logging.getLogger(__name__)

#: Gränserna (README J, "DOI in S1").
ADDR_PER_ACCOUNT_DAY = 1
ADDR_ALL_DAY = 3
PER_HOUR = 300
#: Högst så många bekräftelsemejl per konto och timme (en kunds sida får
#: inte ta hela PER_HOUR).
PER_ACCOUNT_HOUR = 100
#: Rader i kön som är äldre än så skickas inte (personen får anmäla sig igen).
QUEUE_MAX_AGE = timedelta(days=7)
#: Högst så många mejl per tick.
BATCH = 100

DEFAULT_BASE_URL = "https://adx.se"
#: Knappens färg i mejlet (Brev, README F.2: standardfärgen).
ACCENT = "#1A57D6"


def absolute(path):
    """En adress som fungerar utanför sajten (i ett mejl)."""
    base = (getattr(settings, "SITE_BASE_URL", "") or DEFAULT_BASE_URL).rstrip("/")
    return base + path


def doi_ready():
    """Har byrån klarmarkerat bekräftelsemejlen (SES i eu-west-1 med
    produktionsåtkomst, Switchboard.doi_ready_at)? Läser bara, skapar ingen rad."""
    return Switchboard.objects.filter(pk=Switchboard.SOLO_PK, doi_ready_at__isnull=False).exists()


def offers_email(user=None):
    """Får anmälan och Mina utskick erbjuda e-post? När byrån klarmarkerat
    bekräftelsemejlen, och alltid för byrån själv (provanmälan innan
    klarmarkeringen, checklistans steg 6)."""
    from apps.projects.access import is_agency_user

    return doi_ready() or is_agency_user(user)


def _address_limited(account_id, value_hash, day):
    if limits.count("optin_addr", f"{account_id}:{value_hash}", day) >= ADDR_PER_ACCOUNT_DAY:
        return True
    return limits.count("optin_addr_all", value_hash, day) >= ADDR_ALL_DAY


def requeue(consent, now=None):
    """Personen bad om e-post igen medan e-posten redan väntar: lägg raden i
    kön igen, om adressen inte redan fått sitt mejl i dag. True om ett
    mejl väntar efteråt."""
    if consent.status != consents.PENDING or consent.channel != CHANNEL_EMAIL:
        return False
    if consent.confirm_sent_at is None:
        return True
    now = now or timezone.now()
    account_id = consent.contact.account_id
    if _address_limited(account_id, consent.value_hash, limits.day_window(now)):
        return False
    updated = Consent.objects.filter(pk=consent.pk, status=consents.PENDING).update(
        confirm_sent_at=None, changed_at=now
    )
    if updated:
        consent.confirm_sent_at = None
        consent.changed_at = now
    return bool(updated)


def queued(now=None):
    """Väntande e-post utan skickat mejl, hos konton som får skicka (D.2:
    sändningen inte stoppad för kunden), äldst först. Utan hänsyn till
    klarmarkeringen: det är due()."""
    now = now or timezone.now()
    return (
        Consent.objects.filter(
            channel=CHANNEL_EMAIL,
            status=consents.PENDING,
            confirm_sent_at__isnull=True,
            changed_at__gte=now - QUEUE_MAX_AGE,
            contact__account__is_enabled=True,
            contact__account__is_demo=False,
            contact__account__utskick__is_enabled=True,
            contact__account__utskick__sending_blocked=False,
        )
        .filter(
            Q(contact__account__customer__isnull=True)
            | Q(contact__account__customer__is_active=True)
        )
        .exclude(value_hash="")
        .order_by("changed_at", "pk")
    )


def due(now=None, ready=None):
    """Kön som ticken skickar: queued() när bekräftelsemejlen är
    klarmarkerade, annars bara byråns provanmälningar (samtycket ändrat av
    en staff-användare). ready är doi_ready() när den redan är läst."""
    rows = queued(now)
    if ready is None:
        ready = doi_ready()
    if not ready:
        rows = rows.filter(changed_by__isnull=False, changed_by__is_staff=True)
    return rows


def work_exists(now=None):
    """Finns något för fas 3? Falskt när transporten inte kan leverera
    något här (då står raderna kvar i kön)."""
    return transport.can_send() and due(now).exists()


def build_doi(consent, row=None, now=None):
    """Bekräftelsemejlet för ett väntande samtycke. Inget som personen
    skrev kommer med: bara företagets namn (byråns display_name) och länkarna."""
    from . import capture

    contact = consent.contact
    account = contact.account
    row = row or settings_for(account)
    token = tokens.doi_token(consent, now)
    context = {
        "foretag": row.display_name,
        "bekrafta_url": absolute(reverse("utskick_public:confirm", args=[token])),
        "integritet_url": capture.privacy_url(account, row, absolute=True),
        "dagar": tokens.DOI_DAYS,
        "accent": ACCENT,
    }
    return transport.OutgoingMail(
        to=contact.email,
        from_name=row.display_name,
        from_addr=settings.UTSKICK_DOI_FROM,
        subject=f"Bekräfta att du vill få e-post från {row.display_name}",
        text=render_to_string("utskick/mail/doi.txt", context),
        html=render_to_string("utskick/mail/doi.html", context),
    )


def _claim(pk, now):
    """Ta raden ur kön (villkorad uppdatering: två körningar tar aldrig
    samma rad). Samtycket med kontakt och konto, eller None."""
    taken = Consent.objects.filter(
        pk=pk, status=consents.PENDING, confirm_sent_at__isnull=True
    ).update(confirm_sent_at=now)
    if not taken:
        return None
    return (
        Consent.objects.select_related("contact__account__customer", "contact__account__utskick")
        .filter(pk=pk)
        .first()
    )


def _account_full(account_id, hour):
    """Har kontot fått PER_ACCOUNT_HOUR bekräftelsemejl den här timmen?
    Byrån larmas en gång per timme och konto (README J, "DOI in S1")."""
    if limits.count("optin_account_hour", str(account_id), hour) < PER_ACCOUNT_HOUR:
        return False
    from . import alerts

    logger.warning("Utskick: bekräftelsemejlen för konto %s når timgränsen", account_id)
    alerts.agency(
        "Utskick: många bekräftelsemejl från en kund",
        [
            f"Konto {account_id} har fått {PER_ACCOUNT_HOUR} bekräftelsemejl den här timmen.",
            "Resten väntar i kön till nästa timme. Titta på anmälningarna om det ser konstigt ut.",
        ],
        once=f"optin_account_hour:{account_id}",
        window="hour",
    )
    return True


def _release(pk):
    Consent.objects.filter(pk=pk, status=consents.PENDING).update(confirm_sent_at=None)


def send_due(now=None, deadline=None, limit=BATCH):
    """Tickens fas 3 för e-post: skicka bekräftelsemejlen som väntar.
    deadline är time.monotonic() då fasen ska sluta. Returnerar antal
    {"sent", "skipped", "failed", "waiting"} (inga adresser)."""
    now = now or timezone.now()
    summary = {"sent": 0, "skipped": 0, "failed": 0, "waiting": 0}
    if not transport.can_send():
        return summary
    if not keys.check_fingerprints():
        # Fel nyckel i processen: länkarna skulle signeras fel (H.7).
        return summary
    hour = limits.hour_window(now)
    day = limits.day_window(now)
    collect_ok = {}
    full = set()
    rows = due(now).values_list("pk", "contact__account_id")
    for pk, account_id in list(rows[:limit]):
        if deadline is not None and time.monotonic() > deadline:
            break
        if limits.count("optin_hour", "", hour) >= PER_HOUR:
            summary["waiting"] += 1
            break
        if account_id in full or _account_full(account_id, hour):
            # Kontots timme är full: raden väntar i kön till nästa timme.
            full.add(account_id)
            summary["waiting"] += 1
            continue
        consent = _claim(pk, now)
        if consent is None:
            continue
        contact = consent.contact
        account = contact.account
        if account.pk not in collect_ok:
            collect_ok[account.pk] = dpa_ok(account)
        if (
            not collect_ok[account.pk]
            or keys.value_hash(CHANNEL_EMAIL, contact.email) != consent.value_hash
            or _address_limited(account.pk, consent.value_hash, day)
        ):
            summary["skipped"] += 1
            continue
        mail = build_doi(consent, settings_for(account), now)
        result = transport.send(mail, kind=transport.DOI, account_id=account.pk)
        if result.ok or result.unknown:
            key = f"{account.pk}:{consent.value_hash}"
            limits.hit("optin_addr", key, day, ADDR_PER_ACCOUNT_DAY)
            limits.hit("optin_addr_all", consent.value_hash, day, ADDR_ALL_DAY)
            limits.hit("optin_hour", "", hour, PER_HOUR)
            limits.hit("optin_account_hour", str(account.pk), hour, PER_ACCOUNT_HOUR)
            Consent.objects.filter(pk=pk).update(confirm_count=F("confirm_count") + 1)
            summary["sent"] += 1
            continue
        if result.retry or result.stop:
            _release(pk)
            summary["waiting"] += 1
            break
        logger.info("Utskick: bekräftelsemejlet för samtycke %s nekades (%s)", pk, result.error)
        summary["failed"] += 1
    return summary


# ---------------------------------------------------------------------------
# Bekräftelsesidan
# ---------------------------------------------------------------------------


def doi_consent(ref):
    """Samtycket som en äkta bekräftelselänk (tokens.read_doi) gäller, med
    kontakt och konto, eller None. Länken gäller bara e-post och bara den
    adress mejlet skickades till."""
    if ref is None:
        return None
    consent = (
        Consent.objects.select_related("contact__account__customer")
        .filter(pk=ref.consent_id, channel=CHANNEL_EMAIL)
        .first()
    )
    if consent is None or consent.value_hash != ref.value_hash:
        return None
    return consent


def confirm(consent, *, ip_hash="", now=None):
    """Personen bekräftade (POST från bekräftelsesidan): e-posten blir ja,
    bevisat av klicket, och en spärr på adressen tas bort (H.6). Beviset
    behåller texten personen sa ja till, sidan och tiden. Nekas när kontot
    inte får ta in nya bekräftelser (utskick av eller biträdesavtalet inte
    godkänt): Outcome med refused "not_allowed"."""
    now = now or timezone.now()
    contact = consent.contact
    if not can_collect(contact.account):
        return consents.Outcome(consent, refused="not_allowed")
    return consents.set_status(
        contact,
        CHANNEL_EMAIL,
        consents.YES,
        source=Consent.Source.DOI,
        actor=PERSON,
        source_detail=consent.source_detail,
        text_shown=consent.text_shown,
        tracking_ok=consent.tracking_ok,
        collected_at=consent.collected_at,
        confirmed_at=now,
        ip_hash=ip_hash,
        proved=True,
        now=now,
    )
