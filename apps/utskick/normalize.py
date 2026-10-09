"""
Adresser och värden som de sparas i registret (README B.1).

    phone(raw)                  Phone(e164, country, landline, error)
    email(raw)                  "anna@exempelror.example" eller InvalidValue
    org_number(raw)             OrgNumber(value, personal) eller InvalidValue
    looks_like_personnummer(v)  True för 19990101-1234 och liknande (med Luhn)
    field_value(kind, raw, choices)   extrafältets värde som text, eller InvalidValue
    split_name(full)            ("Anna", "Lindqvist")
    first_name(raw)             anmälningssidans förnamn, eller InvalidValue
    mask_phone(e164), mask_email(addr)  "070-*** ** 67", "a***@e***.example"
    display_phone(e164)         "070-123 45 67" (verktygets listor och kontaktkortet)

Telefonnummer tolkas bara med apps.sms.numbers.parse (aldrig
flamingo.sms.normalize_phone: de två är inte överens). Ett giltigt nummer som
inte kan ta emot sms (en fast telefon) blir inte phone utan text i
fields["telefon"] (Phone.landline). Personnummer sparas aldrig, varken som
organisationsnummer eller i ett extrafält.
"""

import re
from datetime import date
from typing import NamedTuple

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

PERSONNUMMER_TEXT = "Personnummer sparas inte i Kontakter."
LANDLINE_FIELD = "telefon"

_PNR_RE = re.compile(r"^(19|20)?(\d{6})[-+]?(\d{4})$")
_NAME_RE = re.compile(r"^[^\W\d_]+(?:[ '\-][^\W\d_]+)*$")
FIRST_NAME_MAX = 40


class InvalidValue(ValueError):
    """Ett värde som inte går att spara, med en förklaring på svenska."""


class Phone(NamedTuple):
    #: E.164 för ett nummer som kan ta emot sms, annars "".
    e164: str
    country: str
    #: Ett giltigt nummer som inte tar emot sms, som text för fields["telefon"].
    landline: str
    #: Förklaringen när numret inte gick att tolka.
    error: str

    @property
    def ok(self):
        return not self.error


class OrgNumber(NamedTuple):
    #: Tio siffror för en juridisk person, annars "".
    value: str
    #: Det såg ut som ett personnummer (enskild firma): sparas inte, och
    #: raden blir en privatperson.
    personal: bool


def phone(raw):
    """Tolka ett nummer. Tomt in ger Phone("", "", "", "")."""
    import phonenumbers

    from apps.sms import numbers

    text = str(raw or "").strip()
    if not text:
        return Phone("", "", "", "")
    try:
        number = numbers.parse(text)
    except numbers.InvalidNumber as exc:
        landline = _landline(text, phonenumbers)
        if landline:
            return Phone("", "", landline, "")
        return Phone("", "", "", str(exc))
    return Phone(number.e164, number.country, "", "")


def _landline(text, phonenumbers):
    """Ett giltigt nummer som inte är ett mobilnummer, i läsbar form."""
    if len(text) > 32:
        return ""
    if text.startswith("00"):
        text = "+" + text[2:]
    try:
        parsed = phonenumbers.parse(text, "SE")
    except phonenumbers.NumberParseException:
        return ""
    if not phonenumbers.is_valid_number(parsed):
        return ""
    fmt = (
        phonenumbers.PhoneNumberFormat.NATIONAL
        if phonenumbers.region_code_for_number(parsed) == "SE"
        else phonenumbers.PhoneNumberFormat.INTERNATIONAL
    )
    return phonenumbers.format_number(parsed, fmt)


def email(raw):
    """Adressen utan blanksteg och i gemener (plustaggen kvar), domänen i
    IDNA-form. Tomt in ger "". InvalidValue om adressen inte är giltig."""
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    if len(text) > 254 or text.count("@") != 1:
        raise InvalidValue("E-postadressen är inte giltig.")
    local, domain = text.rsplit("@", 1)
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        raise InvalidValue("E-postadressen är inte giltig.") from None
    text = f"{local}@{domain}"
    try:
        validate_email(text)
    except ValidationError:
        raise InvalidValue("E-postadressen är inte giltig.") from None
    if len(text) > 254:
        raise InvalidValue("E-postadressen är inte giltig.")
    return text


def email_domain(address):
    return str(address or "").rsplit("@", 1)[-1].lower() if "@" in str(address or "") else ""


def _luhn_ok(digits):
    total = 0
    for i, ch in enumerate(digits):
        n = int(ch) * (2 if i % 2 == 0 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def looks_like_personnummer(value):
    """True för ett svenskt personnummer eller samordningsnummer:
    ^(19|20)?\\d{6}[-+]?\\d{4}$ med giltig kontrollsiffra (Luhn), och tredje
    siffran 0 eller 1 (månaden). Ett organisationsnummer har tredje siffran 2
    eller högre och räknas inte hit."""
    text = re.sub(r"\s+", "", str(value or ""))
    match = _PNR_RE.match(text)
    if not match:
        return False
    ten = match.group(2) + match.group(3)
    if ten[2] not in "01":
        return False
    return _luhn_ok(ten)


def org_number(raw):
    """Organisationsnummer: bara siffror, tio (tolv med sekel blir tio).
    Sparas bara när tredje siffran är 2 eller högre (juridisk person). Ett
    personnummer (enskild firma) ger OrgNumber("", personal=True)."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        return OrgNumber("", False)
    if len(digits) == 12:
        digits = digits[2:]
    if len(digits) != 10:
        raise InvalidValue("Organisationsnumret ska ha tio siffror.")
    if digits[2] in "01":
        return OrgNumber("", True)
    return OrgNumber(digits, False)


def field_value(kind, raw, choices=()):
    """Ett extrafälts värde som det sparas i Contact.fields: text, datum som
    åååå-mm-dd, tal utan tusentalsavgränsare, eller ett av valen. Tomt in ger
    "". Personnummer nekas i alla fält."""
    text = str(raw if raw is not None else "").strip()
    if not text:
        return ""
    if looks_like_personnummer(text):
        raise InvalidValue(PERSONNUMMER_TEXT)
    if kind == "date":
        return _date_value(text)
    if kind == "number":
        compact = text.replace(" ", "").replace(" ", "").replace(",", ".")
        try:
            number = float(compact)
        except ValueError:
            raise InvalidValue("Skriv ett tal.") from None
        return str(int(number)) if number.is_integer() else str(number)
    if kind == "choice":
        options = [str(c) for c in choices or ()]
        for option in options:
            if option.casefold() == text.casefold():
                return option
        raise InvalidValue("Välj ett av alternativen.")
    if len(text) > 500:
        raise InvalidValue("Texten är för lång (högst 500 tecken).")
    return text


def _date_value(text):
    compact = text.replace("/", "-").replace(".", "-")
    for pattern in (r"^(\d{4})-(\d{1,2})-(\d{1,2})$", r"^(\d{4})(\d{2})(\d{2})$"):
        match = re.match(pattern, compact)
        if match:
            year, month, day = (int(g) for g in match.groups())
            break
    else:
        match = re.match(r"^(\d{1,2})-(\d{1,2})-(\d{4})$", compact)
        if not match:
            raise InvalidValue("Skriv datumet som åååå-mm-dd.")
        day, month, year = (int(g) for g in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        raise InvalidValue("Datumet finns inte.") from None


def split_name(full):
    """Fullständigt namn till (förnamn, efternamn): första ordet och resten."""
    parts = str(full or "").split()
    if not parts:
        return "", ""
    return parts[0][:60], " ".join(parts[1:])[:80]


def first_name(raw):
    """Anmälningssidans förnamn: bokstäver, mellanslag, bindestreck och
    apostrof, högst 40 tecken. Tomt in ger ""."""
    text = " ".join(str(raw or "").split())
    if not text:
        return ""
    if len(text) > FIRST_NAME_MAX or not _NAME_RE.match(text):
        raise InvalidValue("Skriv bara ditt förnamn, med bokstäver.")
    return text


def display_phone(e164):
    """+46701234567 -> "070-123 45 67" (svenska nummer), andra länder i
    internationell form ("+45 81 23 45 67"). Tomt in ger ""; ett värde som
    inte går att tolka kommer tillbaka som det är."""
    import phonenumbers

    text = str(e164 or "").strip()
    if not text:
        return ""
    try:
        parsed = phonenumbers.parse(text, "SE")
    except phonenumbers.NumberParseException:
        return text
    fmt = (
        phonenumbers.PhoneNumberFormat.NATIONAL
        if phonenumbers.region_code_for_number(parsed) == "SE"
        else phonenumbers.PhoneNumberFormat.INTERNATIONAL
    )
    return phonenumbers.format_number(parsed, fmt)


def mask_phone(e164):
    """+46701740567 -> "070-*** ** 67" (svenska nummer), annars landsnumret,
    stjärnor och de två sista siffrorna."""
    text = str(e164 or "")
    digits = re.sub(r"\D", "", text)
    if len(digits) < 4:
        return ""
    if text.startswith("+46"):
        national = "0" + digits[2:]
        return f"{national[:3]}-*** ** {national[-2:]}"
    return f"+{digits[:2]} *** ** {digits[-2:]}"


def mask_email(address):
    """anna@exempelror.example -> "a***@e***.example"."""
    text = str(address or "")
    if "@" not in text:
        return ""
    local, domain = text.rsplit("@", 1)
    name, _, tld = domain.rpartition(".")
    if not name:
        name, tld = domain, ""
    masked_domain = f"{name[:1]}***" + (f".{tld}" if tld else "")
    return f"{local[:1]}***@{masked_domain}"
