"""
Kontrollerna av en kampanj (kundresan steg 7): det som måste stämma innan
kunden kan skicka förslaget till granskning.

    validate(campaign) -> [Problem, ...]

Tom lista betyder att förslaget går att skicka. Redigeraren visar varje
problem vid fältet det gäller (Problem.field, .part och .index), och
inskicket stoppas så länge listan inte är tom. Generatorn kör samma
textkontroll (text_problems) på allt AI skriver och slänger det som inte
klarar den, så AI aldrig kan smyga in en siffra eller ett löfte.

Reglerna:

- Googles gränser: rubriker högst 30 tecken, beskrivningar högst 90, minst
  3 rubriker och 2 beskrivningar, högst 15 och 4. Inga dubbletter.
- Siffror: varje tal i en text måste finnas bland kundens bekräftade
  uppgifter (eller i tjänstens namn, området och företagsnamnet). Ett pris
  eller en tid som ingen bekräftat blir aldrig en annons.
- Påståenden som inte går att belägga (billigast, bäst i ...) och löften
  om tider stoppas alltid. Garanti och gratis får bara stå om en bekräftad
  uppgift säger det.
- Rubriker och beskrivningar börjar inte med = + - @. Google Ads Editor-
  filen (exports.py) öppnas ofta i ett kalkylprogram, som läser sådana
  celler som formler; exports.safe_cell sätter då en apostrof först, och
  den följer med in i Google som en del av texten. Kunden skriver om
  början i stället ("Ring +46 8 ..." eller "20 % rabatt på jour").
- Sökord: minst ett, inget som krockar med ett negativt sökord.
- Budget per dag mellan BUDGET_MIN och BUDGET_MAX kr. Ett område.
"""

import re
from dataclasses import dataclass

from .models import (
    BUDGET_MAX,
    BUDGET_MIN,
    DESCRIPTION_COUNT,
    DESCRIPTION_MAX,
    HEADLINE_COUNT,
    HEADLINE_MAX,
)

HEADLINE_MIN = 3
DESCRIPTION_MIN = 2
#: Googles gränser för ett sökord.
KEYWORD_MAX_CHARS = 80
KEYWORD_MAX_WORDS = 10


@dataclass(frozen=True)
class Problem:
    #: Kampanjens fält: headlines, descriptions, keywords, negatives, page,
    #: daily_budget_kr, area.
    field: str
    message: str
    #: Vilken rad i en lista (rubrik nummer 3 är index 2). None = hela fältet.
    index: int | None = None
    #: Delen av sidan (page): title, lead, points, phone, form_title,
    #: questions, note.
    part: str = ""


@dataclass(frozen=True)
class Context:
    """Det texterna får luta sig mot: bekräftade uppgifter, tjänsten,
    området och företagsnamnet."""

    #: Talen som får förekomma (se number_tokens).
    numbers: frozenset
    #: Alla bekräftade uppgifter i gemener, för "står det bland fakta?".
    fact_text: str


# ---------------------------------------------------------------------------
# Siffror
# ---------------------------------------------------------------------------

#: Telefonnummer och andra långa tal med mellanrum eller bindestreck
#: ("08-000 00 00", "100 000") jämförs som en följd av siffror.
_LONG_NUMBER = re.compile(r"(?<!\w)\+?\d[\d \-]{5,}\d(?!\w)")
#: Ett tal som inte sitter ihop med bokstäver före ("m2" är en enhet, inte
#: ett påstående). Decimaler med komma eller punkt räknas som ett tal.
_NUMBER = re.compile(r"(?<![^\W\d_])\d+(?:[.,]\d+)?")


def _simple_numbers(text):
    return {m.group().replace(".", ",") for m in _NUMBER.finditer(text or "")}


def number_tokens(text):
    """Talen i en text: långa tal som bara siffror, övriga som de står
    (decimalpunkt som komma). "Ring 08-000 00 00, 4,8 i betyg" ger
    {"080000000", "4,8"}."""
    text = text or ""
    tokens = {re.sub(r"\D", "", m.group()) for m in _LONG_NUMBER.finditer(text)}
    return tokens | _simple_numbers(_LONG_NUMBER.sub(" ", text))


def allowed_numbers(texts):
    """Talen i de bekräftade texterna, i båda formerna: "08-000 00 00" ger
    både "080000000" och "08", "000", "00"."""
    allowed = set()
    for text in texts:
        allowed |= number_tokens(text) | _simple_numbers(text)
    return frozenset(allowed)


def build_context(fact_values, extra=()):
    """Context ur bekräftade värden plus annat kunden själv angett (tjänstens
    namn, området, företagsnamnet)."""
    values = [v for v in fact_values if v]
    return Context(
        numbers=allowed_numbers(list(values) + [e for e in extra if e]),
        fact_text=" ".join(values).lower(),
    )


def context_for(campaign):
    account = campaign.account
    return build_context(
        account.confirmed_facts().values(),
        extra=(campaign.service.name, campaign.area, account.customer.name),
    )


# ---------------------------------------------------------------------------
# Påståenden och löften
# ---------------------------------------------------------------------------

#: Stoppas alltid: går inte att belägga, eller lovar en tid åt kunden.
ALWAYS_BANNED = (
    (
        re.compile(r"\bbilligast\w*", re.I),
        "Skriv inte att ni är billigast. Det går inte att belägga.",
    ),
    (
        re.compile(r"\b(?:lägsta|lägst|bästa|bäst)\s+pris\w*", re.I),
        "Skriv inte att ni har bäst eller lägst pris. Det går inte att belägga.",
    ),
    (
        re.compile(r"\bbästa?\s+(?:i|på)\b", re.I),
        "Skriv inte att ni är bäst i eller bäst på något. Det går inte att belägga.",
    ),
    (
        re.compile(r"\b(?:snabbast\w*|marknadsledande|nummer ett)\b", re.I),
        "Skriv inga superlativ som inte går att belägga.",
    ),
    (
        # "inom en timme", "inom 2-3 dagar", "inom ett par veckor", "inom 24h".
        re.compile(
            r"\binom\s+(?:[\w-]+\s+){0,2}?(?:min(?:ut\w*)?|tim\w*|dag(?:ar|en)?|dygn\w*|veck\w*)\b"
            r"|\binom\s+\d+\s*h\b",
            re.I,
        ),
        "Lova inga tider, till exempel hur snabbt ni kommer eller hör av er.",
    ),
)

#: Får bara stå när en bekräftad uppgift säger samma sak.
UNLESS_CONFIRMED = (
    (
        re.compile(r"\bgarant\w*", re.I),
        re.compile(r"garant"),
        "Garantier får bara stå om de finns bland dina bekräftade uppgifter.",
    ),
    (
        re.compile(r"\b(?:gratis|kostnadsfri\w*|utan kostnad)\b", re.I),
        re.compile(r"gratis|kostnadsfri|utan kostnad"),
        "Gratis får bara stå om det finns bland dina bekräftade uppgifter.",
    ),
    (
        re.compile(r"\bsamma dag\b", re.I),
        re.compile(r"samma dag"),
        "Samma dag är ett löfte om tid. Det får bara stå om det finns bland "
        "dina bekräftade uppgifter.",
    ),
    (
        re.compile(r"\bdygnet runt\b|\balla dagar\b", re.I),
        re.compile(r"dygnet runt|alla dagar"),
        "Öppettider får bara stå om de finns bland dina bekräftade uppgifter.",
    ),
)


def text_problems(text, context):
    """Problemen i en enskild text (rubrik, beskrivning, en rad på sidan),
    som meddelanden. Tomt = texten klarar kontrollen."""
    text = text or ""
    found = []
    for pattern, message in ALWAYS_BANNED:
        if pattern.search(text):
            found.append(message)
    for pattern, fact_pattern, message in UNLESS_CONFIRMED:
        if pattern.search(text) and not fact_pattern.search(context.fact_text):
            found.append(message)
    for token in sorted(number_tokens(text) - context.numbers):
        found.append(f"Siffran {token} finns inte bland dina bekräftade uppgifter.")
    return found


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


#: Tecknen en cell inte får börja med (exports.FORMULA_PREFIXES har samma
#: plus tabb och CR, som saneringen redan tar bort).
FORMULA_STARTS = ("=", "+", "-", "@")
FORMULA_MESSAGE = (
    "Börja inte med =, +, - eller @. Kalkylprogram och Google Ads Editor läser "
    "det som en formel. Skriv om början, till exempel med ett ord först."
)


def starts_like_formula(text):
    return (text or "").strip().startswith(FORMULA_STARTS)


# ---------------------------------------------------------------------------
# Hela kampanjen
# ---------------------------------------------------------------------------


def _check_list(field, items, context, *, minimum, maximum, max_chars, noun, plural):
    """noun = (obestämd, bestämd): ("rubrik", "rubriken")."""
    indefinite, definite = noun
    problems = []
    count = len(items)
    if count < minimum:
        problems.append(Problem(field, f"Minst {minimum} {plural} behövs. Nu finns {count}."))
    if count > maximum:
        problems.append(Problem(field, f"Högst {maximum} {plural}. Nu finns {count}."))
    seen = set()
    for i, text in enumerate(items):
        length = len(text)
        if length > max_chars:
            problems.append(
                Problem(
                    field,
                    f"{definite.capitalize()} har {length} tecken. Googles gräns är {max_chars}.",
                    index=i,
                )
            )
        key = _norm(text)
        if key in seen:
            problems.append(
                Problem(
                    field,
                    f"Samma {indefinite} finns redan. Varje {indefinite} ska vara unik.",
                    index=i,
                )
            )
        seen.add(key)
        if starts_like_formula(text):
            problems.append(Problem(field, FORMULA_MESSAGE, index=i))
        for message in text_problems(text, context):
            problems.append(Problem(field, message, index=i))
    return problems


def _keyword_problems(keywords, negatives):
    problems = []
    texts = [(k.get("text") or "").strip() for k in keywords if isinstance(k, dict)]
    texts = [t for t in texts if t]
    if not texts:
        problems.append(Problem("keywords", "Kampanjen behöver minst ett sökord."))
        return problems
    negative_words = [_norm(n) for n in negatives if _norm(n)]
    for i, text in enumerate(texts):
        if len(text) > KEYWORD_MAX_CHARS:
            problems.append(
                Problem(
                    "keywords",
                    f"Sökordet har {len(text)} tecken. Googles gräns är {KEYWORD_MAX_CHARS}.",
                    index=i,
                )
            )
        if len(text.split()) > KEYWORD_MAX_WORDS:
            problems.append(
                Problem(
                    "keywords",
                    f"Sökordet har fler än {KEYWORD_MAX_WORDS} ord. Googles gräns är "
                    f"{KEYWORD_MAX_WORDS}.",
                    index=i,
                )
            )
        padded = f" {_norm(text)} "
        for negative in negative_words:
            if f" {negative} " in padded:
                problems.append(
                    Problem(
                        "keywords",
                        f'Sökordet krockar med det negativa sökordet "{negative}". '
                        "Då visas annonsen aldrig för det.",
                        index=i,
                    )
                )
                break
    return problems


#: Sidans textfält i den ordning de visas, med etiketten problemet får.
PAGE_TEXT_PARTS = ("title", "lead", "form_title", "note")


def _page_problems(page, context):
    problems = []
    page = page if isinstance(page, dict) else {}
    if not (page.get("title") or "").strip():
        problems.append(Problem("page", "Sidan behöver en rubrik.", part="title"))
    for part in PAGE_TEXT_PARTS:
        for message in text_problems(page.get(part) or "", context):
            problems.append(Problem("page", message, part=part))
    for i, point in enumerate(page.get("points") or []):
        for message in text_problems(str(point), context):
            problems.append(Problem("page", message, index=i, part="points"))
    for i, question in enumerate(page.get("questions") or []):
        label = question.get("label", "") if isinstance(question, dict) else str(question)
        for message in text_problems(label, context):
            problems.append(Problem("page", message, index=i, part="questions"))
    phone = (page.get("phone") or "").strip()
    if phone and number_tokens(phone) - context.numbers:
        problems.append(
            Problem(
                "page",
                "Telefonnumret finns inte bland dina bekräftade uppgifter.",
                part="phone",
            )
        )
    return problems


def validate(campaign, context=None):
    """Alla problem i kampanjen, i den ordning redigeraren visar flikarna."""
    context = context or context_for(campaign)
    problems = []
    if campaign.service_id and starts_like_formula(campaign.service.name):
        # Tjänstens namn blir annonsgruppen och kampanjens namn i Editor-filen.
        problems.append(Problem("service", FORMULA_MESSAGE))
    if not (campaign.area or "").strip():
        problems.append(Problem("area", "Ange var annonserna ska visas."))
    budget = campaign.daily_budget_kr or 0
    if not BUDGET_MIN <= budget <= BUDGET_MAX:
        problems.append(
            Problem(
                "daily_budget_kr",
                f"Budgeten per dag ska vara mellan {BUDGET_MIN} och {BUDGET_MAX} kr.",
            )
        )
    problems += _check_list(
        "headlines",
        [str(h) for h in campaign.headlines or []],
        context,
        minimum=HEADLINE_MIN,
        maximum=HEADLINE_COUNT,
        max_chars=HEADLINE_MAX,
        noun=("rubrik", "rubriken"),
        plural="rubriker",
    )
    problems += _check_list(
        "descriptions",
        [str(d) for d in campaign.descriptions or []],
        context,
        minimum=DESCRIPTION_MIN,
        maximum=DESCRIPTION_COUNT,
        max_chars=DESCRIPTION_MAX,
        noun=("beskrivning", "beskrivningen"),
        plural="beskrivningar",
    )
    problems += _keyword_problems(campaign.keywords or [], campaign.negatives or [])
    problems += _page_problems(campaign.page, context)
    return problems
