"""
Länkintegritet (mönsterkatalogen §2, mönstret Giovanni själv satte i
katalogen): inga döda länkar får existera i tysthet. Tre delar:

1. **Referens före sträng.** Menyposter seedas med sid-FK (MenuItem.page),
   inte URL-strängar - länken överlever slug-byten, och en avpublicerad
   sida är upptäckbar. Blockens länkar är innehåll (transkriberade ur
   designguiden) och förblir strängar - men de bevakas av resolvern nedan.

2. **Resolvern vet om målet lever.** iter_link_usages() räknar upp VARJE
   lagrad länk - menyposter och blockdata - och blockens URL-fält hittas
   via BLOCK_EDIT_SCHEMA (schemat är enda sanningen om vilka fält som bär
   länkar; ingen handspeglad lista som kan glömma ett fält).
   resolve_status() dömer varje mål: OK, MISSING (ingen rutt/sida),
   UNPUBLISHED (målet finns men är avpublicerat/inaktivt) eller EXTERNAL
   (kontrolleras inte utan att hämtas).

3. **Larmet.** dead_links() driver räknaren på /manage/-översikten och
   länkrapporten - ägaren ser varje trasig länk med plats och mål innan
   någon besökare gör det. Menyer döljer dessutom poster vars sida är
   avpublicerad redan vid rendering (MenuItem.is_alive).
"""

from dataclasses import dataclass, field

from django.urls import Resolver404, resolve

OK = "ok"
MISSING = "missing"
UNPUBLISHED = "unpublished"
EXTERNAL = "external"
SKIPPED = "skipped"  # tomt, ankare - inget att döma

_EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "tel:")


@dataclass
class LinkUsage:
    location: str  # klarspråk: var länken bor ("Huvudmenyn", 'Sidan "Paket"')
    label: str  # den klickbara texten
    url: str
    status: str = OK
    edit_hint: str = ""  # /manage/-vägen där den lagas
    detail: str = ""  # t.ex. vilket blockfält


def resolve_status(url):
    """Döm ett lagrat länkvärde. Databasmedvetet: en rutt som matchar men
    vars sida är avpublicerad är inte OK - besökaren får 404."""
    value = (url or "").strip()
    if not value or value.startswith("#"):
        return SKIPPED
    if value.startswith(_EXTERNAL_PREFIXES):
        return EXTERNAL
    path = value.split("?")[0].split("#")[0]
    if not path.startswith("/"):
        return MISSING
    if not path.endswith("/") and "." not in path.rsplit("/", 1)[-1]:
        path += "/"
    try:
        match = resolve(path)
    except Resolver404:
        return MISSING

    view_name = getattr(match.func, "__name__", "")
    if view_name == "page_detail":
        from apps.website.models import BlockPage

        page = BlockPage.objects.filter(slug=match.kwargs.get("slug")).first()
        if page is None:
            return MISSING
        return OK if page.is_published else UNPUBLISHED
    if view_name == "area_detail":
        from apps.areas.models import Area

        area = Area.objects.filter(slug=match.kwargs.get("slug")).first()
        if area is None:
            return MISSING
        return OK if area.is_active else UNPUBLISHED
    return OK


def _schema_url_fields(block_type):
    """(nyckel, är_listfält, listnyckel) för varje url-fält en blocktyp bär -
    läst ur schemat, aldrig ur en handskriven lista."""
    from apps.manage.block_schema import BLOCK_EDIT_SCHEMA

    schema = BLOCK_EDIT_SCHEMA.get(block_type) or {}
    for spec in schema.get("fields", []):
        if spec["type"] == "url":
            yield spec["key"], None
    for lst in schema.get("lists", []):
        for spec in lst["fields"]:
            if spec["type"] == "url":
                yield spec["key"], lst["key"]


def _nested_get(data, dotted, default=""):
    current = data
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return default
        current = current.get(part, default)
    return current if isinstance(current, str) else default


def iter_link_usages():
    """Varje lagrad länk på sajten: menyposter + blockdatans url-fält."""
    from apps.website.models import Block, Menu

    for menu in Menu.objects.prefetch_related("items__page"):
        where = (
            "Huvudmenyn" if menu.location == "header" else f"Sidfoten: {menu.heading or menu.name}"
        )
        for item in menu.items.all():
            if not item.is_visible:
                continue
            if item.page_id:
                status = OK if (item.page and item.page.is_published) else UNPUBLISHED
                yield LinkUsage(
                    location=where,
                    label=item.label,
                    url=item.get_url() or "",
                    status=status,
                    edit_hint="/manage/menyer/",
                )
            elif item.url:
                yield LinkUsage(
                    location=where,
                    label=item.label,
                    url=item.url,
                    status=resolve_status(item.url),
                    edit_hint="/manage/menyer/",
                )

    for block in Block.objects.select_related("page").filter(is_visible=True):
        data = block.data or {}
        for key, list_key in _schema_url_fields(block.block_type):
            if list_key:
                for i, row in enumerate(data.get(list_key) or []):
                    url = _nested_get(row, key) if isinstance(row, dict) else ""
                    if url:
                        yield LinkUsage(
                            location=f'Sidan "{block.page.title}"',
                            label=str(row.get("label") or row.get("title") or key),
                            url=url,
                            status=resolve_status(url),
                            edit_hint=f"/manage/blocks/{block.pk}/",
                            detail=f"{block.block_type}: {list_key}[{i}].{key}",
                        )
            else:
                url = _nested_get(data, key)
                if url:
                    yield LinkUsage(
                        location=f'Sidan "{block.page.title}"',
                        label=_nested_get(data, key.rsplit(".", 1)[0] + ".label")
                        or data.get("label", "")
                        or block.block_type,
                        url=url,
                        status=resolve_status(url),
                        edit_hint=f"/manage/blocks/{block.pk}/",
                        detail=f"{block.block_type}: {key}",
                    )


def dead_links():
    """Alla länkar vars mål inte fungerar - driver larmet på översikten."""
    return [u for u in iter_link_usages() if u.status in (MISSING, UNPUBLISHED)]
