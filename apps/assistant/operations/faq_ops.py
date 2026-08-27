"""Operationer för FAQ. Validering via manage-vyernas egna formulär."""

from apps.assistant.models import Risk
from apps.faq.models import FAQItem, FAQSection
from apps.manage.faq_views import FAQItemForm, FAQSectionForm

from .base import Operation, OperationError, Prepared, cleaned_subset, register, run_form


def _section(slug):
    section = FAQSection.objects.filter(slug=slug).first()
    if section is None:
        known = ", ".join(FAQSection.objects.values_list("slug", flat=True))
        raise OperationError(f"Okänd FAQ-sektion: {slug}. Kända: {known}")
    return section


def _lista(user):
    return {
        "sektioner": [
            {
                "slug": s.slug,
                "titel": s.title,
                "aktiv": s.is_active,
                "fragor": [
                    {"id": i.pk, "fraga": i.question, "aktiv": i.is_active} for i in s.items.all()
                ],
            }
            for s in FAQSection.objects.prefetch_related("items")
        ]
    }


def pending_section(job, slug):
    """Publikt alias - används även av areas_ops och services_ops."""
    return _pending_section(job, slug)


def _pending_section(job, slug):
    """
    Ett ännu icke godkänt sektionsutkast med den här sluggen.

    Utan det går det inte att skapa sektion och frågor i samma tur:
    sektionen finns inte i databasen förrän den godkänts, så frågorna
    misslyckades med "Okänd FAQ-sektion" och inget utkast skapades alls.
    Det var därför tio sektioner låg tomma (2026-08-21).
    """
    from apps.assistant.models import DraftChange

    rows = job.changes.filter(operation="skapa_faq_sektion", status=DraftChange.Status.PENDING)
    for change in rows:
        if change.payload.get("slug") == slug:
            return change
    return None


def _prepare_skapa_fraga(job, user, sektion_slug, fraga, svar):
    section = FAQSection.objects.filter(slug=sektion_slug).first()
    pending = None if section else _pending_section(job, sektion_slug)
    if section is None and pending is None:
        known = ", ".join(FAQSection.objects.values_list("slug", flat=True))
        raise OperationError(f"Okänd FAQ-sektion: {sektion_slug}. Kända: {known}")

    form, _ = run_form(
        FAQItemForm,
        FAQItem(),
        {"question": fraga, "answer": svar, "is_active": True},
        ["question", "answer", "is_active"],
    )
    payload = cleaned_subset(form, ["question", "answer", "is_active"])
    # Slug, inte id: sektionen kan sakna id ännu. Den slås upp vid apply,
    # och depends_on garanterar att den då hunnit skapas.
    payload["section_slug"] = sektion_slug
    title = section.title if section else pending.payload.get("title", sektion_slug)
    return Prepared(
        payload=payload,
        summary=f"Ny fråga i {title}: {fraga[:80]}",
        depends_on=pending,
    )


def _apply_skapa_fraga(user, payload, target):
    payload = dict(payload)
    slug = payload.pop("section_slug", None)
    section_id = payload.pop("section_id", None)  # äldre utkast
    section = (
        FAQSection.objects.filter(pk=section_id).first()
        if section_id
        else FAQSection.objects.filter(slug=slug).first()
    )
    if section is None:
        raise OperationError(
            f"FAQ-sektionen {slug or section_id} finns inte. Godkänn sektionen först."
        )
    form, _ = run_form(FAQItemForm, FAQItem(section=section), payload, list(payload))
    item = form.save(commit=False)
    item.section = section
    item.save()
    return item


def _prepare_uppdatera_fraga(user, fraga_id, fraga=None, svar=None):
    item = FAQItem.objects.filter(pk=fraga_id).first()
    if item is None:
        raise OperationError(f"Okänd fråga: {fraga_id}. Använd lista_faq för id:n.")
    changed = {}
    if fraga is not None:
        changed["question"] = fraga
    if svar is not None:
        changed["answer"] = svar
    if not changed:
        raise OperationError("Ange ny fråga och/eller nytt svar.")
    form, before = run_form(FAQItemForm, item, changed, ["question", "answer"])
    return Prepared(
        payload=cleaned_subset(form, changed),
        before=before,
        summary=f"Ändrad fråga: {item.question[:80]}",
        target=item,
    )


def _apply_uppdatera_fraga(user, payload, target):
    form, _ = run_form(FAQItemForm, target, payload, ["question", "answer"])
    return form.save()


def _prepare_skapa_sektion(user, titel, beskrivning=""):
    form, _ = run_form(
        FAQSectionForm,
        FAQSection(),
        {"title": titel, "description": beskrivning or "", "is_active": True, "order": 100},
        ["title", "description", "is_active", "order"],
    )
    return Prepared(
        payload=cleaned_subset(form, ["title", "slug", "description", "is_active", "order"]),
        summary=f"Ny FAQ-sektion: {titel}",
    )


def _apply_skapa_sektion(user, payload, target):
    form, _ = run_form(FAQSectionForm, FAQSection(), payload, list(payload))
    return form.save()


_S = {"type": "string"}

register(
    Operation(
        name="lista_faq",
        description="Lista alla FAQ-sektioner och deras frågor (med id:n).",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        risk=Risk.READ,
        read=_lista,
    )
)
register(
    Operation(
        name="skapa_faq_fraga",
        description=(
            "Föreslå en ny fråga+svar i en FAQ-sektion. Sektionen får vara en du "
            "föreslagit i samma tur - frågorna kopplas då till den och godkänns "
            "efter den. Svaret tillåter bara "
            "<p>, <br>, <strong>, <em> och <a> - rubriker och listor stryks av "
            "saneringen och lämnar naken text."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sektion_slug": _S,
                "fraga": _S,
                "svar": _S,
            },
            "required": ["sektion_slug", "fraga", "svar"],
            "additionalProperties": False,
        },
        risk=Risk.TEXT,
        # prepare behöver jobbet för att hitta ett sektionsutkast som ännu
        # inte godkänts.
        wants_job=True,
        prepare=_prepare_skapa_fraga,
        apply=_apply_skapa_fraga,
    )
)
register(
    Operation(
        name="uppdatera_faq_fraga",
        description="Föreslå ändrad text för en befintlig FAQ-fråga.",
        input_schema={
            "type": "object",
            "properties": {
                "fraga_id": {"type": "integer"},
                "fraga": _S,
                "svar": _S,
            },
            "required": ["fraga_id"],
            "additionalProperties": False,
        },
        risk=Risk.TEXT,
        prepare=_prepare_uppdatera_fraga,
        apply=_apply_uppdatera_fraga,
    )
)
register(
    Operation(
        name="skapa_faq_sektion",
        description="Föreslå en ny FAQ-sektion (fyll den sedan med skapa_faq_fraga).",
        input_schema={
            "type": "object",
            "properties": {
                "titel": _S,
                "beskrivning": _S,
            },
            "required": ["titel"],
            "additionalProperties": False,
        },
        risk=Risk.TEXT,
        prepare=_prepare_skapa_sektion,
        apply=_apply_skapa_sektion,
    )
)


def _prepare_uppdatera_sektion(user, sektion_slug, titel=None, beskrivning=None):
    """Ändra en FAQ-sektions rubrik/ingress. Frågorna rörs inte."""
    section = _section(sektion_slug)
    changed = {}
    if titel is not None:
        changed["title"] = titel
    if beskrivning is not None:
        changed["description"] = beskrivning
    if not changed:
        raise OperationError("Ange ny titel och/eller ny beskrivning.")
    form, before = run_form(FAQSectionForm, section, changed, ["title", "description"])
    return Prepared(
        payload=cleaned_subset(form, changed),
        before=before,
        summary=f"Ändrad FAQ-sektion: {section.title}",
        target=section,
    )


def _apply_uppdatera_sektion(user, payload, target):
    form, _ = run_form(FAQSectionForm, target, payload, list(payload))
    return form.save()


def _prepare_sektion_aktiv(user, sektion_slug, aktiv):
    section = _section(sektion_slug)
    return Prepared(
        payload={"is_active": bool(aktiv)},
        before={"is_active": section.is_active},
        summary=f"{'Visar' if aktiv else 'Döljer'} FAQ-sektionen {section.title}",
        target=section,
    )


def _apply_sektion_aktiv(user, payload, target):
    target.is_active = payload["is_active"]
    target.save(update_fields=["is_active", "updated_at"])
    return target


register(
    Operation(
        name="uppdatera_faq_sektion",
        description=(
            "Ändra en FAQ-sektions titel eller beskrivning. Frågorna i den rörs "
            "inte - använd uppdatera_faq_fraga för dem."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sektion_slug": _S,
                "titel": _S,
                "beskrivning": _S,
            },
            "required": ["sektion_slug"],
            "additionalProperties": False,
        },
        risk=Risk.TEXT,
        prepare=_prepare_uppdatera_sektion,
        apply=_apply_uppdatera_sektion,
    )
)
register(
    Operation(
        name="satt_faq_sektion_aktiv",
        description=(
            "Visa eller dölj en hel FAQ-sektion. Dölj i stället för att lämna "
            "en tom eller inaktuell sektion synlig - radering finns inte."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sektion_slug": _S,
                "aktiv": {"type": "boolean"},
            },
            "required": ["sektion_slug", "aktiv"],
            "additionalProperties": False,
        },
        risk=Risk.BUSINESS,
        prepare=_prepare_sektion_aktiv,
        apply=_apply_sektion_aktiv,
    )
)
