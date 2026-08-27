"""
Länksystemet, hela vägen (mönsterkatalogen §2 - mönstret Giovanni själv
satte i katalogen): en intern länk lagras ALDRIG som adress.

Beskrivare i blockdata::

    {"kind": "page", "id": 3}        en sida - id, inte slug: länken
                                     ÖVERLEVER att sidan byter webbadress
    {"kind": "area", "id": 7}        en stadssida
    {"kind": "areas_index"}          stadsöversikten
    {"kind": "email"}                sajtens e-post (mailto:)
    {"kind": "phone"}                sajtens telefon (tel:)
    {"kind": "external", "url": …}   extern adress - kontrolleras inte
    {"kind": "path", "path": …}      ärlig flyktväg för interna rutter
                                     utanför sidsystemet (t.ex. /forfragan/)

Adressen beräknas vid rendering av resolve_link(), som också dömer målet:
OK, UNPUBLISHED (finns men avpublicerad/inaktiv), MISSING (raden borta).
Blockmallarna renderar via {% resolve_link %} och DÖLJER icke-ok-länkar
för besökare; ägaren larmas i stället via dead_links() på /manage/.

Legacy: överallt där en beskrivare väntas accepteras en rå sträng och körs
genom parse_href() först - gamla rader fortsätter rendera tills de sparas
om. Menyposterna har sitt eget referenssystem (MenuItem.page-FK).
"""

from dataclasses import dataclass

from django.urls import Resolver404, resolve, reverse

OK = "ok"
MISSING = "missing"
UNPUBLISHED = "unpublished"
EXTERNAL = "external"
SKIPPED = "skipped"  # tomt/okänt - inget att döma, inget att rendera

_EXTERNAL_PREFIXES = ("http://", "https://")


@dataclass
class ResolvedLink:
    href: str = ""
    status: str = SKIPPED

    @property
    def alive(self):
        """Får länken visas för besökare?"""
        return self.status in (OK, EXTERNAL) and bool(self.href)


@dataclass
class LinkUsage:
    location: str  # klarspråk: var länken bor
    label: str  # den klickbara texten
    target: str  # mänskligt läsbart mål (href eller beskrivning)
    status: str = OK
    edit_hint: str = ""  # /manage/-vägen där den lagas
    detail: str = ""  # t.ex. vilket blockfält


def parse_href(value):
    """Rå sträng -> beskrivare. Interna sökvägar slås upp till id-referens;
    det som inte känns igen blir en ärlig path/external-beskrivare."""
    value = (value or "").strip()
    if not value or value.startswith("#"):
        return None
    if value.startswith(_EXTERNAL_PREFIXES):
        return {"kind": "external", "url": value}
    if value.startswith("mailto:"):
        return {"kind": "email", "address": value[7:]}
    if value.startswith("tel:"):
        return {"kind": "phone", "number": value[4:]}
    if not value.startswith("/"):
        return {"kind": "external", "url": f"https://{value}"}

    path = value.split("?")[0].split("#")[0]
    if not path.endswith("/"):
        path += "/"

    from apps.areas.models import Area
    from apps.website.models import BlockPage

    if path == "/digitalbyra/":
        return {"kind": "areas_index"}
    if path.startswith("/digitalbyra/"):
        slug = path.strip("/").split("/")[-1]
        area = Area.objects.filter(slug=slug).first()
        if area:
            return {"kind": "area", "id": area.pk}
        return {"kind": "path", "path": value}
    if path == "/":
        home = _homepage()
        if home:
            return {"kind": "page", "id": home.pk}
        return {"kind": "path", "path": "/"}

    slug = path.strip("/")
    if "/" not in slug:
        page = BlockPage.objects.filter(slug=slug).first()
        if page:
            return {"kind": "page", "id": page.pk}
    return {"kind": "path", "path": value}


def _homepage():
    from apps.website.models import SiteSettings

    return SiteSettings.load().homepage


def _resolve_path_status(path):
    """Döm en rå intern sökväg (flyktvägen "path"). Ruttmatchning räcker
    inte - catch-all-rutten <slug>/ matchar allt, så vyer som slår upp
    objekt måste få sina objekt kontrollerade."""
    clean = path.split("?")[0].split("#")[0]
    if not clean.endswith("/") and "." not in clean.rsplit("/", 1)[-1]:
        clean += "/"
    try:
        match = resolve(clean)
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


def resolve_link(value):
    """Beskrivare (eller legacy-sträng) -> ResolvedLink med href + status."""
    if isinstance(value, str):
        value = parse_href(value)
    if not isinstance(value, dict):
        return ResolvedLink()

    kind = value.get("kind", "")

    if kind == "page":
        from apps.website.models import BlockPage

        page = BlockPage.objects.filter(pk=value.get("id")).first()
        if page is None:
            return ResolvedLink(status=MISSING)
        home = _homepage()
        href = "/" if home and home.pk == page.pk else page.get_absolute_url()
        return ResolvedLink(href=href, status=OK if page.is_published else UNPUBLISHED)

    if kind == "area":
        from apps.areas.models import Area

        area = Area.objects.filter(pk=value.get("id")).first()
        if area is None:
            return ResolvedLink(status=MISSING)
        return ResolvedLink(
            href=area.get_absolute_url(), status=OK if area.is_active else UNPUBLISHED
        )

    if kind == "areas_index":
        return ResolvedLink(href=reverse("areas:area_list"), status=OK)

    if kind == "email":
        from apps.website.models import SiteSettings

        address = value.get("address") or SiteSettings.load().email
        return ResolvedLink(href=f"mailto:{address}", status=OK) if address else ResolvedLink()

    if kind == "phone":
        from apps.website.models import SiteSettings

        number = value.get("number") or SiteSettings.load().phone
        return ResolvedLink(href=f"tel:{number}", status=OK) if number else ResolvedLink()

    if kind == "external":
        url = value.get("url", "")
        return ResolvedLink(href=url, status=EXTERNAL) if url else ResolvedLink()

    if kind == "path":
        path = value.get("path", "")
        if not path:
            return ResolvedLink()
        return ResolvedLink(href=path, status=_resolve_path_status(path))

    return ResolvedLink()


def describe_target(value):
    """Mänskligt läsbart mål för länkrapporten."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    kind = value.get("kind", "?")
    if kind == "page":
        from apps.website.models import BlockPage

        page = BlockPage.objects.filter(pk=value.get("id")).first()
        return f"Sidan: {page.title}" if page else f"Sida #{value.get('id')} (borta)"
    if kind == "area":
        from apps.areas.models import Area

        area = Area.objects.filter(pk=value.get("id")).first()
        return f"Staden: {area.name}" if area else f"Stad #{value.get('id')} (borta)"
    labels = {"areas_index": "Stadsöversikten", "email": "E-post", "phone": "Telefon"}
    if kind in labels:
        return labels[kind]
    return value.get("url") or value.get("path") or kind


def _schema_url_fields(block_type):
    """(nyckel, listnyckel) för varje länkfält en blocktyp bär - läst ur
    schemat, aldrig ur en handskriven lista."""
    from apps.manage.block_schema import BLOCK_EDIT_SCHEMA

    schema = BLOCK_EDIT_SCHEMA.get(block_type) or {}
    for spec in schema.get("fields", []):
        if spec["type"] == "link":
            yield spec["key"], None
    for lst in schema.get("lists", []):
        for spec in lst["fields"]:
            if spec["type"] == "link":
                yield spec["key"], lst["key"]


def _nested_get(data, dotted, default=""):
    current = data
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return default
        current = current.get(part, default)
    return current


def iter_link_usages():
    """Varje lagrad länk på sajten: menyposter + blockdatans länkfält."""
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
                    target=item.get_url() or f"Sidan: {item.page}",
                    status=status,
                    edit_hint="/manage/menus/",
                )
            elif item.url:
                resolved = resolve_link({"kind": "path", "path": item.url})
                yield LinkUsage(
                    location=where,
                    label=item.label,
                    target=item.url,
                    status=resolved.status,
                    edit_hint="/manage/menus/",
                )

    for block in Block.objects.select_related("page").filter(is_visible=True):
        data = block.data or {}
        for key, list_key in _schema_url_fields(block.block_type):
            if list_key:
                for i, row in enumerate(data.get(list_key) or []):
                    value = row.get(key) if isinstance(row, dict) else None
                    if value:
                        yield LinkUsage(
                            location=f'Sidan "{block.page.title}"',
                            label=str(row.get("label") or row.get("title") or key),
                            target=describe_target(value),
                            status=resolve_link(value).status,
                            edit_hint=f"/manage/blocks/{block.pk}/",
                            detail=f"{block.block_type}: {list_key}[{i}].{key}",
                        )
            else:
                value = _nested_get(data, key, None)
                if value:
                    label = (
                        _nested_get(data, key.rsplit(".", 1)[0] + ".label")
                        if "." in key
                        else data.get("label", "")
                    )
                    yield LinkUsage(
                        location=f'Sidan "{block.page.title}"',
                        label=str(label or block.block_type),
                        target=describe_target(value),
                        status=resolve_link(value).status,
                        edit_hint=f"/manage/blocks/{block.pk}/",
                        detail=f"{block.block_type}: {key}",
                    )


def dead_links():
    """Alla länkar vars mål inte fungerar - driver larmet på översikten."""
    return [u for u in iter_link_usages() if u.status in (MISSING, UNPUBLISHED)]
