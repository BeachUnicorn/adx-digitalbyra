"""
Operationer för blocksidor. Blockdata saneras via block_schema.

En blocksida är en ordnad stapel block som renderas uppifrån och ner. Det AI:n
behöver veta om dem - hur de ser ut, vilken ordning de ska ligga i och vad som
inte går att sätta - kommer från EN källa: `hamta_blockkatalog`, som byggs ur
BLOCK_EDIT_SCHEMA. Beskrivningarna bor i schemat, inte här, så en ny blocktyp
beskriver sig själv (synkvakten i apps/website/tests.py kräver det).
"""

from apps.assistant.models import Risk
from apps.faq.models import FAQSection
from apps.manage.block_schema import (
    BLOCK_EDIT_SCHEMA,
    clean_block_rows,
    clean_block_values,
    field_keys,
    list_specs,
)
from apps.manage.forms import BlockPageForm
from apps.website.models import Block, BlockPage

from .base import (
    Operation,
    OperationError,
    Prepared,
    assert_nothing_lost,
    cleaned_subset,
    register,
    run_form,
)

META_FIELDS = ["title", "meta_title", "meta_description"]

#: Kompositionsreglerna, avlästa ur de elva sidor designen levererade
#: (strict-design-guide.html). De är konventioner, inte spärrar - men modellen
#: ska känna till dem, annars bygger den sidor som inte ser ut som sajten.
COMPOSITION_RULES = [
    "Varje sida börjar med ett hero-block. Alla sidor på sajten gör det.",
    "Nästan varje sida slutar med ett bar-block (CTA-raden).",
    "Blocken visas i den ordning de ligger, uppifrån och ner. skapa_block "
    "lägger alltid det nya blocket SIST - skapa dem i den ordning de ska stå, "
    "eller flytta efteråt med ordna_block.",
    "Tjänstesidorna följer mönstret hero, en punktsektion (why eller steps), "
    "split, ett par prose, faq, related, bar.",
    "En ny sida skapas opublicerad och ligger osynlig tills den publiceras "
    "med satt_sida_publicerad.",
    "En ny sida hamnar INTE i någon meny - menyer ligger utanför det du kan "
    "ändra. Säg till kunden att sidan behöver läggas in i menyn manuellt, "
    "annars nås den bara via direktlänk.",
]

#: Fälttyper modellen inte kan sätta, och varför. Ett tyst tomt värde är
#: värre än ett fel: modellen tror att bilden sattes och går vidare.
_UNSETTABLE = {
    "image": (
        "bilder väljs i mediebiblioteket av en människa - du kan inte se dem "
        "och kan därför inte bedöma om rätt bild valts"
    ),
}


def _page(slug):
    page = BlockPage.objects.filter(slug=slug).first()
    if page is None:
        known = ", ".join(BlockPage.objects.values_list("slug", flat=True))
        raise OperationError(f"Okänd sida: {slug}. Kända: {known}")
    return page


def _pending_page(job, slug):
    """
    Ett ännu icke godkänt sidutkast med den här sluggen.

    Utan det går det inte att skapa en sida och fylla den i samma tur: sidan
    finns inte i databasen förrän kunden godkänt den, så skapa_block svarade
    "Okänd sida" och la inget utkast alls. Modellen kunde alltså skapa sidor
    men aldrig ge dem innehåll - samma fälla som tomma FAQ-sektioner
    (2026-08-21), och lika osynlig för modellen.
    """
    from apps.assistant.models import DraftChange

    rows = job.changes.filter(operation="skapa_sida", status=DraftChange.Status.PENDING)
    for change in rows:
        if change.payload.get("slug") == slug:
            return change
    return None


def _resolve_page(job, slug):
    """(sida, väntande_utkast) - exakt en av dem är satt, annars fel."""
    page = BlockPage.objects.filter(slug=slug).first()
    if page is not None:
        return page, None
    pending = _pending_page(job, slug)
    if pending is None:
        known = ", ".join(BlockPage.objects.values_list("slug", flat=True))
        raise OperationError(
            f"Okänd sida: {slug}. Kända: {known}. Skapa sidan med skapa_sida "
            f"först - du kan lägga block på den i samma tur."
        )
    return None, pending


def _specs(block_type):
    return {spec["key"]: spec for spec in BLOCK_EDIT_SCHEMA[block_type]["fields"]}


def _prepare_values(block_type, falt):
    """
    Förbehandla fältvärden innan saneringen: stoppa det som inte går att
    sätta, och översätt FAQ-sektionens slug till det id blocket lagrar.
    """
    specs = _specs(block_type)
    out = {}
    for key, raw in falt.items():
        spec = specs.get(key)
        if spec is None:
            continue  # okänd nyckel fångas av clean_block_values
        reason = _UNSETTABLE.get(spec["type"])
        if reason and str(raw).strip():
            raise OperationError(
                f"Fältet '{key}' kan du inte sätta: {reason}. Lämna det och be "
                f"kunden fylla i det i /manage/."
            )
        if spec["type"] == "faq_section":
            out[key] = _faq_section_id(raw)
            continue
        out[key] = raw
    return out


def _faq_section_id(raw):
    """FAQ-blocket lagrar ett id; modellen arbetar i slugs överallt annars."""
    value = str(raw or "").strip()
    if not value:
        return ""
    section = FAQSection.objects.filter(slug=value).first()
    if section is None:
        known = ", ".join(FAQSection.objects.values_list("slug", flat=True))
        raise OperationError(
            f"Okänd FAQ-sektion: {value}. Kända: {known}. Sektionen måste vara "
            f"godkänd och finnas innan den kan visas i ett faq-block."
        )
    return str(section.pk)


def _assert_kept(block_type, falt, data):
    """
    Höj fel när saneringen tyst slukade ett värde.

    _clean_value svarar med tom sträng på en ogiltig URL eller längd, och med
    första giltiga alternativet på ett ogiltigt choice-värde. Modellen ser
    bara ett kvitto på att utkastet skapades och skulle upprepa felet. Samma
    resonemang som assert_nothing_lost, men för de fälttyper där förlusten
    inte syns som kapad text.
    """
    specs = _specs(block_type)
    for key, raw in falt.items():
        if not str(raw).strip():
            continue
        spec = specs.get(key)
        if spec is None:
            continue
        stored = _flat(data, key)
        if spec["type"] == "choice":
            allowed = [c[0] for c in spec.get("choices", [])]
            if raw not in allowed:
                raise OperationError(
                    f"Fältet '{key}' måste vara ett av: {', '.join(allowed)}. Du skrev '{raw}'."
                )
            continue
        if stored in ("", {}, None):
            raise OperationError(
                f"Fältet '{key}' ({spec['type']}) godtog inte värdet '{raw}' och "
                f"hade sparats tomt. Kontrollera formatet - en length skrivs "
                f"t.ex. '4rem', en url som https://... eller /en-sida/."
            )


def _block(block_id):
    block = Block.objects.filter(pk=block_id).select_related("page").first()
    if block is None:
        raise OperationError(f"Okänt block: {block_id}. Använd hamta_sida för id:n.")
    if block.block_type not in BLOCK_EDIT_SCHEMA:
        raise OperationError(f"Blocktypen {block.block_type} kan inte redigeras.")
    return block


def _lista(user):
    return {
        "sidor": [
            {
                "slug": p.slug,
                "titel": p.title,
                "publicerad": p.is_published,
                "url": p.get_absolute_url(),
            }
            for p in BlockPage.objects.all()
        ]
    }


def _hamta(user, slug):
    page = _page(slug)
    blocks = []
    for position, block in enumerate(page.blocks.all(), start=1):
        schema = BLOCK_EDIT_SCHEMA.get(block.block_type)
        blocks.append(
            {
                "block_id": block.pk,
                # Ordningen är innehåll, inte metadata: blocken renderas
                # uppifrån och ner och modellen ska kunna resonera om var ett
                # nytt block hamnar och vad ordna_block skulle byta plats på.
                "position": position,
                "typ": block.block_type,
                "typ_namn": schema["label"] if schema else block.block_type,
                "synligt": block.is_visible,
                "redigerbar": schema is not None,
                "faltnycklar": field_keys(block.block_type),
                "listnycklar": [lst["key"] for lst in list_specs(block.block_type)],
                # Bilder syns annars bara som råa id:n nere i data. Modellen
                # kan inte se bilden, men ska kunna svara på om den finns.
                "har_bild": any(
                    key.endswith("image_id") and value for key, value in (block.data or {}).items()
                ),
                "data": block.data,
            }
        )
    return {
        "slug": page.slug,
        "titel": page.title,
        "publicerad": page.is_published,
        "meta_title": page.meta_title,
        "meta_description": page.meta_description,
        "block": blocks,
    }


def _build_data(block_type, current, falt, listor):
    """Sanera fält och listrader ovanpå befintlig data. Delas av skapa/uppdatera."""
    falt = falt or {}
    listor = listor or {}
    data = current or {}
    if falt:
        try:
            data = clean_block_values(block_type, data, _prepare_values(block_type, falt))
        except KeyError as exc:
            raise OperationError(
                f"Okända fältnycklar: {exc}. Giltiga för {block_type}: "
                f"{', '.join(field_keys(block_type)) or '(inga)'}"
            ) from exc
    if listor:
        try:
            data = clean_block_rows(block_type, data, listor)
        except KeyError as exc:
            keys = [lst["key"] for lst in list_specs(block_type)]
            raise OperationError(
                f"Okänd listnyckel eller radfält: {exc}. Listor för {block_type}: "
                f"{', '.join(keys) or '(inga)'}"
            ) from exc

    # Blockfälten går inte genom run_form, så kontrollerna måste ske här också -
    # annars kan struktur försvinna tyst precis som i tjänsternas brödtext.
    for key, raw in falt.items():
        assert_nothing_lost(key, raw, str(_flat(data, key)))
    _assert_kept(block_type, falt, data)
    return data


def _prepare_block(user, block_id, falt=None, listor=None):
    block = _block(block_id)
    if not falt and not listor:
        raise OperationError("Ange minst ett fält eller en lista att ändra.")
    new_data = _build_data(block.block_type, block.data, falt, listor)

    keys = list(falt or {}) + list(listor or {})
    before = {k: str(_flat(block.data, k)) for k in keys}
    after = {k: str(_flat(new_data, k)) for k in keys}
    return Prepared(
        payload={"data": new_data, "andrade_falt": after},
        before=before,
        summary=f"Blocktext på {block.page.title}: {block.get_block_type_display()}",
        target=block,
    )


def _flat(data, key):
    cur = data or {}
    for part in key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return ""
    return cur


def _apply_block(user, payload, target):
    target.data = payload["data"]
    target.save(update_fields=["data", "updated_at"])
    return target


def _prepare_meta(user, slug, **values):
    page = _page(slug)
    changed = {k: v for k, v in values.items() if v is not None}
    if not changed:
        raise OperationError("Ange minst ett fält att ändra.")
    form, before = run_form(BlockPageForm, page, changed, META_FIELDS)
    return Prepared(
        payload=cleaned_subset(form, changed),
        before=before,
        summary=f"Sidinställningar: {page.title} ({', '.join(changed)})",
        target=page,
    )


def _apply_meta(user, payload, target):
    form, _ = run_form(BlockPageForm, target, payload, META_FIELDS)
    return form.save()


def _prepare_skapa_sida(user, titel, meta_description="", meta_title=""):
    values = {
        "title": titel,
        "meta_title": meta_title or "",
        "meta_description": meta_description or "",
        "is_published": False,
        "order": 100,
    }
    form, _ = run_form(BlockPageForm, BlockPage(), values, list(values))
    payload = cleaned_subset(
        form, ["title", "slug", "meta_title", "meta_description", "is_published", "order"]
    )
    # Formuläret av-dubblerar tyst (om-oss -> om-oss-2), så sluggen i payload
    # kolliderar aldrig. Det som ska stoppas är ett steg tidigare: att
    # modellen skapar en andra sida med samma titel utan att veta om den
    # första. Två nästan lika sidor är sällan det någon ville ha.
    from django.utils.text import slugify

    existing = BlockPage.objects.filter(slug=slugify(titel)).first()
    if existing is not None:
        raise OperationError(
            f"Det finns redan en sida '{existing.title}' på /{existing.slug}/. "
            f"Ändra den med uppdatera_sidmeta eller skapa_block, eller välj en "
            f"annan titel om du verkligen menar en ny sida."
        )
    # Sluggen står i sammanfattningen därför att den är sidans adress OCH
    # handtaget modellen måste skicka till skapa_block. Utan den syns den
    # först efter godkännandet, och blocken kan inte läggas i samma tur.
    return Prepared(
        payload=payload,
        summary=f"Ny sida: {titel} (adress /{payload['slug']}/, skapas opublicerad)",
    )


def _apply_skapa_sida(user, payload, target):
    form, _ = run_form(BlockPageForm, BlockPage(), payload, list(payload))
    return form.save()


def _prepare_skapa_block(job, user, sid_slug, blocktyp, falt=None, listor=None):
    page, pending = _resolve_page(job, sid_slug)
    if blocktyp not in BLOCK_EDIT_SCHEMA:
        raise OperationError(
            f"Okänd blocktyp: {blocktyp}. Giltiga: {', '.join(sorted(BLOCK_EDIT_SCHEMA))}"
        )
    data = _build_data(blocktyp, {}, falt, listor)
    title = page.title if page else pending.payload.get("title", sid_slug)
    # Slug, inte id: sidan kan sakna id ännu. Den slås upp vid apply, och
    # depends_on garanterar att den då hunnit skapas.
    return Prepared(
        payload={"page_slug": sid_slug, "block_type": blocktyp, "data": data},
        summary=f"Nytt {BLOCK_EDIT_SCHEMA[blocktyp]['label']}-block på {title}",
        target=page,
        depends_on=pending,
    )


def _apply_skapa_block(user, payload, target):
    from django.db.models import Max

    page_id = payload.get("page_id")  # äldre utkast bär id
    if page_id is None:
        slug = payload.get("page_slug")
        page = BlockPage.objects.filter(slug=slug).first()
        if page is None:
            raise OperationError(f"Sidan {slug} finns inte längre.")
        page_id = page.pk
    next_order = (Block.objects.filter(page_id=page_id).aggregate(m=Max("order"))["m"] or 0) + 1
    return Block.objects.create(
        page_id=page_id,
        block_type=payload["block_type"],
        data=payload["data"],
        order=next_order,
        is_visible=True,
    )


def _prepare_ordna(user, sid_slug, block_ids):
    page = _page(sid_slug)
    current = list(page.blocks.values_list("pk", flat=True))
    if sorted(block_ids) != sorted(current):
        raise OperationError(
            f"block_ids måste innehålla exakt sidans alla block-id, en gång var. "
            f"Sidan {sid_slug} har {current}. Du skickade {list(block_ids)}. "
            f"Block som bara finns som förslag har inget id ännu - godkänn dem "
            f"först, eller skapa dem i rätt ordning från början."
        )
    if block_ids == current:
        raise OperationError("Blocken ligger redan i den ordningen.")

    labels = {b.pk: b.get_block_type_display() for b in page.blocks.all()}
    return Prepared(
        payload={"block_ids": list(block_ids)},
        before={"ordning": " > ".join(labels[pk] for pk in current)},
        summary=(f"Ny blockordning på {page.title}: " + " > ".join(labels[pk] for pk in block_ids)),
        target=page,
    )


def _apply_ordna(user, payload, target):
    blocks = {b.pk: b for b in target.blocks.all()}
    for order, pk in enumerate(payload["block_ids"], start=1):
        block = blocks.get(pk)
        if block is None:
            raise OperationError(f"Blocket {pk} finns inte längre på sidan.")
        block.order = order
        block.save(update_fields=["order", "updated_at"])
    return target


def _prepare_synlig(user, block_id, synligt):
    block = _block(block_id)
    if block.is_visible == bool(synligt):
        raise OperationError(f"Blocket är redan {'synligt' if synligt else 'dolt'}.")
    return Prepared(
        payload={"is_visible": bool(synligt)},
        before={"is_visible": block.is_visible},
        summary=(
            f"{'Visar' if synligt else 'Döljer'} {block.get_block_type_display()}-"
            f"blocket på {block.page.title}"
        ),
        target=block,
    )


def _apply_synlig(user, payload, target):
    target.is_visible = payload["is_visible"]
    target.save(update_fields=["is_visible", "updated_at"])
    return target


def _katalog(user):
    """
    Blockkatalogen: vad varje blocktyp är, hur den ser ut och vad den tar.

    Modellen kan inte se sajten. Utan den här beskrivningen valde den block
    på namnet och gissade fältnycklar - eller anropade hamta_sida på en
    slumpsida för att härma. En källa, byggd ur schemat.
    """
    typer = []
    for key, schema in BLOCK_EDIT_SCHEMA.items():
        typer.append(
            {
                "typ": key,
                "namn": schema["label"],
                "beskrivning": schema["purpose"],
                "falt": [
                    {
                        "nyckel": spec["key"],
                        "typ": spec["type"],
                        "etikett": spec["label"],
                        **({"hjalp": spec["help"]} if spec.get("help") else {}),
                        **(
                            {"alternativ": [c[0] for c in spec["choices"]]}
                            if spec.get("choices")
                            else {}
                        ),
                        **(
                            {"kan_inte_sattas": _UNSETTABLE[spec["type"]]}
                            if spec["type"] in _UNSETTABLE
                            else {}
                        ),
                    }
                    for spec in schema["fields"]
                ],
                "listor": [
                    {
                        "nyckel": lst["key"],
                        "etikett": lst["label"],
                        "radform": (
                            "sträng"
                            if lst.get("simple")
                            else {f["key"]: f["type"] for f in lst["fields"]}
                        ),
                    }
                    for lst in schema.get("lists", [])
                ],
            }
        )
    return {
        "blocktyper": typer,
        "sa_byggs_en_sida": COMPOSITION_RULES,
        "det_du_inte_kan": [
            "Bilder - du kan inte se dem, så de väljs i mediebiblioteket av "
            "en människa. Skapa blocket ändå och be kunden lägga bilden.",
            "Sidans adress (slug) - den skapas ur titeln och kan inte ändras "
            "efteråt, för då slutar befintliga länkar fungera.",
            "Menyer - en ny sida måste läggas in i menyn manuellt av kunden.",
            "Radera - du kan dölja block (satt_block_synligt) och avpublicera "
            "sidor, aldrig ta bort något.",
            "Publicera - allt du föreslår väntar på kundens godkännande.",
        ],
    }


_S = {"type": "string"}

#: Radinnehåll: {listnyckel: [rad, ...]}. En rad är ett objekt med radfältens
#: nycklar - eller en ren sträng för de listor som bara har ett fält
#: (marquee, split.bullets). Båda formerna godtas, så modellen slipper hålla
#: reda på vilken lista som är "simple".
_LISTOR = {
    "type": "object",
    "additionalProperties": {
        "type": "array",
        "items": {
            "oneOf": [
                {"type": "object", "additionalProperties": {"type": "string"}},
                {"type": "string"},
            ]
        },
    },
}

register(
    Operation(
        name="lista_sidor",
        description="Lista alla blocksidor med slug, titel och publiceringsstatus.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        risk=Risk.READ,
        read=_lista,
    )
)
register(
    Operation(
        name="hamta_sida",
        description=(
            "Hämta en sidas alla block med block_id, blocktyp, giltiga fältnycklar "
            "och nuvarande innehåll. Anropa denna innan du ändrar ett block. Varje block visar "
            "har_bild."
        ),
        input_schema={
            "type": "object",
            "properties": {"slug": _S},
            "required": ["slug"],
            "additionalProperties": False,
        },
        risk=Risk.READ,
        read=_hamta,
    )
)
register(
    Operation(
        name="uppdatera_block",
        description=(
            "Föreslå nytt innehåll i ett block. 'falt' är enkla fältnycklar från "
            'hamta_sida, t.ex. {"title": "...", "link.label": "..."}. '
            "'listor' är blockets radinnehåll, t.ex. "
            '{"steps": [{"title": "Kontakt", "text": "..."}]}. En lista du '
            "skickar ERSÄTTER hela listan; fält du inte nämner behålls."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "block_id": {"type": "integer"},
                "falt": {"type": "object", "additionalProperties": {"type": "string"}},
                "listor": _LISTOR,
            },
            "required": ["block_id"],
            "additionalProperties": False,
        },
        risk=Risk.TEXT,
        prepare=_prepare_block,
        apply=_apply_block,
    )
)
register(
    Operation(
        name="uppdatera_sidmeta",
        description="Föreslå ny titel, metatitel eller metabeskrivning för en sida.",
        input_schema={
            "type": "object",
            "properties": {
                "slug": _S,
                "title": _S,
                "meta_title": _S,
                "meta_description": _S,
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
        risk=Risk.TEXT,
        prepare=lambda user, slug, **v: _prepare_meta(user, slug, **v),
        apply=_apply_meta,
    )
)
register(
    Operation(
        name="skapa_sida",
        description=(
            "Föreslå en ny blocksida. Sidan är TOM - den får innehåll först när "
            "du lägger block på den med skapa_block, och du kan göra det direkt "
            "i samma tur (blocken väntar på att sidan godkänns). En sida utan "
            "block är ingen sida. Läs hamta_blockkatalog först så du vet vilka "
            "block som finns och i vilken ordning de ska ligga; börja med hero "
            "och avsluta med bar. Skapas alltid OPUBLICERAD och hamnar inte i "
            "någon meny - säg det till kunden."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "titel": _S,
                "meta_title": _S,
                "meta_description": _S,
            },
            "required": ["titel"],
            "additionalProperties": False,
        },
        risk=Risk.BUSINESS,
        prepare=_prepare_skapa_sida,
        apply=_apply_skapa_sida,
    )
)
register(
    Operation(
        name="skapa_block",
        description=(
            "Föreslå ett nytt block SIST på en sida. Sidan får vara en du "
            "föreslagit i samma tur - blocket kopplas då till sidförslaget och "
            "godkänns tillsammans med det. Eftersom blocket alltid hamnar sist "
            "skapar du blocken i den ordning de ska stå; hero först, bar sist. "
            "'falt' är enkla fältnycklar, 'listor' är radinnehåll (t.ex. "
            '{"chips": [{"value": "99,9 %", "label": "Upptid"}]}) - flera '
            "blocktyper har allt sitt innehåll i listor och blir tomma utan "
            "dem. Kör hamta_blockkatalog för blocktypernas utseende och fält."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sid_slug": _S,
                "blocktyp": {"type": "string", "enum": sorted(BLOCK_EDIT_SCHEMA)},
                "falt": {"type": "object", "additionalProperties": {"type": "string"}},
                "listor": _LISTOR,
            },
            "required": ["sid_slug", "blocktyp"],
            "additionalProperties": False,
        },
        risk=Risk.TEXT,
        wants_job=True,
        prepare=_prepare_skapa_block,
        apply=_apply_skapa_block,
    )
)
register(
    Operation(
        name="hamta_blockkatalog",
        description=(
            "Alla blocktyper: hur de ser ut, vilka fält och listor de tar, "
            "reglerna för hur en sida byggs och vad du inte kan göra. Läs den "
            "INNAN du skapar en sida eller ett block - du kan inte se sajten, "
            "och blocktypens namn räcker inte för att veta hur den renderas."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        risk=Risk.READ,
        read=_katalog,
    )
)
register(
    Operation(
        name="ordna_block",
        description=(
            "Lägg om blockens ordning på en sida. Skicka sidans ALLA block-id i "
            "den ordning de ska visas, uppifrån och ner (hämta dem med "
            "hamta_sida). Block som bara finns som förslag har inget id ännu."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sid_slug": _S,
                "block_ids": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["sid_slug", "block_ids"],
            "additionalProperties": False,
        },
        risk=Risk.TEXT,
        prepare=_prepare_ordna,
        apply=_apply_ordna,
    )
)
register(
    Operation(
        name="satt_block_synligt",
        description=(
            "Visa eller dölj ett block. Ett dolt block ligger kvar med sitt "
            "innehåll men syns inte för besökare - det här är det närmaste du "
            "kommer att ta bort ett block."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "block_id": {"type": "integer"},
                "synligt": {"type": "boolean"},
            },
            "required": ["block_id", "synligt"],
            "additionalProperties": False,
        },
        risk=Risk.BUSINESS,
        prepare=_prepare_synlig,
        apply=_apply_synlig,
    )
)


def _prepare_publicerad(user, slug, publicerad):
    """Publicera eller avpublicera en blocksida."""
    page = _page(slug)
    return Prepared(
        payload={"is_published": bool(publicerad)},
        before={"is_published": page.is_published},
        summary=f"{'Publicerar' if publicerad else 'Avpublicerar'} sidan {page.title}",
        target=page,
    )


def _apply_publicerad(user, payload, target):
    target.is_published = payload["is_published"]
    target.save(update_fields=["is_published", "updated_at"])
    return target


register(
    Operation(
        name="satt_sida_publicerad",
        description=(
            "Publicera eller avpublicera en blocksida. En sida du skapar är "
            "opublicerad tills den publiceras - gör det när innehållet är på "
            "plats, annars ligger sidan osynlig."
        ),
        input_schema={
            "type": "object",
            "properties": {"slug": _S, "publicerad": {"type": "boolean"}},
            "required": ["slug", "publicerad"],
            "additionalProperties": False,
        },
        risk=Risk.BUSINESS,
        prepare=_prepare_publicerad,
        apply=_apply_publicerad,
    )
)
