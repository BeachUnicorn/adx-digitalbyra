"""
Modellklienten: ett anrop, alltid mätt och alltid under tak.

Servern kör i AWS, så modellen går via Bedrock med instansrollen - inga
API-nycklar att distribuera eller rotera. Den direkta Anthropic-vägen finns
kvar som reservläge för utveckling på en maskin utan AWS-uppgifter.

Skillnaden mot MCP-vägen är vem som betalar. Där står kundens eget
Claude-abonnemang för notan; här står vi för den. Därför två saker som inte
är valfria:

* Varje anrop skriver en ``AICall``-rad med tokens och verklig kostnad.
  Utan den syns aldrig marginalen, och en dyr vana upptäcks först på
  fakturan.
* Ett dygnstak kontrolleras FÖRE anropet. En modell som fastnat i en loop,
  eller en bugg hos oss, ska inte kunna kosta obegränsat.

Kostnaden lagras i mikrodollar (heltal): priserna är satta i USD, och att
växla vid varje anrop skulle baka in en kurs som drar iväg.
"""

import logging
import re
import time

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

#: USD per miljon tokens (in, ut). Bedrock är partnerprissatt och ligger
#: något över förstapartspriset - därför två tabeller.
#: Sonnet 4.6 är verifierad mot ADX konto 200810847648 (2026-08-21)
#: och priset delas med adx-projektet. Opus 4.6 svarar men har inget
#: verifierat pris - den faller därför på DEFAULT_PRICE, alltså för högt
#: hellre än för lågt: att gissa ett pris nedåt gör mätaren till en lögn.
BEDROCK_PRICES = {
    "eu.anthropic.claude-sonnet-4-6": (3.5, 17.0),
}
DIRECT_PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
}
DEFAULT_PRICE = (5.0, 25.0)

#: Kundens regel, 2026-08-21: bara Sonnet eller Opus från 4.6 och uppåt,
#: aldrig Haiku. Skälet är innehållskvalitet - adx såg Haiku producera
#: "fyllare dagar"-copy (2026-07-26), och här skriver modellen text som
#: går rakt ut till kundens besökare.
#: Regeln sitter som en spärr och inte bara som ett defaultvärde: en
#: felsatt miljövariabel ska stoppa anropet, inte tyst byta modell.
MIN_FAMILY_VERSION = 4.6
ALLOWED_FAMILIES = ("sonnet", "opus")


def assert_model_allowed(model):
    """
    Höj ModelUnavailable om modellen bryter mot kundens regel.

    Datumstämplar och revisionssuffix rensas först. Utan det läses
    "claude-3-sonnet-20240229" som version 20240229 och en av de äldsta
    modellerna skulle passera som den nyaste.
    """
    name = model.rsplit(".", 1)[-1].lower()
    name = re.sub(r"-v\d+(:\d+)?$", "", name)
    name = re.sub(r"-\d{8}", "", name)

    family = next((f for f in ALLOWED_FAMILIES if f in name), None)
    if family is None:
        raise ModelUnavailable(
            f"Modellen {model} är inte tillåten. Bara Sonnet och Opus får "
            f"användas - aldrig Haiku - eftersom texten går ut till besökare."
        )

    # Två namnskick: "claude-sonnet-4-6" (nytt) och "claude-3-sonnet"
    # (Claude 3, där siffran står FÖRE familjen).
    match = re.search(rf"{family}-(\d+)(?:-(\d+))?", name) or re.search(
        rf"(\d+)(?:-(\d+))?-{family}", name
    )
    if match is None:
        raise ModelUnavailable(
            f"Kan inte utläsa versionen ur {model}. Tillåtna namn ser ut som "
            f"'eu.anthropic.claude-sonnet-4-6'."
        )
    version = float(f"{match.group(1)}.{match.group(2) or 0}")
    if version < MIN_FAMILY_VERSION:
        raise ModelUnavailable(
            f"Modellen {model} är version {version:g}; lägsta tillåtna är {MIN_FAMILY_VERSION:g}."
        )


class BudgetExceeded(Exception):
    """Dygnstaket är nått - anropet gjordes inte, det avvisades."""


class ModelUnavailable(Exception):
    """Leverantören gick inte att nå, eller är inte konfigurerad."""


def provider():
    return getattr(settings, "ASSISTANT_PROVIDER", "bedrock")


def model_id():
    if provider() == "bedrock":
        return getattr(settings, "ASSISTANT_BEDROCK_MODEL", "eu.anthropic.claude-sonnet-4-6")
    return getattr(settings, "ASSISTANT_MODEL", "claude-opus-5")


def model_label():
    """
    Kort, läsbart modellnamn för gränssnittet: "Opus 4.6".

    Kunden ska se vilken modell som arbetar utan att möta
    "eu.anthropic.claude-opus-4-6-v1". Hela id:t visas som title-attribut
    där det behövs för felsökning.
    """
    name = model_id().rsplit(".", 1)[-1].lower()
    name = re.sub(r"-\d{8}", "", name)  # datumstämpeln är inte en version
    match = re.search(r"(sonnet|opus|haiku|fable)-(\d+)(?:-(\d+))?", name) or re.search(
        r"(\d+)()-(sonnet|opus|haiku|fable)", name
    )
    if match is None:
        return model_id()
    parts = match.groups()
    family = next(p for p in parts if p and not p.isdigit())
    nums = [p for p in parts if p and p.isdigit()]
    version = ".".join(nums[:2]) if len(nums) > 1 else (nums[0] if nums else "")
    return f"{family.title()} {version}".strip()


def cost_micros(model, tokens_in, tokens_out, cached_in=0):
    """Kostnad i mikrodollar. Cachade tokens kostar ~10 % av in-priset."""
    table = BEDROCK_PRICES if model.startswith(("eu.", "us.", "anthropic.")) else DIRECT_PRICES
    price_in, price_out = table.get(model, DEFAULT_PRICE)
    usd = (
        tokens_in * price_in / 1_000_000
        + cached_in * price_in * 0.1 / 1_000_000
        + tokens_out * price_out / 1_000_000
    )
    return round(usd * 1_000_000)


def spent_today():
    from .models import AICall

    since = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return AICall.objects.filter(created_at__gte=since).aggregate(s=Sum("cost_micros"))["s"] or 0


def check_budget():
    """Höj BudgetExceeded om dygnstaket är nått. Körs före varje anrop."""
    limit_usd = float(getattr(settings, "ASSISTANT_DAILY_BUDGET_USD", 5.0))
    if spent_today() >= limit_usd * 1_000_000:
        raise BudgetExceeded(
            f"Dygnets AI-budget ({limit_usd:.2f} USD) är förbrukad. "
            f"Assistenten är pausad till imorgon."
        )


def client():
    """
    Bedrock via instansrollen i produktion, CLI-profilen lokalt.

    Ingen nyckel skickas med: boto3:s vanliga kedja hittar rollen på EC2 och
    ~/.aws lokalt. Det är hela poängen med att gå via Bedrock - det finns
    ingen hemlighet att läcka eller rotera.
    """
    if provider() == "bedrock":
        from anthropic import AnthropicBedrock

        return AnthropicBedrock(
            aws_region=getattr(settings, "ASSISTANT_BEDROCK_REGION", "eu-central-1"),
            aws_profile=getattr(settings, "ASSISTANT_AWS_PROFILE", "") or None,
        )

    key = getattr(settings, "ANTHROPIC_API_KEY", "") or ""
    if not key:
        raise ModelUnavailable("ANTHROPIC_API_KEY saknas - den inbyggda assistenten är avstängd.")
    import anthropic

    return anthropic.Anthropic(api_key=key)


def is_configured():
    """Går assistenten att använda alls? Styr om chatten visas som avstängd."""
    if provider() == "bedrock":
        return True  # instansrollen avgör; ett faktiskt fel syns vid anropet
    return bool(getattr(settings, "ANTHROPIC_API_KEY", ""))


def _friendly(exc):
    """
    Översätt de fel som faktiskt inträffar till något handlingsbart.

    Modellåtkomst via Marketplace är den som bet Atlas-projektet
    2026-07-08: utan aws-marketplace-rättigheterna får rollen "Model access
    is denied" vid FÖRSTA anropet mot en ny modell, och felet säger inget
    om IAM.
    """
    text = str(exc)
    if "marketplace" in text.lower() or "Model access is denied" in text:
        return (
            "Bedrock nekar åtkomst till modellen. Serverrollen behöver "
            "bedrock:InvokeModel samt aws-marketplace:ViewSubscriptions och "
            "aws-marketplace:Subscribe, och modellen måste vara aktiverad i "
            "regionen."
        )
    # Engångssteg per AWS-konto, inte per roll: utan formuläret svarar
    # Bedrock "not have access to this operation" och hänvisar till
    # aws-verification@amazon.com - inget som pekar mot rätt åtgärd. Den
    # riktiga texten syns bara i svarskroppen, alltså med --debug.
    if "use case details" in text.lower():
        return (
            "Bedrock kräver att formuläret om användningsfall för Anthropics "
            "modeller fylls i en gång per AWS-konto. Gör det i konsolen under "
            "Bedrock -> Model access, och försök igen om ca 15 minuter."
        )
    # credential_process som misslyckas ger tom utdata, och botocore
    # rapporterar bara att JSON inte gick att tolka. Vanligaste orsaken är
    # att 1Password hunnit låsa sig - det såg ut som ett Bedrock-fel tre
    # gånger under uppsättningen 2026-08-21.
    if "Expecting value" in text:
        return (
            "AWS-uppgifterna kunde inte hämtas. Profilen använder "
            "credential_process mot 1Password - lås upp appen och försök igen. "
            "Kör kommandot i credential_process manuellt för det verkliga felet."
        )
    if "credential" in text.lower() or "NoCredentials" in text:
        return (
            "Inga AWS-uppgifter hittades. På servern ska instansrollen räcka; "
            "lokalt behövs en profil i ~/.aws (sätt ASSISTANT_AWS_PROFILE)."
        )
    if "ValidationException" in text and "model" in text.lower():
        return (
            f"Bedrock känner inte igen modell-id:t ({model_id()}). "
            f"EU-profilerna heter 'eu.anthropic.…' och måste finnas i regionen."
        )
    return text


def call(*, system, messages, tools, user, job=None, max_tokens=8000):
    """
    Ett modellanrop. Returnerar svaret och bokför kostnaden.

    Budgetvakten körs före anropet, aldrig efter: poängen är att inte göra
    det dyra anropet, inte att upptäcka i efterhand att det var dyrt.
    """
    from .models import AICall

    assert_model_allowed(model_id())
    check_budget()

    started = time.monotonic()
    model = model_id()
    try:
        response = client().messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
        )
    except Exception as exc:  # noqa: BLE001
        AICall.objects.create(
            user=user,
            job=job,
            model=model,
            ok=False,
            error=f"{type(exc).__name__}: {exc}"[:500],
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        logger.exception("Modellanropet misslyckades")
        raise ModelUnavailable(_friendly(exc)) from exc

    usage = response.usage
    cached = getattr(usage, "cache_read_input_tokens", 0) or 0
    AICall.objects.create(
        user=user,
        job=job,
        model=model,
        tokens_in=usage.input_tokens,
        tokens_out=usage.output_tokens,
        tokens_cached=cached,
        cost_micros=cost_micros(model, usage.input_tokens, usage.output_tokens, cached),
        stop_reason=response.stop_reason or "",
        duration_ms=int((time.monotonic() - started) * 1000),
        ok=True,
    )
    return response
