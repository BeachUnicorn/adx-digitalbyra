"""
Kontrollerna för ett mejl (README F.5, I.6, H.5). Redigeraren
(app_brev_checks) och Granska (app_views.utskick.review) läser dem.

    BLOCKS, WARNS, TICK = "blocks", "warns", "tick"
    @dataclass Item(level, text, key)
    email_checks(utskick, *, now=None, contact=None, email_count=None,
                 check_links=True) -> list[Item]
        blockerar:  HTML-storleken för de längsta värdena och personliga länkar
                    över MAX_HTML_BYTES ("Gmail kapar större mejl, och då
                    försvinner avregistreringen"); adressen i sidfoten för reklam
                    (Företaget > adress); ämnesraden och platshållarna i ämnesrad
                    och förhandstext; egen domän verifierad, eller ADX-domänen
                    under månadstaket (sending.email.adx_cap_left; med
                    email_count, Granskas antal mejl, prövas att utskicket ryms);
                    varje länkvärd godkänd (E.8); reglerna för information (H.5,
                    byråns undantag släpper bara reklamorden, se nedan); fält som
                    måste fyllas i; ett mejl utan innehåll
        alltid:     avregistreringen i sidfoten och List-Unsubscribe (låst, en bock)
        varnar:     länkarna svarar (links.check_destinations, delad med sms; bara
                    med check_links, högst tio körningar per konto och timme);
                    bilder utan alt-text; saknade värden per platshållare ("214
                    saknar förnamn, "du" används."); vaktens anmärkningar på
                    AI-skriven text (F.7); listor med för få poster; villkor för ett
                    erbjudande som inte bekräftats; omdömen som inte kan visas
    blocking(items) -> list[Item]
    as_json(items) -> list[dict]     [{"level", "text", "key"}] för redigeraren
    email_fingerprint(utskick) -> str
                                     mejlets innehåll som en kort hash: byråns undantag
                                     för information (content_override) gäller e-posten
                                     bara när override["email_fingerprint"] är den här
                                     (manage_sending.info_override sparar den, se
                                     S3-HANDOFF.md, Requests from renderer)

contact: förhandsvisningens kontakt; saknade värden för just den nämns först.
"""

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from urllib.parse import urlsplit

from django.utils import timezone

from . import blocks, registry, render

BLOCKS = "blocks"
WARNS = "warns"
TICK = "tick"
#: Gmail kapar mejl över ungefär 102 kB (F.5).
MAX_HTML_BYTES = 102_000
#: Högst så många kontakter läses för reklamorden i extrafälten.
FIELD_SCAN = 25_000

SIZE_OK_TEXT = (
    "Storlek {kb} av 102 kB (Gmail kapar större mejl, och då försvinner avregistreringen)."
)
SIZE_TEXT = (
    "Mejlet är {kb} kB. Gmail kapar mejl över 102 kB, och då försvinner avregistreringen. "
    "Ta bort block eller korta texterna."
)
ADDRESS_TEXT = "Adressen saknas i sidfoten. Lägg till företagets adress under Företaget."
SUBJECT_TEXT = "Skriv en ämnesrad."
EMPTY_TEXT = "Lägg till innehåll i mejlet."
UNSUBSCRIBE_TEXT = "Avregistrering i sidfoten och med ett klick i e-postprogrammet."
TEXT_VERSION_TEXT = "Textversionen skapas ur blocken."
TEXT_OWN_TEXT = "Textversionen är din egen."
DOMAIN_TEXT = (
    "Avsändardomänen är inte verifierad. Välj ADX-domänen eller verifiera domänen under "
    "Inställningar."
)
CAP_FULL_TEXT = (
    "Taket för ADX-domänen är nått: {cap} mejl i {month}. Verifiera din egen domän under "
    "Inställningar."
)
CAP_TEXT = "Ryms inte i taket: {need} mejl, {left} kvar av {cap} i {month}."
OFFER_INFO_TEXT = registry.OFFER_LOCKED_TEXT
TERMS_TEXT = "Bekräfta villkoren för erbjudandet: bocka i Uppgifterna stämmer."
REVIEWS_TEXT = "Omdömena syns inte i mejlet: koppla Google-profilen under Företaget."
LINKS_OK_TEXT = "{ok} av {total} länkar svarar."
LINKS_BAD_TEXT = "{ok} av {total} länkar svarar. Kontrollera länken till {host}."
LINKS_LATER_TEXT = "Länkarna kontrollerades inte nu. Försök igen senare."
ALT_TEXT = "{n} bild saknar alt-text. Skriv en beskrivning under Media."
ALT_MANY_TEXT = "{n} bilder saknar alt-text. Skriv en beskrivning under Media."
MISSING_FALLBACK_TEXT = '{n} saknar {name}, "{fallback}" används.'
MISSING_EMPTY_TEXT = (
    "{n} saknar {name}, där blir det tomt. Skriv en reservtext, till exempel {example}."
)
#: För andra värden än förnamnet finns inget exempel som passar alla (en
#: reservtext som "..." skulle skickas till alla som saknar värdet).
MISSING_EMPTY_BOX_TEXT = (
    "{n} saknar {name}, där blir det tomt. Skriv en reservtext under Om ett värde saknas i mejlet."
)
REQUIRED_TEXT = "{block}: fyll i {field}."
MIN_ITEMS_TEXT = "{block}: lägg till minst {n}."
GUARD_TEXT = "{block}: {problem}"

#: Platshållarnas namn i texterna.
TAG_WORDS = {"förnamn": "förnamn", "efternamn": "efternamn", "namn": "namn", "företag": "företag"}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Item:
    level: str
    text: str
    key: str = ""


def blocking(items):
    return [item for item in items if item.level == BLOCKS]


def as_json(items):
    return [{"level": item.level, "text": item.text, "key": item.key} for item in items]


def _month_name(now):
    from apps.sms.pricing import STOCKHOLM

    return render.MONTHS[timezone.localtime(now, STOCKHOLM).month - 1]


def _tal(n):
    from apps.flamingo.templatetags.flamingo_app import tal

    return tal(n)


# ---------------------------------------------------------------------------
# Texterna i mejlet
# ---------------------------------------------------------------------------


def email_texts(utskick):
    """Allt kunden skrivit i mejlet som text: ämnesrad, förhandstext,
    blockens textfält (aktiva versioner) och reservtexterna. För
    reklamorden, platshållarna och fingeravtrycket."""
    from apps.flamingo.pagebuilder.registry import ITEMS, TEXT, TEXTAREA

    texts = [utskick.subject or "", utskick.preheader or ""]
    kinds = (TEXT, TEXTAREA, registry.RICH_BASIC, registry.CODE)
    for _block, block_type, fields in blocks.active_blocks(utskick):
        for spec in block_type.fields:
            value = fields.get(spec.key)
            if spec.kind in kinds and value:
                texts.append(str(value))
            elif spec.kind == ITEMS:
                for item in value or []:
                    for sub in spec.items:
                        if sub.kind in kinds and isinstance(item, dict) and item.get(sub.key):
                            texts.append(str(item[sub.key]))
            elif spec.kind == registry.DATE and value and block_type.key == "offer":
                texts.append(f"till {render.date_long(value)}")
    return texts


def email_fingerprint(utskick):
    """Mejlets innehåll (ämnesrad, förhandstext, blockens aktiva fält,
    textversionen och reservtexterna) som en kort hash."""
    data = json.dumps(
        [
            utskick.subject or "",
            utskick.preheader or "",
            [[b.get("type"), fields] for b, _t, fields in blocks.active_blocks(utskick)],
            utskick.text_override or "",
            utskick.merge_fallbacks if isinstance(utskick.merge_fallbacks, dict) else {},
        ],
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:32]


def _override_ok(utskick):
    """Byråns undantag för information gäller e-posten: det gäller texten
    som står nu (sending.checks.override_valid) och mejlet som det såg ut
    när undantaget gavs (email_fingerprint)."""
    from ..sending import checks

    override = utskick.content_override if isinstance(utskick.content_override, dict) else {}
    return checks.override_valid(utskick) and override.get("email_fingerprint") == (
        email_fingerprint(utskick)
    )


def _ad_text(utskick):
    """Texterna med reservtexterna i klamrarna kvar ({förnamn|halva
    priset} prövas också) och utskickets reservtexter."""
    from .. import composer

    def keep_fallback(match):
        _name, bar, fallback = match.group(1).partition("|")
        return f" {fallback} " if bar else " "

    parts = [composer.TOKEN_RE.sub(keep_fallback, text) for text in email_texts(utskick)]
    fallbacks = utskick.merge_fallbacks if isinstance(utskick.merge_fallbacks, dict) else {}
    parts += [str(value) for value in fallbacks.values() if value]
    plain = []
    for part in parts:
        plain.append(blocks.rich_plain(blocks.parse_rich(part)) if "*" in part else part)
    return " ".join(plain)


def _used_tags(utskick):
    from .. import composer

    tags = []
    for text in email_texts(utskick):
        for tag in composer.placeholders(text).tags:
            if tag not in tags:
                tags.append(tag)
    return tags


def _inline_fallback(utskick, tag):
    """Reservtexten för en platshållare: den första i klamrarna ({förnamn|du})
    i texterna, annars utskickets."""
    from .. import composer

    for text in email_texts(utskick):
        for match in composer.TOKEN_RE.finditer(text):
            kind, name, inline = composer._classify(match.group(1))
            if kind == "tag" and name == tag and inline.strip():
                return inline.strip()
    fallbacks = utskick.merge_fallbacks if isinstance(utskick.merge_fallbacks, dict) else {}
    return str(fallbacks.get(tag) or "").strip()


# ---------------------------------------------------------------------------
# Kontrollerna
# ---------------------------------------------------------------------------


def _size_items(utskick):
    size = render.html_size(utskick)
    kb = max(1, math.ceil(size / 1000))
    if size > MAX_HTML_BYTES:
        return [Item(BLOCKS, SIZE_TEXT.format(kb=kb), "size")]
    return [Item(TICK, SIZE_OK_TEXT.format(kb=kb), "size")]


def _sender_items(utskick, now, email_count):
    from django.conf import settings as django_settings

    domain = utskick.sender_domain if utskick.sender_domain_id else None
    if domain is not None:
        if domain.account_id != utskick.account_id or not domain.is_verified:
            return [Item(BLOCKS, DOMAIN_TEXT, "sender")]
        return []
    try:
        from ..sending import email as sending_email

        left = sending_email.adx_cap_left(utskick.account, now)
    except NotImplementedError:
        # Sändningens del av S3 (byggare C) är inte på plats: taket prövas i
        # Granska när den är det.
        return []
    cap = int(getattr(django_settings, "UTSKICK_ADX_MONTHLY_MAIL_CAP", 2000) or 2000)
    month = _month_name(now)
    if left <= 0:
        return [Item(BLOCKS, CAP_FULL_TEXT.format(cap=_tal(cap), month=month), "sender")]
    if email_count is not None and email_count > left:
        return [
            Item(
                BLOCKS,
                CAP_TEXT.format(
                    need=_tal(email_count), left=_tal(left), cap=_tal(cap), month=month
                ),
                "sender",
            )
        ]
    return []


def _host_items(utskick, spots):
    from .. import links
    from . import blocks as email_blocks

    out, pending = [], False
    for spot in spots:
        # S3 (integrationen): kontots egna Flamingo-sidor är alltid fria.
        if email_blocks.own_page(utskick.account, spot.url) is not None:
            continue
        try:
            links.clean_external(utskick.account, spot.url)
        except links.HostPending:
            pending = True
        except links.LinkRefused as exc:
            text = str(exc)
            if all(item.text != text for item in out):
                out.append(Item(BLOCKS, text, "hosts"))
    if pending:
        out.insert(0, Item(BLOCKS, links.PENDING_TEXT, "hosts"))
    return out


def _is_flamingo_page(url):
    from apps.flamingo.exports import landing_host

    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    return host == landing_host() and (parts.path or "").startswith("/lp/")


def _field_ad_problems(utskick, tags):
    """Extrafält ({fält:...}) i ett informationsutskick vars värden ser ut
    som reklam (H.5), en text per fält."""
    from .. import audience
    from ..sending import checks

    keys = [t[len("fält:") :] for t in tags if t.startswith("fält:")]
    if not keys:
        return []
    flagged = []
    rows = audience.contacts(utskick).values_list("fields", flat=True)[:FIELD_SCAN]
    for fields in rows.iterator(chunk_size=2000):
        for key in keys:
            value = (fields or {}).get(key)
            if key not in flagged and value not in (None, "") and checks.looks_like_ad(str(value)):
                flagged.append(key)
        if len(flagged) == len(keys):
            break
    return [checks.INFO_FIELD_TEXT.format(key=key) for key in flagged]


def _information_items(utskick, spots, data):
    """H.5 för e-posten: inga erbjudande- eller prisblock, inga
    Flamingo-sidor, bara länkar till kundens egen webbplats, inga reklamord
    (byråns undantag släpper bara dem) och inga extrafält som ser ut som
    reklam. Skälet prövas av sending.checks.information_problems (sms och
    e-post gemensamt)."""
    from ..sending import checks

    if not utskick.is_information:
        return []
    out = []
    if any(b.get("type") in registry.OFFER_KEYS for b in blocks.doc_blocks(utskick)):
        out.append(Item(BLOCKS, OFFER_INFO_TEXT, "information"))
    hosts = checks.own_hosts(utskick.account)
    # Kartlänken som öppettiderna själva bygger (adressen ur Företaget) är
    # ingen länk kunden skrivit.
    address = (data.get("company") or {}).get("address") or ""
    maps = render.maps_url(address) if address else ""
    lp_said = False
    for spot in spots:
        if _is_flamingo_page(spot.url):
            if not lp_said:
                out.append(Item(BLOCKS, checks.INFO_LP_TEXT, "information"))
                lp_said = True
            continue
        host = urlsplit(spot.url).hostname or ""
        if maps and spot.url == maps:
            continue
        if not checks._own(host, hosts):
            text = checks.INFO_HOST_TEXT.format(host=host or "en annan webbplats")
            if all(item.text != text for item in out):
                out.append(Item(BLOCKS, text, "information"))
    if checks.looks_like_ad(_ad_text(utskick)) and not _override_ok(utskick):
        out.append(Item(BLOCKS, checks.LOOKS_LIKE_AD_TEXT, "information"))
    for text in _field_ad_problems(utskick, _used_tags(utskick)):
        out.append(Item(BLOCKS, text, "information"))
    return out


def _required_items(utskick):
    """Fält som måste fyllas i och listor med för få poster (F.1). De
    stoppar inte sparningen av utkastet, bara utskicket."""
    from apps.flamingo.pagebuilder.registry import ITEMS

    out = []
    for _block, block_type, fields in blocks.active_blocks(utskick):
        for spec in block_type.fields:
            value = fields.get(spec.key)
            if spec.required and value in ("", None, []):
                text = REQUIRED_TEXT.format(block=block_type.name, field=spec.label.lower())
                out.append(Item(BLOCKS, text, "required"))
            if spec.kind == ITEMS and getattr(spec, "min_items", 0):
                count = len([i for i in value or [] if isinstance(i, dict)])
                if count < spec.min_items:
                    word = (spec.item_label or spec.label).lower()
                    n = (
                        f"{spec.min_items} {word}"
                        if spec.min_items == 1
                        else (f"{spec.min_items} av {spec.label.lower()}")
                    )
                    out.append(
                        Item(WARNS, MIN_ITEMS_TEXT.format(block=block_type.name, n=n), "min_items")
                    )
    return out


def _alt_items(data, entries):
    """Bilder utan alt-text: arkivets beskrivning (eller galleriets fält)."""
    missing = 0
    for entry in entries:
        block_type = registry.get_type(entry["type"])
        fields = entry["fields"]
        for spec in block_type.fields:
            if spec.kind == registry.MEDIA and isinstance(fields.get(spec.key), int):
                purpose = render.PURPOSES.get((entry["type"], spec.key), "content")
                if purpose == "avatar":
                    continue
                info = (data.get("images") or {}).get(f"{fields[spec.key]}:{purpose}")
                if info and not str(info.get("alt") or "").strip():
                    missing += 1
        if entry["type"] == "gallery":
            for item in fields.get("items") or []:
                if not isinstance(item, dict) or not isinstance(item.get("image"), int):
                    continue
                info = (data.get("images") or {}).get(f"{item['image']}:content")
                if info and not (item.get("alt") or str(info.get("alt") or "").strip()):
                    missing += 1
    if not missing:
        return []
    text = (ALT_TEXT if missing == 1 else ALT_MANY_TEXT).format(n=missing)
    return [Item(WARNS, text, "alt")]


def _link_items(utskick, spots):
    from .. import links

    urls = list(dict.fromkeys(spot.url for spot in spots))
    if not urls:
        return []
    results = links.check_destinations(utskick.account, urls)
    checked = {url: ok for url, ok in results.items() if ok is not None}
    if not checked:
        return [Item(WARNS, LINKS_LATER_TEXT, "links")]
    ok = sum(1 for value in checked.values() if value)
    total = len(checked)
    if ok == total:
        return [Item(TICK, LINKS_OK_TEXT.format(ok=ok, total=total), "links")]
    bad = next(url for url, value in checked.items() if not value)
    host = urlsplit(bad).hostname or bad
    return [Item(WARNS, LINKS_BAD_TEXT.format(ok=ok, total=total, host=host), "links")]


def _missing_counts(utskick, tags):
    """{platshållare: antal mottagare utan värdet}. Fryst: ur
    Recipient.merge; annars ur urvalets kontakter med en e-postadress."""
    from django.db.models import Q

    from .. import audience
    from ..models import CHANNEL_EMAIL, Recipient

    counts = {}
    if utskick.frozen_at or utskick.freeze_cursor:
        rows = Recipient.objects.filter(
            utskick=utskick, channel=CHANNEL_EMAIL, status=Recipient.Status.QUEUED
        )
        for tag in tags:
            counts[tag] = rows.exclude(merge__has_key=tag).count()
        return counts
    if audience.is_empty(utskick):
        return counts
    base = audience.contacts(utskick).exclude(email="")
    blank = {
        "förnamn": Q(first_name=""),
        "efternamn": Q(last_name=""),
        "företag": Q(company_name=""),
        "namn": Q(first_name="", last_name="", company_name=""),
    }
    for tag in tags:
        if tag in blank:
            query = blank[tag]
        elif tag.startswith("fält:"):
            key = tag[len("fält:") :]
            query = (
                ~Q(fields__has_key=key)
                | Q(**{f"fields__{key}": ""})
                | Q(**{f"fields__{key}__isnull": True})
            )
        else:
            continue
        counts[tag] = base.filter(query).count()
    return counts


def _missing_items(utskick, contact):
    from .. import composer

    tags = _used_tags(utskick)
    if not tags:
        return []
    out = []
    if contact is not None:
        values = composer.merge_values(contact, composer.field_defs(utskick.account))
        for tag in tags:
            if not values.get(tag):
                fallback = _inline_fallback(utskick, tag)
                name = TAG_WORDS.get(tag, tag)
                text = (
                    f'Förhandsvisningens kontakt saknar {name}, "{fallback}" används.'
                    if fallback
                    else f"Förhandsvisningens kontakt saknar {name}, där blir det tomt."
                )
                out.append(Item(WARNS, text, f"missing:{tag}"))
    for tag, n in _missing_counts(utskick, tags).items():
        if not n:
            continue
        name = TAG_WORDS.get(tag, tag)
        fallback = _inline_fallback(utskick, tag)
        if fallback:
            text = MISSING_FALLBACK_TEXT.format(n=_tal(n), name=name, fallback=fallback)
        else:
            if tag == "förnamn":
                text = MISSING_EMPTY_TEXT.format(n=_tal(n), name=name, example="{förnamn|du}")
            else:
                text = MISSING_EMPTY_BOX_TEXT.format(n=_tal(n), name=name)
        out.append(Item(WARNS, text, f"missing:{tag}"))
    return out


def _guard_items(utskick):
    """Vaktens anmärkningar på AI-skrivna versioner (F.7). Kundens egen
    text stoppas aldrig av vakten."""
    from apps.flamingo.pagebuilder.registry import ITEMS, TEXT, TEXTAREA

    ai_blocks = []
    for block, block_type, fields in blocks.active_blocks(utskick):
        version = blocks.active_version(block) or {}
        if version.get("source") == blocks.SOURCE_AI and blocks.is_signed(version):
            ai_blocks.append((block_type, fields))
    if not ai_blocks:
        return []
    try:
        from .. import ai

        guard = ai.make_utskick_guard(utskick.account, utskick)
    except NotImplementedError:
        return []
    kinds = (TEXT, TEXTAREA, registry.RICH_BASIC)
    out = []
    for block_type, fields in ai_blocks:
        texts = []
        for spec in block_type.fields:
            value = fields.get(spec.key)
            if spec.kind in kinds and value:
                texts.append(str(value))
            elif spec.kind == ITEMS:
                for item in value or []:
                    for sub in spec.items:
                        if sub.kind in kinds and isinstance(item, dict) and item.get(sub.key):
                            texts.append(str(item[sub.key]))
        for text in texts:
            for problem in guard.problems(text):
                item = Item(
                    WARNS, GUARD_TEXT.format(block=block_type.name, problem=problem), "guard"
                )
                if item not in out:
                    out.append(item)
    return out


def email_checks(utskick, *, now=None, contact=None, email_count=None, check_links=True):
    """Kontrollerna i ordning: det som blockerar först, sedan bockarna och
    varningarna (se modulens beskrivning)."""
    now = now or timezone.now()
    data = render.build_data(utskick, create=False)
    entries = data["blocks"]
    spots = [
        s
        for s in render.collect_links(utskick, data=data)
        if s.url.lower().startswith(("http://", "https://"))
    ]
    items = []
    if not render.block_views(
        utskick, render.context_for(utskick, mode=render.PREVIEW, snapshot=data)
    ):
        items.append(Item(BLOCKS, EMPTY_TEXT, "empty"))
    if not str(utskick.subject or "").strip():
        items.append(Item(BLOCKS, SUBJECT_TEXT, "subject"))
    for label, text in (("Ämnesraden", utskick.subject), ("Förhandstexten", utskick.preheader)):
        for problem in blocks.merge_problems(utskick.account, text or ""):
            items.append(Item(BLOCKS, f"{label}: {problem}", "tags"))
    if not utskick.is_information and not (data.get("company") or {}).get("address"):
        items.append(Item(BLOCKS, ADDRESS_TEXT, "address"))
    items += _required_items(utskick)
    items += _size_items(utskick)
    items += _sender_items(utskick, now, email_count)
    items += _host_items(utskick, spots)
    items += _information_items(utskick, spots, data)
    items.append(Item(TICK, UNSUBSCRIBE_TEXT, "unsubscribe"))
    items.append(
        Item(
            TICK,
            TEXT_OWN_TEXT if (utskick.text_override or "").strip() else TEXT_VERSION_TEXT,
            "text",
        )
    )
    if check_links:
        items += _link_items(utskick, spots)
    items += _alt_items(data, entries)
    items += _missing_items(utskick, contact)
    terms = utskick.confirmed_terms if isinstance(utskick.confirmed_terms, list) else []
    if terms and utskick.terms_confirmed_at is None:
        items.append(Item(WARNS, TERMS_TEXT, "terms"))
    shown_reviews = set(data.get("reviews") or {})
    if any(e["type"] == "reviews" and e["id"] not in shown_reviews for e in entries):
        items.append(Item(WARNS, REVIEWS_TEXT, "reviews"))
    items += _guard_items(utskick)
    order = {BLOCKS: 0, TICK: 1, WARNS: 2}
    return sorted(items, key=lambda item: order.get(item.level, 3))
