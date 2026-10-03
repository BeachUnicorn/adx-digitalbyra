"""
Teckenkodning och antal sms-delar, räknat som operatörerna räknar.

Ett sms är 140 byte. Med GSM 03.38 (sju bitar per tecken) ryms 160 tecken,
och ett långt meddelande delas i delar om 153 tecken (sju byte går åt till
huvudet som fogar ihop delarna). Finns ett enda tecken utanför GSM-alfabetet
skickas hela meddelandet som UCS-2 (UTF-16): 70 tecken, eller 67 per del.

Tecknen i GSM:s utökade tabell (till exempel { } [ ] ~ ^ \\ | och euro)
kostar två platser var, och ett sådant teckenpar delas aldrig mellan två
delar. I UCS-2 kostar ett tecken utanför grundplanet (de flesta emojis) två
platser, och ett sådant par delas inte heller.

Kontrollerat mot 46elks med dryrun 2026-10-03: 161 tecken gav 2 delar, 306
gav 2 och 307 gav 3; 36 emojis gav 2 delar; 80 par "{}" gav 3 delar. Efter
sändningen är det ändå 46elks egna antal delar som prissätts.
"""

from dataclasses import dataclass

GSM7 = "gsm7"
UCS2 = "ucs2"

#: GSM 03.38, grundtabellen. Radbrytning och vagnretur ingår.
_GSM_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
#: Den utökade tabellen: två platser per tecken (escape + tecknet).
_GSM_EXTENDED = "^{}\\[~]|€\f"

GSM_BASIC = frozenset(_GSM_BASIC)
GSM_EXTENDED = frozenset(_GSM_EXTENDED)

#: Platser i ett ensamt sms och per del i ett delat.
GSM_SINGLE, GSM_PART = 160, 153
UCS2_SINGLE, UCS2_PART = 70, 67


@dataclass(frozen=True)
class Analysis:
    encoding: str
    #: Antal platser meddelandet tar (septetter för GSM, UTF-16-enheter för UCS-2).
    units: int
    parts: int
    #: Tecken som tvingade fram UCS-2 (högst fem, för felmeddelanden och hjälptext).
    non_gsm: tuple = ()


def is_gsm7(text):
    return all(ch in GSM_BASIC or ch in GSM_EXTENDED for ch in text)


def _gsm_widths(text):
    return [2 if ch in GSM_EXTENDED else 1 for ch in text]


def _ucs2_widths(text):
    return [2 if ord(ch) > 0xFFFF else 1 for ch in text]


def _count_parts(widths, single, per_part):
    """Delar för en följd av teckenbredder. Ett tecken som inte ryms helt i
    delen börjar nästa del (en escape-sekvens eller ett surrogatpar delas
    aldrig)."""
    total = sum(widths)
    if total == 0:
        return 0
    if total <= single:
        return 1
    parts, used = 1, 0
    for width in widths:
        if used + width > per_part:
            parts += 1
            used = 0
        used += width
    return parts


def analyse(text):
    """Kodning, platser och delar för meddelandet."""
    text = text or ""
    if is_gsm7(text):
        widths = _gsm_widths(text)
        return Analysis(GSM7, sum(widths), _count_parts(widths, GSM_SINGLE, GSM_PART))
    widths = _ucs2_widths(text)
    odd = []
    for ch in text:
        if ch not in GSM_BASIC and ch not in GSM_EXTENDED and ch not in odd:
            odd.append(ch)
        if len(odd) == 5:
            break
    return Analysis(
        UCS2, sum(widths), _count_parts(widths, UCS2_SINGLE, UCS2_PART), non_gsm=tuple(odd)
    )


def max_length(encoding, parts):
    """Hur många platser som ryms i så många delar."""
    if encoding == GSM7:
        return GSM_SINGLE if parts == 1 else GSM_PART * parts
    return UCS2_SINGLE if parts == 1 else UCS2_PART * parts
