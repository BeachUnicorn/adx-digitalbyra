"""
Tidsfönstret för sms (README D.4 "Quiet hours"): per konto
(UtskickSettings.sms_window), vardag och helg, i hela timmar inom gränserna
08 till 21. Svenska helgdagar räknas som helg. Allt räknas i
STOCKHOLM-tid, så sommartiden sköter sig själv: fönstret 09 till 20 är
09.00 till 20.00 i Sverige både i mars och i oktober.

    is_holiday(day) -> bool                  svensk helgdag (eller afton som räknas som helg)
    window_for(settings_row, day) -> (start, slut)   hela timmar, slutet ingår inte
    sms_window_open(settings_row, now) -> bool
    next_window_start(settings_row, now) -> datetime  now när fönstret är öppet
    window_text(settings_row, day=None) -> "09.00 till 20.00"
    next_start_text(settings_row, now) -> "09.00 i morgon"

Bara utskick och flöden följer fönstret. Svar från Inkorgen, bekräftelser
och STOPP- och START-svar gör det inte, och kundens API aldrig.

Helgdagarna räknas fram här (fasta datum och de som hänger på påsken,
Gauss algoritm för den gregorianska påsken), ingen tabell att underhålla.
Midsommarafton, julafton och nyårsafton är inga helgdagar i lagens mening
men räknas som helg här: ett erbjudande klockan nio på julafton är inget
någon vill ha.
"""

from datetime import date, datetime, time, timedelta
from functools import lru_cache

from django.utils import timezone

from apps.sms.pricing import STOCKHOLM

from .models import SMS_WINDOW_DEFAULT

#: De hårda gränserna (README B.1): inget sms före 08.00 eller efter 21.00.
EARLIEST = 8
LATEST = 21

_WEEKDAYS = ("måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag")
_MONTHS = ("jan", "feb", "mars", "apr", "maj", "juni", "juli", "aug", "sep", "okt", "nov", "dec")


def easter(year):
    """Påskdagen (gregoriansk, Gauss/Meeus algoritm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def _first_weekday(start, weekday):
    """Första dagen från och med start som är weekday (0 = måndag)."""
    return start + timedelta(days=(weekday - start.weekday()) % 7)


@lru_cache(maxsize=64)
def holidays(year):
    """Årets dagar som räknas som helg utöver lördag och söndag."""
    easter_day = easter(year)
    return frozenset(
        {
            date(year, 1, 1),  # nyårsdagen
            date(year, 1, 6),  # trettondedag jul
            easter_day - timedelta(days=2),  # långfredagen
            easter_day,  # påskdagen
            easter_day + timedelta(days=1),  # annandag påsk
            date(year, 5, 1),  # första maj
            easter_day + timedelta(days=39),  # Kristi himmelsfärdsdag
            easter_day + timedelta(days=49),  # pingstdagen
            date(year, 6, 6),  # nationaldagen
            _first_weekday(date(year, 6, 19), 4),  # midsommarafton (fredag 19-25 juni)
            _first_weekday(date(year, 6, 20), 5),  # midsommardagen (lördag 20-26 juni)
            _first_weekday(date(year, 10, 31), 5),  # alla helgons dag (lördag 31 okt-6 nov)
            date(year, 12, 24),  # julafton
            date(year, 12, 25),  # juldagen
            date(year, 12, 26),  # annandag jul
            date(year, 12, 31),  # nyårsafton
        }
    )


def is_holiday(day):
    """Räknas dagen som helg fast den inte är lördag eller söndag?"""
    if isinstance(day, datetime):
        day = timezone.localtime(day, STOCKHOLM).date()
    return day in holidays(day.year)


def is_weekend(day):
    return day.weekday() >= 5 or is_holiday(day)


def _hours(value, fallback):
    """[start, slut] ur inställningen, inom gränserna, annars fallback."""
    try:
        start, end = (int(v) for v in value)
    except (TypeError, ValueError):
        return tuple(fallback)
    start = max(EARLIEST, min(LATEST, start))
    end = max(EARLIEST, min(LATEST, end))
    if start >= end:
        return tuple(fallback)
    return start, end


def window_for(settings_row, day):
    """(starttimme, sluttimme) för dagen: sms får gå från start.00 till
    strax före slut.00. settings_row är kundens UtskickSettings (eller None:
    standardfönstret)."""
    if isinstance(day, datetime):
        day = timezone.localtime(day, STOCKHOLM).date()
    key = "weekend" if is_weekend(day) else "weekday"
    window = getattr(settings_row, "sms_window", None) or {}
    if not isinstance(window, dict):
        window = {}
    return _hours(window.get(key), SMS_WINDOW_DEFAULT[key])


def _local(now):
    return timezone.localtime(now or timezone.now(), STOCKHOLM)


def _at(day, hour):
    """Klockslaget hour.00 den svenska dagen day, som medveten tid. Inga
    timmar mellan 08 och 21 berörs av sommartidens omställning."""
    return datetime.combine(day, time(hour, 0), tzinfo=STOCKHOLM)


def sms_window_open(settings_row, now=None):
    local = _local(now)
    start, end = window_for(settings_row, local.date())
    return start <= local.hour < end


def next_window_start(settings_row, now=None):
    """När fönstret öppnar nästa gång (now själv när det redan är öppet)."""
    now = now or timezone.now()
    local = _local(now)
    for offset in range(0, 8):
        day = local.date() + timedelta(days=offset)
        start, end = window_for(settings_row, day)
        opens, closes = _at(day, start), _at(day, end)
        if offset == 0:
            if local < opens:
                return opens
            if local < closes:
                return now
            continue
        return opens
    return _at(local.date() + timedelta(days=1), EARLIEST)  # pragma: no cover


def window_text(settings_row, day=None):
    """ "09.00 till 20.00" för dagen (standard: i dag i Sverige)."""
    day = day or _local(None).date()
    start, end = window_for(settings_row, day)
    return f"{start:02d}.00 till {end:02d}.00"


def day_word(moment, now=None):
    """ "i dag", "i morgon", "i går", annars "lördag 12 okt"."""
    local = _local(moment)
    today = _local(now).date()
    delta = (local.date() - today).days
    if delta == 0:
        return "i dag"
    if delta == 1:
        return "i morgon"
    if delta == -1:
        return "i går"
    return f"{_WEEKDAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]}"


def clock_text(moment, now=None):
    """ "09.00 i morgon", "10.00 lördag 12 okt"."""
    local = _local(moment)
    return f"{local:%H.%M} {day_word(local, now)}"


def next_start_text(settings_row, now=None):
    """När sms:en går i väg: "09.00 i morgon", eller "nu" när fönstret är öppet."""
    now = now or timezone.now()
    start = next_window_start(settings_row, now)
    if start == now:
        return "nu"
    return clock_text(start, now)
