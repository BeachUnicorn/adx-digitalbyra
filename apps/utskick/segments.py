"""
Segmenten (README B.4, I.11, H.1, J S4): regler som blir ett Q med
Exists-delfrågor, alltid från kontots egna kontakter.

Formen på Segment.rules:

    {"all": [regel | {"any": [regel, ...]}, ...]}

    regel = {"f": fält, "op": operator, "v": värde}     (v saknas för empty,
                                                         not_empty, eligible och
                                                         not_eligible)

Alla regler i "all" ska gälla (OCH); en grupp {"any": [...]} gäller när
minst en av dess regler gäller ("+ Grupp (ELLER)", en nivå, inga grupper i
grupper). Högst MAX_RULES regler (grupperna inräknade regel för regel) och
MAX_GROUPS grupper. Ett tomt "all" är ett segment utan kontakter (aldrig
"alla").

Fälten och deras operatorer (FIELDS):

    list            in, not_in            v = [lista-id]          (owned_ids ContactList)
    tag             in, not_in            v = [tagg-id]           (owned_ids Tag)
    field:<nyckel>  efter FieldDef.kind:
                      text    eq, not_eq, contains, empty, not_empty   v = sträng
                      number  eq, lt, gt, empty, not_empty            v = tal
                      date    before_days, within_days, next_days,
                              before_months, within_months, next_months,
                              empty, not_empty                         v = heltal
                              ("äldre än 5 månader" = before_months 5)
                      choice  in, not_in, empty, not_empty            v = [val]
    contact:<namn>  kontaktens fasta fält (CONTACT_FIELDS): förnamn, efternamn
                    och företag med textens operatorer, e-post med contains,
                    eq, not_eq, empty och not_empty, mobil och
                    organisationsnummer med empty och not_empty
    consent         eligible, not_eligible   v = {"channel": "sms" | "email"}
                                             (kan få erbjudanden: samma regel som
                                             Kontakters "kan få sms",
                                             consent.eligible_contacts)
                    in, not_in            v = {"channel": ..., "status": [Consent.Status]}
    kind            eq                    v = "person" | "company"
    got_utskick     in, not_in            v = [utskick-id]  fick utskicket (SENT_LIKE);
                                          in aldrig den som anmälde det som skräp
                    within_days, not_within_days   v = dagar (något utskick)
    opened          in, not_in, within_days, not_within_days   (låst, se nedan)
    clicked         in, not_in            mottagaren klickade (first_clicked_at,
                                          som rapporten)
                    within_days, not_within_days   ett mänskligt klick (Click)
    visited_lp      within_days, not_within_days   Event lp_visit eller site_visit
    lead            within_days, not_within_days   förfrågan (Lead.contact, inte
                                                   svarstrådarnas och inte skräp)
    answer:<sida>.<fråga>
                    in, not_in            v = [alternativ]  svar i formulär: en
                                          förfrågan som lead ovan (kopplad till
                                          kontakten, inte skräp eller svarstrådar)
                                          valde något av alternativen på sidans
                                          flervalsfråga (Lead.choice_answers,
                                          apps/flamingo/answers.py); sidan är
                                          LandingPage-id och hämtas med kontot
                                          i villkoret (ForeignIds annars)
    replied         within_days, not_within_days   svar i en tråd (ThreadMessage
                                                   in, inte STOPP-trådar)
    source          in, not_in            v = [Contact.Source]
    created         before_days, within_days, before_months, within_months

within_days betyder "minst en de senaste n dagarna", not_within_days "ingen
de senaste n dagarna" (en kontakt utan någon förfrågan alls är med). För
datum (field:<datum>, created) är before_* "äldre än", within_* "inom de
senaste" och next_* "inom de kommande". Dagar och månader räknas i
kalenderdagar och kalendermånader från dagens datum i Stockholm (31 mars
minus en månad är 28 februari); aktiviteten räknas bakåt från nu.

Acceptansens segment "Service i höst" (J S4):

    {"all": [{"f": "field:senaste-service", "op": "before_months", "v": 5},
             {"f": "list", "op": "in", "v": [<Kunder>]},
             {"f": "lead", "op": "not_within_days", "v": 30}]}

Uppföljningen från rapporten ("Följ upp de som inte klickade", I.8):

    {"all": [{"f": "got_utskick", "op": "in", "v": [<utskick>]},
             {"f": "clicked", "op": "not_in", "v": [<utskick>]}]}

Regler (H.1): clean prövar varje list-, tagg- och utskicks-id med
access.owned_ids (ett främmande id ger ForeignIds, vyn svarar 400) och varje
fältnyckel mot kontots FieldDef. En svarsregel prövar att sidan är kontots
(den hämtas med kontot i villkoret, bara de sidor reglerna pekar på;
ForeignIds annars, som owned_ids) och att frågan och alternativen finns på
sidan nu (answers.questions_by_page); den sparade regeln kompileras ändå med
sina nycklar om frågan tas bort sedan (förfrågningarna står kvar).
compile_q börjar aldrig själv: contacts() börjar från
Contact.objects.filter(account=account), och varje delfråga har
list__account=, tag__account=, utskick__account= eller account= i
villkoret, så ett manipulerat id i databasen ger färre kontakter, aldrig
någon annans. En regel som inte går att läsa ger inga kontakter. Varje
villkor är sant eller falskt, aldrig NULL, där det kan negeras
(Coalesce på fältens text); matches_q är "pk IN (delfråga)", så att
utskickets undantag (exclude) aldrig tappar kontakter som saknar ett fält.

"opened" är låst när kontot aldrig haft Spåra öppningar på
(OPENED_LOCKED_TEXT, I.11). Räkningen "kan få sms" och "kan få e-post" är
samma bedömning som Kontakters rubrik och utskicket gör för reklam
(consent.eligible_contacts: samtycket för adressen och spärrlistan), så att
segmentets siffror stämmer med frysningen (veckotaket och landet räknas
bara i utskicket).

    MAX_RULES, MAX_GROUPS, FIELDS, OPS, OPENED_LOCKED_TEXT
    SegmentError(errors, rows=None)           svenska texter per regel, för formuläret
    clean(account, rules) -> dict             prövade regler; ForeignIds eller SegmentError
    clean_partial(account, rules) -> (dict, [plats])
                                              som clean, men en regel utan värde hoppas
                                              över (den levande räkningen medan kunden
                                              bygger); främmande id:n ger ändå ForeignIds
    compile_q(account_id, rules, now=None) -> Q
    contacts(account, rules, now=None) -> QuerySet[Contact]
    count(account, rules, now=None) -> {"total", "sms", "email"}
    refresh(segment, now=None) -> Segment     cached_* och counted_at
    refresh_all(now=None, seconds=120) -> int utskick_daily (D.1): de äldsta räkningarna först
    matches_q(account_id, segment_ids, now=None) -> Q
                                              audience.py: kontakter i något av segmenten
    for_contact(contact, now=None) -> list[Segment]   segmentchipsen på kontaktkortet
    opened_locked(account) -> bool
    describe(account, rules) -> list[str]     "Senaste service äldre än 5 månader", ...
    follow_up_rules(utskick) -> dict          reglerna ovan
    create_follow_up(utskick, *, user, now=None) -> Segment
                                              "Klickade inte: <namn>" (unikt namn, " 2" osv.)
"""

import operator
import re
import time as monotonic_clock
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import reduce

from django.db import IntegrityError, transaction
from django.db.models import (
    BooleanField,
    Case,
    Count,
    DecimalField,
    Exists,
    F,
    OuterRef,
    Q,
    TextField,
    Value,
    When,
)
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Coalesce, Collate
from django.db.models.lookups import (
    Exact,
    GreaterThan,
    GreaterThanOrEqual,
    IContains,
    IExact,
    In,
    LessThan,
    LessThanOrEqual,
    Regex,
)
from django.utils import timezone

from apps.common.security import normalize_typography
from apps.sms.pricing import STOCKHOLM

from . import consent as consents
from .access import ForeignIds, owned_ids, settings_for
from .models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    CHANNELS,
    REKLAM,
    Click,
    Consent,
    Contact,
    ContactList,
    Event,
    FieldDef,
    ListMembership,
    Recipient,
    Segment,
    Tag,
    Thread,
    ThreadMessage,
    Utskick,
)

#: Högst så här många regler och grupper i ett segment.
MAX_RULES = 20
MAX_GROUPS = 5
#: Högst så här många id:n eller val i en regel.
MAX_IDS = 50
#: Gränserna för antal dagar och månader, och för en text.
MAX_DAYS = 3650
MAX_MONTHS = 120
MAX_TEXT = 100

TEXT_OPS = ("eq", "not_eq", "contains", "empty", "not_empty")
NUMBER_OPS = ("eq", "lt", "gt", "empty", "not_empty")
DATE_OPS = (
    "before_days",
    "within_days",
    "next_days",
    "before_months",
    "within_months",
    "next_months",
    "empty",
    "not_empty",
)
CHOICE_OPS = ("in", "not_in", "empty", "not_empty")
ID_OPS = ("in", "not_in")
ACTIVITY_OPS = ("in", "not_in", "within_days", "not_within_days")
WINDOW_OPS = ("within_days", "not_within_days")
CREATED_OPS = ("before_days", "within_days", "before_months", "within_months")
CONSENT_OPS = ("eligible", "not_eligible", "in", "not_in")
#: Operatorer utan värde.
NO_VALUE_OPS = ("empty", "not_empty", "eligible", "not_eligible")

#: Extrafältens operatorer efter FieldDef.kind.
FIELD_KIND_OPS = {
    FieldDef.Kind.TEXT: TEXT_OPS,
    FieldDef.Kind.NUMBER: NUMBER_OPS,
    FieldDef.Kind.DATE: DATE_OPS,
    FieldDef.Kind.CHOICE: CHOICE_OPS,
}

#: Kontaktens fasta fält (f = "contact:<namn>"): namn -> (rubrik, operatorer).
CONTACT_FIELDS = {
    "first_name": ("Förnamn", TEXT_OPS),
    "last_name": ("Efternamn", TEXT_OPS),
    "company_name": ("Företag", TEXT_OPS),
    "email": ("E-post", ("contains", "eq", "not_eq", "empty", "not_empty")),
    "phone": ("Mobil", ("empty", "not_empty")),
    "org_number": ("Organisationsnummer", ("empty", "not_empty")),
}

#: Fält -> operatorer (field:<nyckel> beror på FieldDef.kind och
#: contact:<namn> på CONTACT_FIELDS, se ovan).
FIELDS = {
    "list": ID_OPS,
    "tag": ID_OPS,
    "field": tuple(dict.fromkeys(op for ops in FIELD_KIND_OPS.values() for op in ops)),
    "contact": TEXT_OPS,
    "consent": CONSENT_OPS,
    "kind": ("eq",),
    "got_utskick": ACTIVITY_OPS,
    "opened": ACTIVITY_OPS,
    "clicked": ACTIVITY_OPS,
    "visited_lp": WINDOW_OPS,
    "lead": WINDOW_OPS,
    # Svar i formulär (Giovanni 2026-10-10): answer:<sida>.<fråga>.
    "answer": ID_OPS,
    "replied": WINDOW_OPS,
    "source": ID_OPS,
    "created": CREATED_OPS,
}
OPS = tuple(sorted({op for ops in FIELDS.values() for op in ops}))

OPENED_LOCKED_TEXT = "Öppningar spåras inte. Slå på Spåra öppningar under Inställningar."

SHAPE_TEXT = "Villkoren går inte att läsa. Ladda om sidan och försök igen."
TOO_MANY_RULES_TEXT = f"Ett segment kan ha högst {MAX_RULES} villkor."
TOO_MANY_GROUPS_TEXT = f"Ett segment kan ha högst {MAX_GROUPS} grupper."
FULL_TEXT = f"Du har {Segment.MAX_PER_ACCOUNT} segment. Ta bort ett för att skapa ett nytt."
#: Vad som saknas i en regel, per slag av värde.
MISSING_TEXTS = {
    "field": "Välj vad villkoret gäller.",
    "op": "Välj hur det ska jämföras.",
    "list": "Välj en lista.",
    "tag": "Välj en tagg.",
    "utskick": "Välj ett utskick.",
    "days": "Skriv ett antal dagar.",
    "months": "Skriv ett antal månader.",
    "text": "Skriv ett värde.",
    "number": "Skriv ett tal.",
    "choice": "Välj ett värde.",
    "status": "Välj ett samtycke.",
    "channel": "Välj sms eller e-post.",
    "kind": "Välj privatperson eller företag.",
    "source": "Välj en källa.",
    "answer": "Välj ett svar.",
}
GONE_FIELD_TEXT = "Fältet finns inte längre. Välj ett annat."
GONE_QUESTION_TEXT = "Frågan finns inte längre på sidan. Välj en annan."
ANSWER_CHOICE_TEXT = "Välj ett av frågans svar."
DAYS_TEXT = f"Skriv ett antal dagar från 1 till {MAX_DAYS}."
MONTHS_TEXT = f"Skriv ett antal månader från 1 till {MAX_MONTHS}."
NUMBER_TEXT = "Skriv ett tal, till exempel 2 eller 2,5."
TEXT_LONG_TEXT = f"Skriv högst {MAX_TEXT} tecken."
CHOICE_TEXT = "Välj ett av fältets val."
VALUE_TEXT = "Värdet går inte att använda. Välj igen."
TOO_MANY_IDS_TEXT = f"Välj högst {MAX_IDS} i ett villkor."

#: Inga kontakter. pk__in=[] blir alltid falskt, också i Case/When och
#: under en negation (Django hoppar över den tomma listan rätt).
NOTHING = Q(pk__in=[])

_ISO_DATE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
#: Svarsregelns nyckel: <sidans id>.<frågans nyckel> (fullmatch).
_ANSWER_KEY = re.compile(r"([1-9][0-9]{0,9})\.([a-z0-9][a-z0-9_-]{0,39})")
_NUMBER = r"^-?[0-9]{1,15}([.][0-9]{1,10})?$"
_DECIMAL = DecimalField(max_digits=26, decimal_places=10)
#: Fält som har id:n i v (modellen, och texten när inget är valt).
_ID_MODELS = {
    "list": (ContactList, "list"),
    "tag": (Tag, "tag"),
    "got_utskick": (Utskick, "utskick"),
    "opened": (Utskick, "utskick"),
    "clicked": (Utskick, "utskick"),
}


class SegmentError(ValueError):
    """Reglerna går inte att spara: errors är svenska texter (en per regel
    eller en för hela segmentet), för formuläret och JSON-svaret. rows är
    {plats: text} för reglerna (1 för den första, grupperna inräknade regel
    för regel), så att formuläret kan visa felet vid raden."""

    def __init__(self, errors, rows=None):
        self.errors = list(errors)
        self.rows = dict(rows or {})
        super().__init__("; ".join(self.errors))


class _Missing(Exception):
    """En regel saknar sitt värde (key i MISSING_TEXTS)."""

    def __init__(self, key):
        super().__init__(key)
        self.key = key


class _Bad(Exception):
    """Ett värde i en regel går inte att använda (texten)."""

    def __init__(self, text):
        super().__init__(text)
        self.text = text


# ---------------------------------------------------------------------------
# Tid
# ---------------------------------------------------------------------------


def today_in_stockholm(now=None):
    return timezone.localtime(now or timezone.now(), STOCKHOLM).date()


def shift_months(day, months):
    """Samma dag months månader fram (negativt: bakåt), sista dagen i
    månaden när dagen saknas där (31 mars minus en månad är 28 februari)."""
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _date_window(op, n, today):
    """(från, till) som datum (None är öppet) för en datumoperator."""
    unit_months = op.endswith("_months")
    back = shift_months(today, -n) if unit_months else today - timedelta(days=n)
    ahead = shift_months(today, n) if unit_months else today + timedelta(days=n)
    if op.startswith("before"):
        return None, back
    if op.startswith("within"):
        return back, today
    return today, ahead


def _midnight(day):
    return datetime.combine(day, time(0, 0), tzinfo=STOCKHOLM)


# ---------------------------------------------------------------------------
# Läsa värden (tåliga: ett trasigt värde i databasen ger inga kontakter)
# ---------------------------------------------------------------------------


def _items(rules):
    """Raderna i "all", eller None när formen inte går att läsa."""
    if rules in (None, {}):
        return []
    if not isinstance(rules, dict) or set(rules) - {"all"}:
        return None
    items = rules.get("all", [])
    return items if isinstance(items, list) else None


def _as_list(value):
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _ids(value):
    out = []
    for raw in _as_list(value):
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int) and raw > 0:
            number = raw
        elif isinstance(raw, str) and raw.strip().isdigit() and int(raw) > 0:
            number = int(raw.strip())
        else:
            continue
        if number not in out:
            out.append(number)
    return out[:MAX_IDS]


def _strings(value):
    out = []
    for raw in _as_list(value):
        if isinstance(raw, str) and raw.strip() and raw.strip() not in out:
            out.append(raw.strip())
    return out[:MAX_IDS]


def _count(value, top):
    """Ett heltal 1..top, annars ValueError."""
    if isinstance(value, bool) or value is None:
        raise ValueError
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError
        value = int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            raise ValueError
        value = int(text)
    if not isinstance(value, int) or not 1 <= value <= top:
        raise ValueError
    return value


def _number(value):
    """Ett tal som Decimal ("2,5" går bra), annars ValueError."""
    if isinstance(value, bool) or value is None:
        raise ValueError
    text = str(value).strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError from None
    if not number.is_finite() or abs(number) >= Decimal(10) ** 15:
        raise ValueError
    return number


def _plain(number):
    """Decimal till int eller float för JSON (2 i stället för 2.0)."""
    return int(number) if number == number.to_integral_value() else float(number)


def _text(value):
    if not isinstance(value, str):
        raise ValueError
    return " ".join(normalize_typography(value).split())


def _answer_key(key):
    """(sidans id, frågans nyckel) ur en svarsregels nyckel ("12.tjanst"),
    eller None."""
    match = _ANSWER_KEY.fullmatch(key) if isinstance(key, str) else None
    if match is None:
        return None
    return int(match.group(1)), match.group(2)


def _split(rule):
    """(namn, nyckel, op, v) ur en regel, eller None."""
    if not isinstance(rule, dict):
        return None
    f, op = rule.get("f"), rule.get("op")
    if not isinstance(f, str) or not isinstance(op, str):
        return None
    name, _, key = f.partition(":")
    return name, key, op, rule.get("v")


class _Context:
    """Det en kompilering eller prövning behöver om kontot: tiden,
    extrafälten och flervalsfrågorna på sidorna reglerna pekar på (en
    databasfråga för fälten, en per sida, första gången de behövs)."""

    def __init__(self, account_id, now=None):
        self.account_id = account_id
        self.now = now or timezone.now()
        self.today = today_in_stockholm(self.now)
        self._fields = None
        self._pages = {}

    @property
    def fields(self):
        if self._fields is None:
            self._fields = {d.key: d for d in FieldDef.objects.filter(account_id=self.account_id)}
        return self._fields

    def page_questions(self, page_id):
        """{frågans nyckel: answers.ChoiceQuestion} för sidan som besökarna
        ser den, eller None när sidan inte är kontots (eller inte finns).
        Bara sidan hämtas, med kontot i villkoret, en gång per sida."""
        if page_id not in self._pages:
            from apps.flamingo import answers as form_answers

            found = form_answers.questions_by_page(self.account_id, [page_id])
            questions = found.get(page_id)
            self._pages[page_id] = None if questions is None else {q.key: q for q in questions}
        return self._pages[page_id]


# ---------------------------------------------------------------------------
# Kompileringen: regler -> Q
# ---------------------------------------------------------------------------


def _text_expr(key):
    return Coalesce(KeyTextTransform(key, "fields"), Value(""), output_field=TextField())


def _text_q(expr, op, value):
    """eq, not_eq, contains, empty och not_empty på ett uttryck som aldrig
    är NULL (skiftlägesokänsligt)."""
    if op == "empty":
        return Q(Exact(expr, ""))
    if op == "not_empty":
        return ~Q(Exact(expr, ""))
    text = _text(value)
    if not text:
        return None
    if op == "eq":
        return Q(IExact(expr, text))
    if op == "not_eq":
        return ~Q(IExact(expr, text))
    if op == "contains":
        return Q(IContains(expr, text))
    return None


def _c_list(ctx, key, op, value):
    ids = _ids(value)
    if not ids:
        return None
    hit = Exists(
        ListMembership.objects.filter(
            contact=OuterRef("pk"), list_id__in=ids, list__account_id=ctx.account_id
        )
    )
    return Q(hit) if op == "in" else ~Q(hit)


def _c_tag(ctx, key, op, value):
    ids = _ids(value)
    if not ids:
        return None
    hit = Exists(
        Contact.tags.through.objects.filter(
            contact_id=OuterRef("pk"), tag_id__in=ids, tag__account_id=ctx.account_id
        )
    )
    return Q(hit) if op == "in" else ~Q(hit)


def _c_field(ctx, key, op, value):
    definition = ctx.fields.get(key)
    if definition is None or op not in FIELD_KIND_OPS.get(definition.kind, ()):
        return None
    text = _text_expr(key)
    if op in ("empty", "not_empty"):
        return _text_q(text, op, None)
    kind = definition.kind
    if kind == FieldDef.Kind.TEXT:
        return _text_q(text, op, value)
    if kind == FieldDef.Kind.CHOICE:
        values = _strings(value)
        if not values:
            return None
        hit = Q(In(text, values))
        return hit if op == "in" else ~hit
    raw = KeyTextTransform(key, "fields")
    if kind == FieldDef.Kind.NUMBER:
        number = Case(
            When(Regex(raw, _NUMBER), then=Cast(raw, _DECIMAL)),
            default=None,
            output_field=_DECIMAL,
        )
        wanted = _number(value)
        lookup = {"eq": Exact, "lt": LessThan, "gt": GreaterThan}[op]
        return Q(lookup(number, wanted))
    if kind == FieldDef.Kind.DATE:
        top = MAX_MONTHS if op.endswith("_months") else MAX_DAYS
        start, end = _date_window(op, _count(value, top), ctx.today)
        # Datumen är text åååå-mm-dd: jämförs som text (C-kollationen, siffra
        # för siffra), bara när värdet har den formen. Inget kastas till date,
        # så ett trasigt värde i ett fält stoppar aldrig frågan.
        stamp = Collate(raw, "C")
        q = Q(Regex(raw, _ISO_DATE))
        if start is not None:
            q &= Q(GreaterThanOrEqual(stamp, start.isoformat()))
        if end is not None:
            bound = LessThan if op.startswith("before") else LessThanOrEqual
            q &= Q(bound(stamp, end.isoformat()))
        return q
    return None


def _c_contact(ctx, key, op, value):
    """Kontaktens fasta fält. Namnet kommer ur CONTACT_FIELDS, aldrig ur
    förfrågan; kolumnerna är aldrig NULL."""
    spec = CONTACT_FIELDS.get(key)
    if spec is None or op not in spec[1]:
        return None
    name = key
    if op == "empty":
        return Q(**{name: ""})
    if op == "not_empty":
        return ~Q(**{name: ""})
    text = _text(value)
    if not text:
        return None
    if op == "eq":
        return Q(**{f"{name}__iexact": text})
    if op == "not_eq":
        return ~Q(**{f"{name}__iexact": text})
    if op == "contains":
        return Q(**{f"{name}__icontains": text})
    return None


def _c_consent(ctx, key, op, value):
    if not isinstance(value, dict):
        return None
    channel = value.get("channel")
    if channel not in CHANNELS:
        return None
    if op in ("eligible", "not_eligible"):
        # Villkoret på kontakten själv (consent.eligible_q), inte pk IN (alla
        # kontots kontakter som kan få): i for_contacts CASE för ett enda
        # kort läste den formen hela kontot en gång per segment. Aldrig NULL.
        ok = consents.eligible_q(channel, REKLAM)
        return ok if op == "eligible" else ~ok
    statuses = [s for s in _strings(value.get("status")) if s in Consent.Status.values]
    if not statuses:
        return None
    hit = Exists(
        Consent.objects.filter(contact=OuterRef("pk"), channel=channel, status__in=statuses)
    )
    return Q(hit) if op == "in" else ~Q(hit)


def _c_kind(ctx, key, op, value):
    if value not in Contact.Kind.values:
        return None
    return Q(kind=value)


def _c_source(ctx, key, op, value):
    values = [v for v in _strings(value) if v in Contact.Source.values]
    if not values:
        return None
    return Q(source__in=values) if op == "in" else ~Q(source__in=values)


def _c_created(ctx, key, op, value):
    top = MAX_MONTHS if op.endswith("_months") else MAX_DAYS
    start, end = _date_window(op, _count(value, top), ctx.today)
    if op.startswith("before"):
        return Q(created_at__lt=_midnight(end))
    return Q(created_at__gte=_midnight(start))


def _since(ctx, value):
    return ctx.now - timedelta(days=_count(value, MAX_DAYS))


def _exists(rows, op):
    hit = Exists(rows)
    return Q(hit) if op in ("in", "within_days") else ~Q(hit)


def _c_got_utskick(ctx, key, op, value):
    rows = Recipient.objects.filter(
        contact=OuterRef("pk"),
        utskick__account_id=ctx.account_id,
        status__in=Recipient.SENT_LIKE,
    )
    if op in ("in", "not_in"):
        ids = _ids(value)
        if not ids:
            return None
        got = _exists(rows.filter(utskick_id__in=ids), op)
        if op == "not_in":
            return got
        # "Fick X" tar aldrig med den som anmälde X som skräp: klagomålet
        # spärrar bara e-posten (inbound/events.py), och uppföljningen (I.8)
        # skulle annars nå dem med sms. "Fick inte X" tar inte heller med dem
        # (de fick det), så en regel på ett utskick riktar sig aldrig till dem.
        complained = Recipient.objects.filter(
            contact=OuterRef("pk"),
            utskick__account_id=ctx.account_id,
            utskick_id__in=ids,
            status=Recipient.Status.COMPLAINED,
        )
        return got & ~Q(Exists(complained))
    return _exists(rows.filter(sent_at__gte=_since(ctx, value)), op)


def _c_opened(ctx, key, op, value):
    rows = Recipient.objects.filter(
        contact=OuterRef("pk"), utskick__account_id=ctx.account_id, opened_at__isnull=False
    )
    if op in ("in", "not_in"):
        ids = _ids(value)
        if not ids:
            return None
        return _exists(rows.filter(utskick_id__in=ids), op)
    return _exists(rows.filter(opened_at__gte=_since(ctx, value)), op)


def _c_clicked(ctx, key, op, value):
    if op in ("in", "not_in"):
        ids = _ids(value)
        if not ids:
            return None
        rows = Recipient.objects.filter(
            contact=OuterRef("pk"),
            utskick__account_id=ctx.account_id,
            utskick_id__in=ids,
            first_clicked_at__isnull=False,
        )
        return _exists(rows, op)
    rows = Click.objects.filter(
        contact=OuterRef("pk"),
        account_id=ctx.account_id,
        kind=Click.Kind.HUMAN,
        at__gte=_since(ctx, value),
    )
    return _exists(rows, op)


def _c_visited_lp(ctx, key, op, value):
    rows = Event.objects.filter(
        contact=OuterRef("pk"),
        account_id=ctx.account_id,
        kind__in=(Event.LP_VISIT, Event.SITE_VISIT),
        at__gte=_since(ctx, value),
    )
    return _exists(rows, op)


def _c_lead(ctx, key, op, value):
    from apps.flamingo.models import Lead

    rows = (
        Lead.objects.filter(
            contact=OuterRef("pk"),
            account_id=ctx.account_id,
            created_at__gte=_since(ctx, value),
        )
        .exclude(source=Lead.SOURCE_REPLY)
        .exclude(status=Lead.STATUS_JUNK)
    )
    return _exists(rows, op)


def _c_answer(ctx, key, op, value):
    """Svar i formulär: en förfrågan kopplad till kontakten (som "lead": inte
    svarstrådarnas och inte skräp) valde något av alternativen på sidans
    fråga. Bara kontots förfrågningar; sidan i Lead.choice_answers är alltid
    kampanjens egen (leads.create_lead), aldrig något som postats."""
    from apps.flamingo import answers as form_answers
    from apps.flamingo.models import Lead

    parsed = _answer_key(key)
    options = [o for o in _strings(value) if form_answers.KEY_RE.fullmatch(o)]
    if parsed is None or not options:
        return None
    rows = (
        Lead.objects.filter(contact=OuterRef("pk"), account_id=ctx.account_id)
        .filter(form_answers.chose_q(parsed[0], parsed[1], options))
        .exclude(source=Lead.SOURCE_REPLY)
        .exclude(status=Lead.STATUS_JUNK)
    )
    return _exists(rows, op)


def _c_replied(ctx, key, op, value):
    rows = ThreadMessage.objects.filter(
        thread__contact=OuterRef("pk"),
        thread__account_id=ctx.account_id,
        direction=ThreadMessage.Direction.IN,
        at__gte=_since(ctx, value),
    ).exclude(thread__kind=Thread.Kind.STOP)
    return _exists(rows, op)


_COMPILERS = {
    "list": _c_list,
    "tag": _c_tag,
    "field": _c_field,
    "contact": _c_contact,
    "consent": _c_consent,
    "kind": _c_kind,
    "source": _c_source,
    "created": _c_created,
    "got_utskick": _c_got_utskick,
    "opened": _c_opened,
    "clicked": _c_clicked,
    "visited_lp": _c_visited_lp,
    "lead": _c_lead,
    "answer": _c_answer,
    "replied": _c_replied,
}


def _leaf(ctx, rule):
    parts = _split(rule)
    if parts is None:
        return NOTHING
    name, key, op, value = parts
    compiler = _COMPILERS.get(name)
    if compiler is None or op not in FIELDS.get(name, ()):
        return NOTHING
    try:
        q = compiler(ctx, key, op, value)
    except (TypeError, ValueError, KeyError, ArithmeticError):
        return NOTHING
    return q if q is not None else NOTHING


def _shape_ok(items):
    """Ryms reglerna i gränserna? (en manipulerad rad ska inte bli en
    jättefråga)."""
    leaves = groups = 0
    for item in items:
        if isinstance(item, dict) and "any" in item:
            members = item.get("any")
            if not isinstance(members, list) or not members:
                return False
            groups += 1
            leaves += len(members)
        else:
            leaves += 1
    return leaves <= MAX_RULES and groups <= MAX_GROUPS


def compile_q(account_id, rules, now=None, ctx=None):
    """Reglerna som ett Q på Contact med Exists-delfrågor, varje delfråga med
    kontot i villkoret. Används bara på Contact.objects.filter(account=...).
    Tomma, trasiga eller för många regler ger inga kontakter."""
    ctx = ctx or _Context(account_id, now)
    items = _items(rules)
    if not items or not _shape_ok(items):
        return NOTHING
    parts = []
    for item in items:
        if isinstance(item, dict) and "any" in item:
            parts.append(reduce(operator.or_, (_leaf(ctx, rule) for rule in item["any"])))
        else:
            parts.append(_leaf(ctx, item))
    return reduce(operator.and_, parts)


def contacts(account, rules, now=None):
    """Segmentets kontakter: Contact.objects.filter(account=account) och
    compile_q, sorterade på pk. Tomma regler ger inga kontakter."""
    return (
        Contact.objects.filter(account=account)
        .filter(compile_q(account.pk, rules, now))
        .order_by("pk")
    )


def count(account, rules, now=None):
    """Den levande räkningen (app_segment_count): {"total", "sms", "email"},
    där sms och email är de som kan få reklam i kanalen (som Kontakters
    rubrik, consent.eligible_contacts)."""
    rows = contacts(account, rules, now).order_by()
    # En fråga i stället för tre COUNT som var och en räknade om segmentet.
    counted = rows.aggregate(
        total=Count("pk"),
        sms=Count("pk", filter=consents.eligible_q(CHANNEL_SMS, REKLAM)),
        email=Count("pk", filter=consents.eligible_q(CHANNEL_EMAIL, REKLAM)),
    )
    return {key: counted[key] or 0 for key in ("total", "sms", "email")}


def refresh(segment, now=None):
    """Räkna om segmentet och spara cached_count, cached_sms, cached_email
    och counted_at."""
    now = now or timezone.now()
    counted = count(segment.account, segment.rules, now)
    segment.cached_count = counted["total"]
    segment.cached_sms = counted["sms"]
    segment.cached_email = counted["email"]
    segment.counted_at = now
    Segment.objects.filter(pk=segment.pk).update(
        cached_count=segment.cached_count,
        cached_sms=segment.cached_sms,
        cached_email=segment.cached_email,
        counted_at=now,
    )
    return segment


def refresh_all(now=None, seconds=120):
    """utskick_daily (D.1): räkna om segmenten hos konton med utskick på,
    de äldsta räkningarna först, högst seconds sekunder. Antal räknade."""
    now = now or timezone.now()
    deadline = monotonic_clock.monotonic() + seconds
    rows = (
        Segment.objects.filter(account__is_enabled=True, account__utskick__is_enabled=True)
        .select_related("account")
        .order_by(F("counted_at").asc(nulls_first=True), "pk")
    )
    done = 0
    for segment in rows.iterator(chunk_size=50):
        if monotonic_clock.monotonic() > deadline:
            break
        refresh(segment, now)
        done += 1
    return done


def matches_q(account_id, segment_ids, now=None):
    """Q för kontakter i något av segmenten (Utskick.audience "segments" och
    undantagen). Segmenten hämtas med kontot i villkoret; ett främmande eller
    borttaget id bidrar inte med någon. Formen är pk IN (delfråga), så att
    ett undantag (exclude) aldrig påverkas av villkor som är okända för en
    kontakt."""
    ids = _ids(segment_ids)
    if not ids:
        return NOTHING
    ctx = _Context(account_id, now)
    parts = [
        compile_q(account_id, segment.rules, now, ctx=ctx)
        for segment in Segment.objects.filter(account_id=account_id, pk__in=ids).only("pk", "rules")
    ]
    if not parts:
        return NOTHING
    inner = (
        Contact.objects.filter(account_id=account_id)
        .filter(reduce(operator.or_, parts))
        .values("pk")
    )
    return Q(pk__in=inner)


def for_contact(contact, now=None):
    """Kontots segment som kontakten är med i, för chipsen på kortet (I.7),
    sorterade på namn. En fråga per 20 segment."""
    rows = list(Segment.objects.filter(account_id=contact.account_id).order_by("name", "pk"))
    if not rows:
        return []
    ctx = _Context(contact.account_id, now)
    found = []
    for start in range(0, len(rows), 20):
        batch = rows[start : start + 20]
        flags = {
            f"s{segment.pk}": Case(
                When(compile_q(contact.account_id, segment.rules, now, ctx=ctx), then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            )
            for segment in batch
        }
        row = (
            Contact.objects.filter(pk=contact.pk, account_id=contact.account_id)
            .annotate(**flags)
            .values(*flags)
            .first()
        )
        if row is None:
            return []
        found += [segment for segment in batch if row[f"s{segment.pk}"]]
    return found


def opened_locked(account):
    """True när kontot aldrig haft Spåra öppningar på (inget utskick med
    open_tracking och inte UtskickSettings.open_tracking): regeln "opened"
    är låst."""
    if settings_for(account).open_tracking:
        return False
    return not Utskick.objects.filter(account=account, open_tracking=True).exists()


# ---------------------------------------------------------------------------
# Prövningen (formuläret, JSON och reglerna som sparas)
# ---------------------------------------------------------------------------


def _owned(model, account, value):
    """Id:n ur regeln, alla kontots (owned_ids: ForeignIds annars). Ett tomt
    val (väljarens "Välj lista") är inget id."""
    values = [v for v in _as_list(value) if v not in ("", None)]
    if len(values) > MAX_IDS:
        raise _Bad(TOO_MANY_IDS_TEXT)
    return owned_ids(model, account, values)


def _clean_count(value, op):
    months = op.endswith("_months")
    if value is None or (isinstance(value, str) and not value.strip()):
        raise _Missing("months" if months else "days")
    try:
        return _count(value, MAX_MONTHS if months else MAX_DAYS)
    except ValueError:
        raise _Bad(MONTHS_TEXT if months else DAYS_TEXT) from None


def _clean_text(value):
    if value is None:
        raise _Missing("text")
    try:
        text = _text(value)
    except ValueError:
        raise _Bad(VALUE_TEXT) from None
    if not text:
        raise _Missing("text")
    if len(text) > MAX_TEXT:
        raise _Bad(TEXT_LONG_TEXT)
    return text


def _clean_answer(ctx, key, value):
    """Alternativen i en svarsregel: nycklar som finns på frågan nu, utan
    dubbletter, i den ordning de valdes. _clean_rule har redan prövat sidan
    och frågan."""
    values = [v for v in _as_list(value) if v not in ("", None)]
    if not values:
        raise _Missing("answer")
    if len(values) > MAX_IDS:
        raise _Bad(TOO_MANY_IDS_TEXT)
    page_id, question_key = _answer_key(key)
    question = ctx.page_questions(page_id)[question_key]
    out = []
    for raw in values:
        option = raw.strip() if isinstance(raw, str) else ""
        if option not in question.option_keys:
            raise _Bad(ANSWER_CHOICE_TEXT)
        if option not in out:
            out.append(option)
    return out


def _clean_value(account, ctx, name, key, op, value):
    """Värdet i kanonisk form, eller _Missing / _Bad / ForeignIds."""
    if op in NO_VALUE_OPS and name != "consent":
        return None
    if name == "answer":
        return _clean_answer(ctx, key, value)
    if name in _ID_MODELS and op in ("in", "not_in"):
        model, what = _ID_MODELS[name]
        ids = _owned(model, account, value)
        if not ids:
            raise _Missing(what)
        return ids
    if name in ("got_utskick", "opened", "clicked", "visited_lp", "lead", "replied"):
        return _clean_count(value, op)
    if name == "created":
        return _clean_count(value, op)
    if name == "kind":
        if value in (None, ""):
            raise _Missing("kind")
        if value not in Contact.Kind.values:
            raise _Bad(VALUE_TEXT)
        return value
    if name == "source":
        values = _as_list(value)
        if not [v for v in values if v not in ("", None)]:
            raise _Missing("source")
        if any(v not in Contact.Source.values for v in values if v not in ("", None)):
            raise _Bad(VALUE_TEXT)
        return _strings(values)
    if name == "consent":
        if not isinstance(value, dict):
            raise _Missing("channel")
        channel = value.get("channel")
        if channel in (None, ""):
            raise _Missing("channel")
        if channel not in CHANNELS:
            raise _Bad(VALUE_TEXT)
        if op in ("eligible", "not_eligible"):
            return {"channel": channel}
        statuses = [s for s in _as_list(value.get("status")) if s not in ("", None)]
        if not statuses:
            raise _Missing("status")
        if any(s not in Consent.Status.values for s in statuses):
            raise _Bad(VALUE_TEXT)
        return {"channel": channel, "status": _strings(statuses)}
    if name == "contact":
        return _clean_text(value)
    if name == "field":
        kind = ctx.fields[key].kind
        if kind == FieldDef.Kind.TEXT:
            return _clean_text(value)
        if kind == FieldDef.Kind.NUMBER:
            if value is None or (isinstance(value, str) and not value.strip()):
                raise _Missing("number")
            try:
                return _plain(_number(value))
            except ValueError:
                raise _Bad(NUMBER_TEXT) from None
        if kind == FieldDef.Kind.DATE:
            return _clean_count(value, op)
        if kind == FieldDef.Kind.CHOICE:
            values = [v for v in _as_list(value) if v not in ("", None)]
            if not values:
                raise _Missing("choice")
            options = {str(c).casefold(): str(c) for c in ctx.fields[key].choices or ()}
            out = []
            for raw in values:
                if not isinstance(raw, str) or raw.strip().casefold() not in options:
                    raise _Bad(CHOICE_TEXT)
                canonical = options[raw.strip().casefold()]
                if canonical not in out:
                    out.append(canonical)
            if len(out) > MAX_IDS:
                raise _Bad(TOO_MANY_IDS_TEXT)
            return out
    raise _Bad(VALUE_TEXT)


def _clean_answer_key(account, ctx, key):
    """Svarsregelns sida och fråga: sidan är kontots (ForeignIds annars, som
    owned_ids; sidan hämtas med kontot i villkoret, ctx.page_questions) och
    frågan finns på den nu (GONE_QUESTION_TEXT annars)."""
    parsed = _answer_key(key)
    if parsed is None:
        raise _Bad(SHAPE_TEXT)
    questions = ctx.page_questions(parsed[0])
    if questions is None:
        raise ForeignIds
    if parsed[1] not in questions:
        raise _Bad(GONE_QUESTION_TEXT)


def _clean_rule(account, ctx, rule, locked):
    """En regel i kanonisk form, eller _Missing / _Bad / ForeignIds."""
    if not isinstance(rule, dict):
        raise _Bad(SHAPE_TEXT)
    f = rule.get("f")
    if f in (None, ""):
        raise _Missing("field")
    if not isinstance(f, str):
        raise _Bad(SHAPE_TEXT)
    name, _, key = f.partition(":")
    if name not in FIELDS:
        raise _Bad(SHAPE_TEXT)
    if name == "field" and key not in ctx.fields:
        raise _Bad(GONE_FIELD_TEXT)
    if name == "contact" and key not in CONTACT_FIELDS:
        raise _Bad(SHAPE_TEXT)
    if name == "answer":
        _clean_answer_key(account, ctx, key)
    elif name not in ("field", "contact") and key:
        raise _Bad(SHAPE_TEXT)
    if name == "opened" and locked():
        raise _Bad(OPENED_LOCKED_TEXT)
    op = rule.get("op")
    if op in (None, ""):
        raise _Missing("op")
    if name == "field":
        allowed = FIELD_KIND_OPS.get(ctx.fields[key].kind, ())
    elif name == "contact":
        allowed = CONTACT_FIELDS[key][1]
    else:
        allowed = FIELDS[name]
    if op not in allowed:
        raise _Missing("op")
    value = _clean_value(account, ctx, name, key, op, rule.get("v"))
    out = {"f": f, "op": op}
    if value is not None:
        out["v"] = value
    return out


def _clean(account, rules, partial):
    items = _items(rules)
    if items is None:
        raise SegmentError([SHAPE_TEXT])
    groups = 0
    for item in items:
        if isinstance(item, dict) and "any" in item:
            if not isinstance(item.get("any"), list):
                raise SegmentError([SHAPE_TEXT])
            groups += 1
    # Gränserna prövas före varje id, så att ett för stort segment aldrig
    # blir många frågor.
    if sum(1 for _ in _leaves(items)) > MAX_RULES:
        raise SegmentError([TOO_MANY_RULES_TEXT])
    if groups > MAX_GROUPS:
        raise SegmentError([TOO_MANY_GROUPS_TEXT])
    ctx = _Context(account.pk)
    locked_cache = []

    def locked():
        if not locked_cache:
            locked_cache.append(opened_locked(account))
        return locked_cache[0]

    errors, rows, skipped = [], {}, []
    position = 0
    out = []

    def one(rule):
        nonlocal position
        position += 1
        try:
            return _clean_rule(account, ctx, rule, locked)
        except _Missing as missing:
            if partial:
                skipped.append(position)
                return None
            text = MISSING_TEXTS[missing.key]
        except _Bad as bad:
            text = bad.text
        rows[position] = text
        errors.append(f"Villkor {position}: {text[:1].lower()}{text[1:]}")
        return None

    for item in items:
        if isinstance(item, dict) and "any" in item:
            cleaned = [rule for rule in (one(member) for member in item["any"]) if rule]
            if cleaned:
                out.append({"any": cleaned})
        else:
            rule = one(item)
            if rule:
                out.append(rule)
    if errors:
        raise SegmentError(errors, rows)
    return {"all": out}, skipped


def clean(account, rules):
    """Reglerna ur formuläret eller JSON, prövade: samma form, bara kända
    fält och operatorer, värden av rätt slag, id:n genom owned_ids
    (ForeignIds), fältnycklar som finns hos kontot. SegmentError annars
    (en text per regel som inte går). Tomma regler går igenom ({"all": []});
    vyn kräver minst en regel när segmentet sparas."""
    return _clean(account, rules, partial=False)[0]


def clean_partial(account, rules):
    """Som clean, men en regel som saknar sitt värde (kunden har inte valt
    än) hoppas över: (regler, [platser som hoppades över]). Främmande id:n
    ger ForeignIds och felaktiga värden SegmentError, som i clean."""
    return _clean(account, rules, partial=True)


# ---------------------------------------------------------------------------
# Reglerna i klartext
# ---------------------------------------------------------------------------


def _plural(n, one, many):
    return f"{n} {one if n == 1 else many}"


def _span(op, n):
    months = op.endswith("_months")
    return _plural(n, "månad", "månader") if months else _plural(n, "dag", "dagar")


def _last(n, one, many):
    """ "den senaste dagen", "de senaste 30 dagarna"."""
    return f"den senaste {one}" if n == 1 else f"de senaste {n} {many}"


def _window_text(op, n):
    months = op.endswith("_months")
    one, many = ("månaden", "månaderna") if months else ("dagen", "dagarna")
    if op.startswith("within"):
        return "inom " + _last(n, one, many)
    if op.startswith("next"):
        return f"inom den kommande {one}" if n == 1 else f"inom de kommande {n} {many}"
    return "äldre än " + _span(op, n)


def _names_in(model, account, ids):
    return dict(model.objects.filter(account=account, pk__in=ids).values_list("pk", "name"))


def _join(words):
    words = list(words)
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " eller " + words[-1]


class _Names:
    """Namnen reglerna pekar på, hämtade en gång per modell med kontot i
    villkoret."""

    def __init__(self, account, items):
        wanted = {"list": set(), "tag": set(), "utskick": set(), "page": set()}
        for rule in _leaves(items):
            parts = _split(rule)
            if parts is None:
                continue
            name, key, op, value = parts
            if name in _ID_MODELS and op in ("in", "not_in"):
                wanted[_ID_MODELS[name][1]].update(_ids(value))
            if name == "answer" and _answer_key(key) is not None:
                wanted["page"].add(_answer_key(key)[0])
        self.lists = _names_in(ContactList, account, wanted["list"]) if wanted["list"] else {}
        self.tags = _names_in(Tag, account, wanted["tag"]) if wanted["tag"] else {}
        self.utskick = _names_in(Utskick, account, wanted["utskick"]) if wanted["utskick"] else {}
        self.fields = {d.key: d for d in FieldDef.objects.filter(account=account)}
        self.pages, self.answers = {}, {}
        if wanted["page"]:
            from apps.flamingo import answers as form_answers
            from apps.flamingo.models import LandingPage

            # Bara sidorna reglerna pekar på, med kontot i villkoret.
            self.pages = _names_in(LandingPage, account, wanted["page"])
            self.answers = {
                (q.page_id, q.key): q
                for questions in form_answers.questions_by_page(account.pk, wanted["page"]).values()
                for q in questions
            }


def _leaves(items):
    for item in items or ():
        if isinstance(item, dict) and isinstance(item.get("any"), list):
            yield from item["any"]
        else:
            yield item


_CONSENT_WORDS = {CHANNEL_SMS: "sms", CHANNEL_EMAIL: "e-post"}


def _named(ids, names, one, many, gone):
    """ "listan Kunder", "listorna Kunder, Bromma", eller gone när inget
    finns kvar."""
    found = [names[i] for i in ids if i in names]
    if not found:
        return gone
    return f"{one} {found[0]}" if len(found) == 1 else f"{many} {_join(found)}"


def _describe_rule(rule, names):
    parts = _split(rule)
    if parts is None:
        return "Ett villkor som inte går att läsa"
    name, key, op, value = parts
    try:
        return _describe(name, key, op, value, names)
    except (TypeError, ValueError, KeyError, ArithmeticError):
        return "Ett villkor som inte går att läsa"


_COMPARE_WORDS = {
    "eq": "är",
    "not_eq": "är inte",
    "contains": "innehåller",
    "lt": "är mindre än",
    "gt": "är större än",
}


def _describe_field(name, key, op, value, names):
    """Ett extrafält eller ett av kontaktens fasta fält i klartext."""
    number = False
    if name == "field":
        definition = names.fields.get(key)
        if definition is None:
            return "Ett fält som är borttaget"
        label = definition.label
        number = definition.kind == FieldDef.Kind.NUMBER
    else:
        label = CONTACT_FIELDS[key][0]
    if op == "empty":
        return f"{label} saknas"
    if op == "not_empty":
        return f"{label} finns"
    if op in ("in", "not_in"):
        words = _join(_strings(value))
        return f"{label} är {words}" if op == "in" else f"{label} är inte {words}"
    if op in DATE_OPS:
        top = MAX_MONTHS if op.endswith("_months") else MAX_DAYS
        return f"{label} {_window_text(op, _count(value, top))}"
    shown = str(_plain(_number(value))).replace(".", ",") if number else _text(value)
    return f"{label} {_COMPARE_WORDS[op]} {shown}"


def _describe_answer(key, op, value, names):
    """Som byggarens "valde" och "valde inte":

        Valde Reparation eller Felsökning i "Vilken tjänst önskar du?" (Bilservice)
        Valde inte Reparation i "..." (Bilservice), eller svarade inte
        Valde inget av Reparation och Felsökning i "..." (Bilservice), eller svarade inte

    "valde inte" tar också med kontakterna som aldrig svarade, och det står
    i texten. En fråga eller ett alternativ som inte finns på sidan längre
    står som borttaget."""
    parsed = _answer_key(key)
    question = names.answers.get(parsed) if parsed is not None else None
    page = names.pages.get(parsed[0], "") if parsed is not None else ""
    words = []
    for option in _strings(value):
        word = (question.option_label(option) if question is not None else "") or (
            "ett borttaget svar"
        )
        if word not in words:
            words.append(word)
    words = words or ["ett borttaget svar"]
    if op == "in":
        what = f"Valde {_join(words)}"
    elif len(words) == 1:
        what = f"Valde inte {words[0]}"
    else:
        what = f"Valde inget av {', '.join(words[:-1])} och {words[-1]}"
    if question is None:
        where = f" i en fråga som inte finns längre{f' ({page})' if page else ''}"
    else:
        where = f' i "{question.label}" ({question.page_name})'
    return f"{what}{where}" if op == "in" else f"{what}{where}, eller svarade inte"


def _describe(name, key, op, value, names):
    positive = op in ("in", "within_days", "eq", "eligible")
    if name == "answer":
        return _describe_answer(key, op, value, names)
    if name == "list":
        what = _named(_ids(value), names.lists, "listan", "någon av listorna", "en borttagen lista")
        return f"Finns i {what}" if positive else f"Finns inte i {what}"
    if name == "tag":
        what = _named(_ids(value), names.tags, "taggen", "någon av taggarna", "en borttagen tagg")
        return f"Har {what}" if positive else f"Har inte {what}"
    if name in ("field", "contact"):
        return _describe_field(name, key, op, value, names)
    if name == "consent":
        word = _CONSENT_WORDS[value["channel"]]
        if op == "eligible":
            return f"Kan få erbjudanden via {word}"
        if op == "not_eligible":
            return f"Kan inte få erbjudanden via {word}"
        labels = dict(Consent.Status.choices)
        statuses = _join(labels[s] for s in _strings(value.get("status")))
        verb = "är" if op == "in" else "är inte"
        return f"Samtycke för {word} {verb} {statuses}"
    if name == "kind":
        return f"Typ: {Contact.Kind(value).label}"
    if name == "source":
        labels = dict(Contact.Source.choices)
        words = _join(labels[v] for v in _strings(value))
        return f"Källa: {words}" if op == "in" else f"Källa är inte {words}"
    if name == "created":
        top = MAX_MONTHS if op.endswith("_months") else MAX_DAYS
        n = _count(value, top)
        if op.startswith("before"):
            return f"Tillagd för mer än {_span(op, n)} sedan"
        months = op.endswith("_months")
        one, many = ("månaden", "månaderna") if months else ("dagen", "dagarna")
        return "Tillagd " + _last(n, one, many)
    if op in ("in", "not_in"):
        ids = _ids(value)
        utskick = _named(
            ids, names.utskick, "utskicket", "något av utskicken", "ett borttaget utskick"
        )
        if name == "got_utskick":
            return f"Fick {utskick}" if positive else f"Fick inte {utskick}"
        if name == "opened":
            return f"Öppnade {utskick}" if positive else f"Öppnade inte {utskick}"
        return f"Klickade i {utskick}" if positive else f"Klickade inte i {utskick}"
    n = _count(value, MAX_DAYS)
    last = _last(n, "dagen", "dagarna")
    texts = {
        "got_utskick": ("Fick ett utskick", "Fick inget utskick"),
        "opened": ("Öppnade ett mejl", "Öppnade inget mejl"),
        "clicked": ("Klickade på en länk", "Klickade inte på någon länk"),
        "visited_lp": ("Besökte en sida via en länk", "Besökte ingen sida via en länk"),
        "lead": ("Skickade en förfrågan", "Ingen förfrågan"),
        "replied": ("Svarade", "Svarade inte"),
    }
    yes, no = texts[name]
    return f"{yes if positive else no} {last}"


def describe(account, rules):
    """Reglerna i klartext, en rad per regel eller grupp ("Finns i listan
    Kunder", "Ingen förfrågan de senaste 30 dagarna", "Minst ett av: Har
    taggen VIP; Finns i listan Bromma"). Namnen hämtas med kontot i
    villkoret; det som tagits bort står som borttaget."""
    items = _items(rules)
    if items is None:
        return ["Villkoren går inte att läsa."]
    names = _Names(account, items)
    lines = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("any"), list):
            texts = [_describe_rule(rule, names) for rule in item["any"]]
            if len(texts) == 1:
                lines.append(texts[0])
            elif texts:
                lines.append("Minst ett av: " + "; ".join(texts))
        else:
            lines.append(_describe_rule(item, names))
    return lines


# ---------------------------------------------------------------------------
# Uppföljningen från rapporten (I.8, rapport-byggaren anropar)
# ---------------------------------------------------------------------------


def follow_up_rules(utskick):
    """Reglerna för "Följ upp de som inte klickade": fick utskicket och
    klickade inte på det."""
    return {
        "all": [
            {"f": "got_utskick", "op": "in", "v": [utskick.pk]},
            {"f": "clicked", "op": "not_in", "v": [utskick.pk]},
        ]
    }


FOLLOW_UP_PREFIX = "Klickade inte: "


def create_follow_up(utskick, *, user, now=None):
    """Ett nytt segment med follow_up_rules, namnet "Klickade inte:
    <utskickets namn>" (med " 2", " 3" när namnet finns), räknat. Nekas med
    SegmentError när kontot har MAX_PER_ACCOUNT segment."""
    account = utskick.account
    if Segment.objects.filter(account=account).count() >= Segment.MAX_PER_ACCOUNT:
        raise SegmentError([FULL_TEXT])
    base = f"{FOLLOW_UP_PREFIX}{' '.join(str(utskick.name or '').split())}"[:80].rstrip()
    taken = set(
        Segment.objects.filter(account=account, name__startswith=base[:60]).values_list(
            "name", flat=True
        )
    )
    created_by = user if getattr(user, "is_authenticated", False) else None
    n = 1
    while True:
        suffix = "" if n == 1 else f" {n}"
        name = base[: 80 - len(suffix)].rstrip() + suffix
        n += 1
        if name in taken:
            continue
        try:
            with transaction.atomic():
                segment = Segment.objects.create(
                    account=account,
                    name=name,
                    rules=follow_up_rules(utskick),
                    created_by=created_by,
                )
        except IntegrityError:
            taken.add(name)
            continue
        return refresh(segment, now)
