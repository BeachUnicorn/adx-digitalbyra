"""
Leverantörernas namn i det kunder och besökare ser, ur databasen. Bara
läsning: kommandot skriver ingenting.

    manage.py leverantorsnamn

Koden hålls ren av vakten (apps/common/test_leverantorer.py), men texter som
byrån eller assistenten skrivit och innehåll som seedats ligger i databasen:
kundloggen, kundsynliga ärenden och svar, anteckningen på statussidan,
offerterna och produkterna, den publika sajtens block och Flamingos sidor.
Varje träff skrivs ut med modell, id, fält och ett utdrag; vad som ska
skrivas om avgör byrån (Giovanni 2026-10-10).

Biträdesavtalet (utskick.DpaVersion) listas också, märkt "juridisk text":
där står personuppgiftsbiträdena för att GDPR art. 28 kräver det, och
texten ändras bara av Giovanni, aldrig av koden. Integritetspolicyn på
adx.se är en sida bland website.Block.
"""

import json

from django.apps import apps
from django.core.management.base import BaseCommand

from apps.common.providers import label, names_in

#: (modell, filter, fälten). Bara rader som kunden eller besökaren ser.
SOURCES = (
    ("projects.CustomerLogEntry", {}, ("text",)),
    ("projects.Issue", {"visible_to_customer": True}, ("title", "description")),
    ("projects.Comment", {"is_internal": False}, ("body",)),
    ("monitor.MonitorSettings", {}, ("note",)),
    ("offers.Product", {}, ("name", "description")),
    ("offers.OfferText", {}, ("name", "text")),
    ("offers.Quote", {}, ("project_title", "intro", "includes", "terms")),
    ("offers.QuoteLine", {}, ("label", "description")),
    ("website.Block", {}, ("data",)),
    ("flamingo.LandingPage", {}, ("draft", "published")),
    ("utskick.DpaVersion", {}, ("text",)),
)
#: Juridiska texter: listas, men ska inte skrivas om utan beslut.
LEGAL = {"utskick.DpaVersion"}


def _text(value):
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False)
    return str(value or "")


def _excerpt(text, width=100):
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width] + " (...)"


class Command(BaseCommand):
    help = "Listar leverantörsnamn i kundsynliga rader i databasen. Skriver ingenting."

    def handle(self, *args, **options):
        found = 0
        for model_label, filters, fields in SOURCES:
            try:
                model = apps.get_model(model_label)
            except LookupError:
                continue
            for row in model.objects.filter(**filters).only("pk", *fields).iterator():
                for field in fields:
                    text = _text(getattr(row, field, ""))
                    names = names_in(text, strict=True)
                    if names:
                        found += 1
                        legal = " (juridisk text)" if model_label in LEGAL else ""
                        self.stdout.write(
                            f"{model_label} {row.pk} {field}{legal}: {label(names)}: "
                            f"{_excerpt(text)}"
                        )
        self.stdout.write(f"{found} träff{'ar' if found != 1 else ''}.")
