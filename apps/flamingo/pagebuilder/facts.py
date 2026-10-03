"""
Underlaget för blockens mallinnehåll: kontots bekräftade uppgifter, sorterade
efter vad de kan användas till.

    facts_for(account) -> Facts

Bara FlamingoAccount.usable_fact_rows() läses: bekräftade uppgifter med ett
värde, utan betyg som inte kommer från Google eller ADX. Inget hittas på:
saknas en uppgift lämnar mallen fältet tomt eller blocket utanför
(registry.template_fields returnerar None). Ett pris används bara för sin
egen tjänst (Facts.price_for), och telefonen är ett nummer ur uppgiften
(generator.one_phone).
"""

import re
from dataclasses import dataclass, field

from apps.common.security import sanitize_plain_text

from .. import checks, generator
from ..scan import PRICE_PREFIX

#: Auktorisationer, försäkringar och medlemskap (certifikatblocket). Läses
#: ur nyckel, etikett och värde: "Behörighet: Säker Vatten-auktoriserade".
CERTIFICATE_WORDS = re.compile(
    r"certifi|auktoris|behörig|behorig|försäkr|forsakr|medlem|f-skatt|fskatt"
    r"|säker vatten|saker vatten|våtrum|vatrum|licens|legitimer",
    re.I,
)
GUARANTEE_WORDS = re.compile(r"garanti", re.I)
#: Kvalitetsord och behörigheter: får stå bara när ordet (dess fem första
#: bokstäver) står i en bekräftad uppgift ("auktoriserade" när Säker
#: Vatten-auktoriserade är bekräftat). AI-vakten (ai.Guard) och
#: kontrollerna av certifikat- och garantiblocken (problems.py) använder den.
CLAIM_WORDS = re.compile(
    r"\b(?:erfar\w*|proffs\w*|professionell\w*|expert\w*|specialist\w*|kvalitet\w*|"
    r"noggrann\w*|pålitlig\w*|seriös\w*|nöjd\w*|prisvärd\w*|ledande|kunnig\w*|"
    r"utbildad\w*|certifi\w*|auktoris\w*|behörig\w*|försäkr\w*|legitim\w*|medlem\w*|"
    r"licens\w*|godkänd\w*|marknadens|prisbelönt\w*|miljöcertifi\w*)",
    re.I,
)
HOURS_WORDS = re.compile(r"öppet|oppet|öppettid|oppettid|jour", re.I)
#: Öppettider (inte jouren): faller bort när en uppgift säger dygnet runt.
OPENING_WORDS = re.compile(r"öppet|oppet", re.I)
ROUND_THE_CLOCK = re.compile(r"dygnet runt|dygnsöppe|24/7|24 timmar om dygnet", re.I)
#: Etiketter som inte säger något utöver värdet ("Behörighet: Säker
#: Vatten-auktoriserade" blir "Säker Vatten-auktoriserade").
PLAIN_LABELS = re.compile(
    r"(?:behörighet|behorighet|certifi\w*|auktorisation\w*|försäkring\w*|forsakring\w*|"
    r"medlemskap|garanti|övrigt|ovrigt|uppgift|info)",
    re.I,
)
_LABELLED = re.compile(r"([^:\d]{2,30}):\s+(.+)")  # fullmatch
PERSON_WORDS = re.compile(r"kontaktperson|ägare|agare|grundare|vd\b|delägare", re.I)


def _clean(text, max_length=300):
    return sanitize_plain_text(str(text or ""), max_length=max_length)


def _lower_first(text):
    if len(text) > 1 and (text[1].isupper() or not text[1].isalpha()):
        return text
    return text[:1].lower() + text[1:]


def point_line(label, value):
    """En uppgift som en punkt i vanlig svenska, inte "etikett: värde":
    "Jour dygnet runt, alla dagar", "Grundat 2009", "Öppet vardagar 7-16".
    Säger etiketten inget utöver värdet står värdet ensamt."""
    value = " ".join(str(value or "").split()).rstrip(".!")
    label = " ".join(str(label or "").split()).rstrip(":")
    if not label or label.casefold() in value.casefold() or PLAIN_LABELS.fullmatch(label):
        return value
    if OPENING_WORDS.match(label):
        label = "Öppet"
    return f"{label} {_lower_first(value)}"


def tidy_points(points):
    """Punkter i formen "Etikett: värde" (förslagets sida, generator) som
    point_line, och öppettiderna bort när en punkt säger dygnet runt."""
    out = []
    for point in points:
        point = " ".join(str(point or "").split())
        match = _LABELLED.fullmatch(point)
        out.append(point_line(*match.groups()) if match else point)
    if any(ROUND_THE_CLOCK.search(p) for p in out):
        out = [p for p in out if not OPENING_WORDS.match(p)]
    return [p for p in out if p]


@dataclass
class Facts:
    """Det mallarna får använda. Varje lista är [(etikett, värde)]."""

    company: str
    phone: str = ""
    address: str = ""
    email: str = ""
    hours: str = ""
    person: str = ""
    #: Orterna ur uppgiften om område ("Nacka, Värmdö och Tyresö").
    places: list = field(default_factory=list)
    prices: list = field(default_factory=list)
    certificates: list = field(default_factory=list)
    guarantees: list = field(default_factory=list)
    #: Övriga påståenden som får stå på sidan (samma urval som annonserna:
    #: generator.Info.claims), som färdiga rader.
    claims: list = field(default_factory=list)
    #: Alla bekräftade värden (kontrollernas Context).
    values: list = field(default_factory=list)
    #: Prisuppgifterna med sina nycklar: [(nyckel, etikett, värde)], för
    #: price_for (en sida får bara sin tjänsts pris).
    price_rows: list = field(default_factory=list)
    #: De bekräftade uppgifterna med etiketter, i gemener: för kvalitetsord
    #: och behörigheter (CLAIM_WORDS).
    claims_text: str = ""

    def context(self, extra=()):
        """checks.Context för mallarnas egna texter."""
        return checks.build_context(self.values, extra=(self.company, *extra))

    def price_for(self, service=""):
        """(etikett, pris) som en sida om tjänsten får visa: tjänstens eget
        bekräftade pris (pris-<tjänst>), annars ett pris som inte hör till
        någon tjänst (timpris). Aldrig en annan tjänsts pris. Samma regel
        som generator.page_price. ("", "") utan pris."""
        key = generator.price_fact_key(service) if service else ""
        if key and key != PRICE_PREFIX.rstrip("-"):
            for row_key, label, value in self.price_rows:
                if row_key == key:
                    return label, value
        for row_key, label, value in self.price_rows:
            if not row_key.startswith(PRICE_PREFIX):
                return label, value
        return "", ""

    def prices_for(self, service=""):
        """[(etikett, pris)]: tjänstens pris först, sedan priserna som inte
        hör till någon tjänst. Aldrig en annan tjänsts pris (prisexemplen)."""
        first = self.price_for(service)
        out = [first] if first[1] else []
        for row_key, label, value in self.price_rows:
            if not row_key.startswith(PRICE_PREFIX) and (label, value) not in out:
                out.append((label, value))
        return out

    def claim_ok(self, text):
        """Står varje kvalitetsord och behörighet i texten bland de
        bekräftade uppgifterna (CLAIM_WORDS)?"""
        for match in CLAIM_WORDS.finditer(text or ""):
            if match.group(0).lower()[:5] not in self.claims_text:
                return False
        return True


def facts_for(account):
    rows = account.usable_fact_rows()
    facts = Facts(company=generator.company_name(account.customer))
    for fact in rows:
        label, value = _clean(fact.label, 120), _clean(fact.value)
        if not value:
            continue
        facts.values.append(value)
        kind = generator.fact_kind(fact)
        words = f"{fact.key} {label}"
        facts.claims_text += f" {label} {value}".lower()
        if kind == "phone":
            # Ett nummer ur uppgiften, aldrig hela texten (generator.one_phone).
            facts.phone = facts.phone or generator.one_phone(value)
        elif kind == "address":
            facts.address = facts.address or value
        elif kind == "email":
            facts.email = facts.email or value
        elif kind == "area":
            if not facts.places:
                facts.places = generator.places_of(value)
        elif kind == "price" or fact.key.startswith(PRICE_PREFIX):
            facts.prices.append((label, value))
            facts.price_rows.append((fact.key, label, value))
        elif kind == "name":
            if PERSON_WORDS.search(words) and not facts.person:
                facts.person = value
        elif kind in ("rating", "count", "id", "web"):
            continue
        if GUARANTEE_WORDS.search(f"{words} {value}"):
            facts.guarantees.append((label, value))
        elif CERTIFICATE_WORDS.search(f"{words} {value}"):
            facts.certificates.append((label, value))
        if HOURS_WORDS.search(words) and not facts.hours and kind not in ("phone", "price"):
            facts.hours = value
        if kind not in generator._NOT_CLAIMS:
            facts.claims.append(point_line(label, value))
    # Jour dygnet runt: öppettiderna säger något annat och står inte med.
    round_the_clock = next((v for v in facts.values if ROUND_THE_CLOCK.search(v)), "")
    if round_the_clock:
        facts.hours = round_the_clock
        facts.claims = tidy_points(facts.claims)
    return facts


def price_label(label):
    """'Pris, Rörjour' blir 'Rörjour'; annars etiketten som den är."""
    return re.sub(r"^\s*pris\s*[,:]\s*", "", label or "", flags=re.I).strip() or label
