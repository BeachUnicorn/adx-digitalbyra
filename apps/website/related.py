"""
Intern länkmotor: automatiska syskonlänkar ur sidkategorin.

Problemet motorn löser: interna länkar avgör både om Google hittar en sida
och hur viktig den bedöms vara - men handskrivna länklistor glöms när nya
sidor tillkommer. Mätningen 2026-08-30 hittade sidor med noll inlänkar
trots att de låg i sitemapen.

Lösningen är en RING: alla publicerade sidor i samma kategori sorteras på
slug, och varje sida länkar till de N som följer efter den (med wrap).
Det ger tre garantier utan någon handpåläggning:

  1. Varje sida får exakt N inlänkar och N utlänkar inom sin kategori.
  2. En ny sida glider in i ringen av sig själv - grannarna börjar länka
     till den vid nästa sidvisning, utan att någon rör de andra sidorna.
  3. Länkkraften sprids jämnt: ingen sida blir nav, ingen blir ö.

Handskrivna related-block finns kvar och kompletterar (de är kontextuella,
ringen är strukturell). Sidor utan kategori står utanför motorn.
"""

from bisect import bisect_left

from apps.website.models import BlockPage

RING_SIZE = 6

RING_HEADINGS = {
    "bransch": "Fler branscher vi bygger för",
    "guide": "Fler guider om hemsidor",
    "case": "Fler byggda av oss",
}


def ring_links(page, count=RING_SIZE):
    """
    Sidans N ringgrannar som [{"title": ..., "href": ...}].

    Adressen byggs ur sluggen i stället för get_absolute_url - den slår
    upp SiteSettings per anrop, och kategorisidor är aldrig startsidan.
    """
    if not page.category:
        return []
    rows = list(
        BlockPage.objects.filter(category=page.category, is_published=True)
        .exclude(pk=page.pk)
        .order_by("slug")
        .values_list("slug", "title")
    )
    if not rows:
        return []
    idx = bisect_left([slug for slug, _ in rows], page.slug)
    picked = [rows[(idx + i) % len(rows)] for i in range(min(count, len(rows)))]
    return [{"title": title, "href": f"/{slug}/"} for slug, title in picked]


def ring_heading(page):
    return RING_HEADINGS.get(page.category, "Läs vidare")
