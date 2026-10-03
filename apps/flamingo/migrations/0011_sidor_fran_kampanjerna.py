"""
Varje kampanjs sida (Campaign.page) blir en LandingPage i sidbyggaren, i
designen Ren med blå palett, och kampanjen pekar på den (landing_page).

Mappningen är fryst här (den ändras inte när sidbyggaren ändras):

    title, lead, points, phone   Hero: "Med ringknapp" för "ringer direkt",
                                 annars "Med formulär" (formuläret bredvid)
    form_title, questions, note  Formulär: "Kort" för "ringer direkt",
                                 "Med frågor" för offert, "Boka tid" för
                                 boka tid. Texten under formuläret för
                                 "ringer direkt" ("Medan du väntar") blir
                                 rutan med den rubriken.

Det sidan visade förut följer med: rubriken (eller tjänstens namn när den
var tom), numret ("phone" på sidan vann, även tomt; utan nyckeln den
bekräftade telefonuppgiften), punkterna, formulärets rubrik (eller
standarden för sättet att sälja) och frågorna med samma etiketter, så att
Lead.answers fortsätter att använda etiketten.

Det här anpassas till sidbyggarens schema, så att varje sida går att
spara och publicera:

- Telefonen blir ett nummer: den gamla generatorn skrev hela uppgiften
  ("08-000 00 00 (vardagar) eller 070-000 00 00 (jour)"), och fältet rymmer
  40 tecken. Det första numret som går att tolka följer med; finns inget
  blir fältet tomt (migreringen stoppar aldrig).
- Frågornas nycklar görs om till a-z, 0-9, _ och bindestreck, högst 40
  tecken och unika (granskningen kunde skriva "<40 tecken>-2"). Etiketten,
  som svaren sparas under, ändras inte.
- Texterna kortas till fältens längd (rubriken 120, punkterna 120,
  formulärets text 600 tecken).

Live och pausade kampanjer får blocken publicerade (published_at som
kampanjens publicering); de andra bara som utkast. Campaign.page står kvar
som historik och läses inte längre. Bakåt gör migreringen ingenting:
sidorna står kvar, och 0010 tar bort dem om även den backas.
"""

import re
import secrets
import string

from django.db import migrations
from django.utils import timezone
from django.utils.text import slugify

ALPHABET = string.ascii_letters + string.digits
QUESTION_KINDS = ("text", "textarea", "date")
FORM_TITLES = {
    "call": "Hellre att vi ringer dig?",
    "quote": "Berätta om jobbet",
    "book": "Boka en tid",
}
FORM_VARIANTS = {"call": "short", "quote": "questions", "book": "booking"}
SUBMITS = {"short": "Ring upp mig", "questions": "Skicka", "booking": "Skicka"}


def _id(prefix):
    return prefix + "".join(secrets.choice(ALPHABET) for _ in range(12))


def _block(kind, variant, fields, at):
    version = {"id": _id("v_"), "fields": fields, "source": "template", "by": None, "at": at}
    return {
        "id": _id("b_"),
        "type": kind,
        "variant": variant,
        "active": version["id"],
        "versions": [version],
    }


def _text(value):
    return str(value or "").strip()


PHONE_MAX = 40
KEY_MAX = 40
_PHONE_RUN = re.compile(r"(?<![\w+])(?:\+|00)?\d[\d \-]{5,}\d(?!\w)")
_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")


def _phone_ok(number):
    """Ett nummer som går att ringa (samma regel som sms.normalize_phone)."""
    text = re.sub(r"[^\d+]", "", number)
    if text.startswith("00"):
        text = "+" + text[2:]
    if text.startswith("+"):
        return text[1:].isdigit() and 8 <= len(text) - 1 <= 15
    return text.startswith("0") and text.isdigit() and 8 <= len(text) <= 11


def one_phone(value):
    """Det första numret i texten som går att ringa och ryms, eller ""."""
    text = " ".join(_text(value).split())
    for match in _PHONE_RUN.finditer(text):
        number = match.group().strip(" -")
        if len(number) <= PHONE_MAX and _phone_ok(number):
            return number
    return ""


def question_key(key, label, n, seen):
    """En nyckel som klarar schemat och är unik bland frågorna."""
    base = slugify(key)[:KEY_MAX].strip("-_") or slugify(label)[:KEY_MAX].strip("-_")
    base = base if _KEY.fullmatch(base or "") else f"fraga-{n}"
    candidate, i = base, 2
    while candidate in seen:
        suffix = f"-{i}"
        candidate = base[: KEY_MAX - len(suffix)].rstrip("-_") + suffix
        i += 1
    seen.add(candidate)
    return candidate


def blocks_for(page, mode, service_name, fact_phone, at):
    """Blocken för en gammal sida (Campaign.page) och sättet att sälja."""
    page = page if isinstance(page, dict) else {}
    phone = one_phone(page.get("phone") if "phone" in page else fact_phone)
    form_variant = FORM_VARIANTS.get(mode, "questions")
    points = [_text(p)[:120] for p in page.get("points") or [] if _text(p)][:6]
    questions, old_keys, seen = [], set(), set()
    for raw in page.get("questions") or []:
        if not isinstance(raw, dict):
            continue
        key, label = _text(raw.get("key")), _text(raw.get("label"))
        if not key or not label or key in old_keys:
            continue
        old_keys.add(key)
        kind = raw.get("kind") if raw.get("kind") in QUESTION_KINDS else "text"
        new_key = question_key(key, label, len(questions) + 1, seen)
        questions.append({"key": new_key, "label": label[:120], "kind": kind})
        if len(questions) >= 8:
            break
    note = _text(page.get("note"))[:600]
    hero = {
        "title": (_text(page.get("title")) or _text(service_name))[:120],
        "lead": _text(page.get("lead"))[:600],
        "points": points,
        "phone": phone,
        "image": None,
    }
    form = {
        "title": (_text(page.get("form_title")) or FORM_TITLES.get(mode, "Berätta om jobbet"))[
            :120
        ],
        "questions": questions,
        "note_title": "Medan du väntar" if mode == "call" and note else "",
        "note": note,
        "submit": SUBMITS[form_variant],
    }
    return [
        _block("hero", "call" if mode == "call" else "form", hero, at),
        _block("form", form_variant, form, at),
    ]


def forwards(apps, schema_editor):
    Campaign = apps.get_model("flamingo", "Campaign")
    LandingPage = apps.get_model("flamingo", "LandingPage")
    Fact = apps.get_model("flamingo", "Fact")
    now = timezone.now()
    names = {}
    campaigns = (
        Campaign.objects.filter(landing_page__isnull=True)
        .select_related("service", "account")
        .order_by("pk")
    )
    for campaign in campaigns:
        fact_phone = (
            Fact.objects.filter(account_id=campaign.account_id, key="telefon", confirmed=True)
            .exclude(value="")
            .values_list("value", flat=True)
            .first()
        )
        at = (campaign.created_at or now).isoformat()
        blocks = blocks_for(
            campaign.page, campaign.service.sales_mode, campaign.service.name, fact_phone, at
        )
        taken = names.setdefault(
            campaign.account_id,
            set(
                LandingPage.objects.filter(account_id=campaign.account_id).values_list(
                    "name", flat=True
                )
            ),
        )
        base = (campaign.name or "Sida")[:110]
        name, n = base, 2
        while name in taken:
            name, n = f"{base} ({n})", n + 1
        taken.add(name)
        published = campaign.status in ("live", "paused")
        page = LandingPage.objects.create(
            account_id=campaign.account_id,
            name=name,
            design="ren",
            palette="blue",
            draft={"blocks": blocks},
            published={"blocks": [dict(b) for b in blocks]} if published else {"blocks": []},
            published_at=(campaign.published_at or now) if published else None,
            created_by_id=campaign.created_by_id,
            created_at=campaign.created_at or now,
        )
        campaign.landing_page_id = page.pk
        campaign.save(update_fields=["landing_page"])


class Migration(migrations.Migration):
    dependencies = [
        ("flamingo", "0010_sidbyggaren"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
