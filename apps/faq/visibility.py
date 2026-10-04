"""
Vilka FAQ-sektioner som får synas på den publika sajten.

En aktiv ADX-sektion är publik på /faq/ och /faq/<slug>/, i sitemapen och
därmed i 404-förslagen. En sektion med designen ADX Flamingo är det aldrig:
den visas bara i FAQ-block på Flamingos sidor (/flamingo/, apps/flamingo),
som är öppna sedan 2026-10-04.
"""


def public_sections():
    """Aktiva ADX-sektioner (FAQ-sidorna och sitemapen)."""
    from .models import FAQSection

    return FAQSection.objects.filter(is_active=True, design="")


def sections_for_design(design):
    """Sektionerna ett FAQ-block på en sida med given design får visa:
    ADX-sidor bara ADX-sektioner, Flamingo-sidor båda."""
    from .models import FAQSection

    designs = {"", design or ""}
    return FAQSection.objects.filter(design__in=designs)
