"""
@-omnämnanden i chatten.

Kunden skriver @ och väljer ett objekt ur en sökmeny; i texten landar en
token som `@tjanst:spolning`. Poängen är precision: "serviceavtal" kan vara
en tjänst eller en sida, men en token pekar på exakt en rad i databasen.

Tre delar:

* `search()`  - matar menyn. Söker över tjänster, områden, sidor, FAQ och
  pågående förslag.
* `context_for()` - översätter tokens i ett meddelande till ett
  referensblock som skickas med till modellen. Blocket läggs på vid
  anropet och sparas ALDRIG i meddelandet - kundens text förblir kundens.
* `as_html()` - visar tokens som chips i chatloggen i stället för rå syntax.

En token som inte matchar något ignoreras tyst i kontexten men visas
avvikande i loggen - modellen ska inte få påhittade referenser, och kunden
ska kunna se att något inte träffade.
"""

import re

from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

TOKEN = re.compile(r"@(tjanst|kategori|omrade|sida|faq|fraga|forslag|utkast):([\w-]+)")

#: Etikett per typ, för menyn och chipsen. Målgrupper togs bort
#: 2026-08-21: de är tre statiska rader som aldrig ändras, och en sökmeny
#: ska innehålla det man behöver leta efter.
TYPE_LABELS = {
    "tjanst": "Tjänst",
    "kategori": "Kategori",
    "omrade": "Område",
    "sida": "Sida",
    "faq": "FAQ",
    "fraga": "Fråga",
    "forslag": "Förslag",
    "utkast": "Utkast",
}


def _search_services(q):
    from apps.services.models import Service

    for s in Service.objects.filter(name__icontains=q).order_by("name")[:4]:
        yield {
            "typ": "tjanst",
            "ref": s.slug,
            "label": s.name,
            "sub": "Tjänst" + ("" if s.is_active else " · inaktiv"),
        }


def _search_areas(q):
    from apps.areas.models import Area

    for a in Area.objects.filter(name__icontains=q, is_active=True).order_by("name")[:4]:
        yield {"typ": "omrade", "ref": a.slug, "label": a.name, "sub": "Område"}


def _search_pages(q):
    from apps.website.models import BlockPage

    for p in BlockPage.objects.filter(title__icontains=q).order_by("title")[:3]:
        yield {"typ": "sida", "ref": p.slug, "label": p.title, "sub": "Sida"}


def _search_faq(q):
    from apps.faq.models import FAQSection

    for f in FAQSection.objects.filter(title__icontains=q).order_by("title")[:3]:
        yield {"typ": "faq", "ref": f.slug, "label": f.title, "sub": "FAQ-sektion"}


def _search_categories(q):
    from apps.services.models import ServiceCategory

    for c in ServiceCategory.objects.filter(name__icontains=q).order_by("name")[:3]:
        yield {"typ": "kategori", "ref": c.slug, "label": c.name, "sub": "Kategori"}


def _search_faq_items(q):
    from apps.faq.models import FAQItem

    rows = FAQItem.objects.filter(question__icontains=q).select_related("section")[:3]
    for i in rows:
        yield {
            "typ": "fraga",
            "ref": str(i.pk),
            "label": i.question[:70],
            "sub": f"Fråga · {i.section.title}" if i.section_id else "Fråga",
        }


def _search_drafts(q, user):
    """
    Enskilda VÄNTANDE utkast. Gör att kunden kan peka på ett liggande
    förslag - "ändra @utkast:42" - i stället för att beskriva det i ord.
    """
    from .models import DraftChange

    rows = DraftChange.objects.filter(
        job__user=user, status=DraftChange.Status.PENDING, summary__icontains=q
    ).order_by("-created_at")[:4]
    for change in rows:
        yield {
            "typ": "utkast",
            "ref": str(change.pk),
            "label": change.summary[:70],
            "sub": "Väntande utkast",
        }


def _search_jobs(q, user):
    from .models import AIJob

    rows = AIJob.objects.filter(user=user, title__icontains=q).order_by("-created_at")[:3]
    for j in rows:
        yield {
            "typ": "forslag",
            "ref": str(j.pk),
            "label": j.title or f"Förslag {j.pk}",
            "sub": "Förslag",
        }


SEARCH_SOURCES = (
    _search_services,
    _search_categories,
    _search_areas,
    _search_pages,
    _search_faq,
    _search_faq_items,
)


def _browse(user):
    """
    Listan som visas på ren @, innan kunden hunnit skriva något.

    En tom meny på @ läses som att funktionen inte fungerar - det var
    precis så buggen 2026-08-21 upplevdes. Visa ett tvärsnitt att bläddra
    i, så syns det både att menyn lever och vad som går att peka på.
    """
    out = []
    for source in SEARCH_SOURCES:
        out.extend(list(source(""))[:2])
    out.extend(_search_drafts("", user))
    out.extend(_search_jobs("", user))
    return out


def search(q, user, limit=10):
    """Träffar för menyn, mest specifika typerna först. Tom fråga = bläddra."""
    q = (q or "").strip()
    if not q:
        return _browse(user)[:limit]
    out = []
    for source in SEARCH_SOURCES:
        out.extend(source(q))
    out.extend(_search_drafts(q, user))
    out.extend(_search_jobs(q, user))
    return out[:limit]


def _describe(typ, ref, user):
    """En referensrad för modellen, eller None om token inte träffar."""
    if typ == "tjanst":
        from apps.services.models import Service

        s = Service.objects.filter(slug=ref).first()
        if s:
            return f'@tjanst:{ref} = tjänsten "{s.name}" (slug {s.slug}). Använd hamta_tjanst.'
    elif typ == "omrade":
        from apps.areas.models import Area

        a = Area.objects.filter(slug=ref).first()
        if a:
            return f'@omrade:{ref} = området "{a.name}" (slug {a.slug}). Använd hamta_omrade.'
    elif typ == "sida":
        from apps.website.models import BlockPage

        p = BlockPage.objects.filter(slug=ref).first()
        if p:
            return f'@sida:{ref} = sidan "{p.title}" (slug {p.slug}). Använd hamta_sida.'
    elif typ == "faq":
        from apps.faq.models import FAQSection

        f = FAQSection.objects.filter(slug=ref).first()
        if f:
            return f'@faq:{ref} = FAQ-sektionen "{f.title}" (slug {f.slug}).'
    elif typ == "kategori":
        from apps.services.models import ServiceCategory

        c = ServiceCategory.objects.filter(slug=ref).first()
        if c:
            return (
                f'@kategori:{ref} = tjänstekategorin "{c.name}" (slug {c.slug}). '
                f"Används som kategori_slug i skapa_tjanst."
            )
    elif typ == "fraga":
        from apps.faq.models import FAQItem

        i = (
            FAQItem.objects.select_related("section").filter(pk=ref).first()
            if ref.isdigit()
            else None
        )
        if i:
            section = f' i sektionen "{i.section.title}"' if i.section_id else ""
            return (
                f'@fraga:{ref} = FAQ-frågan "{i.question[:80]}"{section} '
                f"(id {i.pk}). Används som fraga_id i uppdatera_faq_fraga."
            )
    elif typ == "utkast":
        from .models import DraftChange

        change = (
            DraftChange.objects.filter(pk=ref, job__user=user).first() if ref.isdigit() else None
        )
        if change:
            return (
                f"@utkast:{ref} = det {change.get_status_display().lower()} förslaget "
                f'"{change.summary}" (operation {change.operation}, id {change.pk}). '
                f"Vill kunden ändra det: dra_tillbaka_utkast och lägg ett nytt."
            )
    elif typ == "forslag":
        from .models import AIJob

        j = AIJob.objects.filter(pk=ref, user=user).first() if ref.isdigit() else None
        if j:
            pending = "; ".join(c.summary for c in j.changes.all()[:6])
            return f'@forslag:{ref} = förslaget "{j.title or j.pk}". Innehåll: {pending or "tomt"}.'
    return None


def context_for(text, user):
    """
    Referensblock för modellen, eller tom sträng.

    Läggs på vid modellanropet och sparas inte i meddelandet - annars
    skulle kundens text växa med systemtext vid varje visning.
    """
    lines = []
    seen = set()
    for typ, ref in TOKEN.findall(text or ""):
        if (typ, ref) in seen:
            continue
        seen.add((typ, ref))
        line = _describe(typ, ref, user)
        if line:
            lines.append(line)
    if not lines:
        return ""
    return "[Referenser - exakta objekt kunden pekat ut]\n" + "\n".join(lines)


def as_html(text):
    """
    Meddelandetext med tokens som chips. Allt annat escapas.

    Escapningen görs FÖRE chip-ersättningen och chipsen byggs med
    format_html - ordningen är det som gör mark_safe säker här.
    """
    escaped = escape(text or "")

    def chip(match):
        typ, ref = match.group(1), match.group(2)
        return format_html(
            '<span class="m-chip" data-typ="{}">{}<b>{}</b></span>',
            typ,
            TYPE_LABELS.get(typ, typ) + " ",
            ref.replace("-", " "),
        )

    return mark_safe(TOKEN.sub(chip, escaped))
