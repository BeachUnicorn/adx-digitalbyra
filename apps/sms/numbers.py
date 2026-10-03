"""
Mottagarnumret: tolkat, kontrollerat och i E.164-form, med landet det hör
till (phonenumbers, Googles nummerdata).

Priset hos 46elks beror på landet, och vilka länder en kund får skicka till
är ett skydd mot sms-pumpning (bedrägeri där någon låter ett formulär skicka
massor av sms till dyra nummer i andra länder). Därför tolkas varje nummer
här innan något annat händer, och ett nummer som inte går att tolka eller
inte är ett mobilnummer stoppas innan 46elks anropas: 46elks egen provkörning
(dryrun) godtar till exempel "+4612".

Ett nummer utan landsnummer ("070-174 06 05") tolkas som svenskt.
"""

from dataclasses import dataclass

import phonenumbers
from phonenumbers import PhoneNumberType

DEFAULT_REGION = "SE"

#: Nummertyper som kan ta emot sms. FIXED_LINE_OR_MOBILE är länder där
#: numren inte går att skilja åt (till exempel USA).
SMS_TYPES = frozenset({PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE})

#: Landnamn på svenska för de länder kunderna rimligen skickar till. Övriga
#: visas med sin landskod.
COUNTRY_NAMES = {
    "SE": "Sverige",
    "NO": "Norge",
    "DK": "Danmark",
    "FI": "Finland",
    "IS": "Island",
    "AX": "Åland",
    "DE": "Tyskland",
    "NL": "Nederländerna",
    "BE": "Belgien",
    "FR": "Frankrike",
    "ES": "Spanien",
    "PT": "Portugal",
    "IT": "Italien",
    "PL": "Polen",
    "EE": "Estland",
    "LV": "Lettland",
    "LT": "Litauen",
    "GB": "Storbritannien",
    "IE": "Irland",
    "AT": "Österrike",
    "CH": "Schweiz",
    "US": "USA",
    "CA": "Kanada",
}


class InvalidNumber(ValueError):
    pass


@dataclass(frozen=True)
class Number:
    e164: str
    country: str


def country_name(code):
    code = (code or "").upper()
    return COUNTRY_NAMES.get(code, code or "Okänt")


def is_known_region(code):
    return (code or "").upper() in phonenumbers.SUPPORTED_REGIONS


def parse(raw):
    """Number(e164, land) eller InvalidNumber med en förklaring på svenska."""
    text = str(raw or "").strip()
    if not text:
        raise InvalidNumber("Numret saknas.")
    if len(text) > 32:
        raise InvalidNumber("Numret är för långt.")
    if text.startswith("00"):
        text = "+" + text[2:]
    try:
        parsed = phonenumbers.parse(text, DEFAULT_REGION)
    except phonenumbers.NumberParseException:
        raise InvalidNumber("Numret går inte att tolka som ett telefonnummer.") from None
    if not phonenumbers.is_valid_number(parsed):
        raise InvalidNumber("Numret finns inte i nummerplanen för sitt land.")
    if phonenumbers.number_type(parsed) not in SMS_TYPES:
        raise InvalidNumber("Numret är inte ett mobilnummer.")
    country = phonenumbers.region_code_for_number(parsed) or ""
    if len(country) != 2:
        raise InvalidNumber("Numret hör inte till något land.")
    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    return Number(e164=e164, country=country)


def parse_country_list(text):
    """'se, no dk' -> ['SE', 'NO', 'DK'] i ordning utan dubbletter, eller
    ValueError med de koder som inte finns."""
    codes, unknown = [], []
    for part in str(text or "").replace(",", " ").split():
        code = part.strip().upper()
        if not code:
            continue
        if not is_known_region(code):
            unknown.append(part.strip())
        elif code not in codes:
            codes.append(code)
    if unknown:
        raise ValueError("Okända landskoder: " + ", ".join(unknown))
    return codes
