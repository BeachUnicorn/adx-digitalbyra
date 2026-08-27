"""Operationer för blocksidor. Blockdata saneras via block_schema."""

from apps.assistant.models import Risk
from apps.manage.block_schema import (
    BLOCK_EDIT_SCHEMA,
    clean_block_values,
    field_keys,
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


def _page(slug):
    page = BlockPage.objects.filter(slug=slug).first()
    if page is None:
        known = ", ".join(BlockPage.objects.values_list("slug", flat=True))
        raise OperationError(f"Okänd sida: {slug}. Kända: {known}")
    return page


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
    for block in page.blocks.all():
        schema = BLOCK_EDIT_SCHEMA.get(block.block_type)
        blocks.append(
            {
                "block_id": block.pk,
                "typ": block.block_type,
                "typ_namn": schema["label"] if schema else block.block_type,
                "synligt": block.is_visible,
                "redigerbar": schema is not None,
                "faltnycklar": field_keys(block.block_type),
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


def _prepare_block(user, block_id, falt):
    block = _block(block_id)
    if not falt:
        raise OperationError("Ange minst ett fält att ändra.")
    try:
        new_data = clean_block_values(block.block_type, block.data, falt)
    except KeyError as exc:
        raise OperationError(
            f"Okända fältnycklar: {exc}. Giltiga för {block.block_type}: "
            f"{', '.join(field_keys(block.block_type))}"
        ) from exc

    before = {k: str(_flat(block.data, k)) for k in falt}
    after = {k: str(_flat(new_data, k)) for k in falt}

    # Blockfälten går inte genom run_form, så kontrollen måste ske här också -
    # annars kan struktur försvinna tyst precis som i tjänsternas brödtext.
    for key, raw in falt.items():
        assert_nothing_lost(key, raw, after.get(key))
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


def _prepare_skapa_sida(user, titel, meta_description=""):
    values = {
        "title": titel,
        "meta_description": meta_description or "",
        "is_published": False,
        "order": 100,
    }
    form, _ = run_form(BlockPageForm, BlockPage(), values, list(values))
    return Prepared(
        payload=cleaned_subset(
            form, ["title", "slug", "meta_description", "is_published", "order"]
        ),
        summary=f"Ny sida: {titel} (skapas opublicerad)",
    )


def _apply_skapa_sida(user, payload, target):
    form, _ = run_form(BlockPageForm, BlockPage(), payload, list(payload))
    return form.save()


def _prepare_skapa_block(user, sid_slug, blocktyp, falt=None):
    page = _page(sid_slug)
    if blocktyp not in BLOCK_EDIT_SCHEMA:
        raise OperationError(
            f"Okänd blocktyp: {blocktyp}. Giltiga: {', '.join(sorted(BLOCK_EDIT_SCHEMA))}"
        )
    try:
        data = clean_block_values(blocktyp, {}, falt or {})
    except KeyError as exc:
        raise OperationError(
            f"Okända fältnycklar: {exc}. Giltiga för {blocktyp}: {', '.join(field_keys(blocktyp))}"
        ) from exc
    for key, raw in (falt or {}).items():
        assert_nothing_lost(key, raw, _flat(data, key))
    return Prepared(
        payload={"page_id": page.pk, "block_type": blocktyp, "data": data},
        summary=f"Nytt {BLOCK_EDIT_SCHEMA[blocktyp]['label']}-block på {page.title}",
        target=page,
    )


def _apply_skapa_block(user, payload, target):
    from django.db.models import Max

    page_id = payload["page_id"]
    next_order = (Block.objects.filter(page_id=page_id).aggregate(m=Max("order"))["m"] or 0) + 1
    return Block.objects.create(
        page_id=page_id,
        block_type=payload["block_type"],
        data=payload["data"],
        order=next_order,
        is_visible=True,
    )


_S = {"type": "string"}

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
            "Föreslå ny text i ett block. 'falt' är ett objekt med fältnycklar från "
            'hamta_sida, t.ex. {"heading": "...", "link.label": "..."}. Bara '
            "de nycklar du anger ändras; övriga behålls."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "block_id": {"type": "integer"},
                "falt": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["block_id", "falt"],
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
            "Föreslå en ny blocksida. Skapas alltid OPUBLICERAD. Lägg till innehåll "
            "med skapa_block efteråt."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "titel": _S,
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
            "Föreslå ett nytt block sist på en sida. Giltiga blocktyper: "
            + ", ".join(sorted(BLOCK_EDIT_SCHEMA))
            + ". Använd hamta_sida på en sida som redan har blocktypen för att se "
            "vilka fältnycklar den tar."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sid_slug": _S,
                "blocktyp": {"type": "string", "enum": sorted(BLOCK_EDIT_SCHEMA)},
                "falt": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["sid_slug", "blocktyp"],
            "additionalProperties": False,
        },
        risk=Risk.TEXT,
        prepare=_prepare_skapa_block,
        apply=_apply_skapa_block,
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
