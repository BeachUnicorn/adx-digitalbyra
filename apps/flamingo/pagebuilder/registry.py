"""
Blocktyperna i sidbyggaren: namn, ikon, varianter, fält, varför och
mallinnehåll.

    TYPES                 {typ: BlockType}, i bibliotekets ordning
    get_type(typ)         BlockType eller None
    GROUPS                bibliotekets rubriker i ordning
    available(account, ctx=None)
                          {typ: (går att lägga till?, varför inte)}
    template_fields(typ, variant, account, ctx=None)
                          mallens fält ur kontots bekräftade uppgifter, eller
                          None när blocket inte ska finnas (en uppgift saknas)
    BuildContext          tjänsten, orterna, sättet att sälja och tjänstens
                          pris (ett pris används bara för sin egen tjänst)

Ett fält har en sort (Field.kind):

    text       en rad, högst max_length tecken
    textarea   flera rader (radbrytningar behålls), högst max_length tecken
    phone      ett telefonnummer, högst max_length tecken; måste finnas bland
               de bekräftade uppgifterna (problems.page_problems)
    lines      en lista med rader: högst max_items, varje rad högst max_length
    media      ett MediaAsset-id (int) eller None
    items      en lista med poster: högst max_items, varje post ett dict med
               underfälten i Field.items (sorterna text, textarea, choice, key)
    choice     ett av värdena i Field.choices (bara som underfält)
    key        en nyckel med a-z, 0-9 och bindestreck (bara som underfält;
               formulärets frågor). Saknas den skapas den ur etiketten.

Fält med Field.variants används bara i de varianterna, men värdet sparas
kvar när kunden byter variant ("texten följer med"). Variant.limits säger
hur många poster varianten visar (Vanliga frågor: tre eller sex).

Allt är vanlig text: aldrig HTML. Mallarna escapar allt.
"""

import re
from dataclasses import dataclass, field

from .. import checks
from ..models import PAGE_QUESTION_KINDS, Service
from .facts import facts_for, price_label

TEXT = "text"
TEXTAREA = "textarea"
PHONE = "phone"
LINES = "lines"
MEDIA = "media"
ITEMS = "items"
CHOICE = "choice"
KEY = "key"
FIELD_KINDS = (TEXT, TEXTAREA, PHONE, LINES, MEDIA, ITEMS, CHOICE, KEY)

#: Vad ett block kräver för att kunna läggas till (BlockType.requires).
REQUIRES_PRICE = "price_fact"
REQUIRES_CERTIFICATE = "certificate_fact"
REQUIRES_GUARANTEE = "guarantee_fact"
REQUIRES_PHONE = "phone_fact"
REQUIRES_GOOGLE = "google_profile"
REQUIRES_TEXT = {
    REQUIRES_PRICE: "Kräver ett bekräftat pris under Företaget.",
    REQUIRES_CERTIFICATE: "Kräver en bekräftad auktorisation, försäkring eller ett medlemskap.",
    REQUIRES_GUARANTEE: "Kräver en bekräftad garanti under Företaget.",
    REQUIRES_PHONE: "Kräver ett bekräftat telefonnummer under Företaget.",
    REQUIRES_GOOGLE: "Kräver att din Google-profil är kopplad.",
}

QUESTION_KIND_LABELS = {"text": "Kort svar", "textarea": "Längre text", "date": "Datum"}

#: Bibliotekets rubriker (skärm 03 i mockupen), i ordning.
GROUPS = (
    ("top", "Överst"),
    ("trust", "Förtroende"),
    ("content", "Innehåll"),
    ("end", "Avslut"),
)


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str
    max_length: int = 0
    max_items: int = 0
    #: Underfälten för ITEMS.
    items: tuple = ()
    #: (värde, etikett) för CHOICE.
    choices: tuple = ()
    #: Varianterna som visar fältet; tomt = alla.
    variants: tuple = ()
    required: bool = False
    placeholder: str = ""
    #: En post i listan, i singular ("Punkt 1", "Ort 2"); tomt = label.
    item_label: str = ""

    def used_by(self, variant):
        return not self.variants or variant in self.variants

    def empty(self):
        if self.kind in (LINES, ITEMS):
            return []
        if self.kind == MEDIA:
            return None
        return ""

    def sub(self, key):
        return next((f for f in self.items if f.key == key), None)

    def as_dict(self):
        """Schemat som JSON (för redigeraren)."""
        data = {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "max_length": self.max_length,
            "max_items": self.max_items,
            "variants": list(self.variants),
            "required": self.required,
            "placeholder": self.placeholder,
        }
        if self.kind in (LINES, ITEMS):
            data["item_label"] = self.item_label or self.label
        if self.items:
            data["items"] = [f.as_dict() for f in self.items]
        if self.choices:
            data["choices"] = [list(c) for c in self.choices]
        return data


@dataclass(frozen=True)
class Variant:
    key: str
    name: str
    #: {fältnyckel: hur många poster varianten visar}.
    limits: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BlockType:
    key: str
    name: str
    #: Ikonens nyckel (templates/flamingo/pagebuilder/icons.html, #pb-i-<icon>).
    icon: str
    group: str
    variants: tuple
    fields: tuple
    #: En rad om varför blocket hjälper, för kunden och AI.
    why: str
    #: Principen bakom, som en kort etikett.
    principle: str
    #: Se REQUIRES_*; tomt = går alltid att lägga till.
    requires: str = ""
    #: Högst ett av blocket per sida.
    single: bool = False

    @property
    def variant_keys(self):
        return tuple(v.key for v in self.variants)

    @property
    def default_variant(self):
        return self.variants[0].key

    def variant(self, key):
        return next((v for v in self.variants if v.key == key), None)

    def field(self, key):
        return next((f for f in self.fields if f.key == key), None)

    def fields_for(self, variant):
        return tuple(f for f in self.fields if f.used_by(variant))

    def as_dict(self):
        return {
            "key": self.key,
            "name": self.name,
            "icon": self.icon,
            "group": self.group,
            "why": self.why,
            "principle": self.principle,
            "requires": self.requires,
            "single": self.single,
            "variants": [{"key": v.key, "name": v.name, "limits": v.limits} for v in self.variants],
            "fields": [f.as_dict() for f in self.fields],
        }


# ---------------------------------------------------------------------------
# Gränserna
# ---------------------------------------------------------------------------

#: Rubriker, ingresser och texten vid formuläret rymmer det sidorna före
#: sidbyggaren tillät (migreringen 0011 flyttar dem oförändrade).
TITLE_MAX = 120
KICKER_MAX = 80
LEAD_MAX = 600
LINE_MAX = 120
PHONE_MAX = 40
SHORT_MAX = 60
TEXT_MAX = 600
NOTE_MAX = 600
SUBMIT_MAX = 30
QUESTION_MAX = 120
ANSWER_MAX = 400

_TITLE = Field("title", "Rubrik", TEXT, max_length=TITLE_MAX)

TYPES_LIST = (
    BlockType(
        key="hero",
        name="Toppen",
        icon="hero",
        group="top",
        variants=(
            Variant("call", "Med ringknapp"),
            Variant("form", "Med formulär"),
            Variant("image", "Med bild"),
            Variant("text", "Bara text"),
        ),
        fields=(
            Field(
                "kicker",
                "Överrubrik",
                TEXT,
                max_length=KICKER_MAX,
                placeholder="Tjänsten och orten, till exempel Rörjour i Nacka",
            ),
            Field("title", "Rubrik", TEXT, max_length=TITLE_MAX, required=True),
            Field("lead", "Ingress", TEXTAREA, max_length=LEAD_MAX),
            Field("points", "Punkter", LINES, max_length=LINE_MAX, max_items=6, item_label="Punkt"),
            Field("phone", "Telefon", PHONE, max_length=PHONE_MAX),
            Field("image", "Bild", MEDIA, variants=("image",)),
        ),
        why=(
            "Tjänsten och orten överst, en rubrik om vad kunden får, och hur hen når er. "
            "Besökaren bestämmer sig på några sekunder om sidan är rätt."
        ),
        principle="Klarhet först",
        single=True,
    ),
    BlockType(
        key="price",
        name="Pris",
        icon="price",
        group="top",
        variants=(
            Variant("from", "Från-pris"),
            Variant("examples", "Tre prisexempel"),
            Variant("fixed", "Fast pris"),
        ),
        fields=(
            _TITLE,
            Field("price", "Pris", TEXT, max_length=SHORT_MAX, variants=("from", "fixed")),
            Field("text", "Text", TEXTAREA, max_length=LEAD_MAX),
            Field(
                "items",
                "Prisexempel",
                ITEMS,
                max_items=3,
                variants=("examples",),
                item_label="Prisexempel",
                items=(
                    Field("label", "Vad", TEXT, max_length=SHORT_MAX),
                    Field("price", "Pris", TEXT, max_length=SHORT_MAX),
                ),
            ),
            Field("note", "Liten text", TEXT, max_length=160),
        ),
        why=(
            "Ett från-pris tidigt sållar bort dem som inte vill betala. Samma pris "
            "i annonsen sparar pengar på klick som aldrig blir affär."
        ),
        principle="Priset tidigt",
        requires=REQUIRES_PRICE,
    ),
    BlockType(
        key="reviews_google",
        name="Omdömen från Google",
        icon="reviews",
        group="trust",
        variants=(
            Variant("cards", "Tre kort", limits={"reviews": 3}),
            Variant("quote", "Ett stort citat", limits={"reviews": 1}),
            Variant("line", "Betyg i en rad", limits={"reviews": 0}),
        ),
        fields=(_TITLE,),
        why=(
            "Andras erfarenheter väger tyngre än det företaget säger om sig självt. "
            "Hämtas från din Google-profil; texten ändras aldrig."
        ),
        principle="Andras omdömen",
        requires=REQUIRES_GOOGLE,
        single=True,
    ),
    BlockType(
        key="certificates",
        name="Certifikat",
        icon="certificate",
        group="trust",
        variants=(Variant("badges", "Märken"), Variant("icons", "Text med ikoner")),
        fields=(
            _TITLE,
            Field(
                "items",
                "Certifikat",
                ITEMS,
                max_items=6,
                item_label="Certifikat",
                items=(
                    Field("name", "Namn", TEXT, max_length=SHORT_MAX),
                    Field("text", "Text", TEXT, max_length=160),
                ),
            ),
        ),
        why="Auktorisationer, försäkring och medlemskap. Bara det du har bekräftat.",
        principle="Bevis på behörighet",
        requires=REQUIRES_CERTIFICATE,
    ),
    BlockType(
        key="guarantee",
        name="Garanti",
        icon="guarantee",
        group="trust",
        variants=(Variant("short", "Kort"), Variant("terms", "Med villkor")),
        fields=(
            _TITLE,
            Field("text", "Text", TEXTAREA, max_length=LEAD_MAX),
            Field(
                "terms",
                "Villkor",
                LINES,
                max_length=160,
                max_items=5,
                variants=("terms",),
                item_label="Villkor",
            ),
        ),
        why="Tar bort risken för köparen. Visas bara om du har en bekräftad garanti.",
        principle="Minskad risk",
        requires=REQUIRES_GUARANTEE,
    ),
    BlockType(
        key="person",
        name="Personen bakom",
        icon="person",
        group="trust",
        variants=(Variant("image", "Med bild"), Variant("noimage", "Utan bild")),
        fields=(
            Field("name", "Namn", TEXT, max_length=SHORT_MAX, required=True),
            Field("role", "Roll", TEXT, max_length=SHORT_MAX),
            Field("text", "Text", TEXTAREA, max_length=TEXT_MAX),
            Field("image", "Bild", MEDIA, variants=("image",)),
        ),
        why="Ett ansikte och ett namn. Vi köper hellre av någon vi känner igen och tycker om.",
        principle="Någon att känna igen",
    ),
    BlockType(
        key="steps",
        name="Så går det till",
        icon="steps",
        group="content",
        variants=(
            Variant("three", "Tre steg", limits={"steps": 3}),
            Variant("four", "Fyra steg", limits={"steps": 4}),
        ),
        fields=(
            _TITLE,
            Field(
                "steps",
                "Steg",
                ITEMS,
                max_items=4,
                item_label="Steg",
                items=(
                    Field("title", "Steg", TEXT, max_length=SHORT_MAX),
                    Field("text", "Text", TEXT, max_length=160),
                ),
            ),
        ),
        why="Visar vad som händer efter klicket. Mindre osäkerhet, färre som tvekar.",
        principle="Minskad osäkerhet",
    ),
    BlockType(
        key="before_after",
        name="Före och efter",
        icon="beforeafter",
        group="content",
        variants=(Variant("slider", "Reglage"), Variant("pair", "Två bilder")),
        fields=(
            _TITLE,
            Field("before", "Före", MEDIA, required=True),
            Field("after", "Efter", MEDIA, required=True),
            Field("caption", "Bildtext", TEXT, max_length=160),
        ),
        why="Bevis i bild från riktiga jobb, ur mediaarkivet.",
        principle="Bevis",
    ),
    BlockType(
        key="area",
        name="Område",
        icon="area",
        group="content",
        variants=(Variant("list", "Orter i en lista"), Variant("map", "Karta")),
        fields=(
            _TITLE,
            Field("places", "Orter", LINES, max_length=SHORT_MAX, max_items=12, item_label="Ort"),
            Field("text", "Text", TEXTAREA, max_length=LEAD_MAX),
        ),
        why=(
            "Rätt ort bekräftar att företaget kommer till besökaren. Orterna kommer från kampanjen."
        ),
        principle="Relevans",
    ),
    BlockType(
        key="faq",
        name="Vanliga frågor",
        icon="faq",
        group="content",
        variants=(
            Variant("three", "Tre frågor", limits={"items": 3}),
            Variant("six", "Sex frågor", limits={"items": 6}),
        ),
        fields=(
            _TITLE,
            Field(
                "items",
                "Frågor",
                ITEMS,
                max_items=6,
                item_label="Fråga",
                items=(
                    Field("q", "Fråga", TEXT, max_length=QUESTION_MAX),
                    Field("a", "Svar", TEXTAREA, max_length=ANSWER_MAX),
                ),
            ),
        ),
        why="Svarar på invändningarna innan de blir ett skäl att lämna sidan.",
        principle="Bemöter invändningar",
    ),
    BlockType(
        key="form",
        name="Formulär",
        icon="form",
        group="end",
        variants=(
            Variant("short", "Kort"),
            Variant("questions", "Med frågor"),
            Variant("booking", "Boka tid"),
        ),
        fields=(
            _TITLE,
            Field(
                "questions",
                "Frågor",
                ITEMS,
                max_items=8,
                item_label="Fråga",
                items=(
                    Field("key", "Nyckel", KEY, max_length=40),
                    Field("label", "Fråga", TEXT, max_length=QUESTION_MAX),
                    Field(
                        "kind",
                        "Sorts svar",
                        CHOICE,
                        choices=tuple((k, QUESTION_KIND_LABELS[k]) for k in PAGE_QUESTION_KINDS),
                    ),
                ),
            ),
            Field("note_title", "Rubrik på rutan", TEXT, max_length=SHORT_MAX),
            Field("note", "Text vid formuläret", TEXTAREA, max_length=NOTE_MAX),
            Field("submit", "Knappen", TEXT, max_length=SUBMIT_MAX),
        ),
        why=(
            "Varje extra fält tappar några. Namn och telefon räcker ofta; frågor "
            "bara när de behövs."
        ),
        principle="Färre fält",
        single=True,
    ),
    BlockType(
        key="callbar",
        name="Ringremsa",
        icon="callbar",
        group="end",
        variants=(Variant("call", "Ring"), Variant("call_write", "Ring och skriv")),
        fields=(
            Field("title", "Text", TEXT, max_length=SHORT_MAX),
            Field("phone", "Telefon", PHONE, max_length=PHONE_MAX, required=True),
        ),
        why=(
            "Fast längst ner i mobilen. En tydlig handling, alltid inom räckhåll. "
            "Klicket räknas som förfrågan."
        ),
        principle="En handling",
        requires=REQUIRES_PHONE,
        single=True,
    ),
)

TYPES = {t.key: t for t in TYPES_LIST}


def get_type(key):
    return TYPES.get(key)


def schema():
    """Hela registret som JSON-bara dicts (för redigeraren och AI)."""
    return [t.as_dict() for t in TYPES_LIST]


# ---------------------------------------------------------------------------
# Vad som går att lägga till
# ---------------------------------------------------------------------------


def _meets(requires, facts, account, ctx=None):
    if not requires:
        return True
    if requires == REQUIRES_PRICE:
        return bool(ctx_price(facts, ctx or BuildContext())[1])
    if requires == REQUIRES_CERTIFICATE:
        return bool(facts.certificates)
    if requires == REQUIRES_GUARANTEE:
        return bool(facts.guarantees)
    if requires == REQUIRES_PHONE:
        return bool(facts.phone)
    if requires == REQUIRES_GOOGLE:
        return bool(account.google_place_id or account.selected_google_reviews())
    return False


def available(account, facts=None, ctx=None):
    """{typ: (går att lägga till, varför inte)} för biblioteket. Pris,
    certifikat och garanti erbjuds bara med en bekräftad uppgift; priset
    bara när sidans tjänst (ctx) har ett, eller ett pris som inte hör till
    någon tjänst finns."""
    facts = facts or facts_for(account)
    ctx = ctx or BuildContext()
    out = {}
    for block_type in TYPES_LIST:
        ok = _meets(block_type.requires, facts, account, ctx)
        out[block_type.key] = (ok, "" if ok else REQUIRES_TEXT.get(block_type.requires, ""))
    return out


# ---------------------------------------------------------------------------
# Mallinnehållet
# ---------------------------------------------------------------------------


@dataclass
class BuildContext:
    """Det mallarna får veta utöver uppgifterna: kampanjens tjänst, orter
    och sätt att sälja (allt angivet av kunden), och priset som sidan om
    tjänsten får visa (generator.page_price: tjänstens eget bekräftade pris,
    annars ett pris som inte hör till någon tjänst). price None betyder att
    det räknas fram ur uppgifterna och tjänstens namn (ctx_price)."""

    service: str = ""
    places: list = field(default_factory=list)
    mode: str = ""
    price: str | None = None
    price_label: str = ""

    @property
    def place(self):
        return self.places[0] if self.places else ""


def ctx_price(facts, ctx):
    """(etikett, pris) för sidans tjänst, aldrig en annan tjänsts pris
    (Facts.price_for). Räknas fram en gång per BuildContext."""
    if ctx.price is None:
        ctx.price_label, ctx.price = facts.price_for(ctx.service)
    return ctx.price_label, ctx.price


def _upper_first(text):
    return text[:1].upper() + text[1:]


def _fit_lines(lines, context, limit, count):
    """Raderna som ryms och klarar textkontrollen (som generator._fit)."""
    out = []
    for line in lines:
        line = " ".join(str(line or "").split())
        if not line or len(line) > limit or line in out:
            continue
        if checks.text_problems(line, context):
            continue
        out.append(line)
        if len(out) >= count:
            break
    return out


def _join_places(places):
    if not places:
        return ""
    if len(places) == 1:
        return places[0]
    return ", ".join(places[:-1]) + " och " + places[-1]


FORM_TITLES = {
    "short": "Hellre att vi ringer dig?",
    "questions": "Berätta om jobbet",
    "booking": "Boka en tid",
}
FORM_SUBMITS = {
    "short": "Ring upp mig",
    "questions": "Skicka förfrågan",
    "booking": "Skicka önskad tid",
}
FORM_QUESTIONS = {
    "short": [],
    "questions": [
        {"key": "jobbet", "label": "Beskriv jobbet", "kind": "textarea"},
        {"key": "storlek", "label": "Ungefär hur stort är jobbet?", "kind": "text"},
    ],
    "booking": [
        {"key": "dag", "label": "Önskad dag", "kind": "date"},
        {"key": "tid", "label": "Önskad tid på dagen", "kind": "text"},
    ],
}
FORM_NOTES = {"booking": "Tiden är ett önskemål tills vi har bekräftat den."}
#: Formulärets variant efter sättet att sälja.
FORM_VARIANT_BY_MODE = {
    Service.SALES_CALL: "short",
    Service.SALES_QUOTE: "questions",
    Service.SALES_BOOK: "booking",
}

#: Rubriken i Toppen säger vad kunden får, efter sättet att sälja. Tjänsten
#: och orten står i överrubriken ovanför (samma ord som sökningen och
#: annonsen, koll.py). Inga uppgifter, inga löften om tider.
HERO_TITLES = {
    Service.SALES_CALL: "Ring oss, så tar vi hand om resten.",
    Service.SALES_QUOTE: "Beskriv jobbet, så får du en offert.",
    Service.SALES_BOOK: "Boka en tid som passar dig.",
}
#: Ingressen under rubriken ({c} är företagets namn).
HERO_LEADS = {
    Service.SALES_CALL: "Berätta vad som hänt, så bestämmer vi en tid som passar dig.",
    Service.SALES_QUOTE: "Några rader om jobbet räcker. {c} hör av sig med det som behövs "
    "för en offert.",
    Service.SALES_BOOK: "Föreslå dag och tid. Tiden gäller när vi har bekräftat den.",
}

STEPS = {
    Service.SALES_CALL: [
        {"title": "Ring oss", "text": "Berätta vad som hänt och var du finns."},
        {"title": "Vi bestämmer en tid", "text": "Vi kommer överens om när det passar dig."},
        {"title": "Jobbet görs", "text": "Du får veta vad som behöver göras."},
    ],
    Service.SALES_QUOTE: [
        {"title": "Beskriv jobbet", "text": "Fyll i formuläret med det du vet."},
        {"title": "Vi hör av oss", "text": "Vi ställer de frågor som behövs för en offert."},
        {"title": "Du får en offert", "text": "Du bestämmer om du vill gå vidare."},
    ],
    Service.SALES_BOOK: [
        {"title": "Välj dag och tid", "text": "Föreslå en tid som passar dig."},
        {"title": "Vi bekräftar", "text": "Tiden gäller när vi har bekräftat den."},
        {"title": "Vi kommer", "text": "Jobbet görs på den tid ni bestämt."},
    ],
}
FOURTH_STEP = {"title": "Klart", "text": "Säg till om något inte blev som du tänkt dig."}


def lower_first(text):
    """'Rörjour' blir 'rörjour', men 'ROT-avdrag' och 'F-skatt' står kvar."""
    text = text or ""
    if len(text) > 1 and (text[1].isupper() or text[1] in "-0123456789"):
        return text
    return text[:1].lower() + text[1:]


def hero_kicker(service, places):
    """Överrubriken: tjänsten och den första orten ("Rörjour i Nacka"),
    samma ord som annonsen. Tomt utan tjänst."""
    service = _upper_first(" ".join(str(service or "").split()))
    if not service:
        return ""
    place = places[0] if places else ""
    return (f"{service} i {place}" if place else service)[:KICKER_MAX]


def hero_title(mode, fallback=""):
    """Rubriken i Toppen: vad kunden får (HERO_TITLES), annars fallback."""
    return HERO_TITLES.get(mode) or fallback


def certified_phrase(certificate, service, place=""):
    """En behörighet som ett ord framför tjänsten: "Ansvarsförsäkring" blir
    "Ansvarsförsäkrad badrumsrenovering i Nacka", "Säker Vatten-auktoriserade"
    blir "Säker Vatten-auktoriserad rörjour i Nacka". "" när uppgiften inte
    går att göra till ett sådant ord (F-skatt, ett medlemskap)."""
    cert = " ".join(str(certificate or "").split()).rstrip(".")
    service = " ".join(str(service or "").split())
    if not cert or not service:
        return ""
    lower = cert.casefold()
    if lower.endswith("försäkring"):
        word = cert[: -len("ing")] + "ad"
    elif lower.endswith(("ade", "ader")):
        word = cert[: len(cert) - (1 if lower.endswith("ade") else 2)]
    elif lower.endswith(("ad", "at")):
        word = cert
    else:
        return ""
    where = f" i {place}" if place else ""
    return _upper_first(f"{word} {lower_first(service)}{where}")


def price_title(service):
    """Prisblockets lilla rubrik: frågan som priset svarar på. Priset självt
    är den stora rubriken, så rubriken säger aldrig bara "Pris"."""
    service = " ".join(str(service or "").split())
    return f"Vad kostar {lower_first(service)}?" if service else "Vad det kostar"


def _hero(facts, variant, ctx, context):
    service = _upper_first(ctx.service or "")
    company = facts.company
    kicker = hero_kicker(ctx.service, ctx.places)
    fallback = f"{service} i {ctx.place}" if service and ctx.place else (service or company)
    title = hero_title(ctx.mode, fallback) if kicker else fallback
    lead = HERO_LEADS.get(ctx.mode, "").format(c=company)
    return {
        "kicker": kicker if title != kicker else "",
        "title": title[:TITLE_MAX],
        "lead": lead,
        "points": _fit_lines(facts.claims, context, LINE_MAX, 4),
        # Ett nummer ur uppgiften (facts_for, generator.one_phone), aldrig
        # en avhuggen text.
        "phone": facts.phone if len(facts.phone) <= PHONE_MAX else "",
        "image": None,
    }


def _price(facts, variant, ctx, context):
    """Sidans tjänsts pris (ctx_price), och i prisexemplen bara det och
    priser som inte hör till någon tjänst: aldrig en annan tjänsts pris.
    Priset är blockets stora rubrik; titeln är frågan det svarar på."""
    _label, first = ctx_price(facts, ctx)
    if not first:
        return None
    items = [
        {"label": price_label(label)[:SHORT_MAX], "price": value[:SHORT_MAX]}
        for label, value in facts.prices_for(ctx.service)[:3]
    ]
    return {
        "title": price_title(ctx.service)[:TITLE_MAX],
        "price": first[:SHORT_MAX],
        "text": "",
        "items": items,
        "note": "",
    }


def _reviews(facts, variant, ctx, context):
    return {"title": "Vad kunderna säger"}


#: Certifikatblockets rubriker, bästa först. Rubriken får inte säga mer än
#: uppgifterna ("försäkringar" bara med en bekräftad försäkring).
CERTIFICATE_TITLES = (
    "Behörigheter och försäkringar",
    "Behörigheter och medlemskap",
    "Behörigheter",
    "Det här har vi",
)


def _certificates(facts, variant, ctx, context):
    if not facts.certificates:
        return None
    items = [{"name": value[:SHORT_MAX], "text": ""} for _label, value in facts.certificates[:6]]
    title = next((t for t in CERTIFICATE_TITLES if facts.claim_ok(t)), CERTIFICATE_TITLES[-1])
    return {"title": title, "items": items}


def _guarantee(facts, variant, ctx, context):
    """Garantin som den står i uppgiften är rubriken ("Två års garanti på
    arbetet"), aldrig bara "Garanti"."""
    if not facts.guarantees:
        return None
    _label, value = facts.guarantees[0]
    return {"title": value.rstrip(".")[:TITLE_MAX], "text": "", "terms": []}


def _person(facts, variant, ctx, context):
    return {"name": facts.person[:SHORT_MAX], "role": "", "text": "", "image": None}


def _steps(facts, variant, ctx, context):
    steps = [dict(s) for s in STEPS.get(ctx.mode) or STEPS[Service.SALES_QUOTE]]
    if variant == "four":
        steps.append(dict(FOURTH_STEP))
    return {"title": "Så går det till", "steps": steps}


def _before_after(facts, variant, ctx, context):
    return {"title": "Före och efter", "before": None, "after": None, "caption": ""}


def _area(facts, variant, ctx, context):
    places = list(ctx.places or facts.places)
    if not places:
        return None
    return {
        "title": f"Vi jobbar i {_join_places(places[:3])}"[:TITLE_MAX]
        if len(places) <= 3
        else "Här jobbar vi",
        "places": places[:12],
        "text": "",
    }


#: Ord i en uppgift om ROT- eller RUT-avdrag (frågan "Kan jag få ROT-avdrag?").
_DEDUCTION = re.compile(r"\b(rot|rut)(?:-avdrag)?\b", re.I)


def _faq(facts, variant, ctx, context):
    """Frågor som kunder ställer, med svar ur de bekräftade uppgifterna. Inga
    frågor om hur man når företaget: numret och formuläret står redan på
    sidan. Hellre färre frågor än påhittade svar."""
    places = list(ctx.places or facts.places)
    items = []
    if places:
        items.append(
            {"q": "Vilka orter kommer ni till?", "a": f"Vi jobbar i {_join_places(places)}."}
        )
    if ctx.mode == Service.SALES_QUOTE:
        items.append(
            {
                "q": "Hur får jag en offert?",
                "a": "Beskriv jobbet i formuläret så hör vi av oss med frågorna som behövs.",
            }
        )
    elif ctx.mode == Service.SALES_BOOK:
        items.append(
            {
                "q": "Hur bokar jag?",
                "a": "Föreslå en dag och tid i formuläret. Tiden gäller när vi har bekräftat den.",
            }
        )
    _label, price = ctx_price(facts, ctx)
    if price:
        items.append({"q": "Vad kostar det?", "a": f"{price}."})
    if facts.guarantees:
        items.append({"q": "Lämnar ni garanti?", "a": f"Ja. {facts.guarantees[0][1]}."})
    names = [value for _label, value in facts.certificates if not _DEDUCTION.search(value)]
    if names:
        items.append(
            {"q": "Vilka behörigheter och försäkringar har ni?", "a": f"{_join_places(names)}."}
        )
    deduction = next((value for value in facts.values if _DEDUCTION.search(value)), "")
    if deduction:
        kind = _DEDUCTION.search(deduction).group(1).upper()
        items.append({"q": f"Kan jag få {kind}-avdrag?", "a": f"{deduction}."})
    good = []
    for item in items:
        item = {"q": item["q"], "a": item["a"].replace("..", ".")[:ANSWER_MAX]}
        if checks.text_problems(item["a"], context):
            continue
        good.append(item)
    if not good:
        return None
    return {"title": "Vanliga frågor", "items": good[:6]}


def _form(facts, variant, ctx, context):
    return {
        "title": FORM_TITLES[variant],
        "questions": [dict(q) for q in FORM_QUESTIONS[variant]],
        "note_title": "",
        "note": FORM_NOTES.get(variant, ""),
        "submit": FORM_SUBMITS[variant],
    }


def _callbar(facts, variant, ctx, context):
    if not facts.phone or len(facts.phone) > PHONE_MAX:
        return None
    return {"title": f"Ring {facts.company}"[:SHORT_MAX], "phone": facts.phone}


BUILDERS = {
    "hero": _hero,
    "price": _price,
    "reviews_google": _reviews,
    "certificates": _certificates,
    "guarantee": _guarantee,
    "person": _person,
    "steps": _steps,
    "before_after": _before_after,
    "area": _area,
    "faq": _faq,
    "form": _form,
    "callbar": _callbar,
}


def template_fields(type_key, variant, account, ctx=None, facts=None):
    """Mallens fält för ett nytt block, bara ur kontots bekräftade uppgifter
    och ctx (tjänst, orter, sätt att sälja). None när blocket inte ska
    finnas: typen kräver en uppgift som saknas (pris, certifikat, garanti,
    telefon) eller mallen har inget att säga (område utan orter)."""
    block_type = TYPES[type_key]
    facts = facts or facts_for(account)
    ctx = ctx or BuildContext()
    if not _meets(block_type.requires, facts, account, ctx):
        return None
    context = facts.context(extra=(ctx.service, *ctx.places))
    fields = BUILDERS[type_key](facts, variant, ctx, context)
    if fields is None:
        return None
    return {f.key: fields.get(f.key, f.empty()) for f in block_type.fields}
