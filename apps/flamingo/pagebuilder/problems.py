"""
Kontrollerna av en landningssida (samma regler som kampanjens, checks.py).

    page_problems(page, context=None, *, blocks=None) -> [checks.Problem]
    page_context(page) -> checks.Context
    published_problems(page, *, only_fixed=False)
                        problemen på den publicerade sidan, märkta så att
                        kunden ser var de sitter ("på den publicerade sidan")

Tom lista betyder att sidan går att publicera. Varje problem har
field="page", block (blockets id), part (fältet), index (raden eller
posten) och where ("Toppen, rubrik") så att redigeraren kan visa det vid
fältet.

Reglerna:

- Varje text (rubriker, ingresser, rader, frågor, svar) går genom
  checks.text_problems: inga siffror som inte finns bland de bekräftade
  uppgifterna, inga påståenden som inte går att belägga, inga löften om tider,
  och garanti, gratis och öppettider bara när en uppgift säger det.
- Inga AI-typografitecken (tankstreck, typografiska citattecken,
  ellipstecken) i någon text.
- Varje telefonnummer måste finnas bland de bekräftade uppgifterna.
- Sidan behöver Toppen med rubrik och minst ett sätt att nå företaget: ett
  formulär eller ett nummer att ringa (Toppen eller ringremsan).
- Högst en Toppen, ett formulär, en ringremsa och ett omdömesblock.
- Pris, certifikat, garanti och ringremsa bara med den bekräftade uppgift
  blocket kräver (registry.BlockType.requires), också när blocket kom in på
  sidan på något annat sätt än biblioteket (en kopia, AI, ett gammalt
  utkast). Omdömen från Google prövas inte så: blocket syns inte utan
  omdömen.
- I certifikat- och garantiblocken får kvalitetsord och behörigheter
  ("auktoriserade", "försäkrade", "certifierade") bara stå när de finns
  bland de bekräftade uppgifterna, och varje certifikat ska finnas där
  (samma vakt som AI har, facts.CLAIM_WORDS).
- Toppen med formulär behöver ett formulärblock; Toppen med bild, Personen bakom
  med bild och Före och efter behöver sina bilder, och bilderna måste finnas
  i kontots mediaarkiv.
- Varje fråga i formuläret behöver en text. Ett flerval (Flerval, ett svar
  eller flera svar) behöver två till åtta alternativ, högst 60 tecken
  vardera, som inte blir samma svar (blocks.option_problems), och högst två
  flervalsfrågor i ett formulär (Färre fält): den tredje och fler får
  blocks.MSG_CHOICE_COUNT. Problemet står vid frågan ("Formulär, fråga 2").
- Alternativen prövas som texter (löften, påståenden, typografi) bara på ett
  flerval: på en annan fråga visas de inte. De står vid frågan som
  "Formulär, fråga 2, alternativen". Ett alternativ är besökarens svar, inte
  ett påstående, så talen i det ("1", "4 eller fler", "Under 50 kvm")
  prövas inte mot uppgifterna; ett alternativ med ett pris eller en andel
  ("Service 995 kr", "20 %") prövas som vanligt (_option_problems).

Omdömen från Google kontrolleras inte som text: de är kundernas egna ord,
oförändrade, med Googles märkning.
"""

import dataclasses
import re

from apps.common.security import AI_TYPOGRAPHY_CHARS

from .. import checks
from ..models import PAGE_CHOICE_KINDS, MediaAsset
from .blocks import MSG_CHOICE_COUNT, active_fields, option_problems
from .facts import CERTIFICATE_WORDS, CLAIM_WORDS, facts_for
from .registry import (
    CHOICE_QUESTIONS_MAX,
    ITEMS,
    LINES,
    MEDIA,
    PHONE,
    PROFILE_REQUIREMENTS,
    REQUIRES_TEXT,
    TEXT,
    TEXTAREA,
    TYPES,
    BuildContext,
    _meets,
)

MSG_TYPOGRAPHY = (
    'Byt tankstreck, typografiska citattecken och ellipstecken mot vanliga tecken (-, " och ...).'
)
MSG_PHONE = "Telefonnumret finns inte bland dina bekräftade uppgifter."
MSG_NO_HERO = "Sidan behöver blocket Toppen, med rubriken, överst."
MSG_NO_CONTACT = "Sidan behöver ett sätt att nå er: ett formulär eller ett nummer att ringa."
MSG_EMPTY = "Sidan har inga block än."
MSG_CLAIM = "{word} står inte bland dina bekräftade uppgifter. Bekräfta det under Företaget först."
MSG_CERTIFICATE = (
    "Certifikatet står inte bland dina bekräftade uppgifter. Bekräfta det under Företaget först."
)
#: Märkningen av ett problem på den publicerade sidan (published_problems).
PUBLISHED_WHERE = " (på den publicerade sidan)"
PUBLISHED_FIXED = "Utkastet är rättat. Publicera det, så gäller rättelsen."
PUBLISHED_OPEN = "Rätta det i utkastet och publicera."
#: Blocken där kvalitetsord och behörigheter prövas mot uppgifterna.
CLAIM_BLOCKS = ("certificates", "guarantee")
_WORD = re.compile(r"[^\W\d_]{4,}")
#: Ett pris eller en andel i ett flervals alternativ ("995 kr", "20 %",
#: "kr 500"): då prövas alternativets tal mot uppgifterna.
_PRICED = re.compile(
    r"\d\s*(?:kr\b|kronor\b|sek\b|:-|%|procent\b|€|\$|euro?\b)|(?:\bkr|\bsek|€|\$)\s*\d",
    re.I,
)
_STOP = frozenset("och eller med utan från till som har för alla våra vara".split())


def page_context(page, *, also=()):
    """checks.Context för sidan: kontots bekräftade uppgifter, kundens
    namn, och tjänsten och området för varje kampanj som använder sidan
    (och kampanjerna i also, till exempel en som ska byta till sidan)."""
    account = page.account
    extra = [account.customer.name, facts_for(account).company]
    for campaign in [*page.campaigns.select_related("service"), *also]:
        extra += [campaign.service.name if campaign.service_id else "", campaign.area]
    return checks.build_context(account.confirmed_facts().values(), extra=extra)


def _where(block_type, spec, index=None, sub=None):
    label = spec.label.lower()
    if index is not None and sub is not None and sub.key == "options":
        # Formulärets alternativ: "Formulär, fråga 2, alternativen".
        item = (spec.item_label or spec.label).lower()
        return f"{block_type.name}, {item} {index + 1}, alternativen"
    if index is not None and spec.kind in (LINES, ITEMS):
        item = sub.label if sub else (spec.item_label or spec.label)
        label = f"{item.lower()} {index + 1}"
    elif sub is not None:
        label = sub.label.lower()
    return f"{block_type.name}, {label}"


def _texts(block_type, block_fields, variant):
    """(spec, index, underfält, text) för varje text i blocket som varianten
    visar."""
    for spec in block_type.fields_for(variant):
        value = block_fields.get(spec.key)
        if spec.kind in (TEXT, TEXTAREA, PHONE):
            if value:
                yield spec, None, None, str(value)
        elif spec.kind == LINES:
            for i, line in enumerate(value or []):
                yield spec, i, None, str(line)
        elif spec.kind == ITEMS:
            for i, item in enumerate(value or []):
                if not isinstance(item, dict):
                    continue
                for sub in spec.items:
                    if sub.key == "options" and item.get("kind") not in PAGE_CHOICE_KINDS:
                        # Formulärets alternativ syns bara på ett flerval.
                        continue
                    if sub.kind in (TEXT, TEXTAREA) and item.get(sub.key):
                        yield spec, i, sub, str(item[sub.key])


def _option_problems(text, context):
    """checks.text_problems för ett flervals alternativ, ett per rad: talen i
    ett alternativ utan pris eller andel räknas som bekräftade (det är
    besökarens svar, inte ett påstående). Samma meddelande en gång."""
    found = []
    for line in text.splitlines():
        line_context = context
        if not _PRICED.search(line):
            numbers = context.numbers | checks.number_tokens(line)
            line_context = dataclasses.replace(context, numbers=frozenset(numbers))
        for message in checks.text_problems(line, line_context):
            if message not in found:
                found.append(message)
    return found


def _grounded(text, facts):
    """Finns varje ord (fyra bokstäver eller fler) i certifikatets namn
    bland de bekräftade uppgifterna? Jämförs på ordens fem första bokstäver,
    som AI-vakten."""
    words = [w for w in _WORD.findall((text or "").casefold()) if w not in _STOP]
    return all(word[:5] in facts.claims_text for word in words)


def page_problems(page, context=None, *, blocks=None):
    """Problemen på sidan (blocks, annars utkastet). Se modulens docstring."""
    blocks = page.draft_blocks if blocks is None else blocks
    context = context or page_context(page)
    facts = facts_for(page.account)
    problems = []

    def add(message, block=None, spec=None, index=None, sub=None, where=""):
        block_type = TYPES.get(block.get("type")) if block else None
        if not where and block_type is not None:
            where = _where(block_type, spec, index, sub) if spec else block_type.name
        problems.append(
            checks.Problem(
                "page",
                message,
                index=index,
                part=spec.key if spec else "",
                block=(block or {}).get("id", ""),
                where=where,
            )
        )

    if not blocks:
        add(MSG_EMPTY, where="Sidan")
        return problems

    media_ids = set(MediaAsset.objects.filter(account=page.account).values_list("pk", flat=True))
    counts = {}
    has_form = any(b.get("type") == "form" for b in blocks)
    can_call = False
    for block in blocks:
        block_type = TYPES.get(block.get("type"))
        if block_type is None:
            add("Okänd blocktyp. Ta bort blocket.", where="Sidan")
            continue
        counts[block_type.key] = counts.get(block_type.key, 0) + 1
        if block_type.single and counts[block_type.key] == 2:
            add(f"Sidan har mer än ett block av sorten {block_type.name}. Ta bort ett.", block)
        variant = block.get("variant")
        fields = active_fields(block)
        requires = block_type.requires
        # Ett omdömesblock utan profil döljs på sidan i stället (render.py).
        if requires and requires not in PROFILE_REQUIREMENTS:
            # Vilket pris sidan får visa avgörs när blocket skapas (sidans
            # tjänst); här räcker ett bekräftat pris, och siffrorna prövas
            # mot uppgifterna nedan.
            ctx = BuildContext(price=facts.prices[0][1] if facts.prices else "")
            if not _meets(requires, facts, page.account, ctx):
                add(REQUIRES_TEXT[requires], block)

        for spec, index, sub, text in _texts(block_type, fields, variant):
            if any(ch in text for ch in AI_TYPOGRAPHY_CHARS):
                add(MSG_TYPOGRAPHY, block, spec, index, sub)
            if spec.kind == PHONE:
                if checks.number_tokens(text) - context.numbers:
                    add(MSG_PHONE, block, spec)
                continue
            if sub is not None and sub.key == "options":
                messages = _option_problems(text, context)
            else:
                messages = checks.text_problems(text, context)
            for message in messages:
                add(message, block, spec, index, sub)
            if block_type.key in CLAIM_BLOCKS:
                _claim_problems(text, facts, block, block_type, spec, index, sub, add)

        for spec in block_type.fields_for(variant):
            value = fields.get(spec.key)
            if spec.required and spec.kind != MEDIA and not value:
                add(f"Fyll i {spec.label.lower()}.", block, spec)
            if spec.kind == MEDIA:
                if value is None and (spec.required or spec.variants):
                    add(f"Välj en bild ({spec.label.lower()}) ur mediaarkivet.", block, spec)
                elif value is not None and value not in media_ids:
                    add("Bilden finns inte längre i mediaarkivet. Välj en annan.", block, spec)

        if block_type.key == "hero":
            # Varje variant ritar numret som en ringlänk (Med ringknapp som
            # den stora knappen).
            if fields.get("phone"):
                can_call = True
            if variant == "form" and not has_form:
                add("Toppen med formulär behöver ett formulärblock på sidan.", block)
        elif block_type.key == "callbar" and fields.get("phone"):
            can_call = True
        elif block_type.key == "form" and fields.get("questions"):
            questions_spec = block_type.field("questions")
            choices = 0
            for i, question in enumerate(fields["questions"]):
                if not isinstance(question, dict):
                    continue
                if not question.get("label"):
                    add("Skriv frågan.", block, questions_spec, i)
                if question.get("kind") in PAGE_CHOICE_KINDS:
                    choices += 1
                    if choices > CHOICE_QUESTIONS_MAX:
                        add(MSG_CHOICE_COUNT, block, questions_spec, i)
                    for message in option_problems(question.get("options")):
                        add(message, block, questions_spec, i)

    if not counts.get("hero"):
        add(MSG_NO_HERO, where="Sidan")
    if not has_form and not can_call:
        add(MSG_NO_CONTACT, where="Sidan")
    return problems


def _claim_problems(text, facts, block, block_type, spec, index, sub, add):
    """Kvalitetsord och behörigheter i certifikat- och garantiblocken: bara
    det som står bland de bekräftade uppgifterna (facts.CLAIM_WORDS)."""
    for match in CLAIM_WORDS.finditer(text):
        if match.group(0).lower()[:5] not in facts.claims_text:
            add(MSG_CLAIM.format(word=_upper(match.group(0))), block, spec, index, sub)
            return
    # Också inne i ett sammansatt ord ("Ansvarsförsäkrade", "Våtrumsbehörig").
    for match in CERTIFICATE_WORDS.finditer(text):
        if match.group(0).lower() not in facts.claims_text:
            word = re.search(rf"[\w-]*{re.escape(match.group(0))}\w*", text, re.I).group(0)
            add(MSG_CLAIM.format(word=_upper(word)), block, spec, index, sub)
            return
    if block_type.key == "certificates" and sub is not None and sub.key == "name":
        if not _grounded(text, facts):
            add(MSG_CERTIFICATE, block, spec, index, sub)


def _upper(word):
    return word[:1].upper() + word[1:]


def _key(problem):
    return (problem.block, problem.part, problem.index, problem.message)


def published_problems(page, *, context=None, draft_problems=None, only_fixed=False):
    """Problemen på den publicerade versionen av sidan, märkta så att det
    syns var de sitter: where får "(på den publicerade sidan)" och texten
    säger vad som ska göras ("Utkastet är rättat. Publicera det" när samma
    problem inte finns i utkastet, annars "Rätta det i utkastet och
    publicera"). only_fixed: bara de som utkastet redan rättat (de andra
    syns redan bland utkastets problem). Tom lista för en sida som aldrig
    publicerats."""
    if not page.is_published:
        return []
    context = context or page_context(page)
    published = page_problems(page, context, blocks=page.published_blocks)
    if not published:
        return []
    draft = page_problems(page, context) if draft_problems is None else draft_problems
    in_draft = {_key(p) for p in draft}
    out = []
    for problem in published:
        fixed = _key(problem) not in in_draft
        if only_fixed and not fixed:
            continue
        where = (problem.where or "Sidan") + PUBLISHED_WHERE
        message = f"{problem.message} {PUBLISHED_FIXED if fixed else PUBLISHED_OPEN}"
        out.append(dataclasses.replace(problem, where=where, message=message))
    return out


def problem_text(problem):
    """Ett problem som en rad: "Toppen, rubrik: Siffran 24 finns inte ..."."""
    return f"{problem.where}: {problem.message}" if problem.where else problem.message
