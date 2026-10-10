"""
E-postredigeraren för Brev (README F, I.1, I.2, I.4, H.1, D9).

    brev_editor        utskick/<pk>/brev/                  app_brev               GET sidan
    brev_save          utskick/<pk>/brev/spara/            app_brev_save          POST JSON
    brev_render_block  utskick/<pk>/brev/rita/             app_brev_render_block  POST JSON
    brev_image         utskick/<pk>/brev/bild/             app_brev_image         POST JSON
    brev_checks        utskick/<pk>/brev/kontroller/       app_brev_checks        GET JSON
    brev_ai            utskick/<pk>/brev/ai/               app_brev_ai            POST JSON
    brev_preview       utskick/<pk>/brev/forhandsvisning/  app_brev_preview       GET HTML, JSON
    send_test_email(request, account, utskick) -> (ok, text)
                       testmejlet (F.8), anropas av app_views.utskick.utskick_test

Sidan monterar sidbyggarens skript static/js/flamingo-pb.js med profilen
"brev" (F.6, BREV_PROFILE nedan): blocken dras, läggs till, flyttas, tas
bort och skrivs i direkt i mejlet, som på sidorna. Det som bara mejlet har
(ämnesraden, förhandstexten, avsändaren, loggans plats, accentfärgen,
villkoren i erbjudandet, fälten som skrivs i en panel, kontrollerna,
förhandsvisningen, testmejlet och AI) sköts av static/js/flamingo-app-brev.js
ovanpå skriptets publika gränssnitt. Mejlet ritas av email.render (Brev,
inline-stilar och tabeller) i läget "editor".

- brev_save: {"rev", "blocks", "subject", "preheader", "accent",
  "logo_position", "from_name", "sender_domain", "merge_fallbacks",
  "terms_ok", "terms"}. Allt sparas med raden låst och läget prövat igen
  under låset (som app_views.utskick._editing): ett schemalagt utskick som
  ändras blir ett utkast (state.unconfirm, B.2). 409 när någon annan sparat
  emellan (email_rev, StaleRevision) eller när utskicket inte längre går att
  ändra; 400 för ett främmande id (bild eller domän, access.owned_ids) och
  för block som inte klarar schemat. Servern bestämmer vem som skrev varje
  version (_stamp, som pages.stamp_authorship): byrån i kundvyn sparas som
  "adx" med sitt id. "Uppgifterna stämmer" (terms_ok) gäller bara de villkor
  kunden såg (terms), aldrig villkor som ändrats under tiden.
- brev_render_block: hela mejlet i läget "editor" ({"blocks", "accent",
  "logo_position"}), eller blocken med ids, ritade som de är (en tom rad som
  skrivs syns) men prövade mot schemat. Med {"type"} i stället för blocks:
  ett nytt block ur mallen (email.blocks.new_block), eftersom det inte finns
  någon egen adress för det (S3-avvikelse).
- brev_image: {"asset", "purpose"} -> email.images.rendition: bildens
  absoluta adress, mått och alt-text.
- brev_checks: email.checks.email_checks som [{"level", "text", "key"}].
- brev_ai: {"type", "block_id"?, "fields"?, "brief"} -> ai.write_block, eller
  {"sms": true, "brief"} -> ai.write_sms. Inget sparas; demokontot, AI av och
  en tom kvot ger mallens förslag (ai.py).
- brev_preview: hela mejlet i läget "preview" för en kontakt (?kontakt=,
  access.owned_ids) och ett läge (?lage=mobil|dator|morkt). Med Accept JSON
  också ämnesraden, förhandstexten, avsändaren och nästa kontakt ("byt
  kontakt"). Sidan förbjuder skript (CSP) och visas i en ram med sandbox.

Varje vy: @utskick_view (404 när utskick är av, också för byrån i kundvyn),
utskicket via access.owned (404 för ett annat kontos), POST under
@require_POST. Inget skickas härifrån utom testmejlet, och det bara till
den inloggades egen adress eller, för byrån i kundvyn, till kunden med
kryssrutan "Jag skickar det här som ADX åt ..." (F.8, I.4). Kunden mejlas
aldrig av något annat här.
"""

import copy
import json
import logging

from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.templatetags.static import static
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.views.decorators.http import require_POST, require_safe

from apps.common.security import sanitize_plain_text
from apps.flamingo import pagebuilder
from apps.flamingo.models import MediaAsset
from apps.flamingo.pagebuilder.render import EDITING_CSP

from .. import ai, audience, composer, keys, normalize
from .. import suppression as suppressions
from ..access import FOREIGN_IDS_TEXT, ForeignIds, actor_for, owned, owned_ids, utskick_view
from ..email import blocks as email_blocks
from ..email import checks as email_checks
from ..email import images, registry, render, style
from ..models import CHANNEL_EMAIL, INFORMATION, Contact, EmailImage, SenderDomain, Utskick
from ..sending import state
from . import render_utskick

logger = logging.getLogger(__name__)

#: Största JSON-kropp redigeraren skickar (hela mejlet med versioner).
MAX_BODY = 1_000_000
SUBJECT_MAX = 150
PREHEADER_MAX = 150
FROM_NAME_MAX = 80
#: Kontakterna förhandsvisningen kan bläddra mellan ("byt kontakt").
PREVIEW_CONTACTS = 50

NOT_EDITABLE = "Utskicket går inte att ändra nu."
NOT_EMAIL = (
    "Utskicket går bara med sms. Välj en kanal med e-post under Kanal för att skriva ett mejl."
)
UNCONFIRMED = "Utskicket är ett utkast igen. Granska och bekräfta det på nytt."
UNCONFIRM_NOTE = (
    "Utskicket är schemalagt. Ändrar du mejlet blir det ett utkast igen och behöver "
    "bekräftas på nytt i Granska."
)
STALE_TEXT = (
    "Mejlet har sparats från ett annat ställe medan du skrev. Inget har skrivits över. "
    "Ladda om sidan."
)
NOT_VERIFIED = "Domänen är inte verifierad. Verifiera den under Inställningar, Avsändare och svar."
BAD_ACCENT = "Välj en färg som skrivs med # och sex tecken, till exempel #1A57D6."
STAFF_TEXT = "Jag skickar det här som ADX åt {name}."
STAFF_MISSING = "Kryssa i att du skickar som ADX åt {name}."
KEY_TEXT = "Det gick inte att skicka just nu. ADX har fått ett larm."

#: Fältsorterna som skrivs i fältpanelen och aldrig direkt i mejlet (F.6):
#: länkar, datum, tider, e-post, telefon, koder, text med formatering och val.
PANEL_KINDS = (*registry.EMAIL_FIELD_KINDS, "choice")

#: Redigerarens profil för mejlen (static/js/flamingo-pb.js, F.6). Sidornas
#: profil står i apps/flamingo/app_views/pages.PAGE_PROFILE.
BREV_PROFILE = {
    "profile": "brev",
    # Blocken är rader (<tr>) i mejlets tbody (data-brev-canvas), mellan
    # sidhuvudet och sidfoten (data-brev-element, email.render i läget
    # editor). Ett block som läggs sist hamnar före sidfoten.
    "canvasRoot": "[data-brev-canvas]",
    "chromeSelectors": {
        "header": '[data-brev-element="header"]',
        "footer": '[data-brev-element="footer"]',
    },
    "canvasEnd": '[data-brev-element="footer"]',
    # Mejlets färger står inline i varje element; ingen <style> byts.
    "paletteMarker": "--br-accent",
    # Dator: mejlet i 680 px (560 och marginalen; under 620 px gäller
    # mejlets egna mobilregler), Mobil: 375 px.
    "devices": {"desktop": 680, "phone": 375, "fit": "fixed"},
    "placement": {"pairs": [], "endGroup": ""},
    "panelKinds": list(PANEL_KINDS),
    "addWords": {
        "columns.items": "kolumn",
        "prices.items": "tjänst",
        "steps.items": "steg",
        "gallery.items": "bild",
        "faq.items": "fråga",
        "social.items": "länk",
    },
    "wireframes": {},
    "texts": {
        "maxBlocks": "Högst {n} block i ett mejl.",
        "single": "Finns redan i mejlet.",
        "singleCopy": "Mejlet kan bara ha ett block av sorten {name}.",
        "renderFailed": "Mejlet gick inte att rita om. ",
        "first": "Lägg till block först i mejlet",
        "hidden": "Syns inte i mejlet än",
        "stale": "Mejlet har ändrats på ett annat ställe.",
        "versionMade": "En ny version är skapad och vald. Skriv ditt alternativ direkt i mejlet.",
        "versionsNote": "En version är blockets text. Den aktiva syns i mejlet.",
        "readOnlyHint": "Mejlet går inte att ändra här.",
        "pickFirst": "Välj ett block först: tryck på det i mejlet.",
    },
}

#: Testmejlets utfall (transport.Sent.error) i klartext.
TEST_ERRORS = {
    "email_off": state.EMAIL_OFF_TEXT,
    "test_limit": "Du har skickat 10 test i dag. Försök igen i morgon.",
    "adx_cap": (
        "Månadens mejl från ADX-domänen är slut. Verifiera din egen domän under Inställningar."
    ),
    "suppressed": "Adressen har avregistrerat sig från dina mejl.",
    "demo": "Demokontot skickar aldrig.",
}


class _BadRequest(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra

    def response(self):
        return JsonResponse({"ok": False, "error": self.message, **self.extra}, status=self.status)


# ---------------------------------------------------------------------------
# Hjälpare
# ---------------------------------------------------------------------------


def _json_body(request):
    """Kroppen som ett dict, eller _BadRequest (för stor, inte JSON, eller
    så djupt nästlad att den inte går att läsa)."""
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    body = request.body
    if length > MAX_BODY or len(body) > MAX_BODY:
        raise _BadRequest("Ändringen är för stor för att sparas på en gång.", status=413)
    try:
        data = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _BadRequest("Förfrågan gick inte att läsa.") from None
    if not isinstance(data, dict):
        raise _BadRequest("Förfrågan gick inte att läsa.")
    return data


def _foreign():
    return JsonResponse({"ok": False, "error": FOREIGN_IDS_TEXT}, status=400)


def _source_for(request):
    """Källan för en version som den inloggade skriver: byrån eller kunden."""
    return pagebuilder.SOURCE_ADX if request.flamingo.is_agency else pagebuilder.SOURCE_CUSTOMER


def _user(request):
    user = getattr(request, "user", None)
    return user if getattr(user, "is_authenticated", False) else None


def _display_name(request):
    row = getattr(request, "utskick_settings", None)
    return (row.display_name if row else "") or "kunden"


def _doc_blocks(utskick):
    doc = utskick.email_doc if isinstance(utskick.email_doc, dict) else {}
    blocks = doc.get("blocks")
    return blocks if isinstance(blocks, list) else []


def _unsigned(blocks):
    """Blocken utan versionernas signatur, för att se om något ändrats."""
    out = []
    for block in blocks or []:
        if not isinstance(block, dict):
            out.append(block)
            continue
        block = dict(block)
        if isinstance(block.get("versions"), list):
            block["versions"] = [
                {k: v for k, v in version.items() if k != "sig"}
                if isinstance(version, dict)
                else version
                for version in block["versions"]
            ]
        out.append(block)
    return out


def _stamp(utskick, blocks, user, source, now=None):
    """Vem som skrev varje version, avgjort av servern (som
    pages.stamp_authorship):

    - en version som finns sparad med samma fält: det som sparades (källa,
      by, at), så att ingen kan skriva en annans namn på en version;
    - samma id med andra fält: den inloggade med källan source (kunden
      eller byrån);
    - en ny version med serverns signatur (mallen i new_block): som den är;
    - allt annat, också en ny version som påstår att den kommer från mallen
      eller AI: den inloggade med källan source.

    Signaturen tas bort (email.blocks.save signerar det som sparas)."""
    stored = {}
    for block in _doc_blocks(utskick):
        for version in block.get("versions") or [] if isinstance(block, dict) else []:
            if isinstance(version, dict) and isinstance(version.get("id"), str):
                stored[version["id"]] = version
    by = user.pk if isinstance(getattr(user, "pk", None), int) else None
    at = (now or timezone.now()).isoformat()
    out = []
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("versions"), list):
            out.append(block)
            continue
        block = dict(block)
        versions = []
        for version in block["versions"]:
            if not isinstance(version, dict):
                versions.append(version)
                continue
            version = dict(version)
            version_id = version.get("id")
            before = stored.get(version_id) if isinstance(version_id, str) else None
            if before is not None and before.get("fields") == version.get("fields"):
                for key in ("source", "by", "at"):
                    version[key] = before.get(key)
            elif before is None and email_blocks.is_signed(version):
                pass
            else:
                version["by"], version["at"], version["source"] = by, at, source
            version.pop("sig", None)
            versions.append(version)
        block["versions"] = versions
        out.append(block)
    return out


def _line(value, limit):
    """En rad text från redigeraren: utan HTML, typografin normaliserad,
    blanksteg hopslagna."""
    text = sanitize_plain_text(str(value or ""), max_length=limit * 2)
    return " ".join(text.split())[:limit]


def _line_errors(account, text):
    """Fel i ämnesraden eller förhandstexten: okända platshållare, länkar,
    fält som inte finns (email.blocks.merge_problems). Sparas ändå:
    kontrollerna stoppar utskicket."""
    if not text:
        return []
    return list(email_blocks.merge_problems(account, text))


def _fallbacks(account, raw, before):
    """Reservtexterna ({"förnamn": "du"}) ihopslagna med de sparade (sms:et
    delar fältet): en tom text tar bort sin rad. Bara riktiga platshållare."""
    if not isinstance(raw, dict):
        raise _BadRequest("Reservtexterna ska vara ett dict.")
    out = dict(before or {})
    for tag, value in list(raw.items())[:40]:
        if not isinstance(tag, str) or not isinstance(value, (str, type(None))):
            raise _BadRequest("Reservtexterna gick inte att läsa.")
        found = composer.placeholders("{" + tag + "}")
        if found.unknown or not found.tags or found.tags[0] != tag:
            continue
        text = _line(value, composer.VALUE_MAX)
        if text:
            out[tag] = text
        else:
            out.pop(tag, None)
    return out


def _verified_domains(account):
    return SenderDomain.objects.filter(account=account, status=SenderDomain.Status.VERIFIED)


def _doc_fields(account, data):
    """Mejlets fält utanför blocken ur JSON-kroppen: {modellfält: värde}
    för de nycklar som finns. _BadRequest för ett ogiltigt värde,
    ForeignIds för en domän som inte är kontots."""
    fields = {}
    if "subject" in data:
        fields["subject"] = _line(data["subject"], SUBJECT_MAX)
    if "preheader" in data:
        fields["preheader"] = _line(data["preheader"], PREHEADER_MAX)
    if "accent" in data:
        raw = data["accent"]
        if raw in ("", None):
            fields["accent"] = ""
        else:
            accent = style.valid_accent(raw)
            if not accent:
                raise _BadRequest(BAD_ACCENT)
            fields["accent"] = accent
    if "logo_position" in data:
        if data["logo_position"] not in Utskick.LogoPosition.values:
            raise _BadRequest("Välj var loggan ska stå.")
        fields["logo_position"] = data["logo_position"]
    if "sender_domain" in data:
        raw = data["sender_domain"]
        if raw in ("", None):
            fields["sender_domain_id"] = None
        else:
            pk = owned_ids(SenderDomain, account, [raw])[0]
            if not _verified_domains(account).filter(pk=pk).exists():
                raise _BadRequest(NOT_VERIFIED)
            fields["sender_domain_id"] = pk
    if "from_name" in data:
        fields["from_name"] = _line(data["from_name"], FROM_NAME_MAX)
    if fields.get("sender_domain_id", "x") is None:
        # Avsändarnamnet går bara att ändra med en egen domän (B.2).
        fields["from_name"] = ""
    return fields


def _lock(utskick):
    return (
        Utskick.objects.select_for_update(of=("self",))
        .select_related("account__customer")
        .get(pk=utskick.pk)
    )


def _saved_text(moment):
    return f"{timezone.localtime(moment):%H:%M}"


def _step_url(utskick, step):
    return reverse("flamingo:app_utskick_step", args=[utskick.pk, step])


def _optional_url(name, *args):
    try:
        return reverse(name, args=list(args))
    except NoReverseMatch:
        return None


def _static_version(request):
    from apps.manage.context_processors import static_version

    return static_version(request).get("static_version", "1")


def _with_editing_head(html, request):
    """Dokumentet i ramen: inga skript (EDITING_CSP) och redigerarens
    stilmall för duken (flamingo-app-brev.css, del 2). Mallarna escapar
    varje fält; det här är det enda som läggs till."""
    if EDITING_CSP not in html:
        html = html.replace("<head>", f"<head>\n{EDITING_CSP}", 1)
    link = format_html(
        '<link rel="stylesheet" href="{}?v={}" data-br-canvas>',
        static("css/flamingo-app-brev.css"),
        _static_version(request),
    )
    return html.replace("</head>", f"{link}\n</head>", 1)


def _canvas(request, utskick):
    ctx = render.context_for(utskick, mode=render.EDITOR)
    return _with_editing_head(str(render.render_html(utskick, ctx)), request)


def _preview_copy(utskick, blocks=None, data=None):
    """Utskicket med osparade block, färg och loggans plats, för ritningen.
    Raden ändras inte."""
    preview = copy.copy(utskick)
    if blocks is not None:
        preview.email_doc = {"blocks": blocks}
    data = data or {}
    if data.get("accent") not in (None, ""):
        accent = style.valid_accent(data.get("accent"))
        if not accent:
            raise _BadRequest(BAD_ACCENT)
        preview.accent = accent
    elif "accent" in data:
        preview.accent = ""
    if "logo_position" in data:
        if data["logo_position"] not in Utskick.LogoPosition.values:
            raise _BadRequest("Välj var loggan ska stå.")
        preview.logo_position = data["logo_position"]
    return preview


def _validated(account, utskick, blocks):
    """Blocken prövade mot Brevs schema (typer, fält, längder, låsningar och
    bilder på kontot). BlockError blir 400 med felen; ForeignIds fångas av
    vyn (400)."""
    try:
        return email_blocks.validate(account, utskick, blocks)
    except email_blocks.BlockError as exc:
        raise _block_error(exc) from None


def _blocks_from(data, utskick):
    blocks = data.get("blocks")
    if blocks is None:
        return _doc_blocks(utskick)
    if not isinstance(blocks, list):
        raise _BadRequest("Blocken ska vara en lista.")
    if len(blocks) > registry.MAX_BLOCKS:
        raise _BadRequest(f"Högst {registry.MAX_BLOCKS} block i ett mejl.")
    return blocks


def _terms_json(utskick):
    return {
        "terms": utskick.confirmed_terms if isinstance(utskick.confirmed_terms, list) else [],
        "terms_confirmed": bool(utskick.terms_confirmed_at),
    }


# ---------------------------------------------------------------------------
# Biblioteket, färgerna och avsändaren
# ---------------------------------------------------------------------------


def _library(account, utskick):
    """Brevs block för redigeraren (email.registry.library): schemat med
    redigerarens ikoner (#pb-i-brev-<typ>), läget per block och grupperna
    för biblioteket, i bibliotekets ordning."""
    entries = []
    available = {}
    groups = []
    by_group = {}
    for entry in registry.library(account, utskick):
        entry = dict(entry)
        key = entry.get("key", "")
        entry["icon"] = f"brev-{key}"
        ok = bool(entry.pop("ok", True))
        reason = entry.pop("why_not", "") or ""
        group_name = entry.pop("group_name", "") or ""
        entries.append(entry)
        available[key] = {"ok": ok, "reason": reason, "link": ""}
        group = entry.get("group") or "text"
        if group not in by_group:
            by_group[group] = {"key": group, "name": group_name, "items": []}
            groups.append(by_group[group])
        by_group[group]["items"].append({"type": entry, "ok": ok, "reason": reason})
    return entries, available, groups


def _picker(account, utskick):
    """Färgväljaren (F.2, email.style.picker): upp till tre färger ur
    logotypen, sedan mockupens fem, sedan Egen färg."""
    data = style.picker(account)
    swatches = [{**s, "logo": True} for s in data.get("logo") or []]
    swatches += [{**s, "logo": False} for s in data.get("swatches") or []]
    current = utskick.accent or ""
    shown = current or data.get("default") or style.DEFAULT_ACCENT
    return {
        "swatches": swatches,
        "default": data.get("default") or style.DEFAULT_ACCENT,
        "current": current,
        "shown": shown,
        "light": bool(style.is_light(shown)),
        "light_text": data.get("light_text") or style.LIGHT_TEXT,
    }


def _has_logo(account):
    return bool(registry.logo_state(account)[0])


def _logo_shown(account, utskick, has_logo=None):
    """Logotypens läge som mejlet ritas med: utan logotyp alltid "none"
    (F.1), så att valet som syns intryckt är det som ritas."""
    if has_logo is None:
        has_logo = _has_logo(account)
    return utskick.logo_position if has_logo else Utskick.LogoPosition.NONE


def _sender(account, utskick):
    """Avsändaren som mottagaren ser den och valen (ADX-domänen eller en
    verifierad egen domän)."""
    from ..sending import email as sending_email

    domains = list(_verified_domains(account).order_by("domain"))
    try:
        name, address = sending_email.from_for(account, utskick)
    except Exception:  # noqa: BLE001 - avsändaren visas, men stoppar aldrig redigeraren
        logger.exception("Utskick %s: avsändaren gick inte att visa", utskick.pk)
        name, address = "", ""
    return {
        "text": f"{name} <{address}>" if name and address else address,
        "domain_id": utskick.sender_domain_id,
        "own": bool(utskick.sender_domain_id),
        "from_name": utskick.from_name,
        "domains": [
            {"id": d.pk, "address": d.from_address, "from_name": d.from_name} for d in domains
        ],
    }


def _insert_tags(account):
    tags = [
        ("{förnamn}", "Förnamn"),
        ("{efternamn}", "Efternamn"),
        ("{namn}", "Namn"),
        ("{företag}", "Företag"),
    ]
    for definition in composer.field_defs(account).values():
        tags.append(("{" + composer.FIELD_PREFIX + definition.key + "}", definition.label))
    return [{"token": token, "label": label} for token, label in tags]


def _test_targets(request, account):
    """Vart ett testmejl kan gå (F.8): kundens egen adress; byrån i
    kundvyn sin egen adress, och kundens adresser bakom en egen knapp med
    kryssrutan (I.4)."""
    actor = actor_for(request)
    user = _user(request)
    own = (getattr(user, "email", "") or "").strip()
    customers = []
    if actor.staff and account.customer_id:
        customers = sorted(
            {
                u.email.strip()
                for u in account.customer.users.all()
                if (u.email or "").strip() and not u.is_staff
            }
        )
    return {"own": own, "staff": actor.staff, "customers": customers, "demo": account.is_demo}


def _br_config(request, account, utskick, sender, picker):
    pk = utskick.pk
    return {
        "utskick": pk,
        "urls": {
            "save": reverse("flamingo:app_brev_save", args=[pk]),
            "checks": reverse("flamingo:app_brev_checks", args=[pk]),
            "ai": reverse("flamingo:app_brev_ai", args=[pk]),
            "image": reverse("flamingo:app_brev_image", args=[pk]),
            "preview": reverse("flamingo:app_brev_preview", args=[pk]),
            "test": reverse("flamingo:app_utskick_test", args=[pk]),
            "back": _step_url(utskick, "innehall"),
            "review": _step_url(utskick, "granska"),
        },
        "subject": utskick.subject,
        "preheader": utskick.preheader,
        "accent": utskick.accent,
        "defaultAccent": picker["default"],
        # Det sparade valet (ritas utan logotyp som "none"; knapparna visar
        # logo_shown), så att en logotyp som laddas upp senare syns direkt.
        "logoPosition": utskick.logo_position,
        "hasLogo": _has_logo(account),
        "sender": sender,
        "fallbacks": utskick.merge_fallbacks if isinstance(utskick.merge_fallbacks, dict) else {},
        "tags": _insert_tags(account),
        **_terms_json(utskick),
        "information": utskick.purpose == INFORMATION,
        "lightText": style.LIGHT_TEXT,
        "staff": actor_for(request).staff,
        "staffText": STAFF_TEXT.format(name=_display_name(request)),
        "demo": account.is_demo,
        "test": _test_targets(request, account),
    }


def _pb_config(request, account, utskick, entries, available):
    access = request.flamingo
    pk = utskick.pk
    rita = reverse("flamingo:app_brev_render_block", args=[pk])
    return {
        **copy.deepcopy(BREV_PROFILE),
        "pageId": pk,
        "name": utskick.name,
        "rev": utskick.email_rev,
        "palette": None,
        "blocks": _doc_blocks(utskick),
        "state": {"published": False, "changes": False, "kind": "draft", "label": "", "live": []},
        "problems": [],
        "schema": entries,
        "groups": [{"key": key, "name": name} for key, name in registry.GROUPS],
        "available": available,
        "palettes": [],
        "campaigns": [],
        "urls": {
            "save": reverse("flamingo:app_brev_save", args=[pk]),
            "renderBlock": rita,
            # Ett nytt block hämtas också från rita/ ({"type"}), se modulens text.
            "newBlock": rita,
            "publish": None,
            "settings": None,
            "pages": _step_url(utskick, "innehall"),
            "ai_build": None,
            "ai_rewrite": None,
            "koll": None,
            "media_json": _optional_url("flamingo:app_media_json"),
            "media_upload": _optional_url("flamingo:app_media_upload"),
            "media": _optional_url("flamingo:app_media"),
        },
        "me": {
            "id": request.user.pk,
            "source": _source_for(request),
            "agency": access.is_agency,
        },
        "readOnly": bool(access.read_only),
        "maxBlocks": registry.MAX_BLOCKS,
        "maxVersions": pagebuilder.MAX_VERSIONS,
    }


# ---------------------------------------------------------------------------
# Sidan
# ---------------------------------------------------------------------------


@utskick_view
@require_safe
def brev_editor(request, account, pk):
    utskick = owned(Utskick, account, pk)
    if not utskick.has_email:
        messages.info(request, NOT_EMAIL)
        return redirect(_step_url(utskick, "kanal"))
    if utskick.status not in Utskick.EDITABLE:
        messages.info(request, NOT_EDITABLE)
        return redirect("flamingo:app_utskick", utskick.pk)
    try:
        canvas = _canvas(request, utskick)
    except Exception:  # noqa: BLE001 - redigeraren visas även om mejlet inte går att rita
        logger.exception("Utskick %s: mejlet gick inte att rita", utskick.pk)
        canvas = None
    entries, available, groups = _library(account, utskick)
    sender = _sender(account, utskick)
    picker = _picker(account, utskick)
    context = {
        "utskick": utskick,
        "ut_nav": None,
        "canvas_html": canvas,
        "groups": groups,
        "picker": picker,
        "sender": sender,
        "has_logo": _has_logo(account),
        "logo_shown": _logo_shown(account, utskick),
        "logo_positions": Utskick.LogoPosition.choices,
        "terms": _terms_json(utskick),
        "test": _test_targets(request, account),
        "staff_text": STAFF_TEXT.format(name=_display_name(request)),
        "display_name": _display_name(request),
        "is_demo": account.is_demo,
        "information": utskick.purpose == INFORMATION,
        "read_only": bool(request.flamingo.read_only),
        "config": _pb_config(request, account, utskick, entries, available),
        "br_config": _br_config(request, account, utskick, sender, picker),
        "back_url": _step_url(utskick, "innehall"),
        "light_text": style.LIGHT_TEXT,
        "saved": _saved_text(utskick.updated_at),
        "scheduled": utskick.status == Utskick.Status.SCHEDULED,
        "unconfirm_text": UNCONFIRM_NOTE,
    }
    return render_utskick(request, "flamingo/app/utskick/brev_editor.html", "utskick", context)


# ---------------------------------------------------------------------------
# Spara
# ---------------------------------------------------------------------------


def _block_error(exc):
    """BlockError som 400 med felen (texterna och var de sitter)."""
    texts = list(getattr(exc, "texts", None) or [])
    return _BadRequest(
        "Mejlet klarar inte schemat: " + "; ".join(t for t in texts[:3] if t),
        errors=list(exc.errors or [])[:20],
    )


@utskick_view
@require_POST
def brev_save(request, account, pk):
    utskick = owned(Utskick, account, pk)
    now = timezone.now()
    user = _user(request)
    try:
        data = _json_body(request)
        rev = data.get("rev")
        blocks = data.get("blocks")
        if blocks is not None:
            if isinstance(rev, bool) or not isinstance(rev, int):
                raise _BadRequest("Mejlets version saknas. Ladda om sidan.")
            blocks = _blocks_from(data, utskick)
        fields = _doc_fields(account, data)
        terms_ok = data.get("terms_ok") is True
        seen_terms = data.get("terms")
        # 1. Med raden låst och läget prövat under låset (som guidens
        # _editing): versionen, fälten utanför blocken, och ett schemalagt
        # utskick som ändras blir ett utkast. Ticken flyttar ett schemalagt
        # utskick med en villkorlig UPDATE som väntar på låset.
        with transaction.atomic():
            row = _lock(utskick)
            if row.status not in Utskick.EDITABLE:
                raise _BadRequest(NOT_EDITABLE, status=409, locked=True)
            if blocks is not None and row.email_rev != rev:
                raise _BadRequest(STALE_TEXT, status=409, rev=row.email_rev)
            if "merge_fallbacks" in data:
                fields["merge_fallbacks"] = _fallbacks(
                    account, data["merge_fallbacks"], row.merge_fallbacks
                )
            changed_blocks = blocks is not None and _unsigned(
                _stamp(row, blocks, user, _source_for(request), now)
            ) != _unsigned(_doc_blocks(row))
            changed = {name: value for name, value in fields.items() if getattr(row, name) != value}
            unconfirmed = False
            if (changed_blocks or changed) and row.status == Utskick.Status.SCHEDULED:
                if not state.unconfirm(row):
                    raise _BadRequest(NOT_EDITABLE, status=409, locked=True)
                unconfirmed = True
            if changed:
                Utskick.objects.filter(pk=row.pk).update(**changed, updated_at=now)
        # 2. Blocken, utan lås: email.blocks.save prövar email_rev och läget
        # i samma villkorliga UPDATE, och begär nya länkvärdar efteråt (larmet
        # är ett mejl till byrån, som aldrig skickas medan raden är låst).
        if changed_blocks:
            try:
                email_blocks.save(row, blocks, rev=rev, user=user, account=account, now=now)
            except email_blocks.StaleRevision:
                raise _BadRequest(STALE_TEXT, status=409) from None
            except email_blocks.BlockError as exc:
                if (
                    row.status not in Utskick.EDITABLE
                    or not Utskick.objects.filter(pk=row.pk, status__in=Utskick.EDITABLE).exists()
                ):
                    raise _BadRequest(NOT_EDITABLE, status=409, locked=True) from None
                raise _block_error(exc) from None
        row.refresh_from_db()
        # 3. "Uppgifterna stämmer" gäller de villkor kunden såg (F.7), aldrig
        # villkor som ändrats under tiden.
        confirmed_now = False
        if (
            terms_ok
            and row.confirmed_terms
            and isinstance(seen_terms, list)
            and seen_terms == row.confirmed_terms
            and not row.terms_confirmed_at
        ):
            confirmed_now = bool(
                Utskick.objects.filter(
                    pk=row.pk, status__in=Utskick.EDITABLE, terms_confirmed_at=None
                ).update(terms_confirmed_by=user, terms_confirmed_at=now)
            )
            row.refresh_from_db()
    except _BadRequest as exc:
        return exc.response()
    except ForeignIds:
        return _foreign()
    if changed_blocks or changed or confirmed_now:
        actor = actor_for(request)
        logger.info(
            "Utskick %s: mejlet sparat av användare %s (byrån: %s)",
            row.pk,
            getattr(actor.user, "pk", None),
            actor.staff,
        )
    return JsonResponse(
        {
            "ok": True,
            "rev": row.email_rev,
            "saved_at": row.updated_at.isoformat(),
            "saved_text": _saved_text(row.updated_at),
            "problems": [],
            "subject_errors": _line_errors(account, row.subject),
            "preheader_errors": _line_errors(account, row.preheader),
            "status": row.status,
            "message": UNCONFIRMED if unconfirmed else "",
            **_terms_json(row),
        }
    )


# ---------------------------------------------------------------------------
# Rita och nya block
# ---------------------------------------------------------------------------


def _new_block(request, account, utskick, data):
    type_key = data.get("type")
    variant = data.get("variant") or None
    block_type = registry.get_type(type_key) if isinstance(type_key, str) else None
    if block_type is None:
        raise _BadRequest("Okänt block.")
    if variant is not None and not isinstance(variant, str):
        raise _BadRequest("Okänd variant.")
    ok, why = registry.available(account, utskick).get(type_key, (False, ""))
    if not ok:
        raise _BadRequest(why or "Blocket går inte att lägga till.")
    try:
        block = email_blocks.new_block(type_key, account, utskick, user=_user(request))
    except email_blocks.BlockError as exc:
        texts = [e.get("text", "") for e in exc.errors if isinstance(e, dict)]
        raise _BadRequest(
            "; ".join(t for t in texts if t) or "Blocket går inte att lägga till."
        ) from None
    if variant and variant in getattr(block_type, "variant_keys", ()):
        block["variant"] = variant
    fields = data.get("fields")
    if fields is not None:
        if not isinstance(fields, dict):
            raise _BadRequest("Fälten ska vara ett dict.")
        # Fälten kommer från redigeraren (AI-panelen): versionen är den
        # inloggades, vad klienten än säger om källan.
        merged = {**(email_blocks.active_fields(block) or {}), **fields}
        try:
            email_blocks.add_version(
                block,
                merged,
                _source_for(request),
                _user(request),
                account=account,
                utskick=utskick,
            )
        except email_blocks.BlockError as exc:
            raise _block_error(exc) from None
    _validated(account, utskick, [block])
    return JsonResponse({"ok": True, "block": block})


@utskick_view
@require_POST
def brev_render_block(request, account, pk):
    utskick = owned(Utskick, account, pk)
    try:
        data = _json_body(request)
        if "type" in data and "blocks" not in data:
            return _new_block(request, account, utskick, data)
        blocks = _blocks_from(data, utskick)
        # Schemat prövas (typer, fält, längder, bilder på kontot), men det
        # som ritas är blocken som de är: en tom rad som kunden just lagt
        # till ska synas medan den skrivs. Mallarna escapar allt.
        _validated(account, utskick, blocks)
        ids = data.get("ids")
        if ids is not None and (
            not isinstance(ids, list) or not all(isinstance(i, str) for i in ids)
        ):
            raise _BadRequest("ids ska vara en lista med blockens id.")
        preview = _preview_copy(utskick, blocks, data)
    except _BadRequest as exc:
        return exc.response()
    except ForeignIds:
        return _foreign()
    if ids is not None:
        wanted = set(ids)
        ctx = render.context_for(preview, mode=render.EDITOR)
        out = {
            block["id"]: str(render.render_block(preview, block, ctx=ctx))
            for block in blocks
            if isinstance(block, dict) and block.get("id") in wanted
        }
        return JsonResponse({"ok": True, "blocks": out})
    return JsonResponse({"ok": True, "html": _canvas(request, preview)})


# ---------------------------------------------------------------------------
# Bilderna
# ---------------------------------------------------------------------------


@utskick_view
@require_POST
def brev_image(request, account, pk):
    owned(Utskick, account, pk)
    try:
        data = _json_body(request)
        asset_pk = owned_ids(MediaAsset, account, [data.get("asset")])[0]
        purpose = data.get("purpose") or EmailImage.Purpose.CONTENT
        if purpose not in EmailImage.Purpose.values or purpose == EmailImage.Purpose.LOGO:
            raise _BadRequest("Okänd sorts bild.")
    except _BadRequest as exc:
        return exc.response()
    except ForeignIds:
        return _foreign()
    asset = MediaAsset.objects.get(pk=asset_pk, account=account)
    try:
        image = images.rendition(asset, purpose)
    except (OSError, ValueError):
        logger.info("Utskick: bild %s gick inte att göra om för mejl", asset.pk)
        return JsonResponse(
            {"ok": False, "error": "Bilden gick inte att göra om för mejl. Välj en annan bild."},
            status=400,
        )
    return JsonResponse(
        {
            "ok": True,
            "id": image.pk,
            "url": images.absolute_url(image),
            "width": image.width,
            "height": image.height,
            "alt": asset.alt or "",
        }
    )


# ---------------------------------------------------------------------------
# Kontrollerna
# ---------------------------------------------------------------------------


def _email_count(utskick):
    """Hur många som får mejlet i utskickets kanal, för ADX-domänens tak i
    kontrollerna, eller None när urvalet är tomt."""
    if audience.is_empty(utskick):
        return None
    try:
        return int(audience.count(utskick).get("email") or 0)
    except Exception:  # noqa: BLE001 - utan antal prövas bara att taket inte är slut
        logger.exception("Utskick %s: mottagarna gick inte att räkna", utskick.pk)
        return None


def _contact_from(request, account):
    raw = request.GET.get("kontakt") or request.POST.get("kontakt")
    if not raw:
        return None
    pk = owned_ids(Contact, account, [raw])[0]
    return Contact.objects.get(pk=pk, account=account)


@utskick_view
@require_safe
def brev_checks(request, account, pk):
    utskick = owned(Utskick, account, pk)
    try:
        kontakt = _contact_from(request, account)
    except ForeignIds:
        return _foreign()
    # Panelen hämtas efter varje sparning, så den vanliga kontrollen är
    # lätt. Länkarna och mottagarnas antal mot ADX-domänens tak bara när
    # kunden ber om det (?lankar=1): länkkontrollen har en kvot på tio
    # körningar per konto och timme (F.5), och Granska gör båda igen.
    full = request.GET.get("lankar") == "1"
    items = email_checks.email_checks(
        utskick,
        now=timezone.now(),
        contact=kontakt,
        email_count=_email_count(utskick) if full else None,
        check_links=full,
    )
    blocking = email_checks.blocking(items)
    return JsonResponse(
        {
            "ok": True,
            "items": email_checks.as_json(items),
            "blocking": bool(blocking),
            "count": len(blocking) + sum(1 for i in items if i.level == email_checks.WARNS),
        }
    )


# ---------------------------------------------------------------------------
# AI
# ---------------------------------------------------------------------------


def _block_by_id(utskick, block_id):
    for block in _doc_blocks(utskick):
        if isinstance(block, dict) and block.get("id") == block_id:
            return block
    return None


@utskick_view
@require_POST
def brev_ai(request, account, pk):
    utskick = owned(Utskick, account, pk)
    try:
        data = _json_body(request)
    except _BadRequest as exc:
        return exc.response()
    brief = data.get("brief") if isinstance(data.get("brief"), str) else ""
    user = _user(request)
    if data.get("sms") is True:
        result = ai.write_sms(utskick, user=user, brief=brief)
        return JsonResponse(result.as_json(), status=200 if result.ok else 400)
    type_key = data.get("type")
    if not isinstance(type_key, str) or registry.get_type(type_key) is None:
        return JsonResponse({"ok": False, "error": "Välj ett block."}, status=400)
    block = None
    block_id = data.get("block_id")
    if isinstance(block_id, str) and block_id:
        block = _block_by_id(utskick, block_id[:20])
    fields = data.get("fields")
    if isinstance(fields, dict):
        # Blockets text som den står i redigeraren (kanske inte sparad än).
        block = {
            "type": type_key,
            "variant": data.get("variant") if isinstance(data.get("variant"), str) else "",
            "fields": {
                k: v for k, v in list(fields.items())[:40] if isinstance(k, str) and len(k) <= 40
            },
        }
    result = ai.write_block(utskick, type_key, user=user, brief=brief, block=block)
    return JsonResponse(result.as_json(), status=200 if result.ok else 400)


# ---------------------------------------------------------------------------
# Förhandsvisningen
# ---------------------------------------------------------------------------

#: "Mörkt läge": ungefär som de e-postprogram som vänder färgerna själva.
DARK_STYLE = (
    "<style>html{background:#121212;filter:invert(1) hue-rotate(180deg)}"
    "img{filter:invert(1) hue-rotate(180deg)}</style>"
)
PREVIEW_CSP = (
    "default-src 'none'; img-src https: http: data:; style-src 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
)
PREVIEW_MODES = ("mobil", "dator", "morkt")


def _preview_contacts(utskick):
    try:
        return list(audience.contacts(utskick).order_by("pk")[:PREVIEW_CONTACTS])
    except Exception:  # noqa: BLE001 - utan urval visas mejlet med reservtexterna
        return []


@utskick_view
@require_safe
def brev_preview(request, account, pk):
    utskick = owned(Utskick, account, pk)
    lage = request.GET.get("lage") or "dator"
    lage = lage if lage in PREVIEW_MODES else "dator"
    candidates = _preview_contacts(utskick)
    try:
        kontakt = _contact_from(request, account)
    except ForeignIds:
        return _foreign()
    if kontakt is None and candidates:
        kontakt = candidates[0]
    ctx = render.context_for(utskick, mode=render.PREVIEW, contact=kontakt)
    html = str(render.render_html(utskick, ctx))
    if lage == "morkt":
        html = html.replace("</head>", f"{DARK_STYLE}\n</head>", 1)
    if "application/json" in request.headers.get("Accept", ""):
        following = None
        if kontakt is not None and candidates:
            ids = [c.pk for c in candidates]
            if kontakt.pk in ids and len(ids) > 1:
                following = ids[(ids.index(kontakt.pk) + 1) % len(ids)]
            elif kontakt.pk not in ids:
                following = ids[0]
        sender = _sender(account, utskick)
        response = JsonResponse(
            {
                "ok": True,
                "html": html,
                "lage": lage,
                "subject": str(render.subject_for(utskick, ctx)),
                "preheader": str(render.preheader_for(utskick, ctx)),
                "from": sender["text"],
                "kontakt": {"pk": kontakt.pk, "name": kontakt.display_name} if kontakt else None,
                "next": following,
            }
        )
    else:
        response = HttpResponse(html, content_type="text/html; charset=utf-8")
        response["Content-Security-Policy"] = PREVIEW_CSP
    response["Cache-Control"] = "private, no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


# ---------------------------------------------------------------------------
# Testmejlet (F.8)
# ---------------------------------------------------------------------------


def _customer_addresses(account):
    if not account.customer_id:
        return set()
    return {
        (u.email or "").strip().lower()
        for u in account.customer.users.all()
        if (u.email or "").strip() and not u.is_staff
    }


#: Byrån skrev en adress som är kundens eller en kontakts (F.8, I.4).
TEST_NOT_OWN_TEXT = (
    "Adressen är kundens eller en kontakts. Skicka testet till kunden med kryssrutan i stället."
)


def _test_address(request, account, actor):
    """(adress, fel). "mig": den inloggades egen adress. "eget": byrån i
    kundvyn vars inloggning saknar adress skriver sin egen; en av kundens
    adresser eller en kontakts nekas, och testet visas då utan kontakt
    (send_test_email). Har byråns inloggning en adress går testet alltid
    dit. "kunden": byrån till en av kundens inloggningar, bara med
    kryssrutan (I.4)."""
    target = request.POST.get("till") or ""
    user = _user(request)
    if target == "mig":
        address = (getattr(user, "email", "") or "").strip()
        if not address:
            return None, "Din inloggning saknar e-postadress."
        return address, ""
    if target == "eget" and actor.staff:
        own = (getattr(user, "email", "") or "").strip()
        if own:
            return own, ""
        try:
            address = normalize.email(request.POST.get("adress"))
        except normalize.InvalidValue:
            address = ""
        if not address:
            return None, "Skriv en giltig e-postadress."
        if (
            address.lower() in _customer_addresses(account)
            or Contact.objects.filter(account=account, email__iexact=address).exists()
        ):
            return None, TEST_NOT_OWN_TEXT
        return address, ""
    if target == "kunden" and actor.staff:
        address = str(request.POST.get("kund_adress") or "").strip()
        if address.lower() not in _customer_addresses(account):
            return None, "Välj en av kundens adresser."
        if request.POST.get("som_adx") != "1":
            return None, STAFF_MISSING.format(name=_display_name(request))
        return address, ""
    return None, "Välj vart testet ska gå."


def send_test_email(request, account, utskick):
    """Ett testmejl med utskickets sparade mejl (F.8), skickat nu genom
    sending.email.send_test (som räknar ADX-taket och dagens tio test,
    loggar försöket med användaren och om det var byrån, och skriver
    händelsen på en kontakt). Här: vem som får testet (I.4), ämnesraden,
    länkarna som ADX inte godkänt och reglerna för information
    (state.content_problems), spärrlistan, aldrig från demot. Returnerar
    (skickat, text)."""
    from ..sending import email as sending_email
    from ..sending.sms_wrapper import DemoRefused

    now = timezone.now()
    actor = actor_for(request)
    if not utskick.has_email:
        return False, NOT_EMAIL
    address, error = _test_address(request, account, actor)
    if error:
        return False, error
    if account.is_demo:
        return False, TEST_ERRORS["demo"]
    if not (utskick.subject or "").strip():
        return False, "Skriv en ämnesrad innan du skickar ett test."
    content = state.content_problems(utskick)
    if content:
        return False, content[0]
    if suppressions.is_suppressed(account, CHANNEL_EMAIL, value=address):
        return False, TEST_ERRORS["suppressed"]
    typed = (
        request.POST.get("till") == "eget"
        and not (getattr(_user(request), "email", "") or "").strip()
    )
    try:
        kontakt = _contact_from(request, account)
    except ForeignIds:
        return False, FOREIGN_IDS_TEXT
    if kontakt is None:
        candidates = _preview_contacts(utskick)
        kontakt = candidates[0] if candidates else None
    if typed:
        # En adress som ingen har bekräftat får aldrig en kontakts uppgifter:
        # testet visas med reservtexterna.
        kontakt = None
    try:
        sent = sending_email.send_test(
            utskick, address=address, contact=kontakt, actor=actor, now=now
        )
    except DemoRefused:
        return False, TEST_ERRORS["demo"]
    except keys.KeyMismatch:
        return False, KEY_TEXT
    # send_test loggar försöket och skriver händelsen på kontakten.
    if sent.ok:
        return True, f"Testet är skickat till {address}."
    return False, sending_email.error_text(sent) or TEST_ERRORS.get(
        sent.error, "Testet gick inte att skicka."
    )


# ---------------------------------------------------------------------------
# Mejlet i guidens steg (Innehåll och Granska, README I.8)
# ---------------------------------------------------------------------------


def card_context(request, account, utskick):
    """Mejlet som ett kort i steget Innehåll och i Granska: ämnesraden,
    förhandstexten, blocken i ordning, avsändaren, vägen till redigeraren och
    vart ett testmejl kan gå."""
    names = []
    for block in _doc_blocks(utskick):
        block_type = registry.get_type(block.get("type")) if isinstance(block, dict) else None
        if block_type is not None:
            names.append(block_type.name)
    sender = _sender(account, utskick)
    return {
        "url": reverse("flamingo:app_brev", args=[utskick.pk]),
        "preview_url": reverse("flamingo:app_brev_preview", args=[utskick.pk]),
        "subject": utskick.subject,
        "preheader": utskick.preheader,
        "blocks": names,
        "count": len(names),
        "sender": sender["text"],
        "own_domain": sender["own"],
        "test": _test_targets(request, account),
        "staff_text": STAFF_TEXT.format(name=_display_name(request)),
        "editable": utskick.status in Utskick.EDITABLE,
    }
