"""
Integritetspolicyn, användarvillkoren och sms-villkoren namnger inte ADX:s
leverantörer.

Giovanni 2026-10-11: "Unless absolutely necessary, you DO NOT just hand away
information about my infrastructure partners and choices." GDPR art. 13.1 e
tillåter kategorier av mottagare, så sidorna säger "en leverantör av
molndrift", "en sms-leverantör" och så vidare. Bara biträdesavtalet
(/bitradesavtal/ och DpaVersion) namnger underbiträdena, eftersom kunden
enligt art. 28 ska kunna se och invända mot dem.

Sidorna skapades i drift och finns inte i seed-data. Migrationen byter bara
exakt de fraserna, i prose-blocken på de tre sidorna, och är idempotent;
en fras som redan ändrats för hand lämnas.
"""

from django.db import migrations

PAGES = ("integritetspolicy", "anvandarvillkor", "sms-villkor")
REPLACEMENTS = (
    (
        "<li>Amazon Web Services: server och databas i Stockholm, utskick av e-post och "
        "mottagning av svar i Irland.</li>",
        "<li>En leverantör av molndrift: server och databas i Stockholm, utskick av e-post "
        "och mottagning av svar i Irland.</li>",
    ),
    (
        "<li>Sentry: felrapporter när något går fel i koden.</li>",
        "<li>En tjänst för felrapporter när något går fel i koden.</li>",
    ),
    (
        "<li>46elks: utskick och mottagning av sms.</li>",
        "<li>En sms-leverantör: utskick och mottagning av sms.</li>",
    ),
    (
        "<li>Anthropic: AI-tjänst i våra interna arbetsverktyg.</li>",
        "<li>En AI-tjänst i våra interna arbetsverktyg.</li>",
    ),
    ("Google Business Profile och 46elks.", "Google Business Profile och en sms-leverantör."),
    ("Sms:en skickas via vår leverantör 46elks.", "Sms:en skickas via vår sms-leverantör."),
    (
        "Messages are sent through our provider 46elks.",
        "Messages are sent through our SMS provider.",
    ),
)


def _clean(value):
    if isinstance(value, str):
        for old, new in REPLACEMENTS:
            value = value.replace(old, new)
        return value
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def forwards(apps, schema_editor):
    Block = apps.get_model("website", "Block")
    for block in Block.objects.filter(page__slug__in=PAGES, block_type="prose"):
        data = _clean(block.data)
        if data != block.data:
            block.data = data
            block.save(update_fields=["data"])


class Migration(migrations.Migration):
    dependencies = [("website", "0020_inga_leverantorsnamn")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
