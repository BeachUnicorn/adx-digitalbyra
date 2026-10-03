"""
Sidbyggaren för kundernas landningssidor (/lp/<slug>/): block med varianter
och versioner, designen Ren och kopplingen till kampanjerna.

Godkänd UX: adx-marketing/sidbyggaren-mockup.html (skärm 01-11). Beslut
2026-10-03 (Giovanni): en design ("Ren") tills vidare, block med varianter
i stället för fri redigering, versioner per block utan A/B-test, en sida per
kampanj eller en delad sida (kunden väljer), ingen låsning, ändringar på en
live-sida går live direkt när kontrollerna är gröna och byrån larmas.

Modulerna:

    registry.py   blocktyperna: namn, ikon, varianter, fält, varför, mallinnehåll,
                  BuildContext (tjänsten, orterna, sättet att sälja, priset)
    blocks.py     new_block, add_version, activate_version, validate_blocks,
                  form_spec, blocks_from_content, build_ctx, service_ctx,
                  sign_version och is_signed (serverns signatur på en version)
    problems.py   page_problems, page_context, published_problems (kontrollerna)
    render.py     render_block_html, render_page_html, palette_vars
    pages.py      ensure_own_page, use_shared_page, pages_for, campaigns_using,
                  create_page_for_campaign, refresh_from_proposal, save_draft,
                  publish_page, publish_for_campaign, new_page (högst
                  MAX_PAGES per konto), alert_live_change, live_campaigns
    facts.py      kontots bekräftade uppgifter sorterade för mallarna,
                  CLAIM_WORDS (kvalitetsord och behörigheter)
    principles.py principerna bakom blocken (för förklaringarna)
    ai.py         "Bygg sidan åt mig" och "Skriv om" (bara bekräftade uppgifter,
                  vakten, mallarna när AI inte går)
    koll.py       Konverteringskollen

Sidan (models.LandingPage)
==========================

    draft      {"blocks": [<block>, ...]}   det som redigeras
    published  {"blocks": [<block>, ...]}   det besökarna ser; tomt tills
                                            published_at är satt
    rev        heltal, ökar för varje sparat utkast (save_draft och
               publish_page nekar ett gammalt rev: StaleRevision)
    built_for  kampanjen vars förslag byggde sidan, och built_rev utkastets
               rev då: ett nytt förslag bygger bara om sidan för den
               kampanjen och så länge rev är built_rev

Ett block
=========

    {"id": "b_<12 tecken a-z, A-Z, 0-9>",
     "type": "hero",                       registry.TYPES
     "variant": "call",                    en av typens varianter
     "active": "v_<12 tecken>",            den version som visas
     "versions": [
        {"id": "v_<12 tecken>",
         "fields": {...},                  fälten enligt typens schema
         "source": "template" | "ai" | "customer" | "adx",
         "by": <användarens id> | null,
         "at": "<tid i ISO 8601>",
         "sig": "<32 hex>"}]}                serverns signatur (blocks.sign_version)

En variant är blockets form (med eller utan ringknapp, med eller utan bild),
en version är blockets text. Fälten hör till versionen och följer med när
varianten byts; fält som varianten inte visar sparas ändå. En version i
taget är aktiv. Högst blocks.MAX_VERSIONS versioner (add_version tar bort de
äldsta som inte är aktiva) och blocks.MAX_BLOCKS block per sida.

Fältens värden är vanlig text, aldrig HTML: text och textarea är strängar
(textarea med radbrytningar), lines är en lista med strängar, media är ett
MediaAsset-id (int) eller null, items är en lista med dict enligt
underfälten. validate_blocks tar bort HTML, normaliserar AI-typografi och
nekar okända typer, varianter och fält och för lång text i den aktiva
versionen (en äldre version som inte klarar schemat tas bort i stället).
Mallarna escapar allt. Källan ("source") avgör servern: en version som
servern inte känner igen behåller sin källa bara med en giltig signatur.

Formulärets frågor (formulärblockets fält "questions") har formen
{"key": "storlek", "label": "Ungefär hur stort?", "kind": "text"}, där kind
är text, textarea eller date. Fältet i formuläret heter q_<key>, och svaret
sparas som Lead.answers[label], som förut.

Typerna och varianterna (registry.py):

    hero            Toppen                call, form, image, text
    price           Pris                  from, examples, fixed      kräver ett bekräftat pris
    reviews_google  Omdömen från Google   cards, quote, line         kräver Google-profilen
    certificates    Certifikat            badges, icons              kräver en bekräftad uppgift
    guarantee       Garanti               short, terms               kräver en bekräftad garanti
    person          Personen bakom        image, noimage
    steps           Så går det till       three, four
    before_after    Före och efter        slider, pair
    area            Område                list, map (egen SVG, inga kartbilder)
    faq             Vanliga frågor        three, six
    form            Formulär              short, questions, booking
    callbar         Ringremsa             call, call_write (fast längst ner i mobilen)

Typnyckeln "hero" står kvar; namnet som visas är "Toppen". Toppen har en
överrubrik (kicker) med tjänsten och orten, samma ord som annonsen, och en
rubrik (title) som säger vad kunden får. Mallen och AI fyller överrubriken
ur tjänsten och orten (registry.hero_kicker, aldrig AI); Konverteringskollen
letar efter tjänsten och orten i överrubriken eller rubriken. Med bilder i
mediaarkivet blir Toppen för "ringer direkt" varianten med bild.

Toppen med formulär och ett formulärblock direkt efter står bredvid varandra
på en bred skärm (CSS :has och grid i flamingo-lp-ren.css); blocken är ändå
två syskon i HTML:en, och redigeraren ska låta dem vara det.
"""

from .blocks import (
    MAX_BLOCKS,
    MAX_VERSIONS,
    SOURCE_ADX,
    SOURCE_AI,
    SOURCE_CUSTOMER,
    SOURCE_TEMPLATE,
    SOURCES,
    BlockError,
    BlockUnavailable,
    FormSpec,
    activate_version,
    active_fields,
    active_version,
    add_version,
    block_rows,
    blocks_from_content,
    build_ctx,
    form_spec,
    is_signed,
    new_block,
    service_ctx,
    sign_version,
    validate_blocks,
)
from .pages import (
    MAX_PAGES,
    PageError,
    PageLimit,
    PageProblems,
    Published,
    StaleRevision,
    alert_live_change,
    campaigns_using,
    create_page_for_campaign,
    ensure_own_page,
    is_untouched,
    live_campaigns,
    new_page,
    pages_for,
    publish_for_campaign,
    publish_page,
    refresh_from_proposal,
    save_draft,
    template_blocks,
    use_shared_page,
)
from .problems import page_context, page_problems, problem_text, published_problems
from .registry import GROUPS, TYPES, BuildContext, available, get_type, schema
from .render import page_view_context, palette_vars, render_block_html, render_page_html

__all__ = [
    "GROUPS",
    "MAX_BLOCKS",
    "MAX_PAGES",
    "MAX_VERSIONS",
    "SOURCES",
    "SOURCE_ADX",
    "SOURCE_AI",
    "SOURCE_CUSTOMER",
    "SOURCE_TEMPLATE",
    "TYPES",
    "BlockError",
    "BlockUnavailable",
    "BuildContext",
    "FormSpec",
    "PageError",
    "PageLimit",
    "PageProblems",
    "Published",
    "StaleRevision",
    "activate_version",
    "active_fields",
    "active_version",
    "add_version",
    "alert_live_change",
    "available",
    "block_rows",
    "blocks_from_content",
    "build_ctx",
    "campaigns_using",
    "create_page_for_campaign",
    "ensure_own_page",
    "form_spec",
    "get_type",
    "is_signed",
    "is_untouched",
    "live_campaigns",
    "new_block",
    "new_page",
    "page_context",
    "page_problems",
    "page_view_context",
    "pages_for",
    "palette_vars",
    "problem_text",
    "publish_for_campaign",
    "publish_page",
    "published_problems",
    "refresh_from_proposal",
    "render_block_html",
    "render_page_html",
    "save_draft",
    "schema",
    "service_ctx",
    "sign_version",
    "template_blocks",
    "use_shared_page",
    "validate_blocks",
]
