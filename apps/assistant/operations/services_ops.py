"""Operationer för tjänster. Validering och apply via ServiceForm - samma
kodväg som /manage/services/."""

from apps.assistant.models import Risk
from apps.common.security import sanitize_plain_text
from apps.manage.forms import ServiceForm
from apps.services.models import Audience, Service, ServiceCategory, ServiceStep

from .base import (
    Operation,
    OperationError,
    Prepared,
    assert_nothing_lost,
    cleaned_subset,
    register,
    run_form,
)

TEXT_FIELDS = ["name", "description", "body"]

#: Vad `body` faktiskt överlever saneringen som. Står i verktygsbeskrivningen
#: för att modellen annars skriver rubriker och punktlistor som strippas till
#: nakna textrader - den ser aldrig resultatet, så den kan inte upptäcka det.
BODY_HTML_NOTE = (
    "Body tillåter bara <p>, <br>, <strong>, <em> och <a>. Rubriker (<h2>) och "
    "listor (<ul>, <li>) STRYKS av saneringen och lämnar naken text. Skriv "
    "arbetsgången som steg med satt_tjanst_steg i stället, aldrig som en lista "
    "i body."
)


def _service(slug):
    svc = Service.objects.filter(slug=slug).first()
    if svc is None:
        known = ", ".join(Service.objects.values_list("slug", flat=True))
        raise OperationError(f"Okänd tjänst: {slug}. Kända: {known}")
    return svc


def _row(svc):
    return {
        "slug": svc.slug,
        "namn": svc.name,
        "kategori": svc.category.name if svc.category else None,
        "aktiv": svc.is_active,
        # Bara OM en bild finns, inte vilken. Modellen kan inte se bilder,
        # men "vilka tjänster saknar bild?" är en rimlig fråga att kunna
        # svara på - och den kunde den inte alls (2026-08-23).
        "har_bild": svc.image_id is not None,
        "url": svc.get_absolute_url(),
    }


def _lista(user):
    return {
        "tjanster": [_row(s) for s in Service.objects.select_related("category")],
        "kategorier": [
            {
                "slug": c.slug,
                "namn": c.name,
                "aktiv": c.is_active,
                "har_bild": c.image_id is not None,
                "antal_tjanster": c.services.count(),
            }
            for c in ServiceCategory.objects.all()
        ],
    }


def _hamta(user, slug):
    svc = _service(slug)
    data = _row(svc)
    data.update(
        {
            "beskrivning": svc.description,
            "body_html": svc.body,
            "steg": [{"rubrik": s.title, "beskrivning": s.description} for s in svc.steps.all()],
            # Kopplingarna, av samma skäl som för områden: utan dem kan
            # modellen föreslå en FAQ till en tjänst som redan har en.
            "faq_sektion": svc.faq_section.slug if svc.faq_section_id else None,
            "bild": (
                {
                    "filnamn": svc.image.original_filename or svc.image.file.name,
                    "alt_text": svc.image.alt_text,
                }
                if svc.image_id
                else None
            ),
            "malgrupper": [a.name for a in svc.audiences.all()],
            "omraden": [link.area.slug for link in svc.area_links.select_related("area")],
            "tips": BODY_HTML_NOTE,
        }
    )
    return data


def _prepare_text(user, slug, **values):
    svc = _service(slug)
    changed = {k: v for k, v in values.items() if v is not None}
    if not changed:
        raise OperationError("Ange minst ett fält att ändra.")
    form, before = run_form(ServiceForm, svc, changed, TEXT_FIELDS)
    return Prepared(
        payload=cleaned_subset(form, changed),
        before=before,
        summary=f"Textändring: {svc.name} ({', '.join(changed)})",
        target=svc,
    )


def _apply_form_update(form_class, allowed):
    def _apply(user, payload, target):
        form, _ = run_form(form_class, target, payload, allowed)
        return form.save()

    return _apply


def _clean_steps(steg):
    """
    Validera och sanera en arbetsgång. Delas av satt_tjanst_steg och
    skapa_tjanst - stegen ska hålla samma kvalitet oavsett vilken väg de
    kommer in.
    """
    if not isinstance(steg, list) or not steg:
        raise OperationError("Ange minst ett steg.")
    if len(steg) > 12:
        raise OperationError("Högst 12 steg. Slå ihop de kortaste.")

    rows = []
    for i, item in enumerate(steg, start=1):
        title = sanitize_plain_text((item or {}).get("rubrik", ""), max_length=200)
        if not title:
            raise OperationError(f"Steg {i} saknar rubrik.")
        desc = sanitize_plain_text((item or {}).get("beskrivning", ""), max_length=300)
        assert_nothing_lost(f"steg {i} rubrik", (item or {}).get("rubrik", ""), title)
        assert_nothing_lost(f"steg {i} beskrivning", (item or {}).get("beskrivning", ""), desc)
        rows.append({"title": title, "description": desc})
    return rows


def _steps_summary(rows):
    return "\n".join(
        f"{r['title']}: {r['description']}" if r.get("description") else r["title"] for r in rows
    )


def _prepare_steg(user, slug, steg):
    """
    Ersätt tjänstens arbetsgång ("Så går det till") med en ny lista.

    Hela listan ersätts, precis som manage-vyn gör - att slå ihop delvisa
    listor ger tyst dubblering och en ordning ingen bad om.
    """
    svc = _service(slug)
    rows = _clean_steps(steg)

    before = {
        "steg": "\n".join(
            f"{s.title}: {s.description}" if s.description else s.title for s in svc.steps.all()
        )
    }
    after = {"steg": _steps_summary(rows)}
    return Prepared(
        payload={"steg": rows, "andrade_falt": after},
        before=before,
        summary=f"Arbetsgång för {svc.name}: {len(rows)} steg",
        target=svc,
    )


def _apply_steg(user, payload, target):
    # Rensa och skapa om, samma sätt som _save_steps i manage-vyn.
    target.steps.all().delete()
    for order, row in enumerate(payload["steg"]):
        ServiceStep.objects.create(
            service=target,
            title=row["title"],
            description=row.get("description", ""),
            order=order,
        )
    return target


def _prepare_aktiv(user, slug, aktiv):
    svc = _service(slug)
    return Prepared(
        payload={"is_active": bool(aktiv)},
        before={"is_active": svc.is_active},
        summary=f"{'Aktivera' if aktiv else 'Avaktivera'}: {svc.name}",
        target=svc,
    )


def _apply_aktiv(user, payload, target):
    target.is_active = payload["is_active"]
    target.save(update_fields=["is_active", "updated_at"])
    return target


def _prepare_skapa(user, namn, steg, kategori_slug=None, beskrivning="", body=""):
    """
    En komplett tjänst i ett enda förslag.

    `steg` är obligatoriskt i schemat, inte en uppmaning i beskrivningen:
    modellen skapade annars tjänster helt utan arbetsgång trots att
    instruktionen bad om den. Ett krav i schemat går inte att hoppa över.

    Tjänsten skapas AKTIV. Den skapades tidigare inaktiv "tills innehållet
    är klart", men det innebar att kunden godkände ett förslag och sedan
    fick leta upp tjänsten för att tända den - ett dolt extrasteg. Nu
    innehåller förslaget både text och arbetsgång, så det som godkänns är
    en färdig tjänst. Vill man ändå ha den dold finns satt_tjanst_aktiv.
    """
    rows = _clean_steps(steg)
    values = {
        "name": namn,
        "description": beskrivning or "",
        "body": body or "",
        "is_active": True,
        "order": 100,
    }
    if kategori_slug:
        cat = ServiceCategory.objects.filter(slug=kategori_slug).first()
        if cat is None:
            known = ", ".join(ServiceCategory.objects.values_list("slug", flat=True))
            raise OperationError(f"Okänd kategori: {kategori_slug}. Kända: {known}")
        values["category"] = cat.pk
    allowed = ["name", "description", "body", "category", "is_active", "order"]
    form, _ = run_form(ServiceForm, Service(), values, allowed)
    payload = cleaned_subset(form, values)
    payload["steg"] = rows
    payload["andrade_falt"] = {
        "namn": namn,
        "beskrivning": values["description"],
        "arbetsgång": _steps_summary(rows),
    }
    return Prepared(
        payload=payload,
        summary=f"Ny tjänst: {namn} ({len(rows)} steg, alla målgrupper)",
    )


def _apply_skapa(user, payload, target):
    payload = dict(payload)
    rows = payload.pop("steg", [])
    payload.pop("andrade_falt", None)
    form, _ = run_form(ServiceForm, Service(), payload, list(payload))
    service = form.save()
    for order, row in enumerate(rows):
        ServiceStep.objects.create(
            service=service,
            title=row["title"],
            description=row.get("description", ""),
            order=order,
        )
    # Alla aktiva målgrupper kopplas från start (kundens önskemål 2026-08-21):
    # en ny tjänst gäller i praktiken alltid alla, och en tom koppling gör
    # tjänsten osynlig på målgruppssidorna utan att någon förstår varför.
    # Att ta BORT en målgrupp är ett medvetet undantag och görs i /manage/.
    service.audiences.set(Audience.objects.filter(is_active=True))
    return service


_S = {"type": "string"}

register(
    Operation(
        name="lista_tjanster",
        description=(
            "Lista alla tjänster med slug, namn, kategori och status. Visar även "
            "om varje tjänst och kategori HAR en bild (har_bild) - använd det för "
            "frågor om bilder saknas."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        risk=Risk.READ,
        read=_lista,
    )
)
register(
    Operation(
        name="hamta_tjanst",
        description=(
            "Hämta en tjänsts fullständiga innehåll (text och steg). Innehåller "
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
        name="uppdatera_tjanst_text",
        description=(
            "Föreslå ny text för en tjänst (namn, beskrivning och/eller body-HTML). "
            "Blir ett utkast som kunden godkänner.\n\n" + BODY_HTML_NOTE
        ),
        input_schema={
            "type": "object",
            "properties": {
                "slug": _S,
                "name": _S,
                "description": _S,
                "body": _S,
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
        risk=Risk.TEXT,
        prepare=lambda user, slug, **v: _prepare_text(user, slug, **v),
        apply=_apply_form_update(ServiceForm, TEXT_FIELDS),
    )
)
register(
    Operation(
        name="satt_tjanst_steg",
        description=(
            'Sätt tjänstens arbetsgång - det som visas som "Så går det till". '
            "Använd ALLTID den här i stället för att skriva en punktlista i body; "
            "listor stryks av saneringen. Hela listan ersätts, så skicka med alla "
            "steg du vill ha kvar. Max 12 steg."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "slug": _S,
                "steg": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"rubrik": _S, "beskrivning": _S},
                        "required": ["rubrik"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["slug", "steg"],
            "additionalProperties": False,
        },
        risk=Risk.TEXT,
        prepare=_prepare_steg,
        apply=_apply_steg,
    )
)
register(
    Operation(
        name="satt_tjanst_aktiv",
        description="Föreslå att en tjänst visas (aktiv) eller döljs på sajten.",
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
register(
    Operation(
        name="skapa_tjanst",
        description=(
            "Föreslå en ny, komplett tjänst: namn, beskrivning, brödtext OCH "
            "arbetsgång i samma anrop. Arbetsgången är obligatorisk - en tjänst "
            "utan 'Så går det till' är en halvfärdig sida. Tjänsten blir synlig "
            "när kunden godkänner förslaget. Alla "
            "målgrupper kopplas automatiskt. Föreslå gärna i samma tur även en "
            "FAQ-sektion för tjänsten (skapa_faq_sektion + skapa_faq_fraga) - "
            "kunden godkänner varje del för sig och kan avslå det den inte vill ha."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "namn": _S,
                "kategori_slug": _S,
                "beskrivning": _S,
                "steg": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"rubrik": _S, "beskrivning": _S},
                        "required": ["rubrik"],
                        "additionalProperties": False,
                    },
                },
                "body": _S,
            },
            # steg är obligatoriskt i SCHEMAT, inte bara en uppmaning i
            # texten: modellen skapade annars tjänster utan arbetsgång.
            "required": ["namn", "beskrivning", "steg"],
            "additionalProperties": False,
        },
        risk=Risk.BUSINESS,
        prepare=_prepare_skapa,
        apply=_apply_skapa,
    )
)


def _prepare_koppla_faq(job, user, slug, faq_slug):
    """Koppla en FAQ-sektion till en tjänst. Sektionen får vara ett utkast."""
    from apps.faq.models import FAQSection

    from .faq_ops import pending_section

    svc = _service(slug)
    section = FAQSection.objects.filter(slug=faq_slug).first()
    pending = None if section else pending_section(job, faq_slug)
    if section is None and pending is None:
        known = ", ".join(FAQSection.objects.values_list("slug", flat=True))
        raise OperationError(f"Okänd FAQ-sektion: {faq_slug}. Kända: {known}")

    title = section.title if section else pending.payload.get("title", faq_slug)
    return Prepared(
        payload={"faq_slug": faq_slug, "andrade_falt": {"FAQ-sektion": title}},
        before={"FAQ-sektion": svc.faq_section.title if svc.faq_section_id else "ingen"},
        summary=f"Kopplar FAQ '{title}' till {svc.name}",
        target=svc,
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
        name="koppla_faq_till_tjanst",
        description=(
            "Koppla en FAQ-sektion till en tjänst - frågorna visas då på "
            "tjänstesidan. Sektionen får vara en du föreslagit i samma tur."
        ),
        input_schema={
            "type": "object",
            "properties": {"slug": _S, "faq_slug": _S},
            "required": ["slug", "faq_slug"],
            "additionalProperties": False,
        },
        risk=Risk.BUSINESS,
        wants_job=True,
        prepare=_prepare_koppla_faq,
        apply=_apply_koppla_faq,
    )
)
