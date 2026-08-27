"""Operationer för serviceområden. Validering och apply via AreaForm."""

from apps.areas.models import Area, AreaLevel, sort_areas
from apps.assistant.models import Risk
from apps.manage.forms import AreaForm

from .base import Operation, OperationError, Prepared, cleaned_subset, register, run_form

TEXT_FIELDS = ["heading", "intro", "body", "meta_title", "meta_description"]

_LEVELS = {"lan": AreaLevel.REGION, "kommun": AreaLevel.MUNICIPALITY, "ort": AreaLevel.DISTRICT}


# Tjänstekopplingar till orter finns MEDVETET INTE i verktygsytan
# (kundens beslut 2026-08-23). Kombinationssidorna renderar samma
# tjänstetext och samma ortstext med ortsnamnet inbytt i rubriken - 5
# tjänster x 252 områden vore 1 260 nästan identiska sidor, alltså doorway
# pages som Google straffar. En människa kan fortfarande koppla i /manage/,
# där omfattningen syns; en modell som gör det på uppmaning skalar
# misstaget. Återinför inte utan att först lösa unikt innehåll per
# kombination.
def _area(slug):
    area = Area.objects.filter(slug=slug).first()
    if area is None:
        raise OperationError(f"Okänt område: {slug}. Använd lista_omraden för att se alla slugs.")
    return area


#: Utan filter listas bara län och kommuner. Alla 250+ områden i ett svar är
#: ~10 000 tokens som modellen sällan behöver - orterna hämtas per kommun.
_LIST_LIMIT = 120


def _lista(user, niva=None, overordnad_slug=None, sok=None):
    qs = Area.objects.select_related("parent")
    if niva:
        level = _LEVELS.get(niva)
        if level is None:
            raise OperationError("niva måste vara lan, kommun eller ort.")
        qs = qs.filter(level=level)
    if overordnad_slug:
        qs = qs.filter(parent=_area(overordnad_slug))
    if sok:
        qs = qs.filter(name__icontains=sok)
    if not (niva or overordnad_slug or sok):
        # Standardvyn: överblicken, inte hela trädet.
        qs = qs.exclude(level=AreaLevel.DISTRICT)

    areas = sort_areas(qs)
    total = len(areas)
    rows = [
        {
            "slug": a.slug,
            "namn": a.name,
            "niva": a.get_level_display(),
            "overordnad": a.parent.slug if a.parent else None,
            "aktiv": a.is_active,
            "har_bild": a.image_id is not None,
            "har_text": bool(a.body),
        }
        for a in areas[:_LIST_LIMIT]
    ]
    result = {"antal": total, "omraden": rows}
    if total > _LIST_LIMIT:
        result["not"] = (
            f"Visar {_LIST_LIMIT} av {total}. Filtrera med overordnad_slug "
            f"eller sok för att se resten."
        )
    elif not (niva or overordnad_slug or sok):
        result["not"] = "Visar län och kommuner. Ange overordnad_slug för en kommuns orter."
    return result


def _hamta(user, slug):
    area = _area(slug)
    return {
        "slug": area.slug,
        "namn": area.name,
        "niva": area.get_level_display(),
        "overordnad": area.parent.slug if area.parent else None,
        "aktiv": area.is_active,
        # Både det satta värdet OCH det som faktiskt visas. Tidigare såg
        # modellen bara en tom "rubrik" och GISSADE att något genereras -
        # den gissade rätt, men en assistent ska inte behöva gissa.
        "rubrik": area.heading,
        "rubrik_som_visas": area.display_heading,
        "rubrik_autogenererad": not area.heading,
        "intro": area.intro,
        "body_html": area.body,
        "meta_title": area.meta_title,
        "meta_description": area.meta_description,
        # Kopplingarna: utan dem kunde modellen föreslå en koppling som
        # redan fanns, eller en FAQ till ett område som redan har en.
        "faq_sektion": area.faq_section.slug if area.faq_section_id else None,
        "bild": (
            {
                "filnamn": area.image.original_filename or area.image.file.name,
                "alt_text": area.image.alt_text,
            }
            if area.image_id
            else None
        ),
        "tjanster": [
            {
                "slug": link.service.slug,
                "namn": link.service.name,
                "egen_sida": link.has_own_page,
                "malgrupper": [a.name for a in link.audiences.all()] or "alla",
            }
            for link in area.area_services.select_related("service").prefetch_related("audiences")
        ],
        "grannomraden": [n.slug for n in area.neighbours.all()],
        "faq": [{"fraga": i.question, "svar": i.answer} for i in area.faq_items.all()],
        "tips": (
            "Variabler som {{ ort }}, {{ kommun }}, {{ lan }} och {{ phone }} "
            "ersätts automatiskt vid visning - använd dem i stället för "
            "hårdkodade namn. Tom rubrik är NORMALT: sidan visar då "
            "'Rörmokare i <ort>' automatiskt, se rubrik_som_visas. "
            "Är tjänstelistan tom ärvs den från närmaste överordnade område."
        ),
    }


def _prepare_text(user, slug, **values):
    area = _area(slug)
    changed = {k: v for k, v in values.items() if v is not None}
    if not changed:
        raise OperationError("Ange minst ett fält att ändra.")
    form, before = run_form(AreaForm, area, changed, TEXT_FIELDS)
    return Prepared(
        payload=cleaned_subset(form, changed),
        before=before,
        summary=f"Textändring: {area.name} ({', '.join(changed)})",
        target=area,
    )


def _apply_text(user, payload, target):
    form, _ = run_form(AreaForm, target, payload, TEXT_FIELDS)
    return form.save()


def _prepare_skapa(user, namn, niva, overordnad_slug=None, rubrik="", intro="", body=""):
    level = _LEVELS.get(niva)
    if level is None:
        raise OperationError("niva måste vara lan, kommun eller ort.")
    values = {
        "name": namn,
        "level": level,
        "heading": rubrik or "",
        "intro": intro or "",
        "body": body or "",
        "is_active": False,
        "order": 100,
    }
    if overordnad_slug:
        parent = _area(overordnad_slug)
        values["parent"] = parent.pk
    allowed = ["name", "level", "parent", "heading", "intro", "body", "is_active", "order"]
    form, _ = run_form(AreaForm, Area(), values, allowed)
    return Prepared(
        payload=cleaned_subset(form, values),
        summary=f"Nytt område: {namn} ({niva}, skapas inaktivt)",
    )


def _apply_skapa(user, payload, target):
    form, _ = run_form(AreaForm, Area(), payload, list(payload))
    return form.save()


def _prepare_aktiv(user, slug, aktiv):
    area = _area(slug)
    note = ""
    if not aktiv and area.children.exists():
        note = f" (döljer även {area.children.count()} underområden)"
    return Prepared(
        payload={"is_active": bool(aktiv)},
        before={"is_active": area.is_active},
        summary=f"{'Visa' if aktiv else 'Dölj'}: {area.name}{note}",
        target=area,
    )


def _apply_aktiv(user, payload, target):
    target.is_active = payload["is_active"]
    target.save(update_fields=["is_active", "updated_at"])
    return target


def _prepare_grannar(user, slug, grannar):
    """
    Ersätt områdets grannlista. Länkarna längst ner på ortssidan.

    Hela listan ersätts, som manage-vyn gör - delvisa uppdateringar ger
    tyst dubblering och en ordning ingen bad om.
    """
    area = _area(slug)
    if not isinstance(grannar, list):
        raise OperationError("Ange grannarna som en lista av slugs.")
    rows = list(Area.objects.filter(slug__in=grannar))
    missing = set(grannar) - {a.slug for a in rows}
    if missing:
        raise OperationError(f"Okända områden: {', '.join(sorted(missing))}.")
    if area.slug in grannar:
        raise OperationError("Ett område kan inte vara granne med sig självt.")
    return Prepared(
        payload={"granne_ids": [a.pk for a in rows]},
        before={"grannar": ", ".join(n.slug for n in area.neighbours.all()) or "inga"},
        summary=f"Grannområden för {area.name}: {len(rows)} st",
        target=area,
    )


def _apply_grannar(user, payload, target):
    target.neighbours.set(payload["granne_ids"])
    return target


_S = {"type": "string"}

register(
    Operation(
        name="lista_omraden",
        description=(
            "Lista serviceområden. Utan argument: alla län och kommuner. Ange "
            "overordnad_slug för en kommuns orter, niva för en nivå, eller sok för "
            "namnsökning. Sajten har 250+ områden - filtrera hellre än att lista allt. Varje rad "
            "visar har_bild och har_text."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "niva": {"type": "string", "enum": ["lan", "kommun", "ort"]},
                "overordnad_slug": _S,
                "sok": _S,
            },
            "additionalProperties": False,
        },
        risk=Risk.READ,
        read=_lista,
    )
)
register(
    Operation(
        name="hamta_omrade",
        description=(
            "Hämta ett områdes fullständiga innehåll inklusive FAQ. Innehåller "
            "bildens filnamn och alt-text om en bild finns, annars null."
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
        name="uppdatera_omrade_text",
        description=(
            "Föreslå ny text för ett område (rubrik, intro, body-HTML, metatitel, "
            "metabeskrivning). Blir ett utkast som kunden godkänner. Skriv unik, "
            "lokal text - aldrig generisk. Nämn aldrig var firman utgår ifrån.\n\n"
            "Body tillåter bara <p>, <br>, <strong>, <em> och <a>. Rubriker och "
            "listor STRYKS av saneringen och lämnar naken text - skriv löpande "
            "stycken i stället."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "slug": _S,
                "heading": _S,
                "intro": _S,
                "body": _S,
                "meta_title": _S,
                "meta_description": _S,
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
        risk=Risk.TEXT,
        prepare=lambda user, slug, **v: _prepare_text(user, slug, **v),
        apply=_apply_text,
    )
)
register(
    Operation(
        name="skapa_omrade",
        description=(
            "Föreslå ett nytt serviceområde. niva: lan | kommun | ort. Kommuner "
            "kräver overordnad_slug (länet), orter kräver overordnad_slug "
            "(kommunen). Skapas alltid INAKTIVT - kunden aktiverar när det är klart."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "namn": _S,
                "niva": {"type": "string", "enum": ["lan", "kommun", "ort"]},
                "overordnad_slug": _S,
                "rubrik": _S,
                "intro": _S,
                "body": _S,
            },
            "required": ["namn", "niva"],
            "additionalProperties": False,
        },
        risk=Risk.BUSINESS,
        prepare=_prepare_skapa,
        apply=_apply_skapa,
    )
)
register(
    Operation(
        name="satt_omrade_aktiv",
        description="Föreslå att ett område visas eller döljs (döljer även underområden).",
        input_schema={
            "type": "object",
            "properties": {
                "slug": _S,
                "aktiv": {"type": "boolean"},
            },
            "required": ["slug", "aktiv"],
            "additionalProperties": False,
        },
        risk=Risk.BUSINESS,
        prepare=_prepare_aktiv,
        apply=_apply_aktiv,
    )
)


def _prepare_koppla_faq(job, user, omrade_slug, faq_slug):
    """
    Koppla en FAQ-sektion till ett område.

    Sektionen får vara ett utkast från samma tur - då kopplas förslagen med
    depends_on och godkänns i ordning. Utan den här operationen kunde
    modellen skapa en FAQ men aldrig fästa den någonstans, vilket är precis
    vad som hände med Bromma (2026-08-23).
    """
    from apps.faq.models import FAQSection

    from .faq_ops import pending_section

    area = _area(omrade_slug)
    section = FAQSection.objects.filter(slug=faq_slug).first()
    pending = None if section else pending_section(job, faq_slug)
    if section is None and pending is None:
        known = ", ".join(FAQSection.objects.values_list("slug", flat=True))
        raise OperationError(f"Okänd FAQ-sektion: {faq_slug}. Kända: {known}")

    title = section.title if section else pending.payload.get("title", faq_slug)
    before_title = area.faq_section.title if area.faq_section_id else "ingen"
    return Prepared(
        payload={"faq_slug": faq_slug, "andrade_falt": {"FAQ-sektion": title}},
        before={"FAQ-sektion": before_title},
        summary=f"Kopplar FAQ '{title}' till {area.name}",
        target=area,
        depends_on=pending,
    )


def _apply_koppla_faq(user, payload, target):
    from apps.faq.models import FAQSection

    section = FAQSection.objects.filter(slug=payload["faq_slug"]).first()
    if section is None:
        raise OperationError(
            f"FAQ-sektionen {payload['faq_slug']} finns inte. Godkänn sektionen först."
        )
    target.faq_section = section
    target.save(update_fields=["faq_section", "updated_at"])
    return target


register(
    Operation(
        name="koppla_faq_till_omrade",
        description=(
            "Koppla en FAQ-sektion till ett serviceområde - frågorna visas då "
            "på ortssidan. Sektionen får vara en du föreslagit i samma tur."
        ),
        input_schema={
            "type": "object",
            "properties": {"omrade_slug": _S, "faq_slug": _S},
            "required": ["omrade_slug", "faq_slug"],
            "additionalProperties": False,
        },
        risk=Risk.BUSINESS,
        wants_job=True,
        prepare=_prepare_koppla_faq,
        apply=_apply_koppla_faq,
    )
)


register(
    Operation(
        name="satt_grannomraden",
        description=(
            "Sätt vilka områden som länkas som grannar längst ner på "
            "ortssidan. HELA listan ersätts, så skicka med alla du vill "
            "behålla. Välj områden som faktiskt gränsar till varandra - "
            "listan är intern länkning, inte en katalog."
        ),
        input_schema={
            "type": "object",
            "properties": {"slug": _S, "grannar": {"type": "array", "items": _S}},
            "required": ["slug", "grannar"],
            "additionalProperties": False,
        },
        risk=Risk.BUSINESS,
        prepare=_prepare_grannar,
        apply=_apply_grannar,
    )
)
