"""
Priset och månadsunderlaget.

Kundens pris för ett sms = 46elks pris för sms:et + påslaget per del:

    customer_price = provider_cost + markup_ore_per_part * 100 * parts

i tiotusendels krona (models.UNITS_PER_KR). Påslaget sparas på varje sms
när det skickas, så att ett ändrat påslag aldrig ändrar redan skickade sms.

Årsavgiften (yearly_fee_kr, standard 999 kr) läggs på underlaget för den
månad då tjänsteåret börjar: aktiveringsmånaden och samma månad varje år
därefter (SmsAccount.service_year_start), om SMS var aktiverat någon gång
under månaden eller kunden skickat under den (fee_due). Läget när månaden
stängs spelar ingen roll. Avgiften räknas inte mot kostnadstaket: taket
gäller trafiken, annars skulle aktiveringsmånaden spärras direkt.

Kostnadstaket prövas mot månadens summa av customer_price. Stoppade försök
och sms som 46elks inte tog emot har pris 0, och ett reserverat sms räknas
med sitt uppskattade pris tills 46elks svarat. Provkörningar (test_mode)
räknas mot taket i provläge men inte när SMS_SEND_LIVE är på: de faktureras
aldrig, och en kund som provat före driftstarten ska inte ha ätit av taket.

En månad stängs inte för ett konto som har sms från månaden som fortfarande
står som reserverade (close_statement): de hamnar annars aldrig på något
underlag. Byrån stämmer av dem först (service.resolve_check).

Månaderna är svenska kalendermånader (Europe/Stockholm). Beloppen är utan
moms; byrån fakturerar med moms ovanpå.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from . import elks
from .models import UNITS_PER_KR, UNITS_PER_ORE, MonthlyStatement, SmsAccount, SmsMessage

logger = logging.getLogger(__name__)

STOCKHOLM = ZoneInfo("Europe/Stockholm")

#: Pris per del som antas när varken historik eller 46elks provkörning finns
#: (bara när 46elks inte svarar på provkörningen, och då misslyckas sändningen
#: troligen ändå). Högt med flit: det reserverar hellre för mycket.
FALLBACK_PART_COST = 2 * UNITS_PER_KR
#: Hur gamla priser från riktiga sms som får användas som uppskattning.
PRICE_HISTORY = timedelta(days=7)
#: Ett reserverat sms äldre än så här har fastnat: svaret från 46elks kom
#: aldrig. Byrån stämmer av det (needs_check_messages).
STUCK_AFTER = timedelta(minutes=10)


# ---------------------------------------------------------------- belopp


def to_kr(units):
    """Tiotusendels krona -> Decimal i kronor (fyra decimaler, exakt)."""
    return (Decimal(int(units or 0)) / UNITS_PER_KR).quantize(Decimal("0.0001"))


def kr_text(units, decimals=2):
    """'1 234,56' (svensk form, hårt mellanslag mellan tusental)."""
    value = to_kr(units).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    sign = "-" if value < 0 else ""
    whole, _, frac = f"{abs(value):.{decimals}f}".partition(".")
    groups = []
    while whole:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    text = "\xa0".join(groups) or "0"
    return f"{sign}{text},{frac}" if decimals else f"{sign}{text}"


def api_amount(units):
    """Belopp i API:t: kronor som sträng med punkt och fyra decimaler."""
    return f"{to_kr(units):.4f}"


def markup_for(account, parts):
    return account.markup_units_per_part * int(parts or 0)


# ---------------------------------------------------------------- månader


def local_today(now=None):
    return timezone.localtime(now or timezone.now(), STOCKHOLM).date()


def month_of(day):
    return day.replace(day=1)


def next_month(period):
    return (period.replace(day=28) + timedelta(days=4)).replace(day=1)


def previous_month(period):
    return (period.replace(day=1) - timedelta(days=1)).replace(day=1)


def month_bounds(period):
    """(början, slutet) för kalendermånaden, tidszonsmedvetna."""
    start = datetime.combine(month_of(period), time(0, 0), tzinfo=STOCKHOLM)
    end = datetime.combine(next_month(period), time(0, 0), tzinfo=STOCKHOLM)
    return start, end


def current_period(now=None):
    return month_of(local_today(now))


def parse_period(text):
    """'2026-10' -> date(2026, 10, 1), annars ValueError."""
    year, month = str(text or "").split("-", 1)
    return date(int(year), int(month), 1)


# ---------------------------------------------------------------- förbrukning


def messages_in(account, period):
    start, end = month_bounds(period)
    return SmsMessage.objects.filter(account=account, created_at__gte=start, created_at__lt=end)


def _cap_rows(qs):
    """Raderna som räknas mot taket: allt utom provkörningar när sms skickas
    på riktigt (de faktureras aldrig)."""
    return qs.filter(test_mode=False) if elks.is_live() else qs


def month_to_date_units(account, now=None):
    """Månadens summa av kundens pris: det kostnadstaket prövas mot.
    Stoppade och misslyckade utan sändning har pris 0."""
    rows = _cap_rows(messages_in(account, current_period(now)))
    return int(rows.aggregate(s=Sum("customer_price"))["s"] or 0)


def usage(account, now=None):
    """Månadens siffror för portalen, API:t och kundkortet."""
    period = current_period(now)
    qs = messages_in(account, period)
    accepted = qs.filter(status__in=SmsMessage.ACCEPTED).exclude(error_code="provider_error")
    row = qs.aggregate(
        cost=Sum("customer_price"),
        reserved=Sum("customer_price", filter=Q(status=SmsMessage.Status.RESERVED)),
        test=Sum("customer_price", filter=Q(test_mode=True)),
    )
    counts = accepted.aggregate(
        sms=Count("pk"),
        parts=Sum("parts"),
        delivered=Count("pk", filter=Q(status=SmsMessage.Status.DELIVERED)),
        failed=Count("pk", filter=Q(status=SmsMessage.Status.FAILED)),
        pending=Count("pk", filter=Q(status=SmsMessage.Status.SENT)),
    )
    stopped = qs.filter(status__in=SmsMessage.STOPPED).count()
    provider_failed = qs.filter(error_code="provider_error").count()
    cost = int(row["cost"] or 0)
    test_cost = int(row["test"] or 0)
    # Det taket prövas mot (month_to_date_units): utan provkörningar i drift.
    cap_used = cost - test_cost if elks.is_live() else cost
    cap = account.cap_units
    sms = counts["sms"] or 0
    return {
        "period": period,
        "sms": sms,
        "parts": int(counts["parts"] or 0),
        "delivered": counts["delivered"],
        "failed": counts["failed"] + provider_failed,
        "pending": counts["pending"],
        "stopped": stopped,
        "delivered_pct": round(100 * counts["delivered"] / sms) if sms else None,
        "cost": cost,
        #: Varav provkörningar: kostar inget och står aldrig på underlaget.
        "test_cost": test_cost,
        "reserved": int(row["reserved"] or 0),
        "cap": cap,
        "cap_used": cap_used,
        "remaining": max(cap - cap_used, 0),
        "cap_pct": min(100, round(100 * cap_used / cap)) if cap else 100,
        "cap_reached": cap_used >= cap,
    }


# ---------------------------------------------------------------- uppskattning


def recent_part_cost(country, now=None):
    """46elks pris per del till landet enligt de senaste riktiga sms:en (alla
    kunder, senaste PRICE_HISTORY), eller None. Det högsta av de tio
    senaste, så att uppskattningen hellre hamnar över än under."""
    since = (now or timezone.now()) - PRICE_HISTORY
    rows = (
        SmsMessage.objects.filter(
            country=country,
            sent_at__gte=since,
            provider_cost__gt=0,
            parts__gt=0,
            test_mode=False,
            # Priset på ett sms som fick sitt id ur en leveransrapport är
            # självt en uppskattning.
            needs_check=False,
        )
        .exclude(provider_id="")
        .order_by("-sent_at")
        .values_list("provider_cost", "parts")[:10]
    )
    per_part = [cost // parts for cost, parts in rows if parts]
    return max(per_part) if per_part else None


def fee_due(account, period, sms_count=0):
    """Ska årsavgiften med på månadens underlag? Tjänsteårets första månad
    och samma månad varje år, om kunden skickat under månaden eller SMS var
    aktiverat någon gång under den: aktiverat före månadens slut, och
    fortfarande aktiverat eller avstängt först efter att månaden börjat. Så
    blir svaret detsamma oavsett när månaden stängs."""
    start = account.service_year_start
    if not start or not account.yearly_fee_kr:
        return False
    if period < month_of(start) or period.month != start.month:
        return False
    if sms_count:
        return True
    month_start, month_end = month_bounds(period)
    if account.enabled_at is None or account.enabled_at >= month_end:
        return False
    if account.is_enabled:
        return True
    return account.disabled_at is not None and account.disabled_at >= month_start


def next_fee_period(account, now=None):
    """Nästa månad med årsavgift (den pågående räknas), eller None."""
    start = account.service_year_start
    if not start:
        return None
    today = current_period(now)
    candidate = date(max(today.year, start.year), start.month, 1)
    if candidate < today:
        candidate = date(candidate.year + 1, start.month, 1)
    return candidate


# ---------------------------------------------------------------- underlag


def build_statement(account, period):
    """Månadens underlag, osparat. Bara sms som 46elks tog emot räknas, och
    inga provkörningar (test_mode: inget skickades, inget debiteras)."""
    period = month_of(period)
    qs = (
        messages_in(account, period)
        .filter(status__in=SmsMessage.ACCEPTED, customer_price__gt=0, test_mode=False)
        .exclude(error_code="provider_error")
    )
    lines = []
    for row in (
        qs.values("country")
        .annotate(
            sms=Count("pk"),
            parts=Sum("parts"),
            provider_cost=Sum("provider_cost"),
            markup=Sum("markup"),
            total=Sum("customer_price"),
        )
        .order_by("-total", "country")
    ):
        lines.append(
            {
                "country": row["country"] or "",
                "sms": row["sms"],
                "parts": int(row["parts"] or 0),
                "provider_cost": int(row["provider_cost"] or 0),
                "markup": int(row["markup"] or 0),
                "total": int(row["total"] or 0),
            }
        )
    sms_count = sum(line["sms"] for line in lines)
    provider_cost = sum(line["provider_cost"] for line in lines)
    markup = sum(line["markup"] for line in lines)
    fee = account.yearly_fee_units if fee_due(account, period, sms_count) else 0
    return MonthlyStatement(
        account=account,
        period=period,
        sms_count=sms_count,
        parts=sum(line["parts"] for line in lines),
        provider_cost=provider_cost,
        markup=markup,
        fee=fee,
        total=provider_cost + markup + fee,
        lines=lines,
        markup_ore_per_part=account.markup_ore_per_part,
        yearly_fee_kr=account.yearly_fee_kr if fee else 0,
    )


class ReservationsPending(Exception):
    """Kontot har sms från månaden som fortfarande står som reserverade:
    månaden stängs inte förrän de fått sitt läge."""

    def __init__(self, count):
        super().__init__(f"{count} sms från månaden väntar fortfarande på 46elks.")
        self.count = count


def pending_reservations(period, account=None):
    """Sms från månaden (eller tidigare) som fortfarande står som reserverade,
    oavsett ålder: ett sms från 23.59 kan vänta på 46elks när cron stänger
    månaden 03.10, och ett som fastnat väntar på byrån."""
    _start, end = month_bounds(month_of(period))
    qs = SmsMessage.objects.filter(status=SmsMessage.Status.RESERVED, created_at__lt=end)
    return qs.filter(account=account) if account is not None else qs


def close_statement(account, period, user=None, now=None):
    """Frys månadens underlag. Returnerar (underlag, skapat). Ett redan
    stängt underlag lämnas orört; en månad som inte tagit slut stängs inte.
    Ett underlag utan sms och utan avgift sparas inte (None, False).

    Under radlås på kontot, så att två stängningar samtidigt (cron och
    knappen) inte krockar: den andra väntar och hittar den förstas underlag.
    Kastar ReservationsPending om sms från månaden fortfarande står som
    reserverade; då stängs inget för kontot."""
    period = month_of(period)
    if period >= current_period(now):
        raise ValueError("Månaden har inte tagit slut än.")
    with transaction.atomic():
        account = SmsAccount.objects.select_for_update().get(pk=account.pk)
        existing = MonthlyStatement.objects.filter(account=account, period=period).first()
        if existing is not None and existing.is_closed:
            return existing, False
        waiting = pending_reservations(period, account).count()
        if waiting:
            raise ReservationsPending(waiting)
        statement = build_statement(account, period)
        if not statement.sms_count and not statement.fee:
            return None, False
        statement.closed_at = now or timezone.now()
        statement.closed_by = user if user and getattr(user, "is_authenticated", False) else None
        if existing is not None:
            statement.pk = existing.pk
            statement.created_at = existing.created_at
        statement.save()
    return statement, True


@dataclass
class CloseResult:
    """Utfallet när en månad stängs för alla konton (knappen och cron)."""

    period: date
    created: list = field(default_factory=list)
    existing: int = 0
    empty: int = 0
    #: (konto, antal) som inte stängdes: sms från månaden står som reserverade.
    waiting: list = field(default_factory=list)
    #: Konton där stängningen kastade ett fel (loggat); de andra stängdes ändå.
    failed: list = field(default_factory=list)

    @property
    def waiting_sms(self):
        return sum(count for _account, count in self.waiting)

    def summary(self):
        """Samma text för knappen på /manage/sms/ och kommandot: hur många
        som stängdes, och kontona som väntar på reserverade sms (alla, utan
        åldersgräns)."""
        text = f"{self.period:%Y-%m}: {len(self.created)} underlag stängda"
        if self.existing:
            text += f", {self.existing} var redan stängda"
        text += "."
        if self.waiting:
            names = ", ".join(account.customer.name for account, _count in self.waiting)
            text += (
                f" {self.waiting_sms} sms från månaden står fortfarande som reserverade,"
                f" så de här kunderna stängdes inte: {names}. Stäm av sms:en mot 46elks"
                " och stäng igen."
            )
        if self.failed:
            names = ", ".join(account.customer.name for account in self.failed)
            text += f" Stängningen misslyckades för {names}; felet är loggat."
        return text


def close_month(period, user=None, now=None):
    """Stäng månaden för alla SMS-konton, ett i taget: ett konto som inte går
    att stänga hindrar inte de andra."""
    result = CloseResult(period=month_of(period))
    accounts = SmsAccount.objects.select_related("customer").order_by("customer__name")
    for account in accounts:
        try:
            statement, made = close_statement(account, period, user=user, now=now)
        except ReservationsPending as exc:
            result.waiting.append((account, exc.count))
            continue
        except Exception:  # noqa: BLE001 - nästa konto ska stängas ändå
            logger.exception("SMS: månaden gick inte att stänga för konto %s", account.pk)
            result.failed.append(account)
            continue
        if made:
            result.created.append(statement)
        elif statement is not None:
            result.existing += 1
        else:
            result.empty += 1
    return result


def needs_check_messages(account=None, now=None):
    """Sms som byrån ska stämma av mot 46elks: svaret på sändningen var oklart
    eller priset uppskattat (needs_check), eller reservationen har fastnat
    (processen kan ha dött mellan reservationen och 46elks svar)."""
    stuck_before = (now or timezone.now()) - STUCK_AFTER
    qs = SmsMessage.objects.filter(
        Q(needs_check=True) | Q(status=SmsMessage.Status.RESERVED, created_at__lt=stuck_before)
    )
    return qs.filter(account=account) if account is not None else qs


def per_unit_ore(units):
    """Tiotusendelar -> öre som text med upp till två decimaler ('52', '57,5')."""
    value = Decimal(int(units or 0)) / UNITS_PER_ORE
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")
