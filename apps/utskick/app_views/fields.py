"""
Extrafälten (README B.1 FieldDef, H.5 och I.1): kundens egna fält på
kontakterna, högst FieldDef.MAX_PER_ACCOUNT (30) per konto. Ett fält kan
visas i listan under namnet ("Regnr ABC 123"); att välja ett annat flyttar
markeringen (högst ett, villkor i databasen).

    field_list   fälten, ett nytt fält, ändra (rubrik, val, visas i listan)
                 och ta bort (värdena tas bort från kontakterna)

Nyckeln (FieldDef.key) bildas av rubriken när fältet skapas och ändras
aldrig: importens kolumnval ("field:regnummer") och senare kopplingar
bygger på den. Typen går inte att byta efteråt (värdena är sparade efter
typen). Ett val som någon kontakt har kan inte tas bort (värdet skulle
annars stoppa nästa ändring av kontakten); ta bort hela fältet i stället.
Personnummer nekas i fältens värden (normalize.field_value), och sidan
säger att fälten inte är till för personnummer eller hälsouppgifter (H.5).
"""

from django import forms
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q
from django.db.models.fields.json import KeyTextTransform
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.text import slugify

from .. import contacts as register
from .. import normalize
from ..access import owned_ids, utskick_view
from ..models import Contact, FieldDef
from . import render_contacts
from .contacts import Refused, clean_name, count_text

MAX_FIELDS = FieldDef.MAX_PER_ACCOUNT
MAX_CHOICES = 30
CHOICE_MAX = 60
FULL_TEXT = f"Du har {MAX_FIELDS} extrafält. Ta bort ett för att lägga till ett nytt."
SPECIAL_TEXT = "Spara inte personnummer, hälsouppgifter eller liknande i extrafält."
KIND_HELP = {
    FieldDef.Kind.TEXT: "Fritext, till exempel ett regnummer.",
    FieldDef.Kind.DATE: "Ett datum, till exempel senaste service.",
    FieldDef.Kind.NUMBER: "Ett tal, till exempel antal däck i hotellet.",
    FieldDef.Kind.CHOICE: "Ett av flera val som du skriver nedan.",
}


def _fields_url(anchor=""):
    return reverse("flamingo:app_fields") + (f"#{anchor}" if anchor else "")


def _choices(raw):
    """Valen ur textrutan, ett per rad, utan dubbletter."""
    out = []
    for line in str(raw or "").splitlines():
        value = clean_name(line, 200)
        if not value:
            continue
        if len(value) > CHOICE_MAX:
            raise forms.ValidationError(f"Ett val får vara högst {CHOICE_MAX} tecken.")
        if normalize.looks_like_personnummer(value):
            raise forms.ValidationError(normalize.PERSONNUMMER_TEXT)
        if value.casefold() not in (v.casefold() for v in out):
            out.append(value)
    if len(out) > MAX_CHOICES:
        raise forms.ValidationError(f"Högst {MAX_CHOICES} val.")
    return out


class FieldForm(forms.Form):
    label = forms.CharField(label="Rubrik", max_length=60)
    kind = forms.ChoiceField(label="Typ", choices=FieldDef.Kind.choices, required=False)
    choices = forms.CharField(label="Val", required=False, widget=forms.Textarea(attrs={"rows": 4}))
    show_in_list = forms.BooleanField(label="Visa i listan under namnet", required=False)

    def __init__(self, *args, definition=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.definition = definition

    def clean_label(self):
        label = clean_name(self.cleaned_data.get("label"), 60)
        if not label:
            raise forms.ValidationError("Skriv en rubrik.")
        return label

    def clean(self):
        data = super().clean()
        kind = self.definition.kind if self.definition is not None else data.get("kind")
        if kind not in FieldDef.Kind.values:
            self.add_error("kind", "Välj en typ.")
            return data
        data["kind"] = kind
        try:
            data["choices"] = _choices(data.get("choices"))
        except forms.ValidationError as exc:
            self.add_error("choices", exc)
            return data
        if kind == FieldDef.Kind.CHOICE and len(data["choices"]) < 2:
            self.add_error("choices", "Skriv minst två val, ett per rad.")
        elif kind != FieldDef.Kind.CHOICE:
            data["choices"] = []
        return data


def _unique_key(account, label, kind):
    """Nyckeln ur rubriken: "Senaste besök" -> "senaste-besok", ledig hos
    kontot (-2, -3 vid krock). "telefon" hålls fri för fasta nummer
    (normalize.LANDLINE_FIELD) om fältet inte är text."""
    base = slugify(label)[:40].strip("-") or "falt"
    taken = set(FieldDef.objects.filter(account=account).values_list("key", flat=True))
    if kind != FieldDef.Kind.TEXT:
        taken.add(normalize.LANDLINE_FIELD)
    key, n = base, 2
    while key in taken:
        suffix = f"-{n}"
        key = base[: 40 - len(suffix)].rstrip("-") + suffix
        n += 1
    return key


def _set_in_list(account, definition, show):
    """Högst ett fält visas i listan: att välja ett tar bort det andra."""
    if show:
        FieldDef.objects.filter(account=account, show_in_list=True).exclude(
            pk=definition.pk
        ).update(show_in_list=False)
    definition.show_in_list = show


def _field_new(request, account):
    if FieldDef.objects.filter(account=account).count() >= MAX_FIELDS:
        raise Refused(FULL_TEXT)
    form = FieldForm(request.POST)
    if not form.is_valid():
        return {"new_form": form}
    data = form.cleaned_data
    order = (FieldDef.objects.filter(account=account).aggregate(m=Max("order"))["m"] or 0) + 1
    with transaction.atomic():
        definition = FieldDef(
            account=account,
            key=_unique_key(account, data["label"], data["kind"]),
            label=data["label"],
            kind=data["kind"],
            choices=data["choices"],
            order=min(order, 998),
        )
        _set_in_list(account, definition, data["show_in_list"])
        try:
            with transaction.atomic():
                definition.save()
        except IntegrityError:
            raise Refused("Fältet gick inte att spara. Försök igen.") from None
    messages.success(request, f"Fältet {definition.label} är skapat.")
    return redirect(_fields_url(f"falt-{definition.pk}"))


def _definition(request, account):
    pk = owned_ids(FieldDef, account, [request.POST.get("falt")])[0]
    return FieldDef.objects.get(pk=pk, account=account)


def _in_use(account, definition, values):
    """Hur många kontakter som har något av values i fältet."""
    if not values:
        return 0
    return (
        Contact.objects.filter(account=account)
        .annotate(kt_value=KeyTextTransform(definition.key, "fields"))
        .filter(kt_value__in=list(values))
        .count()
    )


def _field_edit(request, account):
    definition = _definition(request, account)
    form = FieldForm(request.POST, definition=definition)
    if not form.is_valid():
        return {"edit_form": form, "edit_pk": definition.pk}
    data = form.cleaned_data
    if definition.kind == FieldDef.Kind.CHOICE:
        kept = {c.casefold() for c in data["choices"]}
        removed = [c for c in definition.choices or () if str(c).casefold() not in kept]
        used = _in_use(account, definition, removed)
        if used:
            form.add_error(
                "choices",
                f"Ett val du tog bort används av {count_text(used, 'kontakt', 'kontakter')}. "
                "Ändra kontakterna först, eller ta bort hela fältet.",
            )
            return {"edit_form": form, "edit_pk": definition.pk}
    with transaction.atomic():
        definition.label = data["label"]
        definition.choices = data["choices"]
        _set_in_list(account, definition, data["show_in_list"])
        definition.save(update_fields=["label", "choices", "show_in_list"])
    messages.success(request, f"Fältet {definition.label} är sparat.")
    return redirect(_fields_url(f"falt-{definition.pk}"))


def _strip_values(account, key):
    """Ta bort fältets värden från kontakterna (och ur sökningen)."""
    changed = []
    contacts = Contact.objects.filter(account=account, fields__has_key=key)
    for kontakt in contacts.iterator(chunk_size=500):
        kontakt.fields = {k: v for k, v in (kontakt.fields or {}).items() if k != key}
        kontakt.search_text = register.search_text_for(kontakt)
        changed.append(kontakt)
        if len(changed) >= 500:
            Contact.objects.bulk_update(changed, ["fields", "search_text"])
            changed = []
    if changed:
        Contact.objects.bulk_update(changed, ["fields", "search_text"])


def _field_delete(request, account):
    definition = _definition(request, account)
    label = definition.label
    with transaction.atomic():
        _strip_values(account, definition.key)
        definition.delete()
    messages.success(request, f"Fältet {label} och dess värden är borttagna.")
    return redirect(_fields_url())


_FIELD_ACTIONS = {
    "new": _field_new,
    "edit": _field_edit,
    "delete": _field_delete,
}


@utskick_view
def field_list(request, account):
    bound = {}
    if request.method == "POST":
        handler = _FIELD_ACTIONS.get(request.POST.get("action", ""))
        if handler is None:
            return redirect("flamingo:app_fields")
        try:
            result = handler(request, account)
        except Refused as exc:
            messages.error(request, exc.message)
            return redirect(_fields_url())
        if not isinstance(result, dict):
            return result
        bound = result  # ett formulär med fel ritas om med det kunden skrev
    definitions = list(FieldDef.objects.filter(account=account).order_by("order", "pk"))
    counts = {}
    if definitions:
        counts = Contact.objects.filter(account=account).aggregate(
            **{f"f{d.pk}": Count("pk", filter=Q(fields__has_key=d.key)) for d in definitions}
        )
    rows = []
    for definition in definitions:
        edit_form = None
        if bound.get("edit_pk") == definition.pk:
            edit_form = bound["edit_form"]
        rows.append(
            {
                "definition": definition,
                "used": counts.get(f"f{definition.pk}", 0),
                "choices_text": "\n".join(str(c) for c in definition.choices or ()),
                "form": edit_form,
            }
        )
    context = {
        "rows": rows,
        "count": len(definitions),
        "max_fields": MAX_FIELDS,
        "full": len(definitions) >= MAX_FIELDS,
        "full_text": FULL_TEXT,
        "special_text": SPECIAL_TEXT,
        "new_form": bound.get("new_form") or FieldForm(initial={"kind": FieldDef.Kind.TEXT}),
        "kinds": [(value, label, KIND_HELP[value]) for value, label in FieldDef.Kind.choices],
    }
    return render_contacts(request, "flamingo/app/kontakter/fields.html", "fields", context)
