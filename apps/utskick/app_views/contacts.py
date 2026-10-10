"""
Kontakter: listan, kortet, lägg till, redigera, samtycke, export, ta bort
och rensa inaktiva (README I.1, I.7, H.3 och H.4).

    contact_list      listan: sök, filter, samtycke per kanal, markera och massändra
    contacts_bulk     massändringen (POST): lägg i lista, tagga, ta bort tagg,
                      exportera (bekräftelse) och ta bort (bekräftelse)
    contact_new       lägg till en kontakt (kräver access.can_collect)
    contact_detail    kontaktkortet: kontaktvägar med samtycke och bevis, extrafält,
                      listor och taggar, sammanfattning och tidslinjen
    contact_edit      ändra uppgifterna (adresser via contacts.change_address)
    contact_consent   ändra samtycket för en kanal (rutan på kortet)
    contact_export    allt om personen som JSON (POST, loggas)
    contact_delete    ta bort personen (bekräftelse, H.4)
    contacts_export   registret som CSV (bekräftelse, POST, loggas, 10 per dag)
    contacts_prune    rensa kontakter som saknar samtycke och inte hörts av på två år

Regler som gäller varje vy här:

- Allt hämtas via kontot: ett id ur adressen med access.owned (404), ett id
  ur ett formulär med access.owned_ids (400 för hela förfrågan).
- Massändringar tar högst BULK_MAX_IDS markerade id:n. "Markera alla som
  matchar" skickar filtret i stället (alla=1 och filtrets fält), så att
  DATA_UPLOAD_MAX_NUMBER_FIELDS aldrig behöver höjas.
- Byrån i kundvyn gör samma sak som kunden, på riktigt, och loggas som
  "ADX (förnamn)" (access.actor_for).
- Varje väg som skapar en kontakt kräver can_collect: utan godkänt
  biträdesavtal skickas kunden till avtalet (dpa.py) och tillbaka hit.
- Exporter är POST med en bekräftelsesida, celler genom
  flamingo.exports.safe_cell, en rad i ExportLog. Inga mejl skickas härifrån.
- I mallar och kontexter heter en kontakt "kontakt" (README "Naming rule").
"""

import csv
import io
import json
from dataclasses import dataclass, field

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Prefetch
from django.http import HttpResponse, HttpResponseNotAllowed
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from apps.common.security import normalize_typography
from apps.flamingo.exports import safe_cell

from .. import consent as consents
from .. import contacts as register
from .. import keys, limits, normalize, timeline
from ..access import (
    actor_for,
    can_collect,
    collect_block_reason,
    current_dpa,
    owned,
    owned_ids,
    user_label,
    utskick_view,
)
from ..models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    Consent,
    Contact,
    ContactList,
    ExportLog,
    FieldDef,
    ListMembership,
    Tag,
)
from . import render_contacts

PER_PAGE = 50
#: Massändringar tar högst så många markerade kontakter (I.7). Fler går via
#: "Markera alla som matchar", som skickar filtret i stället för id:n.
BULK_MAX_IDS = 200
#: Så många kontakter tas bort per förfrågan (massborttagning och rensning),
#: så att förfrågan blir klar i tid. Resten tas bort med ett klick till.
DELETE_MAX = 1000
#: Värdet i listväljaren och taggväljaren för "Ny lista" och "Ny tagg".
NEW = "ny"

KEY_TEXT = "Det gick inte att spara just nu. ADX har fått ett larm."
CHOOSE_TEXT = "Markera minst en kontakt."
EVIDENCE_TEXT = consents.REFUSAL_TEXTS["evidence"]

_MONTHS = ("jan", "feb", "mars", "april", "maj", "juni", "juli", "aug", "sep", "okt", "nov", "dec")
_MONTH_NAMES = (
    "januari",
    "februari",
    "mars",
    "april",
    "maj",
    "juni",
    "juli",
    "augusti",
    "september",
    "oktober",
    "november",
    "december",
)
_CHANNEL_WORD = {CHANNEL_SMS: "Sms", CHANNEL_EMAIL: "E-post"}


# ---------------------------------------------------------------------------
# Tid och tal i klartext (delas av listor, inställningar och avtalet)
# ---------------------------------------------------------------------------


def group(number):
    """2418 -> "2 418" (hårt blanksteg, som flamingo_app.tal)."""
    return f"{int(number):,}".replace(",", "\u00a0")


def count_text(n, one, many):
    """ "1 kontakt", "2 418 kontakter"."""
    return f"{group(n)} {one if n == 1 else many}"


def date_text(moment):
    """ "2 okt 2026" i svensk tid (alltid med år)."""
    if not moment:
        return ""
    local = timezone.localtime(moment)
    return f"{local.day} {_MONTHS[local.month - 1]} {local.year}"


def day_text(moment, now=None):
    """ "i dag", "i går", "2 okt", eller "2 okt 2025" ett annat år."""
    if not moment:
        return ""
    now = timezone.localtime(now or timezone.now())
    local = timezone.localtime(moment)
    if local.date() == now.date():
        return "i dag"
    if (now.date() - local.date()).days == 1:
        return "i går"
    text = f"{local.day} {_MONTHS[local.month - 1]}"
    return text if local.year == now.year else f"{text} {local.year}"


def stamp(moment, now=None):
    """Tidslinjens tid: "i dag 14.32", "8 okt 09.05", "4 mars 2024"."""
    if not moment:
        return ""
    now = timezone.localtime(now or timezone.now())
    local = timezone.localtime(moment)
    day = day_text(moment, now)
    if local.year != now.year:
        return day
    return f"{day} {local:%H.%M}"


def month_text(moment):
    """ "mars 2024" (kontaktkortets "kontakt sedan")."""
    local = timezone.localtime(moment)
    return f"{_MONTH_NAMES[local.month - 1]} {local.year}"


def field_display(definition, value):
    """Ett extrafälts värde som det visas: datum som "4 nov 2025"."""
    if definition is not None and definition.kind == FieldDef.Kind.DATE and value:
        try:
            year, month, day = (int(p) for p in str(value).split("-"))
            return f"{day} {_MONTHS[month - 1]} {year}"
        except (ValueError, IndexError):
            return value
    return value


def clean_name(raw, max_length):
    return " ".join(normalize_typography(str(raw or "")).split())[:max_length]


# ---------------------------------------------------------------------------
# Sökning och filter (listan, massändringen och exporten delar dem)
# ---------------------------------------------------------------------------

S = Consent.Status
#: ?samtycke=: (rubrik, statusar). "studsad" är e-postadressens läge.
CONSENT_FILTERS = {
    "ja": ("Ja", (S.YES,)),
    "befintlig": ("Befintlig kund", (S.EXISTING,)),
    "foretag": ("Företag", (S.COMPANY,)),
    "vantar": ("Väntar", (S.PENDING,)),
    "saknas": ("Saknas", (S.MISSING,)),
    "vill-inte": ("Vill inte", (S.DECLINED,)),
    "avregistrerad": ("Avregistrerad", (S.UNSUBSCRIBED,)),
    "studsad": ("Studsad", ()),
}
#: ?kanal=: samtyckesfiltret för en kanal, eller kontakter med adress där.
CHANNEL_FILTERS = {"sms": ("Sms", CHANNEL_SMS), "epost": ("E-post", CHANNEL_EMAIL)}
#: ?typ=
KIND_FILTERS = {
    "person": ("Privatperson", Contact.Kind.PERSON),
    "foretag": ("Företag", Contact.Kind.COMPANY),
}


@dataclass
class Filters:
    q: str = ""
    lista: ContactList | None = None
    tagg: Tag | None = None
    samtycke: str = ""
    kanal: str = ""
    typ: str = ""
    # S4 (segment-byggaren): ?segment=<id>, ett av kontots segment.
    segment: object = None

    def params(self):
        """Filtret som fält för en adress eller dolda fält i ett formulär."""
        out = {}
        if self.q:
            out["q"] = self.q
        if self.lista is not None:
            out["lista"] = str(self.lista.pk)
        if self.tagg is not None:
            out["tagg"] = str(self.tagg.pk)
        # --- S4 (segment-byggaren) ---
        if self.segment is not None:
            out["segment"] = str(self.segment.pk)
        # --- slut S4
        for name in ("samtycke", "kanal", "typ"):
            if getattr(self, name):
                out[name] = getattr(self, name)
        return out

    @property
    def active(self):
        return bool(self.params())

    @property
    def query(self):
        return urlencode(self.params())

    def summary(self):
        """Filtret i ord, för exportens och borttagningens bekräftelse."""
        parts = []
        if self.q:
            parts.append(f'sökningen "{self.q}"')
        if self.lista is not None:
            parts.append(f"listan {self.lista.name}")
        if self.tagg is not None:
            parts.append(f"taggen {self.tagg.name}")
        # --- S4 (segment-byggaren) ---
        if self.segment is not None:
            parts.append(f"segmentet {self.segment.name}")
        # --- slut S4
        if self.samtycke:
            label = CONSENT_FILTERS[self.samtycke][0].lower()
            channel = f" ({CHANNEL_FILTERS[self.kanal][0].lower()})" if self.kanal else ""
            parts.append(f"samtycke {label}{channel}")
        elif self.kanal:
            parts.append(f"har {CHANNEL_FILTERS[self.kanal][0].lower()}")
        if self.typ:
            parts.append(KIND_FILTERS[self.typ][0].lower())
        return ", ".join(parts)


def _pick(model, account, raw, strict):
    """Listan eller taggen i filtret. strict (formulär): owned_ids, ett
    främmande id ger 400. Annars (adressraden) hoppas ett okänt id över."""
    raw = str(raw or "").strip()
    if not raw:
        return None
    if strict:
        pk = owned_ids(model, account, [raw])[0]
        return model.objects.get(pk=pk, account=account)
    if not raw.isdigit():
        return None
    return model.objects.filter(account=account, pk=int(raw)).first()


def parse_filters(account, data, strict=False):
    """Filters ur ?q=&lista=&tagg=&samtycke=&kanal=&typ= (eller ett formulär)."""
    choice = str(data.get("samtycke") or "")
    channel = str(data.get("kanal") or "")
    kind = str(data.get("typ") or "")
    return Filters(
        q=" ".join(str(data.get("q") or "").split())[:100],
        lista=_pick(ContactList, account, data.get("lista"), strict),
        tagg=_pick(Tag, account, data.get("tagg"), strict),
        samtycke=choice if choice in CONSENT_FILTERS else "",
        kanal=channel if channel in CHANNEL_FILTERS else "",
        typ=kind if kind in KIND_FILTERS else "",
        segment=_segment_filter(account, data, strict),  # S4 (segment-byggaren)
    )


# --- S4 (segment-byggaren): segmentet i filtret ---


def _segment_filter(account, data, strict):
    """Segmentet i ?segment= (som listan: owned_ids i ett formulär, annars
    hoppas ett okänt id över)."""
    from ..models import Segment

    return _pick(Segment, account, data.get("segment"), strict)


# --- slut S4


def filtered(account, filters):
    """Kontots kontakter som matchar filtret (alltid account=account)."""
    contacts = register.search(Contact.objects.filter(account=account), filters.q)
    if filters.lista is not None:
        contacts = contacts.filter(memberships__list=filters.lista)
    if filters.tagg is not None:
        contacts = contacts.filter(tags=filters.tagg)
    # --- S4 (segment-byggaren): segmentet räknas om nu, med kontot i villkoret ---
    if filters.segment is not None:
        from .. import segments as segment_rules

        contacts = contacts.filter(segment_rules.matches_q(account.pk, [filters.segment.pk]))
    # --- slut S4
    if filters.typ:
        contacts = contacts.filter(kind=KIND_FILTERS[filters.typ][1])
    channel = CHANNEL_FILTERS[filters.kanal][1] if filters.kanal else ""
    if filters.samtycke == "studsad":
        contacts = contacts.filter(email_state=Contact.EmailState.BOUNCED)
    elif filters.samtycke:
        rows = Consent.objects.filter(
            contact=OuterRef("pk"), status__in=CONSENT_FILTERS[filters.samtycke][1]
        )
        if channel:
            rows = rows.filter(channel=channel)
        contacts = contacts.filter(Exists(rows))
    elif channel:
        contacts = contacts.exclude(**{"phone" if channel == CHANNEL_SMS else "email": ""})
    return contacts


def list_url(filters=None):
    url = reverse("flamingo:app_contacts")
    query = filters.query if filters is not None else ""
    return f"{url}?{query}" if query else url


@dataclass
class Selection:
    """De kontakter en massändring, export eller borttagning gäller."""

    contacts: object
    count: int
    filters: Filters
    ids: list = field(default_factory=list)
    everything: bool = False

    def hidden(self):
        """Urvalet som dolda fält, för bekräftelsesidans formulär."""
        pairs = [("alla", "1")] if self.everything else [("ids", str(pk)) for pk in self.ids]
        return pairs + list(self.filters.params().items())

    def pks(self):
        return list(self.contacts.values_list("pk", flat=True))


def selection_from(account, data):
    """Urvalet i ett formulär: alla=1 med filtret, eller de markerade id:na
    (högst BULK_MAX_IDS, alla kontots egna, annars 400)."""
    filters = parse_filters(account, data, strict=True)
    if data.get("alla") == "1":
        contacts = filtered(account, filters)
        return Selection(contacts, contacts.count(), filters, everything=True)
    raw = data.getlist("ids") if hasattr(data, "getlist") else data.get("ids")
    ids = owned_ids(Contact, account, raw, limit=BULK_MAX_IDS)
    contacts = Contact.objects.filter(account=account, pk__in=ids)
    return Selection(contacts, len(ids), filters, ids=ids)


# ---------------------------------------------------------------------------
# Listans rader
# ---------------------------------------------------------------------------

#: "Senast" i listan: senaste aktivitetens slag. Senare steg lägger till
#: sina ("Klickade", "Svarade STOPP", "Adressen finns inte").
LAST_LABELS = {
    "imported": "Importerad",
    "signup": "Anmälde sig",
    "lead": "Förfrågan",
    "test_send": "Fick ett testutskick",
    # S2 (utskick-ui-byggaren): svar, STOPP och START (inbound, contacts.touch),
    # klick och besök på landningssidan.
    "reply": "Svarade på sms",
    "stop": "Svarade STOPP",
    "start": "Svarade START",
    "click": "Klickade",
    "lp_visit": "Besökte landningssidan",
    # S3 (integrationen): svar på ett mejl, en adress som inte finns och ett
    # klagomål (inbound/email.py och inbound/events.py, contacts.touch).
    "email_reply": "Svarade på mejl",
    "bounce": "Adressen finns inte,",
    "complaint": "Markerade som skräppost",
}
#: Utan aktivitet: hur kontakten kom in.
SOURCE_LABELS = {
    Contact.Source.IMPORT: "Importerad",
    Contact.Source.SIGNUP: "Anmälde sig",
    Contact.Source.FORM: "Förfrågan",
    Contact.Source.LEAD: "Förfrågan",
    Contact.Source.REPLY: "Svarade",
}


def last_text(kontakt, now=None):
    if kontakt.last_activity_at:
        label = LAST_LABELS.get(kontakt.last_activity_kind, "Aktivitet")
        return f"{label} {day_text(kontakt.last_activity_at, now)}"
    label = SOURCE_LABELS.get(kontakt.source, "Tillagd")
    return f"{label} {day_text(kontakt.created_at, now)}"


def sub_line(kontakt, list_field=None):
    """ "Privatperson · Regnr ABC 123", "Företag · Kontakt: Lisa Berg"."""
    parts = [kontakt.get_kind_display()]
    if kontakt.kind == Contact.Kind.COMPANY and kontakt.full_name:
        parts.append(f"Kontakt: {kontakt.full_name}")
    if list_field is not None:
        value = (kontakt.fields or {}).get(list_field.key)
        if value:
            parts.append(f"{list_field.label} {field_display(list_field, value)}")
    return " · ".join(parts)


def with_rows(contacts):
    """Det listans rader läser: samtycken, taggar och listor i tre frågor."""
    return contacts.prefetch_related(
        "consents",
        "tags",
        Prefetch("memberships", queryset=ListMembership.objects.select_related("list")),
    )


def chip_text(chip):
    """Etiketten med kanalen först när den inte redan säger vilken kanal det
    gäller ("Sms: väntar på bekräftelse"), så att två etiketter i samma rad
    går att skilja åt."""
    label = chip["label"]
    if label.startswith(chip["channel"]) or label.endswith("saknas"):
        return label
    return f"{chip['channel']}: {label[:1].lower()}{label[1:]}"


def chips_for(kontakt):
    """Etiketterna för sms och e-post (läser prefetch_related("consents"))."""
    by_channel = {c.channel: c for c in kontakt.consents.all()}
    out = []
    for channel in (CHANNEL_SMS, CHANNEL_EMAIL):
        chip = consents.chip(kontakt, channel, by_channel.get(channel))
        chip["text"] = chip_text(chip)
        out.append(chip)
    return out


def decorate(rows, account, now=None):
    """Lägg på underraden, etiketterna, "Senast" och listorna (som
    inkorgens kort)."""
    list_field = FieldDef.objects.filter(account=account, show_in_list=True).first()
    for kontakt in rows:
        kontakt.kt_sub = sub_line(kontakt, list_field)
        kontakt.kt_chips = chips_for(kontakt)
        kontakt.kt_last = last_text(kontakt, now)
        kontakt.kt_lists = sorted((m.list for m in kontakt.memberships.all()), key=lambda c: c.name)
    return rows


def _prunable(account):
    """Flaggade av utskick_daily (E.7) och fortfarande utan grund."""
    basis = Consent.objects.filter(
        contact=OuterRef("pk"), status__in=(S.YES, S.EXISTING, S.COMPANY, S.PENDING)
    )
    return Contact.objects.filter(account=account, inactive_flagged_at__isnull=False).exclude(
        Exists(basis)
    )


@utskick_view
def contact_list(request, account):
    now = timezone.now()
    filters = parse_filters(account, request.GET)
    matching = with_rows(filtered(account, filters).order_by("-created_at", "-pk"))
    page = Paginator(matching, PER_PAGE).get_page(request.GET.get("sida"))
    rows = decorate(list(page.object_list), account, now)
    everyone = Contact.objects.filter(account=account)
    total = everyone.count()
    context = {
        "kontakter": rows,
        "page": page,
        "matching_count": page.paginator.count,
        "total": total,
        "sms_ok": consents.eligible_contacts(everyone, CHANNEL_SMS, consents.REKLAM).count()
        if total
        else 0,
        "email_ok": consents.eligible_contacts(everyone, CHANNEL_EMAIL, consents.REKLAM).count()
        if total
        else 0,
        "filters": filters,
        "filter_hidden": list(filters.params().items()),
        "lists": ContactList.objects.filter(account=account).order_by("name"),
        "tags": Tag.objects.filter(account=account).order_by("name"),
        # S4 (segment-byggaren): filtret Segment.
        "segments": account.utskick_segments.order_by("name", "pk"),
        "consent_filters": [(k, v[0]) for k, v in CONSENT_FILTERS.items()],
        "channel_filters": [(k, v[0]) for k, v in CHANNEL_FILTERS.items()],
        "kind_filters": [(k, v[0]) for k, v in KIND_FILTERS.items()],
        "can_collect": can_collect(account),
        "collect_reason": collect_block_reason(account),
        "dpa_missing": current_dpa() is None and not account.is_demo,
        "inactive_count": _prunable(account).count() if total else 0,
        "bulk_max": BULK_MAX_IDS,
        "new_value": NEW,
    }
    return render_contacts(request, "flamingo/app/kontakter/list.html", "contacts", context)


# ---------------------------------------------------------------------------
# Massändringen
# ---------------------------------------------------------------------------


class Refused(Exception):
    """Ett val i formuläret saknas eller är fel: visas som meddelande."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _get_or_create(model, account, name, defaults=None):
    try:
        with transaction.atomic():
            row, _ = model.objects.get_or_create(
                account=account, name=name, defaults=defaults or {}
            )
    except IntegrityError:
        row = model.objects.get(account=account, name=name)
    return row


def list_target(request, account, allow_new=True):
    """Listan i formuläret: lista_id (kontots, annars 400) eller "ny" med
    ny_lista som namn."""
    raw = str(request.POST.get("lista_id") or "").strip()
    if raw == NEW and not allow_new:
        raise Refused("Välj en av dina listor.")
    if raw == NEW:
        name = clean_name(request.POST.get("ny_lista"), 80)
        if not name:
            raise Refused("Skriv ett namn på den nya listan.")
        user = request.user if request.user.is_authenticated else None
        return _get_or_create(ContactList, account, name, {"created_by": user})
    if not raw:
        raise Refused("Välj en lista.")
    pk = owned_ids(ContactList, account, [raw])[0]
    return ContactList.objects.get(pk=pk, account=account)


def tag_target(request, account, allow_new=True):
    """Taggen i formuläret: tagg_id (kontots, annars 400) eller "ny" med
    ny_tagg som namn."""
    raw = str(request.POST.get("tagg_id") or "").strip()
    if raw == NEW and not allow_new:
        raise Refused("Välj en av dina taggar.")
    if raw == NEW:
        name = clean_name(request.POST.get("ny_tagg"), 40)
        if not name:
            raise Refused("Skriv ett namn på den nya taggen.")
        return _get_or_create(Tag, account, name)
    if not raw:
        raise Refused("Välj en tagg.")
    pk = owned_ids(Tag, account, [raw])[0]
    return Tag.objects.get(pk=pk, account=account)


def _bulk_list_add(request, account, selection):
    target = list_target(request, account)
    added = register.add_to_list(target, selection.pks())
    if added:
        messages.success(
            request, f"{count_text(added, 'kontakt', 'kontakter')} lades i listan {target.name}."
        )
    else:
        messages.info(request, f"De markerade fanns redan i listan {target.name}.")
    return redirect(list_url(selection.filters))


def _bulk_tag_add(request, account, selection):
    target = tag_target(request, account)
    added = register.add_tag(target, selection.pks())
    if added:
        messages.success(
            request, f"{count_text(added, 'kontakt', 'kontakter')} fick taggen {target.name}."
        )
    else:
        messages.info(request, f"De markerade hade redan taggen {target.name}.")
    return redirect(list_url(selection.filters))


def _bulk_tag_remove(request, account, selection):
    target = tag_target(request, account, allow_new=False)
    removed = register.remove_tag(target, selection.pks())
    messages.success(
        request,
        f"Taggen {target.name} togs bort från {count_text(removed, 'kontakt', 'kontakter')}.",
    )
    return redirect(list_url(selection.filters))


def _bulk_export(request, account, selection):
    return _render_export(request, account, selection)


def _bulk_delete(request, account, selection):
    if request.POST.get("bekrafta") != "1":
        return _render_delete(request, account, selection=selection)
    done, leads, suppressed = _delete_many(request, selection.contacts)
    if done is None:
        messages.error(request, KEY_TEXT)
        return redirect(list_url(selection.filters))
    text = f"{count_text(done, 'kontakt', 'kontakter')} togs bort."
    if leads:
        text += f" {count_text(leads, 'förfrågan', 'förfrågningar')} togs också bort."
    left = selection.count - done
    if left > 0:
        text += f" {group(left)} är kvar: högst {group(DELETE_MAX)} tas bort åt gången."
    messages.success(request, text)
    return redirect(list_url(selection.filters))


_BULK_ACTIONS = {
    "list_add": _bulk_list_add,
    "tag_add": _bulk_tag_add,
    "tag_remove": _bulk_tag_remove,
    "export": _bulk_export,
    "delete": _bulk_delete,
}


@utskick_view
@require_POST
def contacts_bulk(request, account):
    selection = selection_from(account, request.POST)
    handler = _BULK_ACTIONS.get(request.POST.get("action", ""))
    if handler is None:
        messages.error(request, "Välj vad som ska göras med de markerade.")
        return redirect(list_url(selection.filters))
    if not selection.count:
        messages.error(request, CHOOSE_TEXT)
        return redirect(list_url(selection.filters))
    try:
        return handler(request, account, selection)
    except Refused as exc:
        messages.error(request, exc.message)
        return redirect(list_url(selection.filters))


# ---------------------------------------------------------------------------
# Exporten av registret (H.3)
# ---------------------------------------------------------------------------

EXPORT_LIMIT_TEXT = (
    "Du har exporterat {limit} gånger i dag, som är gränsen. Gränsen börjar om vid midnatt."
)


def _export_state(account, now=None):
    window = limits.day_window(now)
    used = limits.count("export", str(account.pk), window)
    last = (
        ExportLog.objects.filter(account=account)
        .select_related("user")
        .order_by("-at", "-pk")
        .first()
    )
    return {
        "exports_used": min(used, register.EXPORTS_PER_DAY),
        "exports_max": register.EXPORTS_PER_DAY,
        "exports_left": max(0, register.EXPORTS_PER_DAY - used),
        "last_export": last,
        "last_export_text": export_text(last),
    }


def export_text(row):
    """ "Senast exporterad av Anna Lindqvist 2 okt 2026" (eller "av ADX")."""
    if row is None:
        return ""
    if row.as_staff:
        who = "ADX"
    else:
        who = user_label(row.user) or "en användare som tagits bort"
    return f"Senast exporterad av {who} {date_text(row.at)}"


def _render_export(request, account, selection):
    context = {
        "selection": selection,
        "hidden": selection.hidden(),
        "back_url": list_url(selection.filters),
        "field_labels": list(
            FieldDef.objects.filter(account=account)
            .order_by("order", "pk")
            .values_list("label", flat=True)
        ),
    }
    context.update(_export_state(account))
    return render_contacts(request, "flamingo/app/kontakter/export.html", "contacts", context)


EXPORT_HEADER = (
    "Förnamn",
    "Efternamn",
    "Typ",
    "Företag",
    "Organisationsnummer",
    "Mobil",
    "E-post",
    "Samtycke sms",
    "Samtycke e-post",
    "Listor",
    "Taggar",
)


def _consent_word(kontakt, channel, rows):
    if not kontakt.address(channel):
        return ""
    if channel == CHANNEL_EMAIL and kontakt.email_state == Contact.EmailState.BOUNCED:
        return "Studsad"
    row = rows.get(channel)
    return row.get_status_display() if row is not None else S.MISSING.label


def contacts_csv(account, contacts):
    """Urvalet som CSV-text (utan BOM) och antal rader. Varje cell går genom
    safe_cell (formelinjektion)."""
    defs = list(FieldDef.objects.filter(account=account).order_by("order", "pk"))
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    header = [*EXPORT_HEADER, *(d.label for d in defs), "Källa", "Tillagd"]
    writer.writerow([safe_cell(cell) for cell in header])
    rows = 0
    for kontakt in with_rows(contacts.order_by("pk")).iterator(chunk_size=500):
        by_channel = {c.channel: c for c in kontakt.consents.all()}
        values = kontakt.fields or {}
        line = [
            kontakt.first_name,
            kontakt.last_name,
            kontakt.get_kind_display(),
            kontakt.company_name,
            kontakt.org_number,
            normalize.display_phone(kontakt.phone),
            kontakt.email,
            _consent_word(kontakt, CHANNEL_SMS, by_channel),
            _consent_word(kontakt, CHANNEL_EMAIL, by_channel),
            ", ".join(sorted(m.list.name for m in kontakt.memberships.all())),
            ", ".join(t.name for t in kontakt.tags.all()),
            *(values.get(d.key, "") for d in defs),
            kontakt.get_source_display(),
            timezone.localtime(kontakt.created_at).date().isoformat(),
        ]
        writer.writerow([safe_cell(cell) for cell in line])
        rows += 1
    return buffer.getvalue(), rows


@utskick_view
def contacts_export(request, account):
    """GET: bekräftelsen (hela registret eller filtret i adressen). POST med
    bekrafta=1: filen, en rad i ExportLog, högst EXPORTS_PER_DAY per dygn."""
    if request.method != "POST":
        filters = parse_filters(account, request.GET)
        contacts = filtered(account, filters)
        selection = Selection(contacts, contacts.count(), filters, everything=True)
        return _render_export(request, account, selection)
    selection = selection_from(account, request.POST)
    if request.POST.get("bekrafta") != "1":
        return _render_export(request, account, selection)
    if not selection.count:
        messages.error(request, "Det finns inga kontakter att exportera.")
        return redirect(list_url(selection.filters))
    if not register.reserve_export(account):
        messages.error(request, EXPORT_LIMIT_TEXT.format(limit=register.EXPORTS_PER_DAY))
        return _render_export(request, account, selection)
    text, rows = contacts_csv(account, selection.contacts)
    register.log_export(account, actor_for(request), ExportLog.Kind.CONTACTS, rows)
    stamp_day = timezone.localdate().isoformat()
    response = HttpResponse("\ufeff" + text, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="kontakter-{stamp_day}.csv"'
    response["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Lägg till och redigera
# ---------------------------------------------------------------------------

CONSENT_START_CHOICES = [
    ("", "Inget samtycke än"),
    (S.YES, "Har sagt ja"),
    (S.EXISTING, "Befintlig kund"),
]


class ContactForm(forms.Form):
    """Kontaktens uppgifter. Formuläret kontrollerar bara längder och val;
    reglerna (nummer, e-post, organisationsnummer, personnummer, krockar)
    ligger i contacts.clean och kommer tillbaka som ContactError."""

    kind = forms.ChoiceField(
        choices=Contact.Kind.choices, initial=Contact.Kind.PERSON, required=False
    )
    first_name = forms.CharField(label="Förnamn", max_length=60, required=False)
    last_name = forms.CharField(label="Efternamn", max_length=80, required=False)
    company_name = forms.CharField(label="Företag", max_length=120, required=False)
    org_number = forms.CharField(label="Organisationsnummer", max_length=20, required=False)
    phone = forms.CharField(label="Mobil", max_length=32, required=False)
    email = forms.CharField(label="E-post", max_length=254, required=False)
    consent_sms = forms.ChoiceField(choices=CONSENT_START_CHOICES, required=False)
    consent_email = forms.ChoiceField(choices=CONSENT_START_CHOICES, required=False)
    evidence = forms.CharField(label="Var och när?", max_length=300, required=False)

    def __init__(self, *args, defs, new, **kwargs):
        kwargs.setdefault("auto_id", "kt-%s")
        super().__init__(*args, **kwargs)
        self.new = new
        self.defs = defs
        if not new:
            for name in ("consent_sms", "consent_email", "evidence"):
                del self.fields[name]
        for name in ("first_name", "last_name", "company_name", "org_number", "phone", "email"):
            self.fields[name].widget.attrs["autocomplete"] = "off"
        self.fields["phone"].widget.input_type = "tel"
        self.fields["phone"].widget.attrs["placeholder"] = "070-123 45 67"
        self.fields["email"].widget.input_type = "email"
        self.fields["org_number"].widget.attrs["inputmode"] = "numeric"
        self.extra = []
        for definition in defs:
            name = f"falt_{definition.key}"
            if definition.kind == FieldDef.Kind.CHOICE:
                choices = [("", "Inget valt")] + [
                    (str(c), str(c)) for c in definition.choices or ()
                ]
                self.fields[name] = forms.ChoiceField(
                    label=definition.label, choices=choices, required=False
                )
            else:
                widget = forms.TextInput()
                if definition.kind == FieldDef.Kind.DATE:
                    widget = forms.DateInput(attrs={"type": "date", "class": "fl-input"})
                elif definition.kind == FieldDef.Kind.NUMBER:
                    widget = forms.TextInput(attrs={"inputmode": "decimal"})
                self.fields[name] = forms.CharField(
                    label=definition.label, max_length=500, required=False, widget=widget
                )
            self.fields[name].widget.attrs["autocomplete"] = "off"
            self.extra.append((name, definition))

    def extra_fields(self):
        return [(self[name], definition) for name, definition in self.extra]

    def clean_kind(self):
        return self.cleaned_data.get("kind") or Contact.Kind.PERSON

    def clean(self):
        data = super().clean()
        if not self.new:
            return data
        wants = [data.get("consent_sms"), data.get("consent_email")]
        if any(wants) and not (data.get("evidence") or "").strip():
            self.add_error("evidence", EVIDENCE_TEXT)
        if data.get("consent_sms") and not (data.get("phone") or "").strip():
            self.add_error("phone", "Fyll i ett mobilnummer för att spara samtycke till sms.")
        if data.get("consent_email") and not (data.get("email") or "").strip():
            self.add_error("email", "Fyll i en e-postadress för att spara samtycke till e-post.")
        return data

    def register_data(self):
        """Det contacts.create och contacts.update tar emot."""
        data = self.cleaned_data
        out = {
            name: data.get(name) or ""
            for name in (
                "kind",
                "first_name",
                "last_name",
                "company_name",
                "org_number",
                "phone",
                "email",
            )
        }
        out["fields"] = {d.key: data.get(name) or "" for name, d in self.extra}
        return out

    def add_register_error(self, exc):
        """Ett ContactError på rätt fält ("field:regnr" -> falt_regnr)."""
        name = exc.field
        if name and name.startswith("field:"):
            name = f"falt_{name[6:]}"
        if name not in self.fields:
            name = None
        self.add_error(name, exc.message)


def _initial(kontakt):
    initial = {
        "kind": kontakt.kind,
        "first_name": kontakt.first_name,
        "last_name": kontakt.last_name,
        "company_name": kontakt.company_name,
        "org_number": kontakt.org_number,
        "phone": normalize.display_phone(kontakt.phone),
        "email": kontakt.email,
    }
    for key, value in (kontakt.fields or {}).items():
        initial[f"falt_{key}"] = value
    return initial


def _dpa_redirect(request, account, nasta):
    messages.info(request, collect_block_reason(account))
    return redirect(f"{reverse('flamingo:app_dpa')}?nasta={nasta}")


@utskick_view
def contact_new(request, account):
    if not can_collect(account):
        return _dpa_redirect(request, account, "ny")
    defs = list(FieldDef.objects.filter(account=account).order_by("order", "pk"))
    form = ContactForm(request.POST or None, defs=defs, new=True)
    if request.method == "POST" and form.is_valid():
        actor = actor_for(request)
        try:
            kontakt = register.create(
                account,
                form.register_data(),
                source=Contact.Source.MANUAL,
                actor=actor,
                defs={d.key: d for d in defs},
            )
        except register.CollectNotAllowed:
            return _dpa_redirect(request, account, "ny")
        except register.ContactError as exc:
            form.add_register_error(exc)
        except keys.KeyMismatch:
            messages.error(request, KEY_TEXT)
        else:
            _start_consent(request, kontakt, form.cleaned_data, actor)
            messages.success(request, f"{kontakt.display_name} är tillagd.")
            return redirect("flamingo:app_contact", pk=kontakt.pk)
    context = {"form": form, "kontakt": None, "kind_choices": Contact.Kind.choices}
    return render_contacts(request, "flamingo/app/kontakter/form.html", "contacts", context)


def _start_consent(request, kontakt, data, actor):
    """Samtycket som valdes när kontakten lades till (med evidence)."""
    for channel, choice in (
        (CHANNEL_SMS, data.get("consent_sms")),
        (CHANNEL_EMAIL, data.get("consent_email")),
    ):
        if not choice:
            continue
        try:
            outcome = consents.set_status(
                kontakt,
                channel,
                choice,
                source=Consent.Source.MANUAL,
                actor=actor,
                evidence=data.get("evidence", ""),
            )
        except keys.KeyMismatch:
            messages.error(request, KEY_TEXT)
            return
        if outcome.refused:
            messages.warning(request, f"{_CHANNEL_WORD[channel]}: {outcome.refusal_text}")


@utskick_view
def contact_edit(request, account, pk):
    kontakt = owned(Contact, account, pk)
    defs = list(FieldDef.objects.filter(account=account).order_by("order", "pk"))
    if request.method == "POST":
        form = ContactForm(request.POST, defs=defs, new=False)
        if form.is_valid():
            try:
                register.update(
                    kontakt,
                    form.register_data(),
                    actor=actor_for(request),
                    defs={d.key: d for d in defs},
                )
            except register.ContactError as exc:
                form.add_register_error(exc)
                kontakt.refresh_from_db()
            except keys.KeyMismatch:
                messages.error(request, KEY_TEXT)
                kontakt.refresh_from_db()
            else:
                messages.success(request, "Ändringarna är sparade.")
                return redirect("flamingo:app_contact", pk=kontakt.pk)
    else:
        form = ContactForm(initial=_initial(kontakt), defs=defs, new=False)
    context = {"form": form, "kontakt": kontakt, "kind_choices": Contact.Kind.choices}
    return render_contacts(request, "flamingo/app/kontakter/form.html", "contacts", context)


# ---------------------------------------------------------------------------
# Kontaktkortet
# ---------------------------------------------------------------------------

#: Samtycken kunden kan sätta på kortet (consent.set_status prövar reglerna).
MANUAL_STATUS_CHOICES = [
    (S.YES, "Har sagt ja"),
    (S.EXISTING, "Befintlig kund"),
    (S.DECLINED, "Vill inte ha erbjudanden"),
    (S.UNSUBSCRIBED, "Avregistrera: spärra adressen"),
]


class ConsentForm(forms.Form):
    channel = forms.ChoiceField(label="Kanal")
    status = forms.ChoiceField(label="Samtycke", choices=MANUAL_STATUS_CHOICES)
    evidence = forms.CharField(label="Var och när?", max_length=300, required=False)

    def __init__(self, *args, kontakt, **kwargs):
        kwargs.setdefault("auto_id", "kt-consent-%s")
        super().__init__(*args, **kwargs)
        self.fields["channel"].choices = [
            (channel, _CHANNEL_WORD[channel])
            for channel in (CHANNEL_SMS, CHANNEL_EMAIL)
            if kontakt.address(channel)
        ]
        if len(self.fields["channel"].choices) == 1 and not self.initial.get("channel"):
            self.initial["channel"] = self.fields["channel"].choices[0][0]
        self.fields["evidence"].widget.attrs["autocomplete"] = "off"

    def clean(self):
        data = super().clean()
        if data.get("status") in (S.YES, S.EXISTING) and not (data.get("evidence") or "").strip():
            self.add_error("evidence", EVIDENCE_TEXT)
        return data


def _proof(row):
    """Beviset för en kanal som (rubrik, värde)-par, bara det som finns."""
    if row is None:
        return []
    pairs = [("Status", row.get_status_display()), ("Grund", row.get_basis_display())]
    if row.source:
        source = row.get_source_display()
        if row.source_detail:
            source = f"{source} · {row.source_detail}"
        pairs.append(("Källa", source))
    if row.collected_at:
        pairs.append(("Insamlat", stamp(row.collected_at)))
    if row.confirmed_at:
        pairs.append(("Bekräftat", stamp(row.confirmed_at)))
    if row.text_shown:
        pairs.append(("Texten personen såg", row.text_shown))
    if row.evidence:
        pairs.append(("Var och när", row.evidence))
    changed = stamp(row.changed_at)
    if row.changed_by_label:
        changed = f"{changed} av {row.changed_by_label}"
    pairs.append(("Senast ändrat", changed))
    return pairs


def _channels(kontakt):
    by_channel = {c.channel: c for c in kontakt.consents.all()}
    out = []
    for channel in (CHANNEL_SMS, CHANNEL_EMAIL):
        row = by_channel.get(channel)
        address = kontakt.address(channel)
        out.append(
            {
                "key": channel,
                "word": _CHANNEL_WORD[channel],
                "address": normalize.display_phone(address) if channel == CHANNEL_SMS else address,
                "href": f"tel:{address}" if channel == CHANNEL_SMS else f"mailto:{address}",
                "chip": consents.chip(kontakt, channel, row),
                "proof": _proof(row) if address else [],
            }
        )
    return out


def _summary(kontakt, leads):
    """Sammanfattningen: "6 utskick · 2 klick · 1 förfrågan" (I.7). Utskick
    räknas som skickade mottagare, klick som mänskliga klick (S2)."""
    from django.db.models import Sum

    from ..models import Recipient

    sent = Recipient.objects.filter(
        utskick__account_id=kontakt.account_id,
        contact=kontakt,
        status__in=Recipient.SENT_LIKE,
    )
    n_sent = sent.count()
    clicks = int(
        Recipient.objects.filter(utskick__account_id=kontakt.account_id, contact=kontakt).aggregate(
            n=Sum("click_count")
        )["n"]
        or 0
    )
    parts = []
    if n_sent:
        parts.append(count_text(n_sent, "utskick", "utskick"))
    if clicks:
        parts.append(count_text(clicks, "klick", "klick"))
    if leads:
        parts.append(count_text(leads, "förfrågan", "förfrågningar"))
    return " · ".join(parts)


def _header_line(kontakt):
    source = kontakt.get_source_display()
    if kontakt.source != Contact.Source.API:
        source = source.lower()
    return (
        f"{kontakt.get_kind_display()} · kontakt sedan {month_text(kontakt.created_at)}"
        f" · källa: {source}"
    )


#: Källor utan egen rad i tidslinjen: kortet visar "Lades till i Kontakter".
ADDED_BY_HAND = (Contact.Source.MANUAL, Contact.Source.API)


def _render_detail(request, account, kontakt, consent_form=None, status=200, sms=None):
    # S4 (rapport-byggaren): sms är None på kortet och {"text", "error"} när
    # rutan "Skicka sms" är öppen (app_views/contact_sms.py).
    now = timezone.now()
    kontakt = (
        Contact.objects.filter(pk=kontakt.pk, account=account)
        .prefetch_related(
            "consents",
            "tags",
            Prefetch("memberships", queryset=ListMembership.objects.select_related("list")),
        )
        .get()
    )
    defs = list(FieldDef.objects.filter(account=account).order_by("order", "pk"))
    values = kontakt.fields or {}
    fields_shown = [(d.label, field_display(d, values[d.key])) for d in defs if values.get(d.key)]
    member_lists = sorted((m.list for m in kontakt.memberships.all()), key=lambda c: c.name)
    own_tags = list(kontakt.tags.all())
    page = timeline.for_contact(kontakt, page=request.GET.get("sida"))
    items = list(page.items)
    if not page.has_next and kontakt.source in ADDED_BY_HAND:
        # Sist på sista sidan: när kontakten lades till. Import, anmälan och
        # förfrågan har redan en egen rad i tidslinjen.
        items.append(
            timeline.Item(
                at=kontakt.created_at,
                kind="created",
                title="Lades till i Kontakter",
                detail=kontakt.get_source_display(),
            )
        )
    leads = register.linked_leads(kontakt).count()
    context = {
        "kontakt": kontakt,
        "header_line": _header_line(kontakt),
        "channels": _channels(kontakt),
        "fields_shown": fields_shown,
        "member_lists": member_lists,
        "own_tags": own_tags,
        "other_lists": ContactList.objects.filter(account=account)
        .exclude(pk__in=[c.pk for c in member_lists])
        .order_by("name"),
        "other_tags": Tag.objects.filter(account=account)
        .exclude(pk__in=[t.pk for t in own_tags])
        .order_by("name"),
        "summary": _summary(kontakt, leads),
        "timeline_page": page,
        "timeline_items": [{"item": item, "when": stamp(item.at, now)} for item in items],
        "consent_form": consent_form,
        "new_value": NEW,
    }
    # --- S4 (segment-byggaren): segmenten kontakten är med i (chipsen, I.7) ---
    from .. import segments as segment_rules

    context["segment_chips"] = segment_rules.for_contact(kontakt, now)
    # --- slut S4
    # --- S4 (rapport-byggaren): "Svarar oftast", "Skicka sms" och rutan (I.7) ---
    from .contact_sms import card_context

    context.update(card_context(request, account, kontakt, sms))
    # --- slut S4
    return render_contacts(
        request, "flamingo/app/kontakter/detail.html", "contacts", context, status=status
    )


def _card_list_add(request, account, kontakt):
    target = list_target(request, account)
    register.add_to_list(target, [kontakt.pk])
    messages.success(request, f"Lades i listan {target.name}.")


def _card_list_remove(request, account, kontakt):
    target = list_target(request, account, allow_new=False)
    register.remove_from_list(target, [kontakt.pk])
    messages.success(request, f"Togs bort ur listan {target.name}.")


def _card_tag_add(request, account, kontakt):
    target = tag_target(request, account)
    register.add_tag(target, [kontakt.pk])
    messages.success(request, f"Fick taggen {target.name}.")


def _card_tag_remove(request, account, kontakt):
    target = tag_target(request, account, allow_new=False)
    register.remove_tag(target, [kontakt.pk])
    messages.success(request, f"Taggen {target.name} togs bort.")


_CARD_ACTIONS = {
    "list_add": _card_list_add,
    "list_remove": _card_list_remove,
    "tag_add": _card_tag_add,
    "tag_remove": _card_tag_remove,
}


@utskick_view
def contact_detail(request, account, pk):
    kontakt = owned(Contact, account, pk)
    if request.method == "POST":
        handler = _CARD_ACTIONS.get(request.POST.get("action", ""))
        if handler is not None:
            try:
                handler(request, account, kontakt)
            except Refused as exc:
                messages.error(request, exc.message)
        return redirect(reverse("flamingo:app_contact", args=[kontakt.pk]) + "#listor")
    return _render_detail(request, account, kontakt)


@utskick_view
def contact_consent(request, account, pk):
    """Rutan "Ändra samtycke" på kortet. GET ritar kortet med rutan öppen
    (?kanal= väljer kanalen), POST sparar via consent.set_status."""
    kontakt = owned(Contact, account, pk)
    if request.method != "POST":
        channel = request.GET.get("kanal")
        initial = {"channel": channel} if channel in (CHANNEL_SMS, CHANNEL_EMAIL) else {}
        form = ConsentForm(initial=initial, kontakt=kontakt)
        return _render_detail(request, account, kontakt, consent_form=form)
    form = ConsentForm(request.POST, kontakt=kontakt)
    if form.is_valid():
        data = form.cleaned_data
        try:
            outcome = consents.set_status(
                kontakt,
                data["channel"],
                data["status"],
                source=Consent.Source.MANUAL,
                actor=actor_for(request),
                evidence=data.get("evidence", ""),
            )
        except keys.KeyMismatch:
            form.add_error(None, KEY_TEXT)
        else:
            if outcome.refused:
                form.add_error(None, outcome.refusal_text)
            else:
                chip = consents.chip(kontakt, data["channel"], outcome.consent)
                if outcome.changed:
                    messages.success(request, f"{chip['label']}. Ändringen finns i tidslinjen.")
                else:
                    messages.info(request, f"{chip['label']}: ingen ändring.")
                return redirect("flamingo:app_contact", pk=kontakt.pk)
    return _render_detail(request, account, kontakt, consent_form=form)


# ---------------------------------------------------------------------------
# GDPR: export och borttagning per person (H.4)
# ---------------------------------------------------------------------------


@utskick_view
def contact_export(request, account, pk):
    kontakt = owned(Contact, account, pk)
    if request.method != "POST":
        # Efter owned: ett annat kontos kontakt är 404 också för GET.
        return HttpResponseNotAllowed(["POST"])
    data = register.export_contact(kontakt)
    register.log_export(account, actor_for(request), ExportLog.Kind.CONTACT, 1)
    body = json.dumps(data, ensure_ascii=False, indent=2)
    response = HttpResponse(body, content_type="application/json; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="kontakt-{kontakt.pk}.json"'
    response["Cache-Control"] = "no-store"
    return response


def _render_delete(request, account, kontakt=None, selection=None):
    from apps.flamingo.models import Lead

    if kontakt is not None:
        leads = register.linked_leads(kontakt).count()
        count = 1
    else:
        leads = Lead.objects.filter(account=account, contact__in=selection.contacts).count()
        count = selection.count
    context = {
        "kontakt": kontakt,
        "selection": selection,
        "hidden": selection.hidden() if selection is not None else [],
        "count": count,
        "leads": leads,
        "delete_max": DELETE_MAX,
        "over_max": count > DELETE_MAX,
        "back_url": (
            reverse("flamingo:app_contact", args=[kontakt.pk])
            if kontakt is not None
            else list_url(selection.filters)
        ),
    }
    return render_contacts(request, "flamingo/app/kontakter/delete.html", "contacts", context)


def _delete_many(request, contacts, delete_leads=None, suppress=None):
    """Ta bort högst DELETE_MAX kontakter, var och en i sin transaktion.
    (antal, förfrågningar, spärrar), eller (None, 0, 0) vid fel nyckel."""
    if delete_leads is None:
        delete_leads = request.POST.get("forfragningar") == "1"
    if suppress is None:
        suppress = request.POST.get("sparr") == "1"
    actor = actor_for(request)
    done = leads = suppressed = 0
    try:
        for kontakt in contacts.order_by("pk")[:DELETE_MAX]:
            summary = register.delete_contact(
                kontakt, actor=actor, delete_leads=delete_leads, suppress=suppress
            )
            done += 1
            leads += summary["leads"]
            suppressed += summary["suppressed"]
    except keys.KeyMismatch:
        return None, 0, 0
    return done, leads, suppressed


@utskick_view
def contact_delete(request, account, pk):
    kontakt = owned(Contact, account, pk)
    if request.method != "POST":
        return _render_delete(request, account, kontakt=kontakt)
    name = kontakt.display_name
    try:
        summary = register.delete_contact(
            kontakt,
            actor=actor_for(request),
            delete_leads=request.POST.get("forfragningar") == "1",
            suppress=request.POST.get("sparr") == "1",
        )
    except keys.KeyMismatch:
        messages.error(request, KEY_TEXT)
        return redirect("flamingo:app_contact", pk=pk)
    text = f"{name} är borttagen."
    if summary["leads"]:
        text += f" {count_text(summary['leads'], 'förfrågan', 'förfrågningar')} togs också bort."
    if summary["suppressed"]:
        text += " Adresserna ligger på spärrlistan."
    messages.success(request, text)
    return redirect("flamingo:app_contacts")


# ---------------------------------------------------------------------------
# Rensa inaktiva (E.7)
# ---------------------------------------------------------------------------


@utskick_view
def contacts_prune(request, account):
    """Kontakter som utskick_daily flaggat (inget samtycke, två år utan
    aktivitet). Kunden bestämmer: inget tas bort av sig självt. Ingen spärr
    läggs (det är ingen begäran från personen) och förfrågningarna finns kvar."""
    contacts = _prunable(account)
    if request.method == "POST" and request.POST.get("bekrafta") == "1":
        done, _, _ = _delete_many(request, contacts, delete_leads=False, suppress=False)
        if done is None:
            messages.error(request, KEY_TEXT)
        elif done:
            messages.success(
                request, f"{count_text(done, 'inaktiv kontakt', 'inaktiva kontakter')} togs bort."
            )
        return redirect("flamingo:app_contacts")
    count = contacts.count()
    preview = decorate(list(with_rows(contacts.order_by("last_activity_at", "pk"))[:10]), account)
    context = {
        "count": count,
        "kontakter": preview,
        "delete_max": DELETE_MAX,
        "over_max": count > DELETE_MAX,
    }
    return render_contacts(request, "flamingo/app/kontakter/prune.html", "contacts", context)
