"""
Brevs element (README F.1, D9): blocktyperna i e-postens bibliotek.

Återanvänder sidbyggarens Field, Variant och BlockType med deras as_dict
(apps/flamingo/pagebuilder/registry.py). EmailField lägger till två saker
som bara e-posten har: min_items (en lista som ritas med minst så många
poster, kontrolleras i email/checks.py, inte vid sparningen) och bold_only
(rich_basic med bara fetstil). Sidbyggarens blocks.active_fields,
visible_items och media.media_ids_in tar types=EMAIL_TYPES; valideringen
står i email/blocks.py (e-postens fältsorter behöver kontot och ger fel
per block och fält).

De 24 elementen i mockups/flamingo-epost-brev.html: sidhuvudet (1) och
sidfoten (24) hör till dokumentet och står inte i blocklistan; 2 till 23 är
block som går att flytta (BLOCK_KEYS, i bibliotekets ordning). Varje block
har en enda variant ("brev"): Brev är den enda stilen (D9), och valen som
finns (storlek, justering, sida) är fält.

    EMAIL_TYPES             {typ: BlockType} för de 22 blocken
    BLOCK_KEYS              blockens nycklar i ordning (fasta)
    RETIRED_FIELDS          {typ: nycklar} för fält som tagits bort; sparade
                            värden ignoreras (underskriftens script_name)
    DOC_ELEMENTS            ("header", "footer"): dokumentets låsta delar
    GROUPS                  bibliotekets rubriker
    get_type(key)           BlockType eller None
    available(account, utskick) -> {typ: (går att lägga till?, varför inte)}
                            erbjudande och priser för information ("Erbjudanden
                            hör inte hemma i information."), omdömen utan
                            kopplad profil ("Koppla Google-profilen under
                            Företaget."), öppettider utan uppgifter
    logo_state(account) -> (finns en logga?, varför inte)
                            sidhuvudets val: utan logga bara "ingen"
                            ("Ladda upp en logotyp under Media.")
    library(account, utskick) -> list[dict]   as_dict() plus ok och why
    schema() -> list[dict]  alla typer som JSON (redigeraren och AI)
    MAX_BLOCKS = 30

Nya fältsorter (blocks.py validerar dem; sidornas sorter är oförändrade):
URL, DATE, TIME, EMAIL, PHONE, CODE, RICH_BASIC (F.1). TEXT, TEXTAREA,
MEDIA, ITEMS och CHOICE är sidbyggarens.

Omdömesblocket (element 12) har två källor, Google och en omdömessajt som
apps/website/tests.py VvsLegacyGuardTests bara tillåter i namngivna filer;
den här filen står där med namn.
"""

from dataclasses import dataclass

from apps.flamingo.pagebuilder.registry import (
    CHOICE,
    ITEMS,
    MEDIA,
    TEXT,
    TEXTAREA,
    BlockType,
    Field,
    Variant,
)

#: Blocken 2 till 23 i bibliotekets ordning (F.1). Nycklarna är fasta: de
#: sparas i email_doc och används av mallarna templates/utskick/brev/blocks/<key>.html.
BLOCK_KEYS = (
    "hero",
    "heading",
    "text",
    "button",
    "image",
    "image_text",
    "columns",
    "divider",
    "offer",
    "prices",
    "reviews",
    "steps",
    "event",
    "person",
    "video",
    "gallery",
    "faq",
    "hours",
    "callout",
    "signature",
    "spacer",
    "social",
)
#: Dokumentets låsta delar: sidhuvudet med loggan och sidfoten.
DOC_ELEMENTS = ("header", "footer")
#: Block som är låsta för information (H.5).
OFFER_KEYS = ("offer", "prices")
MAX_BLOCKS = 30

#: Fältsorterna som bara e-posten har (F.1).
URL = "url"
DATE = "date"
TIME = "time"
EMAIL = "email"
PHONE = "phone"
CODE = "code"
RICH_BASIC = "rich_basic"
EMAIL_FIELD_KINDS = (URL, DATE, TIME, EMAIL, PHONE, CODE, RICH_BASIC)
#: Alla sorter ett Brev-fält kan ha.
FIELD_KINDS = (TEXT, TEXTAREA, MEDIA, ITEMS, CHOICE, *EMAIL_FIELD_KINDS)

#: Blockens enda variant: Brev är den enda stilen (D9).
VARIANT = "brev"
_VARIANTS = (Variant(VARIANT, "Brev"),)

#: Kraven för att lägga till ett block (BlockType.requires).
REQUIRES_REVIEWS = "review_profile"
REQUIRES_HOURS = "hours_facts"
REQUIRES_TEXT = {
    REQUIRES_REVIEWS: "Koppla Google-profilen under Företaget.",
    REQUIRES_HOURS: "Lägg till öppettider eller adress under Företaget.",
}
OFFER_LOCKED_TEXT = "Erbjudanden hör inte hemma i information."
LOGO_MISSING_TEXT = "Ladda upp en logotyp under Media."

#: Bibliotekets rubriker i ordning.
GROUPS = (
    ("text", "Text"),
    ("image", "Bilder"),
    ("action", "Knappar och erbjudanden"),
    ("trust", "Förtroende och kontakt"),
    ("layout", "Form"),
)

YES_NO = (("yes", "Ja"), ("no", "Nej"))
#: Omdömenas källor; värdena sparas i email_doc.
REVIEW_SOURCES = (("google", "Google"), ("reco", "Reco"))
SOCIAL_NETWORKS = (
    ("facebook", "Facebook"),
    ("instagram", "Instagram"),
    ("linkedin", "LinkedIn"),
    ("youtube", "YouTube"),
    ("tiktok", "TikTok"),
)
SOCIAL_LABELS = dict(SOCIAL_NETWORKS)


@dataclass(frozen=True)
class EmailField(Field):
    """Ett fält i Brev: sidbyggarens Field plus min_items (listor) och
    bold_only (rich_basic utan länkar, kursiv och listor)."""

    min_items: int = 0
    bold_only: bool = False

    def as_dict(self):
        data = super().as_dict()
        if self.kind == ITEMS:
            data["min_items"] = self.min_items
        if self.kind == RICH_BASIC:
            data["bold_only"] = self.bold_only
        return data


F = EmailField


def _type(key, name, icon, group, fields, why, principle, requires=""):
    return BlockType(
        key=key,
        name=name,
        icon=icon,
        group=group,
        variants=_VARIANTS,
        fields=tuple(fields),
        why=why,
        principle=principle,
        requires=requires,
    )


TYPES_LIST = (
    _type(
        "hero",
        "Rubrik och bild",
        "hero",
        "image",
        (
            F("image", "Bild", MEDIA),
            F("kicker", "Överrubrik", TEXT, max_length=40, placeholder="Höstservice"),
            F("title", "Rubrik", TEXT, max_length=90, required=True),
            F("lead", "Ingress", TEXTAREA, max_length=300),
            F("button_text", "Knappens text", TEXT, max_length=30, placeholder="Boka service"),
            F("button_url", "Knappens adress", URL, max_length=500),
        ),
        "Det första mottagaren ser: vad mejlet handlar om och vad hen kan göra.",
        "Klarhet först",
    ),
    _type(
        "heading",
        "Rubrik",
        "heading",
        "text",
        (
            F("text", "Rubrik", TEXT, max_length=90, required=True),
            F("size", "Storlek", CHOICE, choices=(("h2", "Stor"), ("h3", "Liten"))),
        ),
        "Delar upp ett längre mejl så att det går att skumma.",
        "Lätt att skumma",
    ),
    _type(
        "text",
        "Text",
        "text",
        "text",
        (
            F(
                "body",
                "Text",
                RICH_BASIC,
                max_length=3000,
                required=True,
                placeholder=(
                    "Skriv texten. **fet**, *kursiv*, [länk](https://...) och - för en lista."
                ),
            ),
        ),
        "Det du vill säga, med stycken, fetstil, länkar och punktlistor.",
        "Skriv som du pratar",
    ),
    _type(
        "button",
        "Knapp (fylld och kantad)",
        "button",
        "action",
        (
            F("primary_text", "Knappens text", TEXT, max_length=30, required=True),
            F("primary_url", "Knappens adress", URL, max_length=500, required=True),
            F("secondary_text", "Andra knappens text", TEXT, max_length=30),
            F("secondary_url", "Andra knappens adress", URL, max_length=500),
            F("align", "Placering", CHOICE, choices=(("left", "Vänster"), ("center", "Mitten"))),
        ),
        "En tydlig nästa handling. Den andra knappen är för den som vill veta mer först.",
        "En handling i taget",
    ),
    _type(
        "image",
        "Bild med bildtext",
        "image",
        "image",
        (
            F("image", "Bild", MEDIA, required=True),
            F("caption", "Bildtext", TEXT, max_length=160),
            F("url", "Länk från bilden", URL, max_length=500),
        ),
        "En bild säger vem ni är och hur det ser ut när ni gör jobbet.",
        "Visa i stället för att säga",
    ),
    _type(
        "image_text",
        "Bild och text",
        "image_text",
        "image",
        (
            F("image", "Bild", MEDIA),
            F("title", "Rubrik", TEXT, max_length=80),
            F("body", "Text", TEXTAREA, max_length=400),
            F("link_text", "Länkens text", TEXT, max_length=30),
            F("link_url", "Länkens adress", URL, max_length=500),
            F("side", "Bilden till", CHOICE, choices=(("left", "Vänster"), ("right", "Höger"))),
        ),
        "En bild bredvid en kort text. Staplas på mobilen.",
        "Visa i stället för att säga",
    ),
    _type(
        "columns",
        "Kolumner",
        "columns",
        "layout",
        (
            F(
                "items",
                "Kolumner",
                ITEMS,
                max_items=3,
                min_items=2,
                item_label="Kolumn",
                items=(
                    F("number", "Nummer", TEXT, max_length=4),
                    F("title", "Rubrik", TEXT, max_length=40),
                    F("text", "Text", TEXT, max_length=120),
                ),
            ),
        ),
        "Två eller tre korta punkter bredvid varandra. Staplas på mobilen.",
        "Lätt att skumma",
    ),
    _type(
        "divider",
        "Avdelare",
        "divider",
        "layout",
        (),
        "En tunn linje mellan två delar av mejlet.",
        "Luft och ordning",
    ),
    _type(
        "offer",
        "Erbjudande med kod",
        "guarantee",
        "action",
        (
            F("valid_until", "Gäller till", DATE),
            F("title", "Erbjudande", TEXT, max_length=60),
            F("text", "Villkor", TEXT, max_length=160),
            F("code", "Kod", CODE, max_length=20),
        ),
        "Ett erbjudande med ett sista datum och en kod som går att ange vid bokningen.",
        "Ett tydligt erbjudande",
    ),
    _type(
        "prices",
        "Tjänster och priser",
        "price",
        "action",
        (
            F("title", "Rubrik", TEXT, max_length=60),
            F("note", "Liten text", TEXT, max_length=60),
            F(
                "items",
                "Priser",
                ITEMS,
                max_items=10,
                min_items=1,
                item_label="Pris",
                items=(
                    F("name", "Tjänst", TEXT, max_length=60),
                    F("price", "Pris", TEXT, max_length=30),
                ),
            ),
        ),
        "Priset tidigt sparar frågor. Fylls i från de bekräftade priserna.",
        "Priset tidigt",
    ),
    _type(
        "reviews",
        "Omdömen",
        "reviews",
        "trust",
        (
            F("source", "Källa", CHOICE, choices=REVIEW_SOURCES),
            F("count", "Antal omdömen", CHOICE, choices=(("1", "1"), ("2", "2"), ("3", "3"))),
            F("show_summary", "Visa betyget", CHOICE, choices=YES_NO),
        ),
        "Andras erfarenheter väger tyngre än det företaget säger om sig självt.",
        "Andras omdömen",
        REQUIRES_REVIEWS,
    ),
    _type(
        "steps",
        "Så går det till",
        "steps",
        "text",
        (
            F("title", "Rubrik", TEXT, max_length=60),
            F(
                "items",
                "Steg",
                ITEMS,
                max_items=5,
                min_items=2,
                item_label="Steg",
                items=(
                    F(
                        "text",
                        "Steg",
                        RICH_BASIC,
                        max_length=160,
                        bold_only=True,
                        placeholder="**Boka** via knappen eller ring oss.",
                    ),
                ),
            ),
        ),
        "Visar hur enkelt det är, steg för steg.",
        "Ta bort osäkerheten",
    ),
    _type(
        "event",
        "Datum eller händelse",
        "event",
        "action",
        (
            F("date", "Datum", DATE, required=True),
            F("start", "Börjar", TIME),
            F("end", "Slutar", TIME),
            F("title", "Rubrik", TEXT, max_length=80),
            F("place", "Plats", TEXT, max_length=80),
            F("calendar", "Lägg till i kalendern", CHOICE, choices=YES_NO),
        ),
        "Ett datum som syns direkt, med en länk som lägger det i kalendern.",
        "Ett tydligt datum",
    ),
    _type(
        "person",
        "Kontaktperson",
        "person",
        "trust",
        (
            F("photo", "Bild", MEDIA),
            F("name", "Namn", TEXT, max_length=60, required=True),
            F("role", "Roll", TEXT, max_length=60),
            F("phone", "Telefon", PHONE, max_length=40),
            F("email", "E-post", EMAIL, max_length=254),
        ),
        "Ett namn och ett nummer att ringa gör det lätt att höra av sig.",
        "En människa bakom",
    ),
    _type(
        "video",
        "Video",
        "video",
        "image",
        (
            F("thumbnail", "Bild", MEDIA, required=True),
            F("title", "Rubrik", TEXT, max_length=80),
            F("url", "Videons adress", URL, max_length=500, required=True),
        ),
        "En bild med en spelknapp som leder till videon. Mejl kan inte spela video själva.",
        "Visa i stället för att säga",
    ),
    _type(
        "gallery",
        "Bildgalleri",
        "gallery",
        "image",
        (
            F(
                "items",
                "Bilder",
                ITEMS,
                max_items=4,
                min_items=2,
                item_label="Bild",
                items=(
                    F("image", "Bild", MEDIA),
                    F("alt", "Beskrivning", TEXT, max_length=120),
                ),
            ),
        ),
        "Två till fyra bilder, två i varje rad.",
        "Visa i stället för att säga",
    ),
    _type(
        "faq",
        "Vanliga frågor",
        "faq",
        "text",
        (
            F("title", "Rubrik", TEXT, max_length=60),
            F(
                "items",
                "Frågor",
                ITEMS,
                max_items=6,
                min_items=1,
                item_label="Fråga",
                items=(
                    F("q", "Fråga", TEXT, max_length=120),
                    F("a", "Svar", TEXTAREA, max_length=400),
                ),
            ),
        ),
        "Svaren på det mottagarna brukar undra, innan de behöver fråga.",
        "Ta bort osäkerheten",
    ),
    _type(
        "hours",
        "Öppettider, adress och karta",
        "area",
        "trust",
        (
            F("show_hours", "Visa öppettider", CHOICE, choices=YES_NO),
            F("show_address", "Visa adress", CHOICE, choices=YES_NO),
            F("map_text", "Länkens text", TEXT, max_length=30, placeholder="Hitta hit"),
            F("map_image", "Kartbild", MEDIA),
        ),
        "Var ni finns och när ni har öppet, från uppgifterna under Företaget.",
        "Lätt att hitta",
        REQUIRES_HOURS,
    ),
    _type(
        "callout",
        "Ruta (framhävd text)",
        "callout",
        "text",
        (
            F(
                "text",
                "Text",
                RICH_BASIC,
                max_length=300,
                required=True,
                bold_only=True,
                placeholder="**PS.** Det viktigaste en gång till.",
            ),
        ),
        "En kort text i en ruta som sticker ut, till exempel ett PS.",
        "Det viktiga syns",
    ),
    _type(
        "signature",
        "Underskrift",
        "person",
        "trust",
        (
            F("greeting", "Hälsning", TEXT, max_length=40, placeholder="Vänliga hälsningar,"),
            F("photo", "Bild", MEDIA),
            F("name", "Namn", TEXT, max_length=60),
            F("line", "Rad under namnet", TEXT, max_length=120),
            F("phone", "Telefon", PHONE, max_length=40),
        ),
        "Ett mejl från en person läses mer än ett från ett företag.",
        "En människa bakom",
    ),
    _type(
        "spacer",
        "Mellanrum",
        "spacer",
        "layout",
        (F("size", "Storlek", CHOICE, choices=(("m", "Mellan"), ("s", "Litet"), ("l", "Stort"))),),
        "Mer luft mellan två block.",
        "Luft och ordning",
    ),
    _type(
        "social",
        "Sociala medier",
        "social",
        "trust",
        (
            F(
                "items",
                "Länkar",
                ITEMS,
                max_items=5,
                min_items=1,
                item_label="Länk",
                items=(
                    F("network", "Nätverk", CHOICE, choices=SOCIAL_NETWORKS),
                    F("url", "Adress", URL, max_length=500),
                ),
            ),
        ),
        "Länkar till där ni finns i sociala medier.",
        "Lätt att hitta",
    ),
)

EMAIL_TYPES = {t.key: t for t in TYPES_LIST}
assert tuple(EMAIL_TYPES) == BLOCK_KEYS, "EMAIL_TYPES och BLOCK_KEYS i samma ordning"

#: Fält som har tagits bort ur ett block: {typ: nycklar}. Sparade mejl kan
#: ha kvar dem i email_doc. De ritas inte, nekas inte (blocks._Cleaner) och
#: försvinner när mejlet sparas nästa gång; en version som bara skiljer sig
#: i dem räknas som oförändrad (blocks._stamp).
#: Underskriftens script_name ("Namnet i skrivstil") togs bort 2026-10-10
#: (Giovanni): systemets skrivstilar ser ut som ett bröllopskort och ser
#: olika ut på varje enhet, och ett mejl kan inte ha med sig ett eget typsnitt.
RETIRED_FIELDS = {"signature": frozenset({"script_name"})}
assert not any(
    keys & {f.key for f in EMAIL_TYPES[key].fields} for key, keys in RETIRED_FIELDS.items()
), "ett borttaget fält står kvar i schemat"

#: Bibliotekets grupp per block.
GROUP_NAMES = dict(GROUPS)


def get_type(key):
    """BlockType för nyckeln, eller None."""
    return EMAIL_TYPES.get(key)


def schema():
    """Alla Brev-typer som JSON-bara dicts (för redigeraren och AI)."""
    return [t.as_dict() for t in TYPES_LIST]


# ---------------------------------------------------------------------------
# Vad som går att lägga till
# ---------------------------------------------------------------------------


def review_sources(account):
    """{källa: går den att visa?}: Google med en intygad profil som har ett
    betyg eller valda omdömen, Reco med en intygad profil (reco_trusted)."""
    google = bool(account.google_profile_trusted) and (
        account.trusted_google_rating is not None or bool(account.selected_google_reviews())
    )
    return {"google": google, "reco": bool(account.reco_trusted)}


def _hours_ok(account):
    from apps.flamingo.pagebuilder.facts import facts_for

    facts = facts_for(account)
    return bool(facts.hours or facts.address)


def logo_state(account):
    """(finns en logga, varför inte): sidhuvudets loggval (F.1 element 1).
    Utan logga i mediaarkivet går bara "ingen" att välja."""
    from apps.flamingo.models import MediaAsset

    if MediaAsset.objects.filter(account=account, is_logo=True).exists():
        return True, ""
    return False, LOGO_MISSING_TEXT


def available(account, utskick):
    """{typ: (ok, varför inte)} för biblioteket och sparningen (ett nytt
    block som är låst nekas av blocks.validate; ett block som redan står i
    mejlet får stå kvar och kontrolleras i email/checks.py)."""
    info = getattr(utskick, "is_information", False)
    reviews = any(review_sources(account).values())
    hours = _hours_ok(account)
    out = {}
    for key in BLOCK_KEYS:
        ok, why = True, ""
        if info and key in OFFER_KEYS:
            ok, why = False, OFFER_LOCKED_TEXT
        elif key == "reviews" and not reviews:
            ok, why = False, REQUIRES_TEXT[REQUIRES_REVIEWS]
        elif key == "hours" and not hours:
            ok, why = False, REQUIRES_TEXT[REQUIRES_HOURS]
        out[key] = (ok, why)
    return out


def library(account, utskick):
    """Biblioteket som JSON för redigeraren: as_dict() plus ok, why och
    gruppens namn, i bibliotekets ordning."""
    state = available(account, utskick)
    out = []
    for block_type in TYPES_LIST:
        ok, why = state[block_type.key]
        data = block_type.as_dict()
        data.update({"ok": ok, "why_not": why, "group_name": GROUP_NAMES[block_type.group]})
        out.append(data)
    return out
