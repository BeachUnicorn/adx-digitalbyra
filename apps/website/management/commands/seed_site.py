"""
Seedar hela ADX-sajten ur spårade seed-filer (mönsterkatalogen §10:
innehållsseed via git, medvetet urkopplad från deploy).

Källor i seed_data/:
  adx_pages.json   - de elva sidorna, transkriberade EXAKT ur
                     strict-design-guide.html (block, färger, SEO-meta)
  adx_cities.json  - tolv unika stadstexter (doorway-regeln: en sida per
                     stad, båda sökorden i copyn, aldrig mallad text)

Kommandot är idempotent: sidor och block ersätts per slug (deterministisk
ersättning, inte append), tjänster/städer uppdateras per slug. Kör om det
hur många gånger som helst lokalt - men ALDRIG automatiskt vid deploy:
efter första seeden är produktionen sanningskällan.
"""

import json
from pathlib import Path

from django.conf import settings as django_settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.areas.models import Area, AreaLevel
from apps.faq.models import FAQItem, FAQSection
from apps.services.models import Service
from apps.website.models import Block, BlockPage, Menu, MenuItem, SiteSettings

SEED_DIR = Path(django_settings.BASE_DIR) / "seed_data"

# Tjänsterna: slug == BlockPage-slug (sidan seedas ur adx_pages.json).
# short_description = svc-radernas text ur guiden, ordagrant.
SERVICES = [
    (
        "webbutveckling",
        "Webbutveckling",
        "#2456b0",
        "Färdiga paket och skräddarsydda webbplatser och intranät — byggda "
        "för att prestera och förvaltas över tid.",
    ),
    (
        "automation",
        "Automation & Integrationer",
        "#c05a2e",
        "Effektivisera era interna processer med skript, API-integrationer "
        "och automatiserade arbetsflöden.",
    ),
    (
        "content",
        "Managed Content",
        "#a8842a",
        "Copywriting och sökmotoroptimering som gör att rätt kunder hittar er — och stannar.",
    ),
    (
        "hosting",
        "Managed Hosting & Moln",
        "#0e3a52",
        "Trygg drift med övervakning, backup och säkerhet. Multi-tenant-lösningar för kedjor.",
    ),
    (
        "domain",
        "Domain Management",
        "#4a2d73",
        "Domäner och DNS. Registrering, överlåtelser, bevakning av varumärken "
        "och HTTPS/SSL-certifikat samt konfiguration.",
    ),
    (
        "epost",
        "E-post",
        "#813a63",
        "Office 365 och Google Workspace. Management, uppsättning, "
        "licenshantering, användaradministration och säkerhet.",
    ),
]

# Menyerna refererar SIDOR (slug -> MenuItem.page-FK), inte URL-strängar -
# länken överlever slug-byten och en avpublicerad sida döljs + larmas
# (länkregeln i mönsterkatalogen). Ruttmål utanför sidsystemet (städerna)
# är de enda som bär en path, och de bevakas av resolvern i links.py.
HEADER_MENU = [
    ("Hem", "hem", False),
    ("Tjänster", "tjanster", False),
    ("Portfolio", "portfolio", False),
    ("Paket", "paket", False),
    ("Kontakt", "kontakt", False),
    ("Förfrågan", "kontakt", True),
]

FOOTER_MENUS = [
    (
        "Tjänster",
        [
            ("Webbutveckling", "webbutveckling"),
            ("Automation", "automation"),
            ("Managed Content", "content"),
            ("Managed Hosting", "hosting"),
        ],
    ),
    (
        "Drift",
        [
            ("Domäner & DNS", "domain"),
            ("E-post", "epost"),
            ("Molntjänster", "hosting"),
            ("Kedjelösningar", "hosting"),
        ],
    ),
    (
        "Företag",
        [
            ("Portfolio", "portfolio"),
            ("Paket", "paket"),
            ("Städer", "/digitalbyra/"),
            ("Kontakt", "kontakt"),
        ],
    ),
]


class Command(BaseCommand):
    help = "Seedar ADX-sajten ur seed_data/ (idempotent, körs aldrig av deploy)."

    @transaction.atomic
    def handle(self, *args, **options):
        pages_data = json.loads((SEED_DIR / "adx_pages.json").read_text())
        cities_data = json.loads((SEED_DIR / "adx_cities.json").read_text())

        self._seed_settings()
        self._seed_services()
        pages = {p["sida"]: self._seed_page(p, order) for order, p in enumerate(pages_data)}
        self._seed_menus()
        self._convert_links()
        self._seed_cities(cities_data["stader"])

        settings = SiteSettings.load()
        settings.homepage = pages["hem"]
        settings.save(update_fields=["homepage"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Klart: {len(pages)} sidor, {Service.objects.count()} tjänster, "
                f"{Area.objects.count()} städer, {FAQSection.objects.count()} FAQ-sektioner."
            )
        )

    # ------------------------------------------------------------------

    def _seed_settings(self):
        settings = SiteSettings.load()
        settings.name = "ADX"
        settings.email = "hej@adx.se"
        settings.city = "Stockholm"
        settings.default_gradient_color = "#f7fcff"
        settings.footer_about = (
            "ADX är en digitalbyrå i Stockholm som skapar digitala lösningar — "
            "från webb och automation till drift och förvaltning."
        )
        settings.save()

    def _seed_services(self):
        for order, (slug, name, color, short) in enumerate(SERVICES):
            Service.objects.update_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "gradient_color": color,
                    "description": short,
                    "is_active": True,
                    "order": order,
                },
            )

    def _seed_menus(self):
        header, _ = Menu.objects.update_or_create(location="header", defaults={"name": "Huvudmeny"})
        header.items.all().delete()
        for order, (label, target, is_button) in enumerate(HEADER_MENU):
            MenuItem.objects.create(
                menu=header,
                order=order,
                label=label,
                is_button=is_button,
                **self._menu_target(target),
            )

        Menu.objects.filter(location="footer").delete()
        for order, (heading, items) in enumerate(FOOTER_MENUS):
            menu = Menu.objects.create(
                location="footer", name=f"Sidfot: {heading}", heading=heading, order=order
            )
            for i, (label, target) in enumerate(items):
                MenuItem.objects.create(
                    menu=menu, label=label, order=i, **self._menu_target(target)
                )

    def _menu_target(self, target):
        """Slug -> sid-FK; "/path/" -> url-sträng (bara för mål utanför
        sidsystemet). En okänd slug är ett seedfel och ska smälla högt."""
        if target.startswith("/"):
            return {"url": target}
        return {"page": BlockPage.objects.get(slug=target)}

    def _seed_page(self, data, order):
        page, _ = BlockPage.objects.update_or_create(
            slug=data["sida"] if data["sida"] != "hem" else "hem",
            defaults={
                "title": data.get("meta_title", data["sida"]).split("—")[0].split("|")[0].strip(),
                "meta_title": data.get("meta_title", ""),
                "meta_description": data.get("meta_description", ""),
                "gradient_color": data.get("gradient_color", ""),
                "is_published": True,
                "order": order,
            },
        )
        # Deterministisk ersättning, inte append (mönsterkatalogen §2).
        page.blocks.all().delete()
        for i, block in enumerate(data["blocks"]):
            block_data = dict(block["data"])
            if block["type"] == "faq":
                block_data = self._seed_faq_block(data["sida"], block_data)
            Block.objects.create(
                page=page,
                block_type=block["type"],
                data=block_data,
                order=i,
                is_visible=True,
            )
        return page

    def _seed_faq_block(self, page_slug, data):
        """FAQ-frågorna ur transkriptionen blir en riktig FAQ-sektion
        (AI-redaktören kan sedan redigera dem som vanligt)."""
        verbatim = data.pop("faq_verbatim", [])
        if not verbatim:
            return data
        section, _ = FAQSection.objects.update_or_create(
            slug=f"faq-{page_slug}",
            defaults={"title": data.get("title") or "Vanliga frågor", "is_active": True},
        )
        section.items.all().delete()
        for i, qa in enumerate(verbatim):
            FAQItem.objects.create(
                section=section,
                question=qa["q"],
                answer=f"<p>{qa['a']}</p>",
                order=i,
                is_active=True,
            )
        data["faq_section_id"] = section.pk
        return data

    def _seed_cities(self, cities):
        for order, city in enumerate(cities):
            Area.objects.update_or_create(
                slug=city["slug"],
                defaults={
                    "name": city["namn"],
                    "level": AreaLevel.REGION,
                    "intro": city["lead"][:255],
                    "body": city["brodtext"],
                    "meta_title": city["meta_title"][:70],
                    "meta_description": city["meta_description"][:200],
                    "gradient_color": "#2f6f4f",
                    "is_active": True,
                    "order": order,
                },
            )

    def _convert_links(self):
        """Seed-JSON:ens länkar är läsbara strängar ("/kontakt/") - portabelt
        mellan miljöer. Här konverteras de till beskrivare (sid-/stads-ID)
        via parse_href, EFTER att alla sidor finns. I databasen lagras
        aldrig en intern länk som adress (länkregeln, hela vägen)."""
        from apps.website.links import _schema_url_fields, parse_href
        from apps.website.models import Block

        for block in Block.objects.all():
            data = block.data or {}
            changed = False
            for key, list_key in _schema_url_fields(block.block_type):
                if list_key:
                    for row in data.get(list_key) or []:
                        value = row.get(key) if isinstance(row, dict) else None
                        if isinstance(value, str) and value:
                            row[key] = parse_href(value) or ""
                            changed = True
                else:
                    parts = key.split(".")
                    holder = data
                    for part in parts[:-1]:
                        holder = holder.get(part) if isinstance(holder, dict) else None
                        if holder is None:
                            break
                    if isinstance(holder, dict):
                        value = holder.get(parts[-1])
                        if isinstance(value, str) and value:
                            holder[parts[-1]] = parse_href(value) or ""
                            changed = True
            if changed:
                block.data = data
                block.save(update_fields=["data"])
