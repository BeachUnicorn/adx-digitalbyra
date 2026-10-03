"""
Byråns granskning av ADX Flamingo (/manage/flamingo/...), i panelens design
(kundresan steg 8, byråns sida, och publiceringen i steg 8-11):

    queue          granskningskön: att granska (äldst först, bara de där
                   kunden bad om granskning), godkända som inte är
                   publicerade (med orsaken), väntar på kunden, live/pausade,
                   och konverteringarna som ska till Google. Demokontot är
                   inte med förrän byrån ber om det (?demo=1).
    review         en kampanj: förslaget bredvid kundens bekräftade uppgifter
                   och hemsida. Medan kampanjen ligger hos ADX rättar
                   granskaren rubriker, beskrivningar, sökord, negativa
                   sökord och sidan, och skriver varför för varje ändrad del.
                   "Klar, skicka till kunden" sparar diffen i Review.changes
                   och lämnar över till kunden (status needs_customer).
    publish        live (bara när kunden godkänt), pausa och återuppta, och
                   "Tillbaka till granskning" för en godkänd kampanj som
                   inte är publicerad (Google sa nej till innehållet)
    editor_csv     kampanjen som Google Ads Editor-fil (exports.py)
    conversions_csv  offline-konverteringarna i kö som CSV (GET; filen tar
                   sina rader från API:t), och "Markera som exporterade"
                   (POST, bara rader som varit med i en fil)
    google_update  kundkortets Google-koppling: status, kontots id, notering

Alla vyer kräver byrån (staff_required). Ingen vy här mejlar kunden: kunden
ser granskningen i verktyget, och vill byrån säga till gör den det själv
(webapp/CLAUDE.md, inga automatiska kundmejl).

Med Google Ads API inkopplat (google_ads.is_configured(), inte för
demokonton) skapar publiceringen kampanjen hos Google (google_publish.py).
Annars publiceras allt för hand med Editor-filen; sidan visar stegen.
Betalningen hos Google stoppar ingen publicering, men panelen påminner om den.

Granskningen är kundens val (beslut 2026-10-03, Campaign.review_requested).
En kampanj som skickats utan granskning, eller godkänts efter den, går live
direkt när det går (google_publish.publish_approved); hamnar den här i
"Godkända, ej publicerade" står orsaken i Campaign.google_error.
"""

import copy
import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Max, Prefetch, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_http_methods, require_POST

from apps.common.security import sanitize_multiline_text, sanitize_plain_text
from apps.projects.access import staff_required
from apps.projects.models import Customer

from . import checks, exports, google_ads, google_publish
from .models import (
    DESCRIPTION_COUNT,
    DESCRIPTION_MAX,
    HEADLINE_COUNT,
    HEADLINE_MAX,
    MATCH_BROAD,
    MATCH_CHOICES,
    MATCH_PHRASE,
    PAGE_QUESTION_KINDS,
    Campaign,
    ConversionUpload,
    FlamingoAccount,
    Review,
    format_google_ads_id,
)

logger = logging.getLogger(__name__)

#: Googles minimum för en responsiv sökannons.
HEADLINE_MIN = 3
DESCRIPTION_MIN = 2
#: Googles gränser för ett sökord.
KEYWORD_MAX_CHARS = 80
KEYWORD_MAX_WORDS = 10
#: Tomma rader under de befintliga, för nya sökord och frågor.
EXTRA_KEYWORD_ROWS = 3
EXTRA_QUESTION_ROWS = 2
#: Ett tak på hur många rader ett formulär får skicka (skydd mot skräp).
MAX_ROWS = 100

PAGE_TITLE_MAX = 120
PAGE_TEXT_MAX = 600
PAGE_LINE_MAX = 120
PAGE_PHONE_MAX = 40
REASON_MAX = 500
NOTE_MAX = 1000

MATCH_LABELS = {MATCH_PHRASE: "fras", "exact": "exakt", MATCH_BROAD: "bred"}
QUESTION_KIND_LABELS = {"text": "kort svar", "textarea": "längre svar", "date": "datum"}

#: Delarna granskaren kan ändra. Varje del med en ändring kräver ett skäl.
#:   nyckel: (modellfält, rubrik i formuläret, etikett per ändring för kunden)
GROUPS = {
    "headlines": ("headlines", "Rubriker", "Rubrik"),
    "descriptions": ("descriptions", "Beskrivningar", "Beskrivning"),
    "keywords": ("keywords", "Sökord", "Sökord"),
    "negatives": ("negatives", "Negativa sökord", "Negativt sökord"),
    "page_title": ("page", "Sidans rubrik", "Sidans rubrik"),
    "page_lead": ("page", "Ingress", "Sidans ingress"),
    "page_points": ("page", "Punkter", "Punkt på sidan"),
    "page_phone": ("page", "Telefon", "Telefon på sidan"),
    "page_form_title": ("page", "Formulärets rubrik", "Formulärets rubrik"),
    "page_questions": ("page", "Frågor i formuläret", "Fråga i formuläret"),
    "page_note": ("page", "Text under formuläret", "Text under formuläret"),
}

#: Granskarens påminnelser (kundresan steg 8). Visas, sparas inte.
CHECKLIST = (
    "Rätt telefonnummer, samma som bland uppgifterna.",
    "Inga priser som inte är kundens egna.",
    "Rätt sätt att sälja: {sales_mode}.",
    "Inga löften om tider och inga påståenden som billigast eller garanti.",
    "Inget som bryter mot Googles annonspolicy.",
)


def _back_to_card(customer_pk):
    return redirect(reverse("manage:customer_detail", args=[customer_pk]) + "#flamingo")


def _back_to_review(campaign_pk, anchor=""):
    url = reverse("manage:flamingo_review", args=[campaign_pk])
    return redirect(url + (f"#{anchor}" if anchor else ""))


# ---------------------------------------------------------------------------
# Kön
# ---------------------------------------------------------------------------


def _pending_prefetch():
    return Prefetch(
        "reviews",
        queryset=Review.objects.filter(state=Review.STATE_PENDING).order_by("-round"),
        to_attr="pending_reviews",
    )


@dataclass
class QueueItem:
    campaign: Campaign
    review: Review | None = None

    @property
    def customer(self):
        return self.campaign.account.customer

    @property
    def submitted_at(self):
        return self.review.submitted_at if self.review else self.campaign.updated_at


def _real(campaigns, include_demo=False):
    """Utan demokontot (FlamingoAccount.is_demo), om byrån inte bett om det
    (?demo=1): demot ska inte blanda sig med byråns riktiga arbete."""
    if include_demo:
        return campaigns
    return campaigns.filter(account__is_demo=False)


def wants_demo(request):
    """Bad byrån om att se demokunden i kön (?demo=1)?"""
    return request.GET.get("demo") == "1"


def to_review_items(include_demo=False):
    """Kampanjer hos ADX där kunden bad om granskning (eller som byrån tagit
    tillbaka till granskning), äldst inskickad först.

    En kampanj hos ADX som kunden redan godkänt (approved_at satt, ingen ny
    runda) väntar på publicering, inte på granskning."""
    campaigns = _real(
        Campaign.objects.filter(status=Campaign.STATUS_IN_REVIEW)
        .filter(Q(review_requested=True) | Q(reviews__state=Review.STATE_PENDING))
        .distinct(),
        include_demo,
    )
    campaigns = campaigns.select_related("account__customer", "service").prefetch_related(
        _pending_prefetch()
    )
    items = []
    for campaign in campaigns:
        review = campaign.pending_reviews[0] if campaign.pending_reviews else None
        if review is None and campaign.approved_at is not None:
            continue
        items.append(QueueItem(campaign, review))
    items.sort(key=lambda item: (item.submitted_at, item.campaign.pk))
    return items


def approved_not_live(include_demo=False):
    """Godkända av kunden men inte publicerade: byrån har nästa steg. Hit
    kommer en kampanj som godkänts (eller skickats utan granskning) när
    Google Ads API inte är inkopplat, kontot inte är kopplat under ADX, eller
    Google sa nej (orsaken i google_error)."""
    return (
        _real(Campaign.objects.filter(approved_at__isnull=False), include_demo)
        .exclude(status__in=[Campaign.STATUS_LIVE, Campaign.STATUS_PAUSED])
        .exclude(reviews__state=Review.STATE_PENDING)
        .select_related("account__customer", "service")
        .distinct()
        .order_by("approved_at", "pk")
    )


def waiting_on_customer(include_demo=False):
    """Granskade kampanjer som väntar på kundens godkännande. sent_at är när
    den senaste färdiga granskningsrundan lämnades till kunden (inte
    kampanjens senaste ändring)."""
    return (
        _real(
            Campaign.objects.filter(
                status=Campaign.STATUS_NEEDS_CUSTOMER, approved_at__isnull=True
            ),
            include_demo,
        )
        .select_related("account__customer", "service")
        .annotate(sent_at=Max("reviews__reviewed_at", filter=Q(reviews__state=Review.STATE_DONE)))
        .order_by("sent_at", "pk")
    )


def published(include_demo=False):
    return (
        _real(
            Campaign.objects.filter(status__in=[Campaign.STATUS_LIVE, Campaign.STATUS_PAUSED]),
            include_demo,
        )
        .select_related("account__customer", "service")
        .order_by("status", "account__customer__name", "name")
    )


def queued_uploads(customer_pk=None):
    """Konverteringarna i kö som kan exporteras: förfrågningar, klick på
    numret och affärer med gclid (Lead.can_send_to_google). Samtycket avgör
    inte (beslut 2026-10-03). Filen skriver varje sort med sitt namn
    (exports.conversion_names). Demokontons rader är aldrig med, inte heller
    med ?demo=1 (påhittade klick-id, inget till Google)."""
    uploads = (
        ConversionUpload.objects.filter(
            status=ConversionUpload.STATUS_QUEUED,
            lead__account__is_demo=False,
        )
        .exclude(lead__gclid="")
        .select_related("lead__account__customer", "lead__campaign", "lead__service")
        .order_by("created_at", "pk")
    )
    if customer_pk is not None:
        uploads = uploads.filter(lead__account__customer_id=customer_pk)
    return uploads


def queue_counts():
    """Siffrorna till översikten och kön, utan demokontot."""
    return {
        "to_review": len(to_review_items()),
        "to_publish": approved_not_live().count(),
        "conversions": queued_uploads().count(),
    }


def _conversion_groups(uploads):
    """Konverteringarna per kund (varje kund har sitt eget Google Ads-konto
    och laddar upp sin egen fil): kund, rader, antal och summa."""
    groups = {}
    for upload in uploads:
        customer = upload.lead.account.customer
        entry = groups.setdefault(
            customer.pk, {"customer": customer, "uploads": [], "count": 0, "value": 0}
        )
        entry["uploads"].append(upload)
        entry["count"] += 1
        entry["value"] += upload.value_kr or 0
    return sorted(groups.values(), key=lambda g: g["customer"].name.lower())


def staff_state(campaign, pending=None):
    """Kampanjens läge sett från byrån: (text, m-badge-variant). Kampanjens
    egna statustexter är skrivna för kunden ("Väntar på dig")."""
    if campaign.status == Campaign.STATUS_LIVE:
        return "Live", "published"
    if campaign.status == Campaign.STATUS_PAUSED:
        return "Pausad", "draft"
    if campaign.approved_at is not None and pending is None:
        return "Godkänd, ej publicerad", "in_progress"
    if campaign.status == Campaign.STATUS_IN_REVIEW:
        return "Att granska", "alert"
    if campaign.status == Campaign.STATUS_NEEDS_CUSTOMER:
        return "Hos kunden", "ai"
    return "Utkast hos kunden", "draft"


def grouped_changes(changes):
    """En rundas ändringar grupperade per del, med skälet en gång per grupp."""
    groups = []
    for change in changes or []:
        if not isinstance(change, dict):
            continue
        part = change.get("part") or change.get("label") or change.get("field") or ""
        reason = change.get("reason") or ""
        if groups and groups[-1]["part"] == part and groups[-1]["reason"] == reason:
            groups[-1]["items"].append(change)
            continue
        label = GROUPS[part][1] if part in GROUPS else (change.get("label") or "")
        groups.append({"part": part, "label": label, "reason": reason, "items": [change]})
    return groups


@staff_required
def queue(request):
    """Kön. Demokontot är inte med förrän byrån ber om det (?demo=1, länken
    "Visa demokunden"); konverteringarna i kö har det aldrig med."""
    from .google_conversions import queue_context

    include_demo = wants_demo(request)
    uploads = list(queued_uploads())
    exported = ConversionUpload.objects.filter(status=ConversionUpload.STATUS_EXPORTED)
    if not include_demo:
        exported = exported.filter(lead__account__is_demo=False)
    exported = exported.select_related("lead__account__customer").order_by("-exported_at", "-pk")[
        :10
    ]
    return render(
        request,
        "manage/flamingo/queue.html",
        {
            "active": "flamingo",
            "title": "Granska",
            "include_demo": include_demo,
            "has_demo": FlamingoAccount.objects.filter(is_demo=True).exists(),
            "to_review": to_review_items(include_demo),
            "to_publish": approved_not_live(include_demo),
            "waiting": waiting_on_customer(include_demo),
            "published": published(include_demo),
            "uploads": uploads,
            "upload_groups": _conversion_groups(uploads),
            "upload_total": sum(u.value_kr or 0 for u in uploads),
            "exported": exported,
            "conversion_name": exports.conversion_name(),
            **queue_context(include_demo),
        },
    )


# ---------------------------------------------------------------------------
# Granskningsformuläret
# ---------------------------------------------------------------------------


def _plain(value, limit=300):
    return sanitize_plain_text("" if value is None else str(value), max_length=limit)


def _multi(value, limit=2000):
    return sanitize_multiline_text("" if value is None else str(value), max_length=limit).strip()


def _lines(value, limit=300):
    return [line for line in (_plain(raw, limit) for raw in (value or "").splitlines()) if line]


def _keyword_item(item):
    """(text, match) ur ett sparat sökord (dict eller bara text)."""
    if isinstance(item, dict):
        return str(item.get("text") or ""), item.get("match") or MATCH_PHRASE
    return str(item or ""), MATCH_PHRASE


def _keyword_text(text, match):
    return f"{text} ({MATCH_LABELS.get(match, match)})" if text else ""


def _negative_text(item):
    if isinstance(item, dict):
        return str(item.get("text") or "")
    return str(item or "")


def _question_text(label, kind):
    return f"{label} ({QUESTION_KIND_LABELS.get(kind, kind)})" if label else ""


def _list_changes(before, after, ordered):
    """Ändringarna mellan två textlistor som (före, efter)-par.

    Borttagna och tillagda rader blir egna par. En ren omflyttning räknas
    bara när ordningen spelar roll (punkterna på sidan)."""
    removed = Counter(before) - Counter(after)
    added = Counter(after) - Counter(before)
    pairs = []
    for text in before:
        if removed[text]:
            removed[text] -= 1
            pairs.append((text, ""))
    for text in after:
        if added[text]:
            added[text] -= 1
            pairs.append(("", text))
    if not pairs and ordered and before != after:
        pairs.append((" / ".join(before), " / ".join(after)))
    return pairs


#: Tomma rubrik- och beskrivningsrutor som syns efter den sista ifyllda;
#: resten ligger i en utfällbar "Fler" (de postas ändå, tomma).
OPEN_EMPTY_SLOTS = 2


def split_slots(rows, open_empty=OPEN_EMPTY_SLOTS):
    """(synliga, ihopfällda): rutorna fram till den sista ifyllda (eller
    felmarkerade) och open_empty tomma till; resten fälls ihop."""
    last = max(
        (i for i, row in enumerate(rows) if str(row["value"]).strip() or row.get("error")),
        default=-1,
    )
    cut = min(len(rows), last + 1 + open_empty)
    return rows[:cut], rows[cut:]


@dataclass
class ReviewForm:
    """Formulärets rader (för mallen), det nya innehållet och diffen.

    Byggs ur kampanjen (GET) eller ur det postade (POST). Ett värde som inte
    ändrats sparas som det var, även om saneringen hade skrivit det lite
    annorlunda: annars skulle granskaren behöva förklara ändringar hen
    aldrig gjorde."""

    campaign: Campaign
    data: object = None
    headlines: list = field(default_factory=list)
    descriptions: list = field(default_factory=list)
    keywords: list = field(default_factory=list)
    negatives: str = ""
    page: dict = field(default_factory=dict)
    questions: list = field(default_factory=list)
    reasons: dict = field(default_factory=dict)
    note: str = ""
    #: Fältfel: {"page_title": "...", "negatives": "..."}; radfel står på raden.
    errors: dict = field(default_factory=dict)
    #: Fel för en hel del (för få rubriker, inga sökord).
    group_errors: dict = field(default_factory=dict)
    #: Ändrade delar utan skäl.
    reason_errors: dict = field(default_factory=dict)
    content: dict = field(default_factory=dict)
    changes: list = field(default_factory=list)
    changed_groups: set = field(default_factory=set)

    @classmethod
    def initial(cls, campaign):
        form = cls(campaign)
        form._build_initial()
        return form

    @classmethod
    def bound(cls, campaign, data):
        form = cls(campaign, data)
        form._parse()
        return form

    @property
    def is_bound(self):
        return self.data is not None

    @property
    def has_errors(self):
        if self.errors or self.group_errors or self.reason_errors:
            return True
        rows = self.headlines + self.descriptions + self.keywords + self.questions
        return any(row.get("error") for row in rows)

    @property
    def headline_slots(self):
        shown, more = split_slots(self.headlines)
        return {"shown": shown, "more": more}

    @property
    def description_slots(self):
        shown, more = split_slots(self.descriptions)
        return {"shown": shown, "more": more}

    @property
    def groups(self):
        """{nyckel: {label, reason, error, changed}} för mallen."""
        return {
            key: {
                "label": label,
                "reason": self.reasons.get(key, ""),
                "error": self.group_errors.get(key, ""),
                "reason_error": self.reason_errors.get(key, ""),
                "changed": key in self.changed_groups,
            }
            for key, (_field, label, _item) in GROUPS.items()
        }

    # -- GET ---------------------------------------------------------------

    def _build_initial(self):
        c = self.campaign
        old_headlines = [str(h) for h in c.headlines or []]
        old_descriptions = [str(d) for d in c.descriptions or []]
        self.headlines = self._slots("headline", old_headlines, HEADLINE_COUNT, HEADLINE_MAX)
        self.descriptions = self._slots(
            "description", old_descriptions, DESCRIPTION_COUNT, DESCRIPTION_MAX
        )
        self.keywords = [
            {"text": text, "match": match, "error": ""}
            for text, match in (_keyword_item(k) for k in c.keywords or [])
        ] + [{"text": "", "match": MATCH_PHRASE, "error": ""} for _ in range(EXTRA_KEYWORD_ROWS)]
        self.negatives = "\n".join(_negative_text(n) for n in c.negatives or [])
        page = c.page or {}
        self.page = {
            "title": str(page.get("title") or ""),
            "lead": str(page.get("lead") or ""),
            "points": "\n".join(str(p) for p in page.get("points") or []),
            "phone": str(page.get("phone") or ""),
            "form_title": str(page.get("form_title") or ""),
            "note": str(page.get("note") or ""),
        }
        self.questions = [
            {
                "label": str(q.get("label") or ""),
                "kind": q.get("kind") or "text",
                "error": "",
            }
            for q in page.get("questions") or []
            if isinstance(q, dict)
        ] + [{"label": "", "kind": "text", "error": ""} for _ in range(EXTRA_QUESTION_ROWS)]

    @staticmethod
    def _slots(prefix, values, count, limit, posted=None):
        slots = max(count, len(values))
        rows = []
        for i in range(slots):
            value = posted[i] if posted is not None else (values[i] if i < len(values) else "")
            rows.append(
                {
                    "name": f"{prefix}_{i}",
                    "number": i + 1,
                    "value": value,
                    "limit": limit,
                    "error": "",
                }
            )
        return rows

    # -- POST --------------------------------------------------------------

    def _change(self, group, before, after):
        model_field, _label, item_label = GROUPS[group]
        self.changed_groups.add(group)
        self.changes.append(
            {
                "field": model_field,
                "part": group,
                "label": item_label,
                "before": before,
                "after": after,
                "reason": "",
            }
        )

    def _parse(self):
        data = self.data
        c = self.campaign
        self._parse_texts(
            "headlines", "headline", c.headlines, HEADLINE_COUNT, HEADLINE_MAX, HEADLINE_MIN
        )
        self._parse_texts(
            "descriptions",
            "description",
            c.descriptions,
            DESCRIPTION_COUNT,
            DESCRIPTION_MAX,
            DESCRIPTION_MIN,
        )
        self._parse_keywords()
        self._parse_negatives()
        self._parse_page()

        self.note = _multi(data.get("note"), NOTE_MAX)
        for group in GROUPS:
            self.reasons[group] = _plain(data.get(f"reason_{group}"), REASON_MAX)
        for group in sorted(self.changed_groups, key=list(GROUPS).index):
            if not self.reasons[group]:
                self.reason_errors.setdefault(
                    group, "Skriv varför du ändrade det. Kunden ser skälet."
                )
        for change in self.changes:
            change["reason"] = self.reasons[change["part"]]

    def _parse_texts(self, group, prefix, old_values, count, limit, minimum):
        """Rubriker eller beskrivningar: fasta platser, jämförda plats för plats."""
        data = self.data
        old = [str(v) for v in old_values or []]
        slots = max(count, len(old))
        posted = [_plain(data.get(f"{prefix}_{i}"), 300) for i in range(slots)]
        rows = self._slots(prefix, old, count, limit, posted=posted)
        setattr(self, group, rows)

        final, seen = [], {}
        for i in range(slots):
            before_raw = old[i] if i < len(old) else ""
            before = _plain(before_raw, 300)
            after = posted[i]
            if before == after:
                value = before_raw if before_raw.strip() else ""
            else:
                value = after
                self._change(group, before, after)
            if not value.strip():
                continue
            if len(value) > limit:
                rows[i]["error"] = f"{len(value)} tecken. Google tillåter högst {limit}."
            key = value.strip().casefold()
            if key in seen:
                rows[i]["error"] = f"Samma text som nummer {seen[key]}."
            seen.setdefault(key, i + 1)
            final.append(value)
        if len(final) > count:
            self.group_errors[group] = f"Högst {count}. Töm några rutor."
        elif len(final) < minimum:
            self.group_errors[group] = f"Google kräver minst {minimum}."
        self.content[group] = final

    def _parse_keywords(self):
        data = self.data
        old = [_keyword_item(k) for k in self.campaign.keywords or []]
        old_raw = list(self.campaign.keywords or [])
        texts = data.getlist("kw_text")[:MAX_ROWS]
        matches = data.getlist("kw_match")[:MAX_ROWS]
        valid_matches = dict(MATCH_CHOICES)
        rows = []
        for i, raw in enumerate(texts):
            match = matches[i] if i < len(matches) else MATCH_PHRASE
            rows.append({"text": _plain(raw, 300), "match": match, "error": ""})
        while len(rows) < len(old):
            rows.append({"text": "", "match": MATCH_PHRASE, "error": ""})
        self.keywords = rows

        final, seen = [], {}
        for i, row in enumerate(rows):
            text, match = row["text"], row["match"]
            if text and match not in valid_matches:
                row["error"] = "Välj matchningstyp."
                match = MATCH_PHRASE
            before_text, before_match = old[i] if i < len(old) else ("", MATCH_PHRASE)
            before = _keyword_text(_plain(before_text, 300), before_match)
            after = _keyword_text(text, match)
            if before == after:
                if not before:
                    continue
                final.append(old_raw[i])
            else:
                self._change("keywords", before, after)
                if not text:
                    continue
                final.append({"text": text, "match": match})
            if len(text) > KEYWORD_MAX_CHARS:
                row["error"] = f"{len(text)} tecken. Google tillåter högst {KEYWORD_MAX_CHARS}."
            elif len(text.split()) > KEYWORD_MAX_WORDS:
                row["error"] = f"Högst {KEYWORD_MAX_WORDS} ord."
            key = (text.casefold(), match)
            if text and key in seen:
                row["error"] = "Sökordet finns redan med samma matchningstyp."
            seen.setdefault(key, i)
        if not final:
            self.group_errors["keywords"] = "Kampanjen behöver minst ett sökord."
        self.content["keywords"] = final

    def _parse_negatives(self):
        old_raw = list(self.campaign.negatives or [])
        old = [_plain(_negative_text(n), 300) for n in old_raw]
        self.negatives = self.data.get("negatives", "")
        new, seen = [], set()
        for text in _lines(self.negatives, 300)[: MAX_ROWS * 2]:
            if text.casefold() not in seen:
                seen.add(text.casefold())
                new.append(text)
        pairs = _list_changes(old, new, ordered=False)
        for before, after in pairs:
            self._change("negatives", before, after)
        if not pairs:
            self.content["negatives"] = old_raw
        else:
            by_text = {}
            for text, raw in zip(old, old_raw, strict=False):
                by_text.setdefault(text, raw)
            self.content["negatives"] = [by_text.get(text, text) for text in new]
        too_long = [text for text in new if len(text) > KEYWORD_MAX_CHARS]
        if too_long:
            self.errors["negatives"] = (
                f"Högst {KEYWORD_MAX_CHARS} tecken per rad: {too_long[0][:40]}"
            )

    def _parse_page(self):
        data = self.data
        old_page = dict(self.campaign.page or {})
        page = dict(old_page)
        posted = {
            "title": data.get("page_title", ""),
            "lead": data.get("page_lead", ""),
            "points": data.get("page_points", ""),
            "phone": data.get("page_phone", ""),
            "form_title": data.get("page_form_title", ""),
            "note": data.get("page_note", ""),
        }
        self.page = posted

        scalars = (
            ("title", "page_title", _plain, PAGE_TITLE_MAX),
            ("phone", "page_phone", _plain, PAGE_PHONE_MAX),
            ("form_title", "page_form_title", _plain, PAGE_TITLE_MAX),
            ("lead", "page_lead", _multi, PAGE_TEXT_MAX),
            ("note", "page_note", _multi, PAGE_TEXT_MAX),
        )
        for key, group, clean, limit in scalars:
            before = clean(old_page.get(key) or "", 4000)
            after = clean(posted[key], 4000)
            if before == after:
                continue
            self._change(group, before, after)
            page[key] = after
            if len(after) > limit:
                self.errors[group] = f"{len(after)} tecken. Håll det under {limit}."

        old_points_raw = [str(p) for p in old_page.get("points") or []]
        old_points = [_plain(p, 300) for p in old_points_raw]
        new_points = _lines(posted["points"], 300)[:MAX_ROWS]
        pairs = _list_changes(old_points, new_points, ordered=True)
        for before, after in pairs:
            self._change("page_points", before, after)
        if pairs:
            page["points"] = new_points
            if any(len(p) > PAGE_LINE_MAX for p in new_points):
                self.errors["page_points"] = f"Håll varje punkt under {PAGE_LINE_MAX} tecken."

        self._parse_questions(old_page, page)
        self.content["page"] = page

    def _parse_questions(self, old_page, page):
        data = self.data
        old_raw = [q for q in old_page.get("questions") or [] if isinstance(q, dict)]
        labels = data.getlist("q_label")[:MAX_ROWS]
        kinds = data.getlist("q_kind")[:MAX_ROWS]
        rows = []
        for i, raw in enumerate(labels):
            kind = kinds[i] if i < len(kinds) else "text"
            rows.append({"label": _plain(raw, 300), "kind": kind, "error": ""})
        while len(rows) < len(old_raw):
            rows.append({"label": "", "kind": "text", "error": ""})
        self.questions = rows

        final, keys, changed = [], set(), False
        for i, row in enumerate(rows):
            label, kind = row["label"], row["kind"]
            if label and kind not in PAGE_QUESTION_KINDS:
                row["error"] = "Välj sorts svar."
                kind = "text"
            old = old_raw[i] if i < len(old_raw) else None
            before = (
                _question_text(_plain(old.get("label") or "", 300), old.get("kind") or "text")
                if old
                else ""
            )
            after = _question_text(label, kind)
            if before != after:
                changed = True
                self._change("page_questions", before, after)
            if not label:
                continue
            if len(label) > PAGE_TITLE_MAX:
                row["error"] = f"Håll frågan under {PAGE_TITLE_MAX} tecken."
            if before == after and old is not None:
                item = old
            else:
                key = (old or {}).get("key") or slugify(label)[:40] or f"fraga-{i + 1}"
                item = {"key": key, "label": label, "kind": kind}
            key, n = item.get("key") or f"fraga-{i + 1}", 2
            base = key
            while key in keys:
                key, n = f"{base}-{n}", n + 1
            keys.add(key)
            if key != item.get("key"):
                item = {**item, "key": key}
            final.append(item)
        if changed:
            page["questions"] = final

    # -- Resultatet ----------------------------------------------------------

    def proposal(self):
        """En osparad kopia av kampanjen med det nya innehållet (för
        kontrollerna)."""
        proposal = copy.copy(self.campaign)
        for name, value in self.content.items():
            setattr(proposal, name, value)
        return proposal


# ---------------------------------------------------------------------------
# Kontrollerna (apps/flamingo/checks.py, samma som kundens inskick)
# ---------------------------------------------------------------------------


#: Var ett problem från checks.py sitter, på svenska (Problem.field/.part).
CHECK_FIELDS = {
    "headlines": "Rubrik",
    "descriptions": "Beskrivning",
    "keywords": "Sökord",
    "negatives": "Negativa sökord",
    "daily_budget_kr": "Budget",
    "area": "Område",
    "name": "Namn",
}
CHECK_PAGE_PARTS = {
    "title": "Sidans rubrik",
    "lead": "Ingress",
    "points": "Punkt",
    "phone": "Telefon på sidan",
    "form_title": "Formulärets rubrik",
    "questions": "Fråga",
    "note": "Text under formuläret",
}
#: Listor där problemets index blir ett nummer ("Rubrik 3").
NUMBERED = {"headlines", "descriptions", "keywords", "points", "questions"}


def _problem_where(field_name, index=None, part=""):
    """ "Rubrik 3", "Ingress", "Sökord 2" ur checks.Problem.field/.index/.part."""
    if not field_name and not part:
        return ""
    if field_name == "page":
        where = CHECK_PAGE_PARTS.get(part, "Sidan")
        numbered = part in NUMBERED
    else:
        where = CHECK_FIELDS.get(field_name, field_name or "")
        numbered = field_name in NUMBERED
    if numbered and isinstance(index, int):
        where = f"{where} {index + 1}"
    return where


def _problem_text(problem):
    """Ett checks.Problem som en rad text: "Rubrik 3: Siffran 24 finns inte ..."."""
    where = _problem_where(problem.field, problem.index, problem.part)
    return f"{where}: {problem.message}" if where else problem.message


def run_checks(campaign):
    """Kontrollerna (checks.validate, samma som stoppar kundens inskick) på
    kampanjens innehåll, som en lista med texter.

    None när kontrollerna inte gick att köra; granskningen fungerar ändå,
    men sidan säger att inga kontroller kördes."""
    try:
        problems = checks.validate(campaign)
    except Exception:
        logger.exception("Flamingo-kontrollerna kunde inte köras (kampanj %s)", campaign.pk)
        return None
    return [_problem_text(p) for p in problems]


# ---------------------------------------------------------------------------
# Granskningen
# ---------------------------------------------------------------------------


def _is_editable(campaign, pending):
    """Byrån rättar bara medan kampanjen ligger hos ADX för granskning."""
    return campaign.status == Campaign.STATUS_IN_REVIEW and (
        pending is not None or campaign.approved_at is None
    )


def _can_publish(campaign, pending):
    return (
        campaign.approved_at is not None
        and pending is None
        and campaign.status not in (Campaign.STATUS_LIVE, Campaign.STATUS_PAUSED)
    )


def _publish_blockers(campaign, pending):
    """Varför kampanjen inte kan gå live än, som texter (tom = den kan).

    Betalningen hos Google stoppar inte: annonserna visas först när kunden
    lagt in den, och ADX ligger inte ute med några pengar (beslut
    2026-10-03). Panelen påminner om den i stället. Kontot måste däremot
    vara kopplat under ADX med sitt id (demokontot har inget id)."""
    account = campaign.account
    blockers = []
    if campaign.approved_at is None:
        blockers.append("Kunden har inte godkänt den granskade versionen.")
    if pending is not None:
        blockers.append("En ny runda väntar på granskning.")
    if not account.is_enabled or not account.customer.is_active:
        blockers.append("ADX Flamingo är avstängt för kunden, eller kunden är inaktiv.")
    if not account.google_linked:
        blockers.append(
            "Google-kontot är inte markerat som kopplat under ADX. "
            "Ändra det på kundkortet när det är gjort."
        )
    elif not account.is_demo and len(google_ads.digits(account.google_ads_customer_id)) != 10:
        blockers.append("Google Ads-kontots id saknas. Skriv det på kundkortet.")
    return blockers


def _review_context(campaign, form, pending, problems, problems_need_ack=False):
    account = campaign.account
    facts = list(account.facts.all())
    reviews = list(campaign.reviews.select_related("reviewer", "submitted_by").order_by("-round"))
    latest_done = next((r for r in reviews if r.state == Review.STATE_DONE), None)
    editable = _is_editable(campaign, pending)
    state = staff_state(campaign, pending)
    return {
        "active": "flamingo",
        "title": campaign.name,
        "campaign": campaign,
        "account": account,
        "customer": account.customer,
        "pending": pending,
        "reviews": reviews,
        "latest_done": latest_done,
        "facts": facts,
        "confirmed_count": sum(1 for f in facts if f.confirmed and f.value),
        "form": form,
        "editable": editable,
        "groups": form.groups if form else {},
        "match_choices": MATCH_CHOICES,
        "question_kinds": [(k, QUESTION_KIND_LABELS[k].capitalize()) for k in PAGE_QUESTION_KINDS],
        "problems": problems,
        "problems_need_ack": problems_need_ack,
        "checklist": [
            line.format(sales_mode=campaign.get_sales_mode_display().lower()) for line in CHECKLIST
        ],
        "headline_max": HEADLINE_MAX,
        "description_max": DESCRIPTION_MAX,
        "can_publish": _can_publish(campaign, pending),
        "publish_blockers": _publish_blockers(campaign, pending),
        **google_publish.panel(campaign),
        "landing_full_url": exports.landing_page_url(campaign),
        "keyword_list": [
            (text, MATCH_LABELS.get(match, match))
            for text, match in (_keyword_item(k) for k in campaign.keywords or [])
            if text
        ],
        "negative_list": [t for t in (_negative_text(n) for n in campaign.negatives or []) if t],
        "question_list": [
            (q.get("label"), QUESTION_KIND_LABELS.get(q.get("kind"), q.get("kind")))
            for q in (campaign.page or {}).get("questions") or []
            if isinstance(q, dict) and q.get("label")
        ],
        "latest_changes": grouped_changes(latest_done.changes) if latest_done else [],
        "state_label": state[0],
        "state_badge": state[1],
    }


@staff_required
@require_http_methods(["GET", "POST"])
def review(request, pk):
    campaign = get_object_or_404(
        Campaign.objects.select_related("account__customer", "service"), pk=pk
    )
    pending = campaign.pending_review()
    editable = _is_editable(campaign, pending)

    if request.method == "POST":
        if not editable:
            messages.error(
                request,
                "Kampanjen ligger inte hos ADX för granskning. Inget sparades.",
            )
            return _back_to_review(campaign.pk)
        form = ReviewForm.bound(campaign, request.POST)
        if not form.has_errors:
            problems = run_checks(form.proposal())
            if problems and not request.POST.get("accept_checks"):
                messages.error(
                    request,
                    "Kontrollerna hittade något. Rätta det, eller bocka i att du läst "
                    "kontrollerna och skicka igen.",
                )
                context = _review_context(campaign, form, pending, problems, True)
                return render(request, "manage/flamingo/review.html", context, status=400)
            return _finish_review(request, campaign.pk, form)
        messages.error(request, "Något behöver rättas innan det kan skickas till kunden.")
        problems = run_checks(campaign)
        context = _review_context(campaign, form, pending, problems)
        return render(request, "manage/flamingo/review.html", context, status=400)

    form = ReviewForm.initial(campaign) if editable else None
    return render(
        request,
        "manage/flamingo/review.html",
        _review_context(campaign, form, pending, run_checks(campaign)),
    )


def _finish_review(request, campaign_pk, form):
    """Spara granskningen: innehållet, diffen med skälen, och lämna över
    till kunden. Inget mejl: kunden ser det i verktyget."""
    now = timezone.now()
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update().get(pk=campaign_pk)
        pending = (
            Review.objects.select_for_update()
            .filter(campaign=campaign, state=Review.STATE_PENDING)
            .order_by("-round")
            .first()
        )
        if not _is_editable(campaign, pending):
            messages.error(
                request,
                "Någon annan hann före: kampanjen ligger inte längre hos ADX. Inget sparades.",
            )
            return _back_to_review(campaign_pk)
        if pending is None:
            # Kampanjen kom hit utan en runda (äldre data): skapa den nu, med
            # innehållet som det var när granskningen började.
            pending = Review.objects.create(
                campaign=campaign,
                round=campaign.next_round(),
                submitted_at=campaign.updated_at or now,
                snapshot=campaign.content_snapshot(),
            )
        for name, value in form.content.items():
            setattr(campaign, name, value)
        campaign.status = Campaign.STATUS_NEEDS_CUSTOMER
        # Kunden godkänner den granskade versionen, inte den inskickade.
        campaign.approved_at = None
        campaign.approved_by = None
        campaign.save()

        pending.changes = form.changes
        pending.note = form.note
        pending.reviewer = request.user
        pending.reviewed_at = now
        pending.state = Review.STATE_DONE
        pending.save(update_fields=["changes", "note", "reviewer", "reviewed_at", "state"])

    count = len(form.changes)
    if count:
        word = "ändring" if count == 1 else "ändringar"
        summary = f"{count} {word} sparade"
    else:
        summary = "Inga ändringar"
    messages.success(
        request,
        f"{summary}. Kampanjen väntar nu på kundens godkännande i verktyget. "
        "Kunden har inte mejlats; säg till själv om det behövs.",
    )
    return redirect("manage:flamingo_queue")


# ---------------------------------------------------------------------------
# Publicering, paus och återupptagning
# ---------------------------------------------------------------------------


@staff_required
@require_POST
def publish(request, pk):
    """action=publish (live), pause, resume eller return.

    Med Google Ads API (google_publish.py) görs det hos Google först och här
    när Google svarat. Utan API:t, för demokonton och för kampanjer som
    publicerades för hand görs samma sak i Google för hand (stegen står på
    sidan); här ändras kampanjens status och därmed landningssidan. Med
    manual=1 publiceras för hand också när API:t är inkopplat (när Google
    sagt nej och byrån lagt in kampanjen med Editor-filen).

    return tar en godkänd kampanj som inte är publicerad tillbaka till
    granskning: en ny runda hos ADX, och kundens godkännande gäller inte
    längre. Byrån rättar det Google sa nej till och skickar den till kunden,
    som godkänner igen. Kunden mejlas inte."""
    action = request.POST.get("action") or "publish"
    if action not in ("publish", "pause", "resume", "return"):
        raise Http404
    campaign = get_object_or_404(
        Campaign.objects.select_related("account__customer", "service"), pk=pk
    )
    if action == "return":
        return _return_to_review(request, campaign.pk)
    if action == "publish":
        use_api = (
            google_publish.api_available(campaign.account) and request.POST.get("manual") != "1"
        )
    else:
        use_api = google_publish.uses_api(campaign)
    if use_api:
        return _publish_with_api(request, campaign, action)

    now = timezone.now()
    with transaction.atomic():
        campaign = get_object_or_404(
            Campaign.objects.select_for_update(of=("self",)).select_related(
                "account__customer", "service"
            ),
            pk=pk,
        )
        pending = campaign.pending_review()

        if action == "pause":
            if campaign.status != Campaign.STATUS_LIVE:
                messages.error(request, "Bara en kampanj som är live kan pausas.")
                return _back_to_review(campaign.pk, "publicering")
            campaign.status = Campaign.STATUS_PAUSED
            campaign.save(update_fields=["status", "updated_at"])
            messages.success(
                request,
                "Pausad här, och landningssidan är stängd. Pausa kampanjen i "
                "Google Ads också. Kunden har inte mejlats.",
            )
            return _back_to_review(campaign.pk, "publicering")

        if action == "resume":
            if campaign.status != Campaign.STATUS_PAUSED:
                messages.error(request, "Bara en pausad kampanj kan återupptas.")
                return _back_to_review(campaign.pk, "publicering")
            blockers = _publish_blockers(campaign, pending)
            if blockers:
                messages.error(request, "Kan inte återupptas: " + " ".join(blockers))
                return _back_to_review(campaign.pk, "publicering")
            campaign.status = Campaign.STATUS_LIVE
            campaign.save(update_fields=["status", "updated_at"])
            messages.success(
                request,
                "Live igen, och landningssidan är öppen. Slå på kampanjen i "
                "Google Ads också. Kunden har inte mejlats.",
            )
            return _back_to_review(campaign.pk, "publicering")

        if campaign.status == Campaign.STATUS_LIVE:
            messages.info(request, "Kampanjen är redan live.")
            return _back_to_review(campaign.pk, "publicering")
        blockers = _publish_blockers(campaign, pending)
        if campaign.status == Campaign.STATUS_PAUSED:
            blockers.append("Kampanjen är pausad: använd Återuppta.")
        if blockers:
            messages.error(request, "Inget publicerades. " + " ".join(blockers))
            return _back_to_review(campaign.pk, "publicering")

        google_id = re.sub(r"[\s-]", "", request.POST.get("google_campaign_id", ""))
        if google_id and not re.fullmatch(r"\d{1,20}", google_id):
            messages.error(request, "Kampanjens id hos Google är bara siffror. Inget publicerades.")
            return _back_to_review(campaign.pk, "publicering")

        campaign.status = Campaign.STATUS_LIVE
        campaign.published_at = now
        # Ett tidigare fel (API:t efter kundens godkännande) gäller inte längre.
        campaign.google_error = ""
        fields = ["status", "published_at", "google_error", "updated_at"]
        if google_id:
            campaign.google_campaign_id = google_id
            fields.append("google_campaign_id")
        campaign.save(update_fields=fields)

    messages.success(
        request,
        f"{campaign.name} är live. Landningssidan är öppen på {campaign.landing_url}. "
        "Kunden har inte mejlats.",
    )
    return _back_to_review(campaign.pk, "publicering")


def _return_to_review(request, pk):
    """Tillbaka till granskning: bara en godkänd kampanj som inte är live
    eller pausad och inte redan har en runda hos ADX."""
    with transaction.atomic():
        campaign = get_object_or_404(Campaign.objects.select_for_update(of=("self",)), pk=pk)
        pending = campaign.pending_review()
        if not _can_publish(campaign, pending):
            messages.error(
                request,
                "Bara en godkänd kampanj som inte är publicerad kan tas tillbaka till "
                "granskning. Inget ändrades.",
            )
            return _back_to_review(pk, "publicering")
        Review.objects.create(
            campaign=campaign,
            round=campaign.next_round(),
            submitted_by=request.user,
            snapshot=campaign.content_snapshot(),
            taken_back=True,
        )
        campaign.status = Campaign.STATUS_IN_REVIEW
        campaign.approved_at = None
        campaign.approved_by = None
        campaign.save(update_fields=["status", "approved_at", "approved_by", "updated_at"])
    messages.success(
        request,
        "Kampanjen ligger hos ADX för granskning igen och kundens godkännande gäller inte "
        "längre. Rätta det Google sa nej till och skicka den till kunden. Kunden har inte "
        "mejlats.",
    )
    return _back_to_review(pk)


def _publish_with_api(request, campaign, action):
    """publish med Google Ads API: Google först, sedan status här. Ett fel
    från Google lämnar kampanjen som den var och visas för byrån. Varken vi
    eller Google mejlar kunden."""
    back = _back_to_review(campaign.pk, "publicering")
    pending = campaign.pending_review()

    if action == "pause":
        try:
            google_publish.pause(campaign)
        except google_publish.PublishError as exc:
            messages.error(
                request,
                f"Inget pausades. {exc.message} Pausa kampanjen i Google Ads för hand om "
                "det brådskar.",
            )
            return back
        messages.success(
            request,
            "Pausad hos Google och här, och landningssidan är stängd. Kunden har inte mejlats.",
        )
        return back

    if action == "resume":
        blockers = _publish_blockers(campaign, pending)
        if campaign.status == Campaign.STATUS_PAUSED and blockers:
            messages.error(request, "Kan inte återupptas: " + " ".join(blockers))
            return back
        try:
            google_publish.resume(campaign)
        except google_publish.PublishError as exc:
            messages.error(request, f"Inget återupptogs. {exc.message}")
            return back
        messages.success(
            request,
            "Live igen hos Google och här, och landningssidan är öppen. Kunden har inte mejlats.",
        )
        return back

    if campaign.status == Campaign.STATUS_LIVE:
        messages.info(request, "Kampanjen är redan live.")
        return back
    blockers = _publish_blockers(campaign, pending)
    if campaign.status == Campaign.STATUS_PAUSED:
        blockers.append("Kampanjen är pausad: använd Återuppta.")
    if blockers:
        messages.error(request, "Inget publicerades. " + " ".join(blockers))
        return back
    try:
        result = google_publish.go_live(campaign, request.user)
    except google_publish.PublishError as exc:
        messages.error(request, f"Inget publicerades. {exc.message}")
        return back
    if result.already_live:
        messages.info(request, "Kampanjen är redan live.")
        return back
    parts = [
        f"{campaign.name} är live hos Google (kampanj {result.campaign_id}), och "
        f"landningssidan är öppen på {campaign.landing_url}."
    ]
    if result.adopted:
        parts.append(
            "Kampanjen fanns redan hos Google från ett tidigare försök och kopplades hit i "
            "stället för att skapas en gång till."
        )
    if result.auto_tagging_enabled:
        parts.append("Automatisk taggning slogs på i kundens Google Ads-konto.")
    if google_publish.billing_missing(campaign.account):
        parts.append(google_publish.MSG_BILLING)
    parts.append("Kunden har inte mejlats.")
    messages.success(request, " ".join(parts))
    return back


# ---------------------------------------------------------------------------
# Filerna
# ---------------------------------------------------------------------------


def _csv_response(text, filename, bom=False):
    response = HttpResponse(("﻿" if bom else "") + text, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@staff_required
def editor_csv(request, pk):
    campaign = get_object_or_404(
        Campaign.objects.select_related("account__customer", "service"), pk=pk
    )
    filename = f"flamingo-{campaign.page_slug or campaign.pk}-editor.csv"
    return _csv_response(exports.google_ads_editor_csv(campaign), filename, bom=True)


def _customer_param(request):
    raw = (request.POST.get("kund") or request.GET.get("kund") or "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise Http404
    return int(raw)


@staff_required
@require_http_methods(["GET", "POST"])
def conversions_csv(request):
    """GET: konverteringarna i kö som Googles importfil (alla kunder, eller
    ?kund=<pk>). Nedladdningen ändrar en sak: raderna i filen får
    downloaded_at och skickas aldrig med API:t (google_conversions._due), så
    att ingen konvertering räknas två gånger. En rad som API:t håller på att
    skicka är låst och hoppas över (SKIP LOCKED); den kommer med i nästa fil
    om Google inte tar emot den.

    POST: "Markera som exporterade" för raderna i formuläret (upload=<pk>,
    samma kundfilter). Bara rader som står i kö och har varit med i en
    nedladdad fil ändras, så en förfrågan som kommit in efter nedladdningen
    aldrig markeras utan att ha varit med i filen."""
    customer_pk = _customer_param(request)
    if customer_pk is not None and not Customer.objects.filter(pk=customer_pk).exists():
        raise Http404
    uploads = queued_uploads(customer_pk)
    stamp = timezone.localtime().strftime("%Y%m%d")
    suffix = f"-kund-{customer_pk}" if customer_pk is not None else ""
    filename = f"flamingo-konverteringar{suffix}-{stamp}.csv"

    if request.method == "POST":
        ids = {int(v) for v in request.POST.getlist("upload") if str(v).isdigit()}
        if not ids:
            messages.error(request, "Inga rader valda. Inget markerades.")
            return redirect(reverse("manage:flamingo_queue") + "#konverteringar")
        chosen = uploads.filter(pk__in=ids)
        with transaction.atomic():
            count = chosen.filter(downloaded_at__isnull=False).update(
                status=ConversionUpload.STATUS_EXPORTED,
                exported_at=timezone.now(),
                response={"export": filename, "by": request.user.get_username()},
            )
        word = "konvertering" if count == 1 else "konverteringar"
        messages.success(request, f"{count} {word} markerade som exporterade.")
        never = chosen.filter(downloaded_at__isnull=True).count()
        if never:
            messages.warning(
                request,
                f"{never} har inte varit med i en nedladdad fil och står kvar i kö. Ladda ner "
                "filen och ladda upp den i Google Ads först.",
            )
        return redirect(reverse("manage:flamingo_queue") + "#konverteringar")

    with transaction.atomic():
        rows = list(uploads.select_for_update(skip_locked=True, of=("self",)))
        ConversionUpload.objects.filter(
            pk__in=[row.pk for row in rows], downloaded_at__isnull=True
        ).update(downloaded_at=timezone.now())
    return _csv_response(exports.offline_conversions_csv(rows), filename)


# ---------------------------------------------------------------------------
# Google-kopplingen på kundkortet
# ---------------------------------------------------------------------------


@staff_required
@require_POST
def google_update(request, pk):
    """Byrån bockar av Google-kopplingen för hand (README steg 3): status,
    kontots id (tio siffror, sparas 123-456-7890) och en notering. Utan
    Google Ads API är det här vägen; med API:t finns knapparna i
    manage_google.google_account, och det här är reserven.

    Byts id:t töms det som gällde det förra kontot hos Google (betalningen,
    förfrågan, läsningen; google_accounts.forget_previous_account) och
    kampanjernas gamla fel från Google. Ett id som ett annat Flamingo-konto
    har sparas aldrig. Kunden mejlas inte; kunden ser statusen i verktyget."""
    from .google_accounts import clear_campaign_errors, forget_previous_account

    customer = get_object_or_404(Customer, pk=pk)
    account = FlamingoAccount.objects.filter(customer=customer).first()
    if account is None:
        messages.error(request, "Aktivera ADX Flamingo för kunden först.")
        return _back_to_card(customer.pk)

    status = request.POST.get("google_status", "")
    if status not in dict(FlamingoAccount.GOOGLE_CHOICES):
        messages.error(request, "Välj en status för Google-kopplingen. Inget sparades.")
        return _back_to_card(customer.pk)
    google_id = format_google_ads_id(request.POST.get("google_ads_customer_id", ""))
    if google_id is None:
        messages.error(
            request,
            "Kontots id ska vara tio siffror, till exempel 123-456-7890. Inget sparades.",
        )
        return _back_to_card(customer.pk)
    needs_id = (
        FlamingoAccount.GOOGLE_ID_GIVEN,
        FlamingoAccount.GOOGLE_LINKED,
        FlamingoAccount.GOOGLE_BILLING_OK,
    )
    if status in needs_id and not google_id:
        messages.error(request, "Skriv kontots id för den statusen. Inget sparades.")
        return _back_to_card(customer.pk)

    changed = google_id != account.google_ads_customer_id
    if changed and google_id and not account.is_demo:
        other = (
            FlamingoAccount.objects.filter(google_ads_customer_id=google_id, is_demo=False)
            .exclude(pk=account.pk)
            .select_related("customer")
            .first()
        )
        if other is not None:
            messages.error(
                request,
                f"Id:t {google_id} används redan av {other.customer.name}. Ett Google Ads-konto "
                "hör till en kund. Inget sparades.",
            )
            return _back_to_card(customer.pk)
    fields = ["google_status", "google_ads_customer_id", "google_note"]
    if changed:
        fields += forget_previous_account(account)
    account.google_status = status
    account.google_ads_customer_id = google_id
    account.google_note = _plain(request.POST.get("google_note"), 300)
    try:
        with transaction.atomic():
            account.save(update_fields=[*fields, "updated_at"])
    except IntegrityError:
        messages.error(
            request,
            f"Id:t {google_id} sparades nyss på ett annat Flamingo-konto. Inget sparades.",
        )
        return _back_to_card(customer.pk)
    if changed:
        clear_campaign_errors(account)
    messages.success(
        request,
        f"Google för {customer.name}: {account.get_google_status_display()}. "
        "Kunden har inte mejlats.",
    )
    return _back_to_card(customer.pk)
