"""
Svaren i formuläret: flervalsfrågorna på landningssidorna (Giovanni
2026-10-10, "bygg flervalet med 1-7").

En fråga i formulärblocket med kind "one" (Flerval, ett svar) eller "many"
(Flerval, flera svar) har sina alternativ i "options", ett per rad
(pagebuilder/blocks.parse_options). Ett alternativs nyckel kommer ur texten
(blocks.option_key: "Felsökning" blir "felsokning"; en text utan a-z och
0-9 får "alt-" och en hash av texten), så att den står kvar när
alternativen byter ordning. Byts texten blir det ett nytt alternativ; de
gamla svaren står kvar under sin gamla text i rapporten.

Svaret sparas på två ställen när förfrågan skapas (leads.create_lead):

    Lead.answers[frågan]   de valda alternativen som text, i alternativens
                           ordning ("Bilservice, Reparation"), som de andra
                           svaren: inkorgen och GDPR-utdraget visar dem
    Lead.choice_answers    [{"page": <sidans id>, "q": <frågans nyckel>,
                             "label": <frågan>, "multi": bool,
                             "o": [<alternativens nycklar>],
                             "labels": [<alternativen som de stod>]}]
                           en post per besvarad flervalsfråga; page är
                           kampanjens sida, aldrig något som postats

Förval från en länk: ?val=<fråga>.<alternativ>, flera med komma eller med
flera val= (?val=tjanst.reparation,tjanst.felsokning). Alternativen är
redan ikryssade när sidan öppnas (bara GET och HEAD). Okända frågor och
alternativ och allt som inte har formen ignoreras tyst, ett flerval med ett
svar tar det första giltiga, och en postning prövas som vanligt: val är
aldrig mer än ett förval. val står inte bland leads.TRACKING_KEYS och
hamnar alltså aldrig i Lead.utm.

Gränssnittet mot utskicken (segmenten och länkarna i apps/utskick läser
bara det här, aldrig JSON:en direkt):

    PRESELECT_PARAM                      "val"
    KEY_RE                               frågans och alternativets nyckel
                                         (fullmatch)
    ChoiceQuestion                       en flervalsfråga på en sida
    questions_for_page(page)             sidans flerval som besökarna ser
                                         (LandingPage.live_blocks)
    questions_for_account(account_id)    alla kontots sidors flerval
    questions_by_page(account_id, ids)   {sida: [flerval]} för kontots sidor
                                         bland ids (bara de sidorna hämtas)
    preselect_value(fråga, alternativ)   "tjanst.reparation"
    preselect_of(url)                    det första giltiga val i adressen
    with_preselect(url, värde)           adressen med val bytt ("" tar bort)
    preselect_choices(kampanj)           [(värde, "Alternativ (Fråga)")]
    preselect_label(kampanj, värde)      "Alternativ (Fråga)" eller ""
    chose_q(sida, fråga, alternativ)     Q på Lead: valde något av dem

Rapporten per sida (app_views/pages.page_answers): page_report(page) och
answered_page_ids(account).
"""

import functools
import operator
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.db.models import Q

from apps.common.security import sanitize_plain_text

from .models import LandingPage, Lead
from .pagebuilder import registry
from .pagebuilder.blocks import QUESTION_KEY, QUESTIONS_MAX, form_spec

#: Adressens parameter för förvalet.
PRESELECT_PARAM = "val"
#: Frågans och alternativets nyckel (prövas med fullmatch).
KEY_RE = QUESTION_KEY
#: Högst så många värden och tecken i ?val= läses.
PRESELECT_MAX_TOKENS = 16
PRESELECT_MAX_CHARS = 400
_PRESELECT = re.compile(r"([a-z0-9][a-z0-9_-]{0,39})\.([a-z0-9][a-z0-9_-]{0,39})")  # fullmatch
#: Lead.choice_answers: frågans text och ett alternativs text som de sparas.
QUESTION_LABEL_MAX = 120
OPTION_LABEL_MAX = 120


@dataclass(frozen=True)
class ChoiceQuestion:
    """En flervalsfråga på en sida, som besökarna ser den."""

    page_id: int
    page_name: str
    key: str
    label: str
    multi: bool
    required: bool
    #: ((alternativets nyckel, text), ...) i sidans ordning.
    options: tuple

    @property
    def option_keys(self):
        return tuple(key for key, _label in self.options)

    def option_label(self, key):
        return next((label for k, label in self.options if k == key), "")


def _page_questions(page, blocks):
    spec = form_spec(blocks)
    if spec is None:
        return []
    return [
        ChoiceQuestion(
            page_id=page.pk,
            page_name=page.name,
            key=q["key"],
            label=q["label"],
            multi=q["multi"],
            required=q["required"],
            options=tuple((o["key"], o["label"]) for o in q["options"]),
        )
        for q in spec.questions
        if q["choice"] and q["visible"]
    ]


def questions_for_page(page):
    """Sidans flervalsfrågor som besökarna ser dem (den publicerade sidan,
    eller utkastet för en sida som aldrig publicerats), i sidans ordning.
    Bara frågor som syns (minst två alternativ)."""
    return _page_questions(page, page.live_blocks)


#: Fälten en sidas flerval behöver (namnet och blocken).
_PAGE_COLUMNS = ("account", "name", "draft", "published", "published_at")


def questions_for_account(account_id):
    """Alla flervalsfrågor på kontots sidor, sidorna i namnordning
    (segmentbyggarens väljare)."""
    out = []
    pages = LandingPage.objects.filter(account_id=account_id).only(*_PAGE_COLUMNS)
    for page in pages.order_by("name", "pk"):
        out.extend(questions_for_page(page))
    return out


def questions_by_page(account_id, page_ids):
    """{sidans id: [ChoiceQuestion]} för kontots sidor bland page_ids, i en
    fråga med kontot i villkoret. En sida som inte är kontots (eller inte
    finns) saknas bland nycklarna; en sida utan flerval har en tom lista.
    För segmenten, som bara behöver sidorna reglerna pekar på."""
    ids = {pk for pk in page_ids if isinstance(pk, int) and not isinstance(pk, bool)}
    if not ids:
        return {}
    pages = LandingPage.objects.filter(account_id=account_id, pk__in=ids).only(*_PAGE_COLUMNS)
    return {page.pk: questions_for_page(page) for page in pages}


# ---------------------------------------------------------------------------
# Förvalet från en länk (?val=)
# ---------------------------------------------------------------------------


def preselect_value(question_key, option_key):
    """Värdet i ?val= för ett alternativ: "tjanst.reparation"."""
    return f"{question_key}.{option_key}"


def parse_preselect(raw_values):
    """[(fråga, alternativ)] ur ?val= (request.GET.getlist), utan dubbletter.
    Högst PRESELECT_MAX_TOKENS värden och PRESELECT_MAX_CHARS tecken läses;
    allt som inte har formen fråga.alternativ hoppas över."""
    if isinstance(raw_values, str):
        raw_values = [raw_values]
    out, chars, tokens = [], 0, 0
    for raw in raw_values or ():
        if not isinstance(raw, str):
            continue
        chars += len(raw)
        if chars > PRESELECT_MAX_CHARS:
            break
        for token in raw.split(","):
            tokens += 1
            if tokens > PRESELECT_MAX_TOKENS:
                return out
            match = _PRESELECT.fullmatch(token.strip())
            if match is None:
                continue
            pair = (match.group(1), match.group(2))
            if pair not in out:
                out.append(pair)
    return out


def initial_for(spec, raw_values):
    """LeadForm:s initial ur ?val=: {fält: alternativ} för ett flerval med
    ett svar (det första giltiga vinner) och {fält: [alternativ]} för flera
    svar. Bara frågor som syns och alternativ som finns."""
    pairs = parse_preselect(raw_values)
    if spec is None or not pairs:
        return {}
    questions = {q["key"]: q for q in spec.questions if q.get("choice") and q.get("visible")}
    initial = {}
    for question_key, option_key in pairs:
        question = questions.get(question_key)
        if question is None or option_key not in {o["key"] for o in question["options"]}:
            continue
        if question["multi"]:
            chosen = initial.setdefault(question["field"], [])
            if option_key not in chosen:
                chosen.append(option_key)
        else:
            initial.setdefault(question["field"], option_key)
    return initial


def preselect_of(url):
    """Det första giltiga förvalet i adressens ?val= ("tjanst.reparation"),
    eller ""."""
    try:
        query = urlsplit(str(url or "")).query
    except ValueError:
        return ""
    values = [v for k, v in parse_qsl(query, keep_blank_values=True) if k == PRESELECT_PARAM]
    pairs = parse_preselect(values)
    return preselect_value(*pairs[0]) if pairs else ""


def with_preselect(url, value):
    """Adressen med ?val= bytt mot value (ett giltigt "fråga.alternativ"),
    eller utan val när value är tomt eller inte har formen. Andra parametrar
    och #fragmentet står kvar."""
    parts = urlsplit(str(url or ""))
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != PRESELECT_PARAM
    ]
    value = str(value or "").strip()
    if value and _PRESELECT.fullmatch(value):
        query.append((PRESELECT_PARAM, value))
    return urlunsplit(parts._replace(query=urlencode(query)))


def preselect_choices(campaign):
    """[(värde, "Alternativ (Fråga)")] för kampanjens sida, i sidans ordning;
    [] när kampanjen inte har någon sida eller sidan inget flerval.
    Alternativet står först, så att det syns också när en smal väljare
    kortar texten."""
    page = campaign.landing_page if campaign is not None and campaign.landing_page_id else None
    if page is None:
        return []
    return [
        (preselect_value(q.key, key), f"{label} ({q.label})")
        for q in questions_for_page(page)
        for key, label in q.options
    ]


def preselect_label(campaign, value):
    """Förvalets text, som "Reparation (Vilken tjänst önskar du?)", när det
    finns på kampanjens sida, annars "" (frågan eller alternativet är borta)."""
    if not value:
        return ""
    return dict(preselect_choices(campaign)).get(value, "")


# ---------------------------------------------------------------------------
# Svaren på förfrågan
# ---------------------------------------------------------------------------


def chose_q(page_id, question_key, option_keys):
    """Q på Lead: förfrågan valde minst ett av alternativen på sidans fråga
    (jsonb @> i Postgres). Inga alternativ ger ett Q som aldrig matchar."""
    parts = [
        Q(choice_answers__contains=[{"page": page_id, "q": question_key, "o": [key]}])
        for key in option_keys
    ]
    if not parts:
        return Q(pk__in=[])
    return functools.reduce(operator.or_, parts)


def _plain(value, max_length):
    return sanitize_plain_text(str(value or ""), max_length=max_length).strip()


def _clean_entry(entry, page_id):
    if not isinstance(entry, dict):
        return None
    question = entry.get("q")
    keys, labels = entry.get("o"), entry.get("labels")
    if not isinstance(question, str) or not KEY_RE.fullmatch(question):
        return None
    if not isinstance(keys, list) or not isinstance(labels, list):
        return None
    if not keys or len(keys) != len(labels) or len(keys) > registry.OPTIONS_MAX:
        return None
    clean_keys, clean_labels = [], []
    for key, label in zip(keys, labels, strict=True):
        if not isinstance(key, str) or not KEY_RE.fullmatch(key) or key in clean_keys:
            return None
        text = _plain(label, OPTION_LABEL_MAX) if isinstance(label, str) else ""
        if not text:
            return None
        clean_keys.append(key)
        clean_labels.append(text)
    label = _plain(entry.get("label"), QUESTION_LABEL_MAX)
    if not label:
        return None
    return {
        "page": page_id,
        "q": question,
        "label": label,
        "multi": entry.get("multi") is True,
        "o": clean_keys,
        "labels": clean_labels,
    }


def clean_choices(raw, page_id):
    """Lead.choice_answers ur LeadForm.lead_data()["choices"]: varje post
    prövas (nycklarna, lika många texter som nycklar, texterna som ren
    text), en post med fel hoppas över, högst en per fråga och högst
    blocks.QUESTIONS_MAX. page_id är kampanjens sida (leads.create_lead); ett
    "page" i raw läses aldrig. Utan en sida sparas inget (en post utan sida
    går inte att räkna eller välja i ett segment)."""
    if not isinstance(raw, list):
        return []
    if not isinstance(page_id, int) or isinstance(page_id, bool):
        return []
    page = page_id
    out, seen = [], set()
    for entry in raw[:QUESTIONS_MAX]:
        clean = _clean_entry(entry, page)
        if clean is None or clean["q"] in seen:
            continue
        seen.add(clean["q"])
        out.append(clean)
    return out


def _entries(lead):
    value = lead.choice_answers if isinstance(lead.choice_answers, list) else []
    return [e for e in value if isinstance(e, dict)]


def card_text(lead):
    """Inkorgens kort när förfrågan saknar meddelande: det första flervalets
    svar ("Bilservice, Reparation"), eller ""."""
    for entry in _entries(lead):
        labels = entry.get("labels")
        if isinstance(labels, list):
            texts = [str(label) for label in labels if isinstance(label, str) and label]
            if texts:
                return ", ".join(texts)
    return ""


# ---------------------------------------------------------------------------
# Rapporten per sida
# ---------------------------------------------------------------------------


def _counted_leads(account_id):
    """Förfrågningarna som räknas: från formuläret, inte skräp."""
    return (
        Lead.objects.filter(account_id=account_id, source=Lead.SOURCE_FORM)
        .exclude(status=Lead.STATUS_JUNK)
        .order_by()
    )


def answered_page_ids(account):
    """Id:n på kontots sidor som har minst ett flervalssvar (inte skräp)."""
    ids = (
        _counted_leads(account.pk)
        .exclude(choice_answers=[])
        .values_list("choice_answers__0__page", flat=True)
        .distinct()
    )
    return {pk for pk in ids if isinstance(pk, int) and not isinstance(pk, bool)}


def _tally(page):
    """{fråga: {"answered", "counts", "labels", "label", "multi",
    "any_multi"}} ur förfrågningarna på sidan, äldst först, så att den
    senaste texten på en fråga eller ett alternativ vinner. any_multi: något
    av svaren gavs med flera svar (frågan kan ha bytt sort sedan)."""
    rows = (
        _counted_leads(page.account_id)
        .filter(choice_answers__contains=[{"page": page.pk}])
        .order_by("created_at", "pk")
        .values_list("choice_answers", flat=True)
    )
    stats = {}
    for value in rows:
        seen = set()
        for entry in value if isinstance(value, list) else []:
            if not isinstance(entry, dict) or entry.get("page") != page.pk:
                continue
            question = entry.get("q")
            keys, labels = entry.get("o"), entry.get("labels")
            if not isinstance(question, str) or question in seen or not isinstance(keys, list):
                continue
            seen.add(question)
            labels = labels if isinstance(labels, list) else []
            row = stats.setdefault(
                question,
                {
                    "answered": 0,
                    "counts": {},
                    "labels": {},
                    "label": "",
                    "multi": False,
                    "any_multi": False,
                },
            )
            row["answered"] += 1
            row["label"] = str(entry.get("label") or row["label"] or question)
            row["multi"] = entry.get("multi") is True
            row["any_multi"] = row["any_multi"] or row["multi"]
            chosen = set()
            for i, key in enumerate(keys):
                if not isinstance(key, str) or key in chosen:
                    continue
                chosen.add(key)
                row["counts"][key] = row["counts"].get(key, 0) + 1
                if i < len(labels) and isinstance(labels[i], str) and labels[i]:
                    row["labels"][key] = labels[i]
    return stats


def _option_row(key, label, n, answered, current):
    pct = round(100 * n / answered, 1) if answered else None
    return {
        "key": key,
        "label": label,
        "n": n,
        "pct": pct,
        # SVG-stapelns bredd av 100 (viewBox), minst 1 så att ett svar syns.
        "width": max(1, round(100 * n / answered)) if n and answered else 0,
        "current": current,
    }


def _earlier_options(row, skip, answered):
    rest = [(key, n) for key, n in row["counts"].items() if key not in skip]
    rest.sort(key=lambda item: (-item[1], row["labels"].get(item[0], item[0]).casefold()))
    return [_option_row(key, row["labels"].get(key, key), n, answered, False) for key, n in rest]


def page_report(page):
    """Svaren på sidans flervalsfrågor, för rapporten:

        [{"key", "label", "multi", "any_multi", "current", "answered",
          "options": [{"key", "label", "n", "pct", "width", "current"}]}]

    answered är antalet förfrågningar som svarade på frågan, och pct är
    andelen av dem (flera svar kan bli mer än 100 procent tillsammans;
    any_multi säger att något räknat svar hade flera, också när frågan har
    ett svar nu).
    Skräp och andra källor än formuläret räknas inte. Frågorna som finns på
    sidan nu kommer först i sidans ordning, med alla alternativ (också de
    utan svar) och sedan de som inte finns längre (current False, flest svar
    först). Sist frågor som tagits bort från sidan."""
    stats = _tally(page)
    out = []
    current_keys = set()
    for question in questions_for_page(page):
        current_keys.add(question.key)
        row = stats.get(question.key) or {
            "answered": 0,
            "counts": {},
            "labels": {},
            "any_multi": False,
        }
        answered = row["answered"]
        options = [
            _option_row(key, label, row["counts"].get(key, 0), answered, True)
            for key, label in question.options
        ]
        options += _earlier_options(row, set(question.option_keys), answered)
        out.append(
            {
                "key": question.key,
                "label": question.label,
                "multi": question.multi,
                "any_multi": row["any_multi"],
                "current": True,
                "answered": answered,
                "options": options,
            }
        )
    gone = [(key, row) for key, row in stats.items() if key not in current_keys]
    gone.sort(key=lambda item: (-item[1]["answered"], item[1]["label"].casefold()))
    for key, row in gone:
        out.append(
            {
                "key": key,
                "label": row["label"],
                "multi": row["multi"],
                "any_multi": row["any_multi"],
                "current": False,
                "answered": row["answered"],
                "options": _earlier_options(row, set(), row["answered"]),
            }
        )
    return out


def draft_only(page):
    """Har utkastet ett flerval som besökarna inte ser än (sidan är
    publicerad utan det)? För rapportens tomma läge."""
    if not page.is_published:
        return False
    return bool(_page_questions(page, page.draft_blocks)) and not questions_for_page(page)
