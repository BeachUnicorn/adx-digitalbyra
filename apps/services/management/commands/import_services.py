"""
Import services into a specific category. Additive only — never deletes.

Usage:
    python manage.py import_services

Adds the hardcoded services below to category_id=14. Skip any service whose
name already exists (idempotent). Creates ServiceSteps in order.
"""
# ruff: noqa: E501

from django.core.management.base import BaseCommand
from django.utils.text import slugify

from apps.services.models import Service, ServiceStep

CATEGORY_ID = 14

SERVICES = [
    {
        "name": "Luftning av element",
        "description": "Vi luftar dina element och kontrollerar systemtrycket for jamn och effektiv varme.",
        "body": (
            "Luft i systemet gor att elementen blir kalla upptill, porlar och varmer "
            "samre. Vi luftar dina element, kontrollerar trycket och fyller pa vatten "
            "vid behov, sa att varmen fordelas jamnt igen. En enkel atgard som ofta "
            "gor stor skillnad."
        ),
        "steps": [
            ("Kontroll av symptom", "Vi lokaliserar vilka element som ar kalla eller porlar."),
            ("Luftning", "Vi luftar elementen tills all luft ar ute och varmen fordelas jamnt."),
            (
                "Tryckkontroll och pafyllning",
                "Vi kontrollerar systemtrycket och fyller pa vatten vid behov.",
            ),
        ],
    },
    {
        "name": "Byte av termostat och radiatorventil",
        "description": "Vi byter termostater och ventiler for full kontroll over temperaturen i varje rum.",
        "body": (
            "En trasig eller karvande termostat ger ojamn varme och onodig energiforbrukning. "
            "Vi byter termostater och radiatorventiler sa att du far full kontroll over "
            "temperaturen i varje rum - och slipper element som star pa for fullt i onodan."
        ),
        "steps": [
            ("Felkontroll", "Vi bedomer om termostaten eller ventilen ar trasig eller karvar."),
            (
                "Avstangning och byte",
                "Vi stanger av, byter ut termostat eller ventil och tatar anslutningen.",
            ),
            (
                "Funktionskontroll",
                "Vi provkor och kontrollerar att temperaturen gar att reglera som den ska.",
            ),
        ],
    },
    {
        "name": "Injustering av radiatorsystem",
        "description": "Vi balanserar flodet i radiatorsystemet for jamnare varme och lagre energikostnad.",
        "body": (
            "Ar vissa rum kalla medan andra blir for varma? Da behover systemet balanseras. "
            "Vi injusterar flodet i radiatorsystemet sa att alla element far ratt mangd varme, "
            "vilket ger ett jamnare inomhusklimat och en lagre energikostnad."
        ),
        "steps": [
            ("Genomgang av systemet", "Vi kartlagger hur varmen fordelas och var problemen finns."),
            (
                "Installning av floden",
                "Vi justerar ventilerna sa att varje element far ratt mangd varme.",
            ),
            (
                "Kontroll och finjustering",
                "Vi foljer upp att alla rum blir jamnt varma och finjusterar vid behov.",
            ),
        ],
    },
    {
        "name": "Felsokning av kalla element",
        "description": "Systematisk felsokning nar element inte blir varma - vi hittar orsaken och atgardar den.",
        "body": (
            "Blir ett eller flera element inte varma vet vi var vi ska leta - luft, slam, "
            "en trasig ventil, fel tryck eller problem med cirkulationspumpen. Vi felsoker "
            "systematiskt, hittar orsaken och atgardar den, i stallet for att gissa."
        ),
        "steps": [
            (
                "Felsokning pa plats",
                "Vi gar systematiskt igenom luft, tryck, ventiler, slam och cirkulationspump.",
            ),
            (
                "Atgard",
                "Vi atgardar orsaken, till exempel luftar, byter en ventil eller spolar systemet.",
            ),
            (
                "Kontroll",
                "Vi kontrollerar att elementen blir varma igen och att systemet fungerar.",
            ),
        ],
    },
    {
        "name": "Spolning av varmesystem",
        "description": "Vi spolar ur slam och avlagringar for battre varme och langre livslangd pa systemet.",
        "body": (
            "Med tiden samlas slam och avlagringar i ror och element, vilket forsamrar "
            "varmen och sliter pa systemet. Vi spolar ur systemet och kan tillsatta "
            "skyddsmedel som motverkar nya avlagringar - for battre varme och langre livslangd."
        ),
        "steps": [
            ("Bedomning", "Vi kontrollerar tecken pa slam och avlagringar i systemet."),
            ("Spolning", "Vi spolar ur ror och element och avlagsnar slam och fororeningar."),
            (
                "Skydd och pafyllning",
                "Vi tillsatter skyddsmedel vid behov, fyller pa och luftar systemet.",
            ),
            ("Kontroll", "Vi kontrollerar att varmen forbattrats och att allt ar tatt."),
        ],
    },
    {
        "name": "Atgard av lackande element",
        "description": "Vi tatar eller byter ut lackande delar innan det leder till vattenskada.",
        "body": (
            "Ett droppande element eller en lackande ventil kan snabbt leda till vattenskada "
            "om det inte atgardas. Vi tatar eller byter ut de delar som lacker och kontrollerar "
            "att resten av systemet ar tatt och sakert."
        ),
        "steps": [
            (
                "Lokalisering av lackan",
                "Vi hittar var det lacker, vid koppling, ventil eller sjalva elementet.",
            ),
            ("Atgard", "Vi tatar eller byter ut den lackande delen."),
            (
                "Tat- och tryckkontroll",
                "Vi provtrycker och sakerstaller att systemet ar tatt igen.",
            ),
        ],
    },
    {
        "name": "Installation av vattenburen handdukstork",
        "description": "Fackmassig installation av handdukstork kopplad till ditt befintliga varmesystem.",
        "body": (
            "En vattenburen handdukstork ger bade skon varme i badrummet och torra handdukar. "
            "Vi installerar och ansluter handdukstorken till ditt befintliga varmesystem pa "
            "ett fackmassigt och sakert satt."
        ),
        "steps": [
            (
                "Planering",
                "Vi bestammer placering och kontrollerar anslutningsmojligheter mot varmesystemet.",
            ),
            (
                "Rordragning och montering",
                "Vi drar fram ror, monterar handdukstorken och ansluter den.",
            ),
            (
                "Igangkorning och genomgang",
                "Vi fyller pa, luftar, kontrollerar tatheten och gar igenom resultatet.",
            ),
        ],
    },
]


class Command(BaseCommand):
    help = "Import services into category 14. Additive only, skips existing."

    def handle(self, *args, **options):
        created_count = 0
        skipped_count = 0

        for svc_data in SERVICES:
            name = svc_data["name"]
            if Service.objects.filter(name=name).exists():
                self.stdout.write(f"  SKIP (exists): {name}")
                skipped_count += 1
                continue

            slug = slugify(name)
            # Ensure unique slug
            base_slug = slug
            i = 2
            while Service.objects.filter(slug=slug).exists():
                slug = f"{base_slug}-{i}"
                i += 1

            service = Service.objects.create(
                category_id=CATEGORY_ID,
                name=name,
                slug=slug,
                description=svc_data["description"],
                body=f"<p>{svc_data['body']}</p>",
                is_active=True,
                is_rot_eligible=True,
            )

            for order, (title, desc) in enumerate(svc_data["steps"]):
                ServiceStep.objects.create(
                    service=service,
                    title=title,
                    description=desc,
                    order=order,
                )

            self.stdout.write(
                self.style.SUCCESS(f"  CREATED: {name} ({len(svc_data['steps'])} steps)")
            )
            created_count += 1

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(f"Done. Created {created_count}, skipped {skipped_count}.")
        )
