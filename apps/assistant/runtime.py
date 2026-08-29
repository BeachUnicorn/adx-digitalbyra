"""
Den delade körkärnan: ett verktygsanrop, oavsett varifrån.

MCP-servern och den inbyggda chatten är två framsidor på samma motor. Det
här är motorn: verktygslistan byggs ur operationsregistret, läsoperationer
körs direkt, och skrivoperationer blir utkast som kunden godkänner.

Att båda vägarna delar den här filen är hela poängen med registret - en ny
operation tänds i chatten och i MCP samtidigt, och säkerhetsgränsen kan
inte glömmas bort i den ena.
"""

import json
import logging

from django.conf import settings
from django.core.cache import cache

from .models import AIJob, DraftChange, Risk
from .operations import REGISTRY, OperationError

logger = logging.getLogger(__name__)

#: Anrop per användare och timme. Ett generöst tak som ändå stoppar en
#: modell som fastnat i en loop.
RATE_LIMIT_PER_HOUR = 300

#: Väntande utkast per jobb. Skyddar databasen från samma sorts loop.
MAX_PENDING_PER_JOB = 100

#: Läsverktygen presenteras först, så modellen ser "hämta först"-verktygen
#: innan skrivverktygen.
_RISK_ORDER = {Risk.READ: 0, Risk.TEXT: 1, Risk.BUSINESS: 2}


def feature_enabled(feature):
    """Är modulen påslagen? Operationer utan modul är alltid på."""
    from django.conf import settings

    if not feature:
        return True
    return bool(getattr(settings, "ASSISTANT_FEATURES", {}).get(feature, False))


def available_operations():
    """
    Operationerna AI:n faktiskt får använda just nu.

    Avstängda moduler filtreras bort HÄR, i den delade kärnan, så att MCP,
    stdio och den inbyggda chatten alla ser samma yta. En modul som är
    avstängd finns inte i verktygslistan - modellen kan inte sakna det den
    aldrig sett, och slipper föreslå något kunden inte har.
    """
    return [op for op in REGISTRY.values() if feature_enabled(op.feature)]


def tool_descriptions():
    """Operationsregistret som (namn, beskrivning, schema, läsbar) i visningsordning."""
    for op in sorted(available_operations(), key=lambda o: (_RISK_ORDER[o.risk], o.name)):
        description = op.description
        if op.risk == Risk.TEXT:
            description += "\n\nSkapar ett utkast som kunden godkänner."
        elif op.risk == Risk.BUSINESS:
            description += (
                "\n\nPåverkar affärsdata eller vad som syns publikt. Skapar ett "
                "utkast som kunden godkänner separat, en i taget."
            )
        yield op.name, description, op.input_schema, op.risk == Risk.READ


def review_url(job):
    base = (getattr(settings, "SITE_BASE_URL", "") or "").rstrip("/")
    return f"{base}{job.get_absolute_url()}"


def draft_url(change):
    """
    Djuplänk till ETT förslag, inte till listan.

    Över MCP är länken hela vägen tillbaka till kunden - hen sitter i
    Claude-appen och ska inte behöva leta rätt på kortet bland tjugo
    andra. Ankaret hoppar till kortet och markerar det.
    """
    base = (getattr(settings, "SITE_BASE_URL", "") or "").rstrip("/")
    return f"{base}{change.job.get_absolute_url()}#utkast-{change.pk}"


def check_rate_limit(user):
    key = f"assistant:rate:{user.pk}"
    count = cache.get_or_set(key, 0, timeout=3600)
    if count >= RATE_LIMIT_PER_HOUR:
        raise OperationError("Gränsen för antal anrop per timme är nådd. Försök igen senare.")
    try:
        cache.incr(key)
    except ValueError:  # nyckeln hann gå ut mellan get_or_set och incr
        cache.set(key, 1, timeout=3600)


def job_for_session(user, session_key):
    """
    Ett AIJob per session, så ett uppdrag kan granskas och ångras samlat.

    Utan sessionsnyckel (stateless klient) faller vi tillbaka på ett jobb
    per användare och dag - hellre en grov gruppering än en rad per anrop.
    """
    from django.utils import timezone

    key = session_key or f"dag-{timezone.now():%Y-%m-%d}"
    job, _created = AIJob.objects.get_or_create(
        user=user,
        session_key=key[:128],
        status=AIJob.Status.OPEN,
        defaults={"title": f"AI-session {timezone.now():%-d %b %H:%M}"},
    )
    return job


def run_operation(user, job_getter, name, arguments):
    """
    Kör en operation och returnera text åt modellen.

    `job_getter` är en funktion som ger jobbet att hänga utkast på - den
    anropas bara när ett utkast faktiskt ska skapas, så en ren läs-session
    inte lämnar tomma jobb efter sig.
    """
    from .draft import propose

    op = REGISTRY.get(name)
    if op is None:
        raise OperationError(f"Okänt verktyg: {name}")
    # Andra spärren: en avstängd modul syns inte i verktygslistan, men en
    # klient kan anropa vad som helst över MCP - filtrering i listan är
    # inte en behörighetskontroll.
    if not feature_enabled(op.feature):
        raise OperationError(f"Verktyget {name} är inte aktiverat för den här webbplatsen.")

    check_rate_limit(user)

    if op.risk == Risk.READ:
        return json.dumps(op.read(user, **(arguments or {})), ensure_ascii=False, default=str)

    job = job_getter()
    pending = job.changes.filter(status=DraftChange.Status.PENDING).count()
    if pending >= MAX_PENDING_PER_JOB:
        raise OperationError(
            f"Det finns redan {pending} utkast som väntar på godkännande. "
            f"Be kunden granska dem innan du föreslår fler."
        )

    change = propose(job, name, arguments or {})
    job.log_call(name, change.summary)

    return json.dumps(
        {
            "status": "utkast_skapat",
            "utkast_id": change.pk,
            "sammanfattning": change.summary,
            "riskklass": change.get_risk_display(),
            "publicerat": False,
            "granska": draft_url(change),
            "granska_alla": review_url(job),
            "not": (
                "Ändringen är INTE publicerad. Avsluta med länken i 'granska' - "
                "den öppnar just det här förslaget hos kunden."
            ),
        },
        ensure_ascii=False,
        default=str,
    )


#: Läsbara etiketter för operationerna. Chatten visade tidigare råa
#: funktionsnamn ("uppdatera_tjanst_text"), vilket läser som felsökning och
#: inte som en assistent som arbetar. Saknas en etikett faller visningen
#: tillbaka på namnet - fult men aldrig trasigt. `test_every_operation_has_a
#: _label` ser till att nya operationer inte glöms bort.
STEP_LABELS = {
    "lista_omraden": "Listar områden",
    "hamta_omrade": "Läser område",
    "uppdatera_omrade_text": "Skriver om områdestext",
    "skapa_omrade": "Skapar område",
    "satt_omrade_aktiv": "Ändrar områdets synlighet",
    "hamta_skrivguide": "Läser skrivguiden",
    "lista_utkast": "Listar utkast",
    "hamta_statistik": "Hämtar besöksstatistik",
    "lista_faq": "Listar FAQ",
    "skapa_faq_fraga": "Skapar FAQ-fråga",
    "uppdatera_faq_fraga": "Skriver om FAQ-fråga",
    "skapa_faq_sektion": "Skapar FAQ-sektion",
    "lista_sidor": "Listar sidor",
    "hamta_sida": "Läser sida",
    "uppdatera_block": "Skriver om textblock",
    "uppdatera_sidmeta": "Uppdaterar sidans metatexter",
    "skapa_sida": "Skapar sida",
    "skapa_block": "Skapar textblock",
    "lista_tjanster": "Listar tjänster",
    "hamta_tjanst": "Läser tjänst",
    "uppdatera_tjanst_text": "Skriver om tjänstetext",
    "satt_tjanst_steg": "Sätter arbetsgång",
    "satt_tjanst_aktiv": "Ändrar tjänstens synlighet",
    "skapa_tjanst": "Skapar tjänst",
    "koppla_faq_till_omrade": "Kopplar FAQ till område",
    "koppla_faq_till_tjanst": "Kopplar FAQ till tjänst",
    "dra_tillbaka_utkast": "Drar tillbaka förslag",
    "uppdatera_faq_sektion": "Ändrar FAQ-sektion",
    "satt_faq_sektion_aktiv": "Ändrar FAQ-sektionens synlighet",
    "satt_sida_publicerad": "Ändrar sidans publicering",
    "satt_grannomraden": "Sätter grannområden",
    "hamta_blockkatalog": "Läser blockkatalogen",
    "ordna_block": "Lägger om blockordningen",
    "satt_block_synligt": "Ändrar blockets synlighet",
}


def step_label(name):
    """Läsbar etikett för en operation, eller namnet om den saknas."""
    return STEP_LABELS.get(name, name)
