"""
Operationsregistret - AI:ns enda väg in i systemet.

En operation är namn + beskrivning + JSON Schema + riskklass + kod. Registret
är medvetet MCP-format från dag ett: MCP-verktygen genereras 1:1 härifrån.

Tre riskklasser styr flödet:
- READ:     `read(user, **params)` körs direkt och svaret går till modellen.
- TEXT:     `prepare(...)` validerar och blir en DraftChange som kan
            klumpgodkännas; `apply(...)` körs först vid godkännande.
- BUSINESS: som TEXT men godkänns alltid per ändring (priser, synlighet,
            nytt publikt innehåll).

prepare-steget kör samma formulär/sanering som manage-vyerna, så modellen får
valideringsfel direkt och kan rätta sig. apply validerar IGEN vid
godkännandet - läget kan ha ändrats sedan förslaget lades.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from django.forms.models import model_to_dict

from apps.assistant.models import Risk


class OperationError(Exception):
    """Valideringsfel som ska tillbaka till modellen som verktygsfel."""


@dataclass
class Prepared:
    """Resultatet av ett lyckat prepare-steg - blir en DraftChange."""

    payload: dict
    summary: str
    before: dict | None = None
    target: object | None = None  # befintligt objekt; None vid skapande
    #: Utkast som måste godkännas FÖRE detta. Används när ett förslag pekar
    #: på något ett annat förslag ska skapa - en FAQ-fråga i en sektion som
    #: bara finns som utkast.
    depends_on: object | None = None


@dataclass(frozen=True)
class Operation:
    name: str
    description: str
    input_schema: dict
    risk: str
    #: Modul som operationen tillhör, eller None för alltid tillgänglig.
    #: Moduler slås på och av i ASSISTANT_FEATURES - kunden betalar per
    #: modul, och en obetald modul ska varken synas eller gå att anropa.
    feature: str | None = None
    #: Sant om prepare behöver se jobbet, t.ex. för att hitta utkast som
    #: ännu inte godkänts. Signaturen blir då prepare(job, user, **params).
    wants_job: bool = False
    read: Callable | None = None  # READ: (user, **params) -> dict
    prepare: Callable | None = None  # TEXT/BUSINESS: (user, **params) -> Prepared
    apply: Callable | None = None  # TEXT/BUSINESS: (user, payload, target) -> obj


REGISTRY: dict[str, Operation] = {}


def register(op: Operation) -> Operation:
    if op.name in REGISTRY:
        raise RuntimeError(f"Operationen {op.name} är redan registrerad.")
    if op.risk == Risk.READ:
        assert op.read is not None, op.name
    else:
        assert op.prepare is not None and op.apply is not None, op.name
    REGISTRY[op.name] = op
    return op


def schema(properties: dict, required: list[str]) -> dict:
    """JSON Schema-kropp med de regler alla operationer delar."""
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


# --- Tyst dataförlust -------------------------------------------------------
#
# Saneringen tar bort taggar den inte tillåter och kapar text som är för lång.
# Båda sker tyst. Modellen ser aldrig resultatet - den får ett kvitto på att
# utkastet skapades - så den kan inte upptäcka att strukturen försvann och
# gör om samma sak nästa gång.
#
# Därför stoppas det här i stället: om något gick förlorat blir det ett
# verktygsfel med vad som hände och vad man ska göra i stället. Modellen
# rättar sig själv på samma sätt som vid en okänd slug.

_TAG = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)\b")

#: Vad man ska göra i stället, per tagg som inte överlever.
_MARKUP_ADVICE = {
    "ul": "Använd satt_tjanst_steg för arbetsgång; annars löpande stycken.",
    "ol": "Använd satt_tjanst_steg för arbetsgång; annars löpande stycken.",
    "li": "Använd satt_tjanst_steg för arbetsgång; annars löpande stycken.",
    "h1": "Rubriker finns inte i fritext - skriv en fristående mening.",
    "h2": "Rubriker finns inte i fritext - skriv en fristående mening.",
    "h3": "Rubriker finns inte i fritext - skriv en fristående mening.",
    "table": "Tabeller stöds inte - skriv innehållet som text.",
}


def _tags(text):
    return {m.group(1).lower() for m in _TAG.finditer(text or "")}


def _lost_length(raw, cleaned):
    """Hur många tecken saneringen kapade bort, eller 0."""
    if not isinstance(raw, str) or not cleaned:
        return 0
    from apps.common.security import sanitize_plain_text, sanitize_rich_html_basic

    for sanitiser in (sanitize_plain_text, sanitize_rich_html_basic):
        full = sanitiser(raw, max_length=10**7)
        # Kapning ger alltid ett prefix; annan skillnad är inte kapning.
        if full != cleaned and full.startswith(cleaned):
            return len(full) - len(cleaned)
    return 0


def assert_nothing_lost(field, raw, cleaned):
    """Höj OperationError om saneringen tog bort struktur eller kapade text."""
    if not isinstance(raw, str) or not raw:
        return

    lost_tags = _tags(raw) - _tags(cleaned or "")
    if lost_tags:
        advice = " ".join(
            dict.fromkeys(_MARKUP_ADVICE[t] for t in sorted(lost_tags) if t in _MARKUP_ADVICE)
        )
        raise OperationError(
            f"Fältet '{field}' innehöll <{'>, <'.join(sorted(lost_tags))}> som inte "
            f"tillåts och som togs bort av saneringen - texten hade blivit "
            f"sönderhackad. Tillåtna taggar: <p>, <br>, <strong>, <em>, <a>. "
            + (advice or "Skriv om utan de taggarna.")
        )

    cut = _lost_length(raw, cleaned)
    if cut:
        raise OperationError(
            f"Fältet '{field}' är {cut} tecken för långt och hade kapats mitt i "
            f"texten. Korta ner det till {len(cleaned)} tecken."
        )


# --- Formulärhjälpare -------------------------------------------------------


def run_form(form_class, instance, new_values, allowed_fields, form_kwargs=None):
    """
    Validera `new_values` (begränsat till `allowed_fields`) genom samma
    ModelForm som manage-vyn använder, ovanpå instansens nuvarande värden.

    Returnerar (form, before) där before är de gamla värdena för de fält som
    faktiskt ändras. Höjer OperationError med läsbara fel vid ogiltig input.
    """
    unknown = set(new_values) - set(allowed_fields)
    if unknown:
        raise OperationError(f"Okända fält: {', '.join(sorted(unknown))}")

    base = model_to_dict(instance) if instance is not None and instance.pk else {}
    data = {**base, **new_values}
    form = form_class(data=data, instance=instance, **(form_kwargs or {}))
    if not form.is_valid():
        lines = [f"{fname}: {'; '.join(errs)}" for fname, errs in form.errors.items()]
        raise OperationError("Ogiltig input - " + " | ".join(lines))

    # Alla skrivoperationer går genom run_form, så det räcker att kontrollera
    # här att saneringen inte tyst åt upp något.
    for field, raw in new_values.items():
        assert_nothing_lost(field, raw, form.cleaned_data.get(field))

    before = None
    if instance is not None and instance.pk:
        before = {f: base.get(f) for f in new_values}
    return form, before


def cleaned_subset(form, fields):
    """Formulärets sanerade värden för just de fält förslaget gäller."""
    out = {}
    for f in fields:
        value = form.cleaned_data.get(f)
        if hasattr(value, "pk"):  # FK-instans -> id, JSON-vänligt
            value = value.pk
        out[f] = value
    return out
