"""
Segmentbyggaren (README I.1, I.11, H.1, J S4), under Kontakter med fliken
Listor (segmenten listas på Listor, app_views/lists.py).

    segment_new      kontakter/segment/ny/        GET formuläret, POST sparar (namn, villkor)
    segment_detail   kontakter/segment/<pk>/      GET, POST action=save | delete
    segment_count    kontakter/segment/antal/     POST {"rules": ...} som JSON, eller
                                                  formulärets fält (flamingo-app-segment.js
                                                  skickar formuläret som det står) ->
                                                  {"ok", "total", "sms", "email", "total_text",
                                                   "text", "note", "lines"}
                                                  ("388 kan få sms · 301 kan få e-post")

Formulärets fält per villkor n (sidans rader numreras i ordning; skriptet ger
en ny rad nästa lediga nummer):

    r<n>_g      gruppen: "" (OCH) eller en nyckel som g1 (raderna i en grupp: ELLER)
    r<n>_f      fältet: list, tag, field:<nyckel>, contact:<namn>, consent:sms,
                consent:email, kind, source, created, got_utskick, opened, clicked,
                visited_lp, lead, replied
    r<n>_op     operatorn; för datum before, within eller next med r<n>_unit
                days eller months (blir before_months och så vidare)
    r<n>_v      värdet: en väljare (flera för in och not_in), en text eller ett tal
    r<n>_n      antal dagar eller månader

rules_from_post bygger Segment.rules av fälten och segments.clean prövar dem.
Utan skript är "+ Villkor", "+ Grupp (ELLER)", "+ Villkor i gruppen", "Ta
bort" och "Visa valen" knappar som skickar formuläret och ritar om sidan
(inget sparas förrän Spara segmentet); skriptet gör samma sak på sidan och
räknar medan kunden bygger.

Reglerna prövas med segments.clean: ett främmande id ger 400 (ForeignIds,
utskick_view; också för knapparna som bara ritar om), andra fel visas per
regel. Räkningen anropas medan kunden bygger, så den har en gräns per konto
(Counter "segment_count", 60 i minuten). Ett segment som används i ett
utskick som inte är klart går inte att ta bort. Raderna staplas under 760
px; inga style-attribut, inget inline-skript.
"""

import json
import re

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .. import audience, limits, segments
from ..access import owned, utskick_view
from ..models import Consent, Contact, ContactList, FieldDef, Segment, Tag, Utskick
from ..segments import SegmentError
from . import render_contacts
from .contacts import clean_name, group

#: Räkningens gräns per konto och minut (limits.hit, scope segment_count).
COUNT_SCOPE = "segment_count"
COUNT_PER_MINUTE = 60
COUNT_BODY_MAX = 64 * 1024
#: Formulärets rader läses högst så här många (fler ger felet från clean).
MAX_FORM_ROWS = segments.MAX_RULES + 5
#: Så många av de senaste utskicken går att välja i en regel.
UTSKICK_CHOICES = 100

NAME_TEXT = "Skriv ett namn på segmentet."
EXISTS_TEXT = "Det finns redan ett segment med det namnet."
EMPTY_TEXT = "Lägg till minst ett villkor."
ROWS_TEXT = "Rätta villkoren som är markerade."
IN_USE_TEXT = "Segmentet används i {names}. Ta bort det ur mottagarna där först."
LIMIT_TEXT = "Räkningen tar en paus. Den kommer tillbaka om en stund."
#: När räkningen inte svarar med JSON (till exempel 400 för ett främmande id).
COUNT_ERROR_TEXT = "Räkningen gick inte att göra just nu. Segmentet räknas när du sparar det."
READ_TEXT = "Villkoren går inte att läsa."
NO_RULES_NOTE = "Lägg till ett villkor för att se vilka som matchar."

#: Operatorer som byggs ihop med enheten (r<n>_unit): before -> before_months.
COMPOSED = ("before", "within", "next")
LIST_OPS = ("in", "not_in")
SCALAR_OPS = ("eq", "not_eq", "contains", "lt", "gt")
_ROW_FIELD = re.compile(r"^r(\d{1,3})_f$")
#: Nya rader börjar med det här fältet.
DEFAULT_FIELD = "list"

# --- Operatorernas texter i byggaren (fältet står före: "Lista finns i Kunder") ---

TEXT_FORM_OPS = (
    ("eq", "är"),
    ("not_eq", "är inte"),
    ("contains", "innehåller"),
    ("empty", "saknas"),
    ("not_empty", "finns"),
)
NUMBER_FORM_OPS = (
    ("eq", "är"),
    ("lt", "är mindre än"),
    ("gt", "är större än"),
    ("empty", "saknas"),
    ("not_empty", "finns"),
)
DATE_FORM_OPS = (
    ("before", "äldre än"),
    ("within", "inom senaste"),
    ("next", "inom kommande"),
    ("empty", "saknas"),
    ("not_empty", "finns"),
)
CHOICE_FORM_OPS = (
    ("in", "är"),
    ("not_in", "är inte"),
    ("empty", "saknas"),
    ("not_empty", "finns"),
)
ACTIVITY_FORM_OPS = {
    "got_utskick": (
        ("in", "fick"),
        ("not_in", "fick inte"),
        ("within_days", "fick något de senaste"),
        ("not_within_days", "fick inget de senaste"),
    ),
    "opened": (
        ("in", "öppnade"),
        ("not_in", "öppnade inte"),
        ("within_days", "öppnade något de senaste"),
        ("not_within_days", "öppnade inget de senaste"),
    ),
    "clicked": (
        ("in", "klickade i"),
        ("not_in", "klickade inte i"),
        ("within_days", "klickade de senaste"),
        ("not_within_days", "klickade inte de senaste"),
    ),
    "visited_lp": (
        ("within_days", "besökte en sida de senaste"),
        ("not_within_days", "besökte ingen sida de senaste"),
    ),
    "lead": (
        ("within_days", "skickade en de senaste"),
        ("not_within_days", "skickade ingen de senaste"),
    ),
    "replied": (
        ("within_days", "svarade de senaste"),
        ("not_within_days", "svarade inte de senaste"),
    ),
}
ACTIVITY_LABELS = {
    "got_utskick": "Utskick",
    "opened": "Öppnade mejl",
    "clicked": "Klick",
    "visited_lp": "Besök via länk",
    "lead": "Förfrågan",
    "replied": "Svar",
}


# ---------------------------------------------------------------------------
# Byggarens fält: vad som går att välja och hur värdet skrivs
# ---------------------------------------------------------------------------


def _ops_text(ops):
    return " ".join(ops)


def _pick(ops, options, placeholder, label):
    return {
        "kind": "pick",
        "ops": _ops_text(ops),
        "options": [(str(value), text) for value, text in options],
        "placeholder": placeholder,
        "label": label,
    }


def _count_widget(ops, units):
    return {
        "kind": "count",
        "ops": _ops_text(ops),
        "units": units,
        "max": segments.MAX_DAYS,
    }


class Builder:
    """Det som går att välja i kontots segmentbyggare: fälten i grupper
    (väljaren) och per fält operatorerna och värdets väljare. Namnen hämtas
    alltid med kontot i villkoret."""

    def __init__(self, account):
        self.account = account
        self.locked = segments.opened_locked(account)
        self.lists = list(
            ContactList.objects.filter(account=account).order_by("name").values_list("pk", "name")
        )
        self.tags = list(
            Tag.objects.filter(account=account).order_by("name").values_list("pk", "name")
        )
        self.utskick = list(
            Utskick.objects.listed()
            .filter(account=account, frozen_at__isnull=False)
            .order_by("-frozen_at", "-pk")
            .values_list("pk", "name")[:UTSKICK_CHOICES]
        )
        self.fields = list(FieldDef.objects.filter(account=account).order_by("order", "pk"))
        self.specs = {}
        self._build()

    def _add(self, value, label, ops, widgets=()):
        self.specs[value] = {
            "value": value,
            "label": label,
            "ops": list(ops),
            "widgets": list(widgets),
        }

    def _build(self):
        self._add(
            "list",
            "Lista",
            (("in", "finns i"), ("not_in", "finns inte i")),
            [_pick(LIST_OPS, self.lists, "Välj lista", "Lista")],
        )
        self._add(
            "tag",
            "Tagg",
            (("in", "har"), ("not_in", "har inte")),
            [_pick(LIST_OPS, self.tags, "Välj tagg", "Tagg")],
        )
        for definition in self.fields:
            value = f"field:{definition.key}"
            kind = definition.kind
            if kind == FieldDef.Kind.NUMBER:
                widgets = [{"kind": "number", "ops": "eq lt gt", "label": "Tal"}]
                self._add(value, definition.label, NUMBER_FORM_OPS, widgets)
            elif kind == FieldDef.Kind.DATE:
                widgets = [_count_widget(COMPOSED, True)]
                self._add(value, definition.label, DATE_FORM_OPS, widgets)
            elif kind == FieldDef.Kind.CHOICE:
                options = [(c, c) for c in definition.choices or ()]
                widgets = [_pick(LIST_OPS, options, "Välj värde", "Värde")]
                self._add(value, definition.label, CHOICE_FORM_OPS, widgets)
            else:
                widgets = [{"kind": "text", "ops": "eq not_eq contains", "label": "Värde"}]
                self._add(value, definition.label, TEXT_FORM_OPS, widgets)
        self._add(
            "kind",
            "Typ",
            (("eq", "är"),),
            [_pick(("eq",), Contact.Kind.choices, "Välj typ", "Typ")],
        )
        self._add(
            "source",
            "Källa",
            (("in", "är"), ("not_in", "är inte")),
            [_pick(LIST_OPS, Contact.Source.choices, "Välj källa", "Källa")],
        )
        self._add(
            "created",
            "Tillagd i Kontakter",
            (("before", "för mer än"), ("within", "inom senaste")),
            [
                _count_widget(("before", "within"), True),
                {"kind": "word", "ops": "before", "text": "sedan"},
            ],
        )
        for name, (label, allowed) in segments.CONTACT_FIELDS.items():
            ops = [op for op in TEXT_FORM_OPS if op[0] in allowed]
            ops.sort(key=lambda op: allowed.index(op[0]))
            widgets = []
            if any(op in allowed for op in SCALAR_OPS):
                widgets = [{"kind": "text", "ops": "eq not_eq contains", "label": label}]
            self._add(f"contact:{name}", label, ops, widgets)
        statuses = [(value, text) for value, text in Consent.Status.choices]
        for channel, word in (("sms", "sms"), ("email", "e-post")):
            self._add(
                f"consent:{channel}",
                f"Samtycke för {word}",
                (
                    ("eligible", "kan få erbjudanden"),
                    ("not_eligible", "kan inte få erbjudanden"),
                    ("in", "är"),
                    ("not_in", "är inte"),
                ),
                [_pick(LIST_OPS, statuses, "Välj samtycke", "Samtycke")],
            )
        for name, ops in ACTIVITY_FORM_OPS.items():
            widgets = []
            if "in" in dict(ops):
                widgets.append(_pick(LIST_OPS, self.utskick, "Välj utskick", "Utskick"))
            widgets.append(_count_widget(("within_days", "not_within_days"), False))
            self._add(name, ACTIVITY_LABELS[name], ops, widgets)

    def field_groups(self):
        """Väljarens grupper: [(rubrik, [(värde, text, låst)])]."""

        def entries(values):
            return [(v, self.specs[v]["label"], False) for v in values if v in self.specs]

        activity = []
        for name in ACTIVITY_FORM_OPS:
            if name == "opened" and self.locked:
                activity.append((name, f"{ACTIVITY_LABELS[name]} (låst)", True))
            else:
                activity.append((name, ACTIVITY_LABELS[name], False))
        groups = [
            ("Listor och taggar", entries(["list", "tag"])),
            ("Extrafält", entries([f"field:{d.key}" for d in self.fields])),
            (
                "Kontakten",
                entries(
                    ["kind", "source", "created"]
                    + [f"contact:{n}" for n in segments.CONTACT_FIELDS]
                ),
            ),
            ("Samtycke", entries(["consent:sms", "consent:email"])),
            ("Aktivitet", activity),
        ]
        return [(title, rows) for title, rows in groups if rows]

    def default_rule(self):
        spec = self.specs[DEFAULT_FIELD]
        return {"f": DEFAULT_FIELD, "op": spec["ops"][0][0]}

    # --- en regel som en rad i formuläret ----------------------------------

    def row(self, n, group_key, rule, error=""):
        """Raden för formuläret ur en regel (sparad, ur JSON eller rå ur
        formuläret): fältet, operatorn (datum delad i op och enhet), värdena
        som text och väljarna med det valda markerat."""
        rule = rule if isinstance(rule, dict) else {}
        f = rule.get("f") if isinstance(rule.get("f"), str) else ""
        op = rule.get("op") if isinstance(rule.get("op"), str) else ""
        value = rule.get("v")
        if f == "consent":
            channel = value.get("channel") if isinstance(value, dict) else ""
            f = f"consent:{channel}"
            value = value.get("status") if isinstance(value, dict) else None
        spec = self.specs.get(f)
        if f == "opened" and self.locked:
            spec = None
            error = error or segments.OPENED_LOCKED_TEXT
        if spec is None and f and not error:
            error = segments.GONE_FIELD_TEXT if f.startswith("field:") else READ_TEXT
        op_values = [o for o, _label in spec["ops"]] if spec else []
        unit = "days"
        for prefix in COMPOSED:
            if op in (f"{prefix}_days", f"{prefix}_months") and prefix in op_values:
                unit = op.rsplit("_", 1)[1]
                op = prefix
        if spec and op not in op_values:
            op = op_values[0]
        values = [str(v) for v in (value if isinstance(value, list) else [value]) if v is not None]
        scalar = "" if isinstance(value, (list, dict)) or value is None else str(value)
        widgets = []
        for widget in spec["widgets"] if spec else ():
            shown = dict(widget)
            if widget["kind"] == "pick":
                known = {v for v, _t in widget["options"]}
                chosen = [v for v in values if v]
                missing = [v for v in chosen if v not in known]
                if missing and op in widget["ops"].split():
                    shown["options"] = widget["options"] + self._extra_options(f, missing)
                    known = {v for v, _t in shown["options"]}
                    if any(v not in known for v in missing) and not error:
                        error = "Det valda finns inte längre. Välj igen."
                shown["multiple"] = len(chosen) > 1
            widgets.append(shown)
        return {
            "n": n,
            "group": group_key,
            "field": f if spec else "",
            "spec": spec,
            "op": op,
            "unit": unit,
            "values": values,
            "scalar": scalar,
            "count": scalar,
            "widgets": widgets,
            "error": error,
        }

    def _extra_options(self, f, missing):
        """Ett utskick som är för gammalt för väljaren (eller inte fryst)
        men står i en sparad regel: med i väljaren, med kontot i villkoret."""
        if f not in ("got_utskick", "opened", "clicked"):
            return []
        ids = [int(v) for v in missing if v.isdigit()]
        rows = Utskick.objects.filter(account=self.account, pk__in=ids).values_list("pk", "name")
        return [(str(pk), name) for pk, name in rows]

    def blank_row(self, spec_value, n="__N__"):
        """En tom rad för skriptets mallar."""
        spec = self.specs[spec_value]
        return self.row(n, "", {"f": spec_value, "op": spec["ops"][0][0]})

    def form_items(self, rules, row_errors=None):
        """Raderna och grupperna för formuläret: [{"row"} | {"group", "key",
        "rows"}], numrerade i ordning. Reglerna kan vara råa (formuläret)."""
        row_errors = row_errors or {}
        items_in = segments._items(rules) or []
        items, n, groups = [], 0, 0
        for item in items_in:
            if isinstance(item, dict) and "any" in item:
                groups += 1
                key = f"g{groups}"
                members = item["any"] if isinstance(item["any"], list) else []
                rows = []
                for rule in members:
                    rows.append(self.row(n, key, rule, row_errors.get(n + 1, "")))
                    n += 1
                items.append({"group": True, "key": key, "rows": rows})
            else:
                error = row_errors.get(n + 1, "")
                items.append({"group": False, "row": self.row(n, "", item, error)})
                n += 1
        return items, n, groups


# ---------------------------------------------------------------------------
# Formuläret -> Segment.rules
# ---------------------------------------------------------------------------


def _rule_from_post(data, n):
    p = f"r{n}_"
    field = str(data.get(p + "f") or "").strip()[:80]
    op = str(data.get(p + "op") or "").strip()[:20]
    name, _, key = field.partition(":")
    if op in COMPOSED:
        op = f"{op}_{'months' if data.get(p + 'unit') == 'months' else 'days'}"
    if name == "consent":
        value = {"channel": key}
        if op in LIST_OPS:
            value["status"] = data.getlist(p + "v")
        return {"f": "consent", "op": op, "v": value, "_n": n}
    rule = {"f": field, "op": op, "_n": n}
    if op in LIST_OPS:
        rule["v"] = data.getlist(p + "v")
    elif op in SCALAR_OPS or name == "kind":
        rule["v"] = data.get(p + "v", "")
    elif op.endswith(("_days", "_months")):
        rule["v"] = data.get(p + "n", "")
    return rule


def rules_from_post(data):
    """Segment.rules ur formulärets fält (se modulens docstring), råa:
    segments.clean prövar dem. Varje regel bär sitt radnummer i "_n" (för
    Ta bort utan skript); clean och compile_q läser bara f, op och v."""
    indexes = sorted({int(m.group(1)) for key in data if (m := _ROW_FIELD.match(key))})
    items, groups = [], {}
    for n in indexes[:MAX_FORM_ROWS]:
        rule = _rule_from_post(data, n)
        key = str(data.get(f"r{n}_g") or "").strip()[:8]
        if key:
            if key not in groups:
                groups[key] = {"any": []}
                items.append(groups[key])
            groups[key]["any"].append(rule)
        else:
            items.append(rule)
    return {"all": items}


def _edit(builder, rules, action):
    """Knapparna utan skript: + Villkor, + Grupp (ELLER), + Villkor i
    gruppen och Ta bort, på de råa reglerna."""
    items = rules["all"]
    if action == "add_rule":
        items.append(builder.default_rule())
    elif action == "add_group":
        items.append({"any": [builder.default_rule(), builder.default_rule()]})
    elif action.startswith("add_to:g") and action[8:].isdigit():
        position = int(action[8:])
        found = [item for item in items if isinstance(item, dict) and "any" in item]
        if 1 <= position <= len(found):
            found[position - 1]["any"].append(builder.default_rule())
    elif action.startswith("remove:") and action[7:].isdigit():
        n = int(action[7:])
        kept = []
        for item in items:
            if isinstance(item, dict) and "any" in item:
                item["any"] = [r for r in item["any"] if r.get("_n") != n]
                if item["any"]:
                    kept.append(item)
            elif item.get("_n") != n:
                kept.append(item)
        rules["all"] = kept
    return rules


# ---------------------------------------------------------------------------
# Räkningen och sidan
# ---------------------------------------------------------------------------


def count_texts(counted):
    """ "412" och "388 kan få sms · 301 kan få e-post"."""
    return (
        group(counted["total"]),
        f"{group(counted['sms'])} kan få sms · {group(counted['email'])} kan få e-post",
    )


def _skipped_note(skipped):
    if not skipped:
        return ""
    if len(skipped) == 1:
        return f"Villkor {skipped[0]} räknas inte förrän det är ifyllt."
    numbers = ", ".join(str(n) for n in skipped[:-1]) + f" och {skipped[-1]}"
    return f"Villkor {numbers} räknas inte förrän de är ifyllda."


def live_count(account, raw, now=None):
    """Räkningen för de (kanske ofullständiga) reglerna ur formuläret eller
    JSON: {"ok", "total", "sms", "email", "total_text", "text", "note",
    "lines"}. Främmande id:n ger ForeignIds (400)."""
    try:
        rules, skipped = segments.clean_partial(account, raw)
    except SegmentError as exc:
        return {
            "ok": False,
            "total": None,
            "sms": None,
            "email": None,
            "total_text": "",
            "text": "",
            "note": exc.errors[0] if exc.errors else READ_TEXT,
            "lines": [],
        }
    note = _skipped_note(skipped) or ("" if rules["all"] else NO_RULES_NOTE)
    return _counted(account, rules, now, note)


def stored_count(account, rules, now=None):
    """Räkningen för ett sparat segment: reglerna prövas inte igen (en lista
    som tagits bort sedan ska inte ge 400), compile_q och describe tål det
    som inte finns längre och räknar det som ingen."""
    note = "" if segments._items(rules) else NO_RULES_NOTE
    return _counted(account, rules, now, note)


def _counted(account, rules, now, note):
    counted = segments.count(account, rules, now)
    total_text, text = count_texts(counted)
    return {
        "ok": True,
        **counted,
        "total_text": total_text,
        "text": text,
        "note": note,
        "lines": segments.describe(account, rules),
    }


def used_by(account, segment):
    """Utskicken som inte är klara och har segmentet bland mottagarna eller
    undantagen (namnen)."""
    names = []
    rows = (
        Utskick.objects.listed()
        .filter(account=account)
        .exclude(status__in=Utskick.FINISHED)
        .only("pk", "name", "audience")
    )
    for row in rows:
        aud = audience.stored(row)
        if segment.pk in aud["segments"] or segment.pk in aud["exclude"]["segments"]:
            names.append(row.name)
    return names


def _render(
    request,
    account,
    segment,
    *,
    name,
    rules,
    builder=None,
    row_errors=None,
    notices=(),
    name_error="",
    counted=None,
    status=200,
):
    builder = builder or Builder(account)
    items, rows, groups = builder.form_items(rules, row_errors)
    if counted is None:
        counted = live_count(account, rules)
    context = {
        "segment": segment,
        "name": name,
        "name_error": name_error,
        "notices": list(notices),
        "items": items,
        "next_index": rows,
        "group_count": groups,
        "field_groups": builder.field_groups(),
        "templates": [
            blank for blank in (builder.blank_row(v) for v in builder.specs) if blank["spec"]
        ],
        "new_row": builder.blank_row(DEFAULT_FIELD),
        "counted": counted,
        "count_error_text": COUNT_ERROR_TEXT,
        "opened_locked": builder.locked,
        "opened_locked_text": segments.OPENED_LOCKED_TEXT,
        "used_by": used_by(account, segment) if segment is not None else [],
        "full": segment is None
        and Segment.objects.filter(account=account).count() >= Segment.MAX_PER_ACCOUNT,
        "full_text": segments.FULL_TEXT,
        "max_rules": segments.MAX_RULES,
        "max_groups": segments.MAX_GROUPS,
        "action_url": (
            reverse("flamingo:app_segment", args=[segment.pk])
            if segment is not None
            else reverse("flamingo:app_segment_new")
        ),
    }
    return render_contacts(
        request, "flamingo/app/kontakter/segment.html", "lists", context, status=status
    )


def _save(request, account, segment):
    """Spara (action=save): namnet och reglerna prövas, ett nytt segment
    skapas eller det befintliga ändras, och räkningen sparas."""
    name = clean_name(request.POST.get("namn"), 80)
    raw = rules_from_post(request.POST)
    builder = Builder(account)
    row_errors, notices, name_error = {}, [], ""
    rules = None
    if not name:
        name_error = NAME_TEXT
    try:
        rules = segments.clean(account, raw)
    except SegmentError as exc:
        row_errors = exc.rows
        notices = [ROWS_TEXT] if exc.rows else exc.errors
    else:
        if not rules["all"]:
            notices.append(EMPTY_TEXT)
    if segment is None and not notices and not name_error:
        if Segment.objects.filter(account=account).count() >= Segment.MAX_PER_ACCOUNT:
            notices.append(segments.FULL_TEXT)
    if name_error or notices or row_errors:
        return _render(
            request,
            account,
            segment,
            name=name,
            rules=raw,
            builder=builder,
            row_errors=row_errors,
            notices=notices,
            name_error=name_error,
        )
    created = segment is None
    row = segment or Segment(account=account, created_by=_user(request))
    row.name = name
    row.rules = rules
    try:
        with transaction.atomic():
            row.save()
    except IntegrityError:
        return _render(
            request,
            account,
            segment,
            name=name,
            rules=raw,
            builder=builder,
            name_error=EXISTS_TEXT,
        )
    segments.refresh(row)
    _total, text = count_texts(
        {"total": row.cached_count, "sms": row.cached_sms, "email": row.cached_email}
    )
    verb = "skapat" if created else "sparat"
    messages.success(
        request, f"Segmentet {row.name} är {verb}. {group(row.cached_count)} matchar: {text}."
    )
    return redirect("flamingo:app_segment", pk=row.pk)


def _user(request):
    user = getattr(request, "user", None)
    return user if user is not None and user.is_authenticated else None


def _redraw(request, account, segment, action):
    """Knapparna som bara ändrar formuläret (utan skript): ritar om sidan med
    raden eller gruppen tillagd eller borttagen. Inget sparas, men varje id
    prövas ändå (ForeignIds, 400)."""
    name = clean_name(request.POST.get("namn"), 80)
    builder = Builder(account)
    raw = _edit(builder, rules_from_post(request.POST), action)
    return _render(request, account, segment, name=name, rules=raw, builder=builder)


def _delete(request, account, segment):
    names = used_by(account, segment)
    if names:
        listed = ", ".join(names[:3]) + (" med flera" if len(names) > 3 else "")
        messages.error(request, IN_USE_TEXT.format(names=f"utskicket {listed}"))
        return redirect("flamingo:app_segment", pk=segment.pk)
    name = segment.name
    segment.delete()
    messages.success(request, f"Segmentet {name} är borttaget. Kontakterna finns kvar.")
    return redirect(reverse("flamingo:app_lists") + "#segment")


def _post(request, account, segment):
    action = str(request.POST.get("action") or "save")
    if action == "delete" and segment is not None:
        return _delete(request, account, segment)
    if action == "save":
        return _save(request, account, segment)
    return _redraw(request, account, segment, action)


@utskick_view
@require_http_methods(["GET", "HEAD", "POST"])
def segment_new(request, account):
    if request.method == "POST":
        return _post(request, account, None)
    builder = Builder(account)
    rules = {"all": [builder.default_rule()]}
    return _render(request, account, None, name="", rules=rules, builder=builder)


@utskick_view
@require_http_methods(["GET", "HEAD", "POST"])
def segment_detail(request, account, pk):
    segment = owned(Segment, account, pk)
    if request.method == "POST":
        return _post(request, account, segment)
    now = timezone.now()
    builder = Builder(account)
    counted = stored_count(account, segment.rules, now)
    # Sidan räknar ändå: listans siffra blir lika färsk.
    Segment.objects.filter(pk=segment.pk).update(
        cached_count=counted["total"],
        cached_sms=counted["sms"],
        cached_email=counted["email"],
        counted_at=now,
    )
    return _render(
        request,
        account,
        segment,
        name=segment.name,
        rules=segment.rules,
        builder=builder,
        counted=counted,
    )


@utskick_view
@require_POST
def segment_count(request, account):
    now = timezone.now()
    window = now.replace(second=0, microsecond=0)
    if limits.hit(COUNT_SCOPE, str(account.pk), window, COUNT_PER_MINUTE):
        return JsonResponse({"ok": False, "note": LIMIT_TEXT}, status=429)
    if len(request.body) > COUNT_BODY_MAX:
        return JsonResponse({"ok": False, "note": READ_TEXT}, status=400)
    if request.content_type == "application/json":
        try:
            data = json.loads(request.body or b"{}")
        except ValueError:
            return JsonResponse({"ok": False, "note": READ_TEXT}, status=400)
        if not isinstance(data, dict):
            return JsonResponse({"ok": False, "note": READ_TEXT}, status=400)
        raw = data.get("rules")
    else:
        raw = rules_from_post(request.POST)
    return JsonResponse(live_count(account, raw, now))
