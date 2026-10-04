"""
Konverteringskollen (skärm 06 i adx-marketing/sidbyggaren-mockup.html):
läser sidan och bockar av det som brukar skilja en sida som ger
förfrågningar från en som inte gör det. Varje punkt har en knapp som gör
jobbet.

    koll(page, account=None, *, which="draft", blocks=None) -> dict

    {"score": 7, "total": 9, "summary": "Två saker kan göra sidan bättre.",
     "items": [{"key", "ok", "title", "text", "principle_key",
                "principle_label", "action"}]}

Reglerna är fasta (inget AI) och tas bara med när de gäller sidan:

    rubrik          Överrubriken eller rubriken i Toppen innehåller tjänsten
                    och orten, för varje kampanj som visar sidan (samma
                    budskap som annonsen)
    forsta_blocket  Ringknapp i första blocket, eller Toppen med formulär och
                    formuläret direkt efter (blocks[1])
    pris            Från-pris tidigt, när ett pris är bekräftat
    omdomen         Omdömen eller betyg från Google på sidan, eller Recos
                    ruta (blocket Omdömen från Reco) med en profil som är
                    intygad som kundens (utan profil: länken till omdömena)
    formular        Formuläret har högst FORM_MAX_FIELDS fält, räknat som
                    besökaren ser det (FormSpec.fields: frågorna, namn och
                    telefon, meddelandet och e-posten)
    en_handling     En huvudhandling, inga handlingar som tävlar
    bilder          Riktiga bilder ur mediaarkivet (bara när sidan har ett
                    block med bild, eller kontot har bilder att visa)
    numret          Numret går att trycka på (alltid ok när sidan har ett)
    slutet          Sidan slutar med en handling: formuläret eller ringremsan
                    sist
    latt            Bilderna väger tillsammans högst LIGHT_MAX_BYTES

action är {"kind": "add_block" | "open_panel" | "select_block" | "link",
"label", och type, variant, after_id, panel, block_id eller url}, eller
None. Redigeraren (static/js/flamingo-pb-ai.js) kopplar den till
window.FlamingoPB.
"""

import logging

from .. import reco
from ..models import MediaAsset
from . import principles
from .ai import _price_for, default_service, has_place, has_service, safe_reverse
from .blocks import active_fields, form_spec
from .registry import MEDIA, TYPES

logger = logging.getLogger(__name__)

#: Formuläret: högst så många fält som besökaren ser (FormSpec.fields).
FORM_MAX_FIELDS = 5
#: Varianterna av Toppen som har ringknappen som huvudhandling. Med bild och
#: bara text leder i stället till formuläret när sidans formulär har frågor
#: (hero_main, samma regel som render.py).
CALL_VARIANTS = ("call", "image", "text")


def hero_main(hero, form):
    """Toppens huvudhandling: "call", "form" eller "" (render._prepare
    ritar knapparna efter samma regel)."""
    if hero is None:
        return ""
    variant = hero.get("variant")
    phone = bool((active_fields(hero).get("phone") or "").strip())
    if variant == "form":
        return "form"
    if variant in ("image", "text") and form is not None and form.get("variant") != "short":
        return "form"
    return "call" if variant in CALL_VARIANTS and phone else ""


#: Bilderna på sidan tillsammans (filerna i full storlek), i byte.
LIGHT_MAX_BYTES = 1_500_000

_WORDS = {1: "ett", 2: "två", 3: "tre", 4: "fyra", 5: "fem", 6: "sex", 7: "sju", 8: "åtta"}
_SAKER = {1: "En sak", 2: "Två saker", 3: "Tre saker", 4: "Fyra saker", 5: "Fem saker"}


def _number(n):
    return _WORDS.get(n, str(n))


def _mb(size):
    return f"{max(size, 100_000) / 1_000_000:.1f}".replace(".", ",")


def _item(key, ok, title, text, principle_key, action=None):
    return {
        "key": key,
        "ok": bool(ok),
        "title": title,
        "text": text,
        "principle_key": principle_key,
        "principle_label": principles.label(principle_key),
        "action": None if ok else action,
    }


def _select(block, label):
    return {"kind": "select_block", "block_id": block.get("id", ""), "label": label}


def _first(blocks, type_key):
    return next((b for b in blocks if b.get("type") == type_key), None)


def _media_ids(block):
    """Bildernas id i blocket som varianten visar."""
    block_type = TYPES.get(block.get("type"))
    if block_type is None:
        return []
    fields = active_fields(block)
    return [
        fields[spec.key]
        for spec in block_type.fields_for(block.get("variant"))
        if spec.kind == MEDIA and isinstance(fields.get(spec.key), int)
    ]


def _file_size(asset):
    try:
        return asset.file.size if asset.file else 0
    except (OSError, ValueError):
        return 0


def koll(page, account=None, *, which="draft", blocks=None):
    account = account or page.account
    blocks = page.blocks_for(which) if blocks is None else list(blocks)
    if not blocks:
        return {"score": 0, "total": 0, "summary": "Sidan har inga block än.", "items": []}
    items = []
    hero = _first(blocks, "hero")
    hero_fields = active_fields(hero) if hero else {}
    form = _first(blocks, "form")
    callbar = _first(blocks, "callbar")
    callbar_phone = (active_fields(callbar).get("phone") or "").strip() if callbar else ""
    hero_phone = (hero_fields.get("phone") or "").strip()
    campaigns = list(page.campaigns.select_related("service").order_by("name", "pk"))

    # Samma budskap som annonsen ------------------------------------------
    with_service = [c for c in campaigns if c.service_id]
    if hero is not None and with_service:
        from .. import generator

        # Överrubriken och rubriken tillsammans: tjänsten och orten står i
        # överrubriken, rubriken säger vad kunden får.
        title = " ".join(
            [str(hero_fields.get("kicker") or ""), str(hero_fields.get("title") or "")]
        )
        lacking = []
        for campaign in with_service:
            places = generator.places_of(campaign.area)
            if not has_service(title, campaign.service.name):
                lacking.append(f'"{campaign.service.name.lower()}"')
            if places and not has_place(title, places):
                lacking.append(f'"{places[0]}"')
        lacking = list(dict.fromkeys(lacking))
        first = with_service[0]
        first_place = (generator.places_of(first.area) or [""])[0]
        words = " och ".join(
            w
            for w in (f'"{first.service.name.lower()}"', f'"{first_place}"' if first_place else "")
            if w
        )
        if not lacking:
            items.append(
                _item(
                    "rubrik",
                    True,
                    "Samma ord som annonsen",
                    f"Överst på sidan står {words}.",
                    "samma_budskap",
                )
            )
        else:
            items.append(
                _item(
                    "rubrik",
                    False,
                    "Toppen saknar ord från annonsen",
                    f"Sökningen och annonsen säger {words}. Överrubriken och rubriken saknar "
                    f"{' och '.join(lacking)}. Samma ord visar direkt att sidan är rätt.",
                    "samma_budskap",
                    _select(hero, "Ändra överrubriken"),
                )
            )

    # Ringknapp eller formulär överst -------------------------------------
    first_block = blocks[0]
    first_fields = active_fields(first_block)
    is_hero = first_block.get("type") == "hero"
    first_main = hero_main(first_block, form) if is_hero else ""
    call_first = first_main == "call" and bool(first_fields.get("phone"))
    # Med bild eller bara text: knappen till formuläret står överst.
    button_first = first_main == "form" and first_block.get("variant") != "form"
    # Formuläret syns direkt bara när det står direkt efter Toppen (bredvid
    # på en bred skärm, under i mobilen).
    form_next = len(blocks) > 1 and blocks[1].get("type") == "form"
    form_first = is_hero and first_block.get("variant") == "form" and form_next
    if call_first or form_first or button_first:
        items.append(
            _item(
                "forsta_blocket",
                True,
                "Ringknappen syns direkt"
                if call_first
                else "Formuläret syns direkt"
                if form_first
                else "Knappen till formuläret syns direkt",
                "Det första besökaren ser är hur hen når er, också i mobilen.",
                "klarhet",
            )
        )
    else:
        if hero is None:
            action = {
                "kind": "add_block",
                "type": "hero",
                "variant": "call" if hero_phone or callbar_phone else "form",
                "label": "Lägg till Toppen",
            }
        else:
            action = _select(hero, "Visa Toppen")
        text = "Byt Toppen till Med ringknapp eller Med formulär, så syns handlingen direkt."
        if hero is not None and not is_hero:
            text = "Flytta Toppen överst, med ringknapp eller formulär."
        elif hero is not None and hero.get("variant") in CALL_VARIANTS and not hero_phone:
            text = "Ringknappen i Toppen behöver ett bekräftat telefonnummer."
        elif hero is not None and hero.get("variant") == "form" and form is None:
            text = "Toppen med formulär behöver ett formulärblock direkt efter."
        elif hero is not None and hero.get("variant") == "form":
            text = (
                "Formuläret står längre ner. Flytta det direkt efter Toppen, så står det "
                "bredvid rubriken på en bred skärm och direkt under i mobilen."
            )
            action = _select(form, "Visa formuläret")
        items.append(
            _item(
                "forsta_blocket",
                False,
                "Ingen ringknapp eller formulär överst",
                text,
                "klarhet",
                action,
            )
        )

    # Från-pris -------------------------------------------------------------
    service = default_service(page, account)
    rows = list(account.usable_fact_rows())
    price, _label, amount = _price_for(account, service, rows)
    if price:
        price_block = _first(blocks, "price")
        index = blocks.index(price_block) if price_block else None
        hero_text = " ".join(
            [str(hero_fields.get("title") or ""), str(hero_fields.get("lead") or "")]
            + [str(p) for p in hero_fields.get("points") or []]
        )
        in_hero = bool(amount) and amount in hero_text
        if (index is not None and index <= 2) or in_hero:
            where = "i Toppen" if in_hero else "högt upp på sidan"
            items.append(
                _item(
                    "pris", True, "Från-priset syns tidigt", f"{price} står {where}.", "pris_tidigt"
                )
            )
        elif price_block is not None:
            items.append(
                _item(
                    "pris",
                    False,
                    "Priset står långt ner",
                    "Flytta upp prisblocket, eller skriv priset i Toppen. Den som ser priset "
                    "tidigt och hör av sig vet redan vad det kostar.",
                    "pris_tidigt",
                    _select(price_block, "Visa prisblocket"),
                )
            )
        else:
            items.append(
                _item(
                    "pris",
                    False,
                    "Inget pris",
                    "Med priset tidigt hör de av sig som tycker att priset är rätt. Du har "
                    f"bekräftat {price}.",
                    "pris_tidigt",
                    {
                        "kind": "add_block",
                        "type": "price",
                        "variant": "from",
                        "after_id": hero.get("id") if hero else "",
                        "label": "Lägg till prisblock",
                    },
                )
            )

    # Socialt bevis -----------------------------------------------------------
    reviews = account.selected_google_reviews()
    rating = account.trusted_google_rating  # bara en profil som är intygad
    reviews_block = _first(blocks, "reviews_google")
    # Recos ruta, eller de valda omdömena från Reco (Utvalda), räknas när
    # profilen är intygad som kundens och blocket ritar något (reco.py).
    reco_block = _first(blocks, "reviews_reco")
    reco_trusted = account.reco_trusted
    reco_shows = reco_block is not None and reco.block_shows(account, reco_block.get("variant"))
    if rating is not None:
        rating_text = f"{rating:.1f}".replace(".", ",")
        count = f", {account.google_review_count} omdömen" if account.google_review_count else ""
        have = f"{rating_text} på Google{count}"
    elif reviews:
        have = f"{len(reviews)} valda omdömen från Google"
    else:
        have = ""
    reviews_url = safe_reverse("flamingo:app_reviews")
    reviews_link = (
        {"kind": "link", "url": reviews_url, "label": "Koppla Google-profilen"}
        if reviews_url
        else None
    )
    google_visible = reviews_block is not None and (
        bool(reviews) or (reviews_block.get("variant") == "line" and rating is not None)
    )
    if google_visible:
        items.append(_item("omdomen", True, "Omdömen från Google", f"{have}.", "socialt_bevis"))
    elif reco_shows:
        items.append(
            _item(
                "omdomen",
                True,
                "Omdömen från Reco",
                "Blocket visar betyget och omdömena från er profil på Reco.",
                "socialt_bevis",
            )
        )
    elif reco_block is not None and reco_trusted:
        # Utvalda utan valda omdömen (och utan betyg i en rad).
        items.append(
            _item(
                "omdomen",
                False,
                "Omdömena från Reco syns inte",
                "Välj omdömen från er profil på Reco, eller byt blocket till Recos egen ruta.",
                "socialt_bevis",
                {"kind": "link", "url": reviews_url + "#reco", "label": "Välj omdömen"}
                if reviews_url
                else _select(reco_block, "Visa blocket"),
            )
        )
    elif reco_block is not None:
        items.append(
            _item(
                "omdomen",
                False,
                "Recos ruta syns inte",
                "Er profil på Reco är inte kopplad eller inte intygad som er, så blocket "
                "Omdömen från Reco syns inte på sidan.",
                "socialt_bevis",
                {"kind": "link", "url": reviews_url + "#reco", "label": "Öppna Omdömen"}
                if reviews_url
                else _select(reco_block, "Visa blocket"),
            )
        )
    elif not have and reco_trusted:
        after = _first(blocks, "price") or hero
        items.append(
            _item(
                "omdomen",
                False,
                "Inga omdömen på sidan",
                "Er profil på Reco är kopplad. Recos ruta nära knappen ger förtroende.",
                "socialt_bevis",
                {
                    "kind": "add_block",
                    "type": "reviews_reco",
                    "variant": "stor",
                    "after_id": after.get("id") if after else "",
                    "label": "Lägg till Recos ruta",
                },
            )
        )
    elif not have:
        items.append(
            _item(
                "omdomen",
                False,
                "Inga omdömen från Google",
                "Koppla din Google-profil och välj omdömen. Andras erfarenheter väger tyngre "
                "än det ni säger själva.",
                "socialt_bevis",
                reviews_link,
            )
        )
    elif reviews_block is None:
        after = _first(blocks, "price") or hero
        items.append(
            _item(
                "omdomen",
                False,
                "Inga omdömen på sidan",
                f"Du har {have}. Omdömen nära knappen ger förtroende.",
                "socialt_bevis",
                {
                    "kind": "add_block",
                    "type": "reviews_google",
                    "variant": "cards" if len(reviews) >= 3 else ("line" if rating else "quote"),
                    "after_id": after.get("id") if after else "",
                    "label": "Lägg till omdömen",
                },
            )
        )
    else:
        items.append(
            _item(
                "omdomen",
                False,
                "Omdömena syns inte",
                "Välj omdömen i din Google-profil, eller byt blocket till Betyg i en rad.",
                "socialt_bevis",
                reviews_link or _select(reviews_block, "Visa omdömesblocket"),
            )
        )

    # Formulärets fält -----------------------------------------------------
    if form is not None:
        # Som besökaren ser formuläret: frågorna, namn och telefon, och
        # meddelandet och e-posten när varianten visar dem.
        count = (form_spec(blocks) or form_spec([form])).fields
        ok = count <= FORM_MAX_FIELDS
        items.append(
            _item(
                "formular",
                ok,
                f"Formuläret har {_number(count)} fält",
                "Namn, telefon och bara det som behövs."
                if ok
                else "Varje extra fält tappar några. Ta bort frågor som inte behövs för att "
                "höra av sig.",
                "farre_falt",
                _select(form, "Visa formuläret"),
            )
        )

    # En huvudhandling ------------------------------------------------------
    has_call = bool(hero_phone or callbar_phone)
    if hero is not None and (has_call or form is not None):
        main = hero_main(hero, form)
        competing, text = None, ""
        if main == "call" and form is not None and form.get("variant") != "short":
            competing = form
            text = (
                "Ringknappen och ett långt formulär tävlar. Byt formuläret till Kort, så "
                "leder allt till samtalet."
            )
        elif main == "form" and callbar is not None and callbar.get("variant") == "call":
            competing = callbar
            text = (
                "Ringremsan tävlar med formuläret. Byt den till Ring och skriv, eller ta bort den."
            )
        elif not main and has_call and form is not None:
            competing = hero
            text = "Välj en huvudhandling i Toppen: ringknapp eller formulär."
        if competing is None and main:
            items.append(
                _item(
                    "en_handling",
                    True,
                    "En huvudhandling",
                    'Allt leder till "Ring".' if main == "call" else "Allt leder till formuläret.",
                    "en_handling",
                )
            )
        elif competing is not None:
            items.append(
                _item(
                    "en_handling",
                    False,
                    "Handlingar som tävlar",
                    text,
                    "en_handling",
                    _select(competing, "Visa blocket"),
                )
            )

    # Riktiga bilder -------------------------------------------------------
    used = {}
    for block in blocks:
        for media_id in _media_ids(block):
            used.setdefault(media_id, block)
    assets = {a.pk: a for a in MediaAsset.objects.filter(account=account, pk__in=list(used))}
    shown = {pk: block for pk, block in used.items() if pk in assets}
    empty_media = None
    for block in blocks:
        wants_image = block.get("type") == "before_after" or (
            block.get("type") == "hero" and block.get("variant") == "image"
        )
        if wants_image and not any(pk in assets for pk in _media_ids(block)):
            empty_media = block
            break
    if shown and empty_media is None:
        n = len(shown)
        items.append(
            _item(
                "bilder",
                True,
                "Riktiga bilder",
                f"{_number(n).capitalize()} {'bild' if n == 1 else 'bilder'} ur ditt mediaarkiv.",
                "riktiga_bilder",
            )
        )
    else:
        title = "Inga bilder på sidan"
        if empty_media is not None:
            action = _select(empty_media, "Välj bilden")
            text = "Blocket väntar på en bild ur mediaarkivet."
            title = "En bild saknas"
        elif MediaAsset.objects.filter(account=account, is_logo=False).exists():
            action = {"kind": "open_panel", "panel": "media", "label": "Öppna mediaarkivet"}
            text = (
                "Du har bilder i mediaarkivet. En bild från ett eget jobb i Toppen visar "
                "direkt vad ni gör."
            )
        else:
            action = {"kind": "open_panel", "panel": "media", "label": "Öppna mediaarkivet"}
            text = "Bilder från egna jobb visar vad ni gör. Ladda upp några i mediaarkivet."
        items.append(_item("bilder", False, title, text, "riktiga_bilder", action))

    # Numret går att trycka på ----------------------------------------------
    if has_call:
        items.append(
            _item(
                "numret",
                True,
                "Numret går att trycka på",
                "Klicket räknas som förfrågan.",
                "en_handling",
            )
        )

    # Sidan slutar med en handling ------------------------------------------
    last = blocks[-1]
    if last.get("type") in ("form", "callbar"):
        items.append(
            _item(
                "slutet",
                True,
                "Sidan slutar med en handling",
                "Den som läst hela sidan har formuläret eller numret framför sig."
                if last.get("type") == "form"
                else "Den som läst hela sidan har numret framför sig.",
                "en_handling",
            )
        )
    else:
        end_action = None
        if callbar is None and (hero_phone or callbar_phone):
            end_action = {
                "kind": "add_block",
                "type": "callbar",
                "variant": "call_write" if form is not None else "call",
                "after_id": last.get("id", ""),
                "label": "Lägg till ringremsan sist",
            }
        elif form is None:
            end_action = {
                "kind": "add_block",
                "type": "form",
                "variant": "short" if hero_phone else "questions",
                "after_id": last.get("id", ""),
                "label": "Lägg till formuläret sist",
            }
        else:
            end_action = _select(callbar or form, "Visa blocket")
        items.append(
            _item(
                "slutet",
                False,
                "Sidan slutar utan en handling",
                "Den som läst hela sidan ska ha formuläret eller numret framför sig. Lägg "
                "ringremsan eller formuläret sist.",
                "en_handling",
                end_action,
            )
        )

    # Sidan är lätt ---------------------------------------------------------
    if shown:
        sizes = {pk: _file_size(assets[pk]) for pk in shown}
        logo = MediaAsset.objects.filter(account=account, is_logo=True).order_by("-pk").first()
        total = sum(sizes.values()) + (_file_size(logo) if logo is not None else 0)
        if total <= LIGHT_MAX_BYTES:
            items.append(
                _item(
                    "latt",
                    True,
                    "Sidan är lätt",
                    f"Bilderna väger {_mb(total)} MB tillsammans.",
                    "snabb_sida",
                )
            )
        else:
            heaviest = max(sizes, key=sizes.get)
            items.append(
                _item(
                    "latt",
                    False,
                    "Tunga bilder",
                    f"Bilderna väger {_mb(total)} MB tillsammans. Byt den tyngsta mot en "
                    "mindre bild, så laddar sidan snabbare i mobilen.",
                    "snabb_sida",
                    _select(shown[heaviest], "Visa bilden"),
                )
            )

    items.sort(key=lambda item: item["ok"])
    score = sum(1 for item in items if item["ok"])
    left = len(items) - score
    if left == 0:
        summary = "Allt i kollen ser bra ut."
    else:
        summary = f"{_SAKER.get(left, f'{left} saker')} kan göra sidan bättre."
    return {"score": score, "total": len(items), "summary": summary, "items": items}
