"""
Publiceringen med Google Ads API (kundresan steg 8): kampanjen skapas i
kundens Google Ads-konto med ett klick i /manage/flamingo/, och pausas och
återupptas där.

    api_available(account)          API-vägen gäller kontot: inkopplat och
                                    inte ett demokonto
    uses_api(campaign)              kampanjen skapades (eller togs över) med
                                    API:t, så paus och återupptagning går dit
    build_operations(campaign, id)  ändringarna i anropet (ren funktion)
    go_live(campaign, user)         skapa hos Google, sedan live här
    publish_approved(campaign, user)  kundens godkännande: live direkt när
                                    det går, annars byråns kö (Outcome)
    pause(campaign), resume(campaign)
    panel(campaign)                 publiceringspanelens läge (mallen)

manage_review.publish går den här vägen när google_ads.is_configured() och
kontot inte är ett demokonto. Annars publicerar byrån för hand med
Editor-filen (exports.py), precis som innan API:t fanns. En kampanj som
publicerades för hand pausas och återupptas också för hand.

Kundens godkännande (app_views/campaigns.py) publicerar direkt med
publish_approved: när kunden skickar in utan att be om granskning, och när
kunden godkänner det ADX granskat (beslut 2026-10-03, granskningen är
kundens val). Går det inte (API:t är inte inkopplat, kontot är inte kopplat
under ADX, Google eller kontrollerna säger nej) står kampanjen kvar som
godkänd men inte publicerad, med orsaken i google_error, och byrån
publicerar från kön.

go_live, i ordning:

1. Kontrollerna (checks.validate, med landningssidan) ska vara tomma,
   kontot kopplat under ADX med ett id och området ska ge minst en ort.
   Betalningen hos Google krävs inte: annonserna visas först när kunden
   lagt in den, och ADX ligger inte ute med några pengar (beslut
   2026-10-03). En landningssida som aldrig publicerats publiceras nu
   (pagebuilder.publish_for_campaign).
2. Spärren (Campaign.claim_google_publish) tas med en egen UPDATE som
   sparas direkt, utanför transaktionen nedan. Den finns kvar om processen
   dör mitt i ett anrop, och säger då åt nästa försök att leta först.
3. Kampanjraden låses (select_for_update, nowait) medan Google anropas. Ett
   andra klick samtidigt får "pågår redan", och ett klick efteråt ser att
   kampanjen redan är live.
4. Kontots valuta måste vara SEK (budgeten räknas i kronor), och automatisk
   taggning slås på så att klickets gclid når landningssidan.
5. Var spärren redan satt (ett tidigare försök som inte vet hur det gick, eller
   ett klick samtidigt) letas kampanjen upp hos Google på sitt namn (det
   försöket skickade och det nuvarande) och tas över i stället för att skapas
   en gång till, men bara om innehållet är detsamma som det försöket
   skickade (google_publish_sent, en hash av hela anropet). Har kampanjen
   ändrats sedan dess tas den inte över: den gamla pausas hos Google och
   byrån tar bort den där innan den publicerar igen. Finns inget hos Google
   släpps spärren och försöket börjar om.
6. Allt skapas i ett googleAds:mutate, allt eller inget. Tillfälliga
   resursnamn med negativa id knyter ihop budgeten, kampanjen och
   annonsgruppen.
7. Först när Google svarat sparas resursnamnen, och kampanjen blir live med
   published_at. Då öppnas landningssidan.

Ett fel från Google ändrar inte kampanjens status: felet sparas i
Campaign.google_error och byrån får en svensk text (PublishError). Spärren
släpps när det är säkert att inget skapades (Google sa nej till hela
anropet, eller det gjordes aldrig). Kom inget svar ligger den kvar, så att
nästa försök letar innan det skapar, och felet säger att kampanjen kan vara
igång hos Google medan landningssidan är stängd.

Ett Google Ads-id som ett annat Flamingo-konto har publiceras aldrig
(FlamingoAccount.google_id_shared).

Kampanjen hos Google:

- Namnet "Flamingo: <namn> #<pk>": unikt, och det ett nytt försök letar efter.
- Budget per dag i mikros (kronor gånger en miljon), standardleverans, inte delad.
- Sök, bara Google sök (inte sökpartner eller display), Maximera klick
  (targetSpend), platsinriktning på närvaro, svenska.
- En radie per ort ur området (generator.places_of), campaign.radius_km km.
- De negativa sökorden på kampanjen, sökorden i en annonsgrupp och en
  responsiv sökannons till landningssidan, med samma texter och
  matchningstyper som Editor-filen (exports.keyword_rows och negative_rows).
- Deklarationen om politisk reklam i EU: innehåller ingen.

Kunden mejlas aldrig härifrån. Demokonton anropar aldrig Google.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import timedelta

from django.db import DatabaseError, transaction
from django.utils import timezone

from . import checks, exports, generator, google_ads, limits, pagebuilder
from .models import DESCRIPTION_COUNT, HEADLINE_COUNT, Campaign, FlamingoAccount, Review

logger = logging.getLogger(__name__)

NAME_PREFIX = "Flamingo: "
#: Svenska bland Googles språk (languageConstants, "sv").
LANGUAGE_SWEDISH = "languageConstants/1015"
COUNTRY_CODE = "SE"
CURRENCY = "SEK"
#: Tillfälliga id i anropet: knyter ihop det som skapas i samma mutate.
TEMP_BUDGET_ID = -1
TEMP_CAMPAIGN_ID = -2
TEMP_AD_GROUP_ID = -3
#: Betalningen är klar hos Google (billing_setup.status).
BILLING_APPROVED = "APPROVED"
ERROR_MAX = 500

MSG_DEMO = "Demokontot publiceras aldrig hos Google. Markera det som live för hand."
MSG_NOT_LINKED = (
    "Google-kontot är inte kopplat under ADX med ett id (tio siffror). Ändra det på "
    "kundkortet när det är gjort."
)
MSG_NO_PLACES = (
    "Området går inte att läsa som orter, och utan en ort skulle annonserna visas i hela "
    "världen. Rätta området i granskningen."
)
MSG_BUSY = (
    "Någon annan publicerar eller ändrar kampanjen just nu. Ladda om sidan om en stund och "
    "se om den redan är live."
)
MSG_PAUSED = "Kampanjen är pausad: använd Återuppta."
MSG_NOT_APPROVED = "Kunden har inte godkänt den granskade versionen."
MSG_PENDING = "En ny runda väntar på granskning."
MSG_NO_CAMPAIGN = (
    "Google svarade utan kampanjens id. Ladda om sidan om en stund: nästa försök letar "
    "upp kampanjen hos Google innan något skapas igen."
)
MSG_SHARED_ID = (
    "Google Ads-id:t finns också på ett annat Flamingo-konto, så inget publicerades. Rätta "
    "id:t på kundkortet."
)
#: Efter ett anrop som inte fick ett tydligt svar (ingen kontakt, Googles fel 5xx).
MSG_NO_ANSWER = (
    "Det är oklart om kampanjen skapades: den kan redan vara igång hos Google medan "
    "landningssidan är stängd. Publicera igen från granskningssidan; försöket letar upp "
    "kampanjen hos Google och tar över den."
)
MSG_STALE = (
    'Ett tidigare försök som inte fick svar skapade kampanjen "{name}" hos Google med '
    "innehållet som gällde då. Kampanjen har ändrats sedan dess, så den togs inte över{paused}. "
    "Ta bort den gamla kampanjen i Google Ads och publicera igen."
)
#: Ett nytt inskick strax efter ett misslyckat försök anropar inte Google.
RETRY_AFTER = timedelta(minutes=15)
MSG_COOLDOWN = (
    "Förra försöket hos Google misslyckades nyss, så Google anropades inte igen. Publicera "
    "från granskningssidan när felet är rättat."
)
MSG_DAILY_LIMIT = (
    "Kontot har nått dagens gräns för publiceringar hos Google efter inskick, så Google "
    "anropades inte. Publicera från granskningssidan."
)
#: Varför panelen visar vägen för hand.
MSG_MANUAL_DEMO = "Demokontot publiceras aldrig hos Google."
MSG_MANUAL_NOT_CONFIGURED = "Google Ads API är inte inkopplat."
MSG_MANUAL_PUBLISHED = (
    "Kampanjen publicerades för hand, så den pausas och återupptas för hand i Google Ads."
)
MSG_BILLING = "Annonserna visas först när kunden lagt in betalning i sitt Google Ads-konto."

#: Vad varje sorts ändring heter när Google säger nej till just den.
_OPERATION_LABELS = {
    "campaignBudgetOperation": "budgeten",
    "campaignOperation": "kampanjen",
    "adGroupOperation": "annonsgruppen",
    "adGroupAdOperation": "annonsen",
}
#: Svarets nycklar i mutateOperationResponses och var de sparas.
_RESULT_KEYS = {
    "campaignBudgetResult": "budget",
    "campaignResult": "campaign",
    "adGroupResult": "ad_group",
    "adGroupAdResult": "ad",
}
_CRITERION_RESULTS = ("campaignCriterionResult", "adGroupCriterionResult")


class PublishError(Exception):
    """Varför inget publicerades, pausades eller återupptogs, på svenska för
    byrån. Innehåller aldrig en nyckel: texter från Google kommer ur
    GoogleAdsError.message, som redan är tvättad. record: texten sparas också
    som kampanjens google_error (läget hos Google, inte ett klick i fel läge)."""

    def __init__(self, message, *, google_error=None, record=False):
        self.message = str(message)
        self.google_error = google_error
        self.record = record
        super().__init__(self.message)

    def __str__(self):
        return self.message


#: Vad publish_approved gjorde (Outcome.kind).
OUTCOME_LIVE = "live"
OUTCOME_FAILED = "failed"
OUTCOME_NOT_LINKED = "not_linked"
OUTCOME_MANUAL = "manual"
OUTCOME_DEMO = "demo"
#: Ett oväntat fel i publiceringen efter kundens godkännande.
MSG_UNEXPECTED = "Publiceringen avbröts av ett oväntat fel. Publicera från granskningssidan."


@dataclass(frozen=True)
class Outcome:
    """Vad som hände efter kundens godkännande (publish_approved)."""

    #: OUTCOME_LIVE, OUTCOME_FAILED, OUTCOME_NOT_LINKED, OUTCOME_MANUAL
    #: eller OUTCOME_DEMO.
    kind: str
    #: Varför den inte publicerades, på svenska för byrån ("" när live).
    reason: str = ""
    #: Kampanjens id hos Google när den blev live.
    campaign_id: str = ""

    @property
    def is_live(self):
        return self.kind == OUTCOME_LIVE


@dataclass(frozen=True)
class PublishResult:
    #: Kampanjens id hos Google (siffror).
    campaign_id: str
    #: Kampanjen fanns redan hos Google (ett tidigare försök) och togs över.
    adopted: bool = False
    #: Kampanjen var redan live: inget gjordes.
    already_live: bool = False
    #: Automatisk taggning slogs på i kundens konto nu.
    auto_tagging_enabled: bool = False


# ---------------------------------------------------------------------------
# Vilken väg
# ---------------------------------------------------------------------------


def api_available(account):
    """Publiceringen går via Google Ads API för kontot: API:t är inkopplat
    och kontot är inte ett demokonto."""
    return not account.is_demo and google_ads.is_configured()


def linked_with_id(account):
    """Kontot ligger under ADX förvaltarkonto och har ett eget id (tio
    siffror, inget annat Flamingo-konto har det): det go_live kräver av
    kontot."""
    return (
        account.google_linked
        and len(google_ads.digits(account.google_ads_customer_id)) == 10
        and not account.google_id_shared
    )


def approval_path(account):
    """Vad kundens godkännande leder till för kontot, innan något görs:
    OUTCOME_LIVE (publiceras direkt med API:t), OUTCOME_NOT_LINKED (väntar
    på att kontot kopplas under ADX), OUTCOME_MANUAL (byrån publicerar för
    hand) eller OUTCOME_DEMO. Kampanjsidan säger det vid knappen."""
    if account.is_demo:
        return OUTCOME_DEMO
    if not linked_with_id(account):
        return OUTCOME_NOT_LINKED
    if not google_ads.is_configured():
        return OUTCOME_MANUAL
    return OUTCOME_LIVE


def _campaign_resource(campaign):
    resources = campaign.google_resources if isinstance(campaign.google_resources, dict) else {}
    return str(resources.get("campaign") or "")


def uses_api(campaign):
    """Paus och återupptagning går via Google: kampanjen skapades eller togs
    över med API:t (resursnamnet finns) och API:t är fortfarande inkopplat.
    Ett id som byrån skrev in för hand räknas inte: då pausar byrån själv."""
    return bool(_campaign_resource(campaign)) and api_available(campaign.account)


def billing_missing(account):
    """Betalningen hos Google är inte klar (eller inte avbockad). Stoppar
    inget, men annonserna visas inte förrän den finns."""
    return not account.google_ready and account.google_billing_status != BILLING_APPROVED


def campaign_url(campaign):
    """Kampanjen i Google Ads, eller "". Google dokumenterar inga länkar in i
    sitt gränssnitt: länken öppnar kampanjen när kundens konto är valt."""
    campaign_id = google_ads.digits(campaign.google_campaign_id)
    if not campaign_id:
        return ""
    return f"https://ads.google.com/aw/campaigns?campaignId={campaign_id}"


def panel(campaign):
    """Publiceringspanelens läge (manage/flamingo/_publish.html)."""
    account = campaign.account
    published = campaign.status in (Campaign.STATUS_LIVE, Campaign.STATUS_PAUSED)
    configured = not account.is_demo and google_ads.is_configured()
    if published:
        use_api = configured and bool(_campaign_resource(campaign))
    else:
        use_api = configured
    if account.is_demo:
        reason = MSG_MANUAL_DEMO
    elif not configured:
        reason = MSG_MANUAL_NOT_CONFIGURED
    elif not use_api:
        reason = MSG_MANUAL_PUBLISHED
    else:
        reason = ""
    return {
        "publish_api": use_api,
        "publish_manual_reason": reason,
        # Demokontots id:n är påhittade: ingen länk till Google Ads.
        "google_campaign_url": "" if account.is_demo else campaign_url(campaign),
        "billing_missing": billing_missing(account),
        "billing_note": MSG_BILLING,
        "google_places": generator.places_of(campaign.area),
        # Så många som skickas (utan dubbletter), inte så många som står i listan.
        "google_keyword_count": len(_unique_criteria(exports.keyword_rows(campaign))),
        "google_negative_count": len(_unique_criteria(exports.negative_rows(campaign))),
    }


# ---------------------------------------------------------------------------
# Anropet
# ---------------------------------------------------------------------------


def campaign_name(campaign):
    """Kampanjens namn hos Google: "Flamingo: Rörjour Nacka #12". Unikt per
    kampanj här, så att ett nytt försök kan leta upp den."""
    return f"{NAME_PREFIX}{campaign.name} #{campaign.pk}"


def _unique_criteria(rows):
    """(text, Googles matchningstyp) utan dubbletter. exports ger
    ("badrum", "Phrase") och ("jobb", "Negative Broad")."""
    seen, result = set(), []
    for text, criterion in rows:
        match = criterion.removeprefix("Negative ").upper()
        key = (text.casefold(), match)
        if key not in seen:
            seen.add(key)
            result.append((text, match))
    return result


def build_operations(campaign, customer_id):
    """Ändringarna som skapar kampanjen hos Google, i den ordning Google
    kräver (det som pekas på skapas först). Ren funktion: ingen databas,
    inget nätverk. Kastar ValueError om området inte ger någon ort."""
    base = f"customers/{google_ads.digits(customer_id)}"
    budget = f"{base}/campaignBudgets/{TEMP_BUDGET_ID}"
    campaign_rn = f"{base}/campaigns/{TEMP_CAMPAIGN_ID}"
    ad_group = f"{base}/adGroups/{TEMP_AD_GROUP_ID}"
    name = campaign_name(campaign)
    places = generator.places_of(campaign.area)
    if not places:
        raise ValueError("Området ger ingen ort.")

    def campaign_criterion(body):
        return {"campaignCriterionOperation": {"create": {"campaign": campaign_rn, **body}}}

    operations = [
        {
            "campaignBudgetOperation": {
                "create": {
                    "resourceName": budget,
                    "name": name,
                    "amountMicros": google_ads.to_micros(campaign.daily_budget_kr),
                    "deliveryMethod": "STANDARD",
                    "explicitlyShared": False,
                }
            }
        },
        {
            "campaignOperation": {
                "create": {
                    "resourceName": campaign_rn,
                    "name": name,
                    "advertisingChannelType": "SEARCH",
                    "status": "ENABLED",
                    "campaignBudget": budget,
                    "targetSpend": {},
                    "networkSettings": {
                        "targetGoogleSearch": True,
                        "targetSearchNetwork": False,
                        "targetContentNetwork": False,
                        "targetPartnerSearchNetwork": False,
                    },
                    "geoTargetTypeSetting": {"positiveGeoTargetType": "PRESENCE"},
                    "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
                }
            }
        },
        campaign_criterion({"language": {"languageConstant": LANGUAGE_SWEDISH}}),
    ]
    for place in places:
        operations.append(
            campaign_criterion(
                {
                    "proximity": {
                        "address": {"cityName": place, "countryCode": COUNTRY_CODE},
                        "radius": float(campaign.radius_km),
                        "radiusUnits": "KILOMETERS",
                    }
                }
            )
        )
    for text, match in _unique_criteria(exports.negative_rows(campaign)):
        operations.append(
            campaign_criterion({"negative": True, "keyword": {"text": text, "matchType": match}})
        )
    operations.append(
        {
            "adGroupOperation": {
                "create": {
                    "resourceName": ad_group,
                    "name": campaign.service.name if campaign.service_id else campaign.name,
                    "campaign": campaign_rn,
                    "status": "ENABLED",
                    "type": "SEARCH_STANDARD",
                }
            }
        }
    )
    for text, match in _unique_criteria(exports.keyword_rows(campaign)):
        operations.append(
            {
                "adGroupCriterionOperation": {
                    "create": {
                        "adGroup": ad_group,
                        "status": "ENABLED",
                        "keyword": {"text": text, "matchType": match},
                    }
                }
            }
        )
    headlines = [str(h).strip() for h in campaign.headlines or [] if str(h).strip()]
    descriptions = [str(d).strip() for d in campaign.descriptions or [] if str(d).strip()]
    operations.append(
        {
            "adGroupAdOperation": {
                "create": {
                    "adGroup": ad_group,
                    "status": "ENABLED",
                    "ad": {
                        "finalUrls": [exports.landing_page_url(campaign)],
                        "responsiveSearchAd": {
                            "headlines": [{"text": h} for h in headlines[:HEADLINE_COUNT]],
                            "descriptions": [{"text": d} for d in descriptions[:DESCRIPTION_COUNT]],
                        },
                    },
                }
            }
        }
    )
    return operations


def sent_marker(campaign, customer_id):
    """Vad ett försök skickar till Google: {"name", "fingerprint"}, där
    fingerprint är en hash av hela anropet (build_operations). Samma
    innehåll ger samma hash."""
    raw = json.dumps(
        build_operations(campaign, customer_id), sort_keys=True, ensure_ascii=False, default=str
    )
    return {
        "name": campaign_name(campaign),
        "fingerprint": hashlib.sha256(raw.encode()).hexdigest()[:32],
    }


def describe_operation(operation):
    """En ändring i anropet som text för byrån: 'sökordet "rörjour nacka"'."""
    if not isinstance(operation, dict) or not operation:
        return ""
    kind, body = next(iter(operation.items()))
    create = (body or {}).get("create") or {}
    if kind in _OPERATION_LABELS:
        return _OPERATION_LABELS[kind]
    keyword = (create.get("keyword") or {}).get("text") or ""
    if kind == "adGroupCriterionOperation":
        return f'sökordet "{keyword}"'
    if kind == "campaignCriterionOperation":
        if "language" in create:
            return "språket"
        if "proximity" in create:
            city = ((create["proximity"].get("address") or {}).get("cityName")) or ""
            return f"orten {city}".strip()
        if keyword:
            return f'det negativa sökordet "{keyword}"'
    return ""


def _resources_from(payload):
    """Resursnamnen ur mutate-svaret: {"budget", "campaign", "ad_group",
    "ad", "criteria": [...]}."""
    resources = {"criteria": []}
    for item in (payload or {}).get("mutateOperationResponses") or []:
        if not isinstance(item, dict):
            continue
        for key, result in item.items():
            name = result.get("resourceName") if isinstance(result, dict) else ""
            if not name:
                continue
            if key in _RESULT_KEYS:
                resources.setdefault(_RESULT_KEYS[key], str(name))
            elif key in _CRITERION_RESULTS:
                resources["criteria"].append(str(name))
    return resources


def _error_text(error, operations=None):
    """Googles fel som en text för byrån, med vilken ändring det gällde."""
    text = error.message
    if operations and error.errors:
        index = error.errors[0].get("index")
        if isinstance(index, int) and 0 <= index < len(operations):
            label = describe_operation(operations[index])
            if label:
                text = f"{text} (gäller {label})"
    return text[:ERROR_MAX]


# ---------------------------------------------------------------------------
# Kundens konto
# ---------------------------------------------------------------------------


def _prepare_account(account, customer_id):
    """Valutan måste vara SEK, och automatisk taggning slås på (gclid till
    landningssidan). True om taggningen slogs på nu."""
    query = (
        "SELECT customer.id, customer.currency_code, customer.auto_tagging_enabled FROM customer"
    )
    row = next(iter(google_ads.search(customer_id, query)), None)
    customer = (row or {}).get("customer") or {}
    currency = str(customer.get("currencyCode") or "")
    if currency != CURRENCY:
        if not currency:
            raise PublishError(
                "Google sa inte vilken valuta kundens konto har. Inget publicerades.",
                record=True,
            )
        raise PublishError(
            f"Kundens Google Ads-konto har valutan {currency[:10]}, inte {CURRENCY}. Budgeten "
            "räknas i kronor, så inget publicerades. Valutan går inte att byta i ett "
            "befintligt konto: skapa ett nytt konto i SEK och koppla det.",
            record=True,
        )
    turned_on = False
    if customer.get("autoTaggingEnabled") is not True:
        google_ads.request(
            "POST",
            f"customers/{customer_id}:mutate",
            {
                "operation": {
                    "update": {
                        "resourceName": f"customers/{customer_id}",
                        "autoTaggingEnabled": True,
                    },
                    "updateMask": "autoTaggingEnabled",
                }
            },
        )
        turned_on = True
    if account.google_auto_tagging is not True:
        FlamingoAccount.objects.filter(pk=account.pk).update(google_auto_tagging=True)
        account.google_auto_tagging = True
    return turned_on


def find_existing(customer_id, campaign, names=None):
    """Kampanjen hos Google med kampanjens namn, eller ett av names (inte
    borttagen), som Googles rad {"resourceName", "id", "name", "status",
    "campaignBudget"}, eller None."""
    wanted = [n for n in dict.fromkeys(names or [campaign_name(campaign)]) if n]
    listed = ", ".join(google_ads.gaql_string(name) for name in wanted)
    query = (
        "SELECT campaign.resource_name, campaign.id, campaign.name, campaign.status, "
        f"campaign.campaign_budget FROM campaign WHERE campaign.name IN ({listed}) "
        "AND campaign.status != 'REMOVED' LIMIT 1"
    )
    for row in google_ads.search(customer_id, query):
        found = row.get("campaign") if isinstance(row, dict) else None
        if isinstance(found, dict) and found.get("resourceName"):
            return found
    return None


def _status_operation(resource_name, status):
    return {
        "campaignOperation": {
            "update": {"resourceName": resource_name, "status": status},
            "updateMask": "status",
        }
    }


def _customer_of(resource_name):
    """'customers/1234567890/campaigns/5' blir '1234567890'."""
    parts = str(resource_name or "").split("/")
    return google_ads.digits(parts[1]) if len(parts) > 1 else ""


# ---------------------------------------------------------------------------
# Publicera, pausa, återuppta
# ---------------------------------------------------------------------------


def _lock(pk):
    """Kampanjraden låst för resten av transaktionen. Väntar inte: håller
    någon annan raden (ett klick till, en annan flik) blir det PublishError.
    FOR NO KEY UPDATE, så att en förfrågan till kampanjen kan sparas medan
    Google anropas."""
    try:
        return (
            Campaign.objects.select_for_update(nowait=True, no_key=True, of=("self",))
            .select_related("account__customer", "service")
            .get(pk=pk)
        )
    except DatabaseError:
        raise PublishError(MSG_BUSY) from None


def _store_error(campaign, text):
    Campaign.objects.filter(pk=campaign.pk).update(google_error=text[:ERROR_MAX])
    campaign.google_error = text[:ERROR_MAX]


class _StartOver(Exception):
    """Spärren var satt men inget finns hos Google: släpp den och börja om
    (så att det som skickas sparas med spärren, google_publish_sent)."""


def _pause_stale(customer_id, existing):
    """Pausa en gammal kampanj hos Google som inte tas över. True om den är
    pausad (nu eller redan)."""
    if existing.get("status") == "PAUSED":
        return True
    try:
        google_ads.mutate(customer_id, [_status_operation(existing["resourceName"], "PAUSED")])
    except google_ads.GoogleAdsError as exc:
        logger.warning("Flamingo: en gammal kampanj kunde inte pausas hos Google: %s", exc.codes)
        return False
    return True


def go_live(campaign, user=None, now=None, _again=False):
    """Skapa kampanjen i kundens Google Ads-konto och gör den live här.

    Returnerar PublishResult. Kastar PublishError med en svensk text när
    inget publicerades; kampanjens status är då oförändrad. Anropas utanför
    en transaktion (spärren ska sparas för sig). Se modulens docstring."""
    account = campaign.account
    if account.is_demo:
        raise PublishError(MSG_DEMO)
    if not google_ads.is_configured():
        raise PublishError(google_ads.MSG_NOT_CONFIGURED)
    customer_id = google_ads.digits(account.google_ads_customer_id)
    if account.google_id_shared:
        raise PublishError(MSG_SHARED_ID)
    if not linked_with_id(account):
        raise PublishError(MSG_NOT_LINKED)
    if not generator.places_of(campaign.area):
        raise PublishError(MSG_NO_PLACES)
    try:
        problems = checks.validate(campaign)
    except Exception:
        logger.exception("Flamingo-kontrollerna kunde inte köras (kampanj %s)", campaign.pk)
        raise PublishError("Kontrollerna kunde inte köras. Inget publicerades.") from None
    if problems:
        raise PublishError(
            f"Kontrollerna hittade {len(problems)} problem i kampanjen. Inget publicerades. "
            f"Det första: {problems[0].message}"
        )
    # Landningssidan: en sida som aldrig publicerats publiceras nu, innan
    # Google anropas (pagebuilder.publish_for_campaign). Säger kontrollerna
    # nej publiceras ingenting. Går Google sedan inte att nå står sidan som
    # publicerad, men den syns inte förrän kampanjen är live.
    try:
        pagebuilder.publish_for_campaign(campaign, user)
    except pagebuilder.PageError as exc:
        raise PublishError(
            f"Sidan kunde inte publiceras. {exc.message} Inget publicerades."
        ) from None

    now = now or timezone.now()
    current = Campaign.objects.select_related("account__customer", "service").get(pk=campaign.pk)
    if not generator.places_of(current.area):
        raise PublishError(MSG_NO_PLACES)
    sending = sent_marker(current, customer_id)
    fresh = campaign.claim_google_publish(now, sent=sending)
    # Säkert att inget finns hos Google: då släpps spärren vid ett fel. Sätts
    # först när raden är låst (ett klick som inte fick låset vet ingenting).
    nothing_at_google = False
    no_answer = False
    operations = existing = None
    try:
        with transaction.atomic():
            locked = _lock(campaign.pk)
            nothing_at_google = fresh
            if locked.status == Campaign.STATUS_LIVE:
                return PublishResult(
                    campaign_id=google_ads.digits(locked.google_campaign_id), already_live=True
                )
            if locked.status == Campaign.STATUS_PAUSED:
                raise PublishError(MSG_PAUSED)
            if locked.approved_at is None:
                raise PublishError(MSG_NOT_APPROVED)
            if Review.objects.filter(campaign=locked, state=Review.STATE_PENDING).exists():
                raise PublishError(MSG_PENDING)
            if not generator.places_of(locked.area):
                raise PublishError(MSG_NO_PLACES)
            marker = sent_marker(locked, customer_id)
            if fresh and marker != sending:
                # Kampanjen ändrades mellan läsningen och låset.
                raise PublishError(MSG_BUSY)

            if not fresh:
                earlier = locked.google_publish_sent
                earlier = earlier if isinstance(earlier, dict) else {}
                existing = find_existing(
                    customer_id, locked, names=[earlier.get("name"), marker["name"]]
                )
                if existing is None:
                    if _again:
                        # Någon annan tog spärren under tiden: den är deras.
                        raise PublishError(MSG_BUSY)
                    nothing_at_google = True
                    raise _StartOver
                if earlier.get("fingerprint") and earlier["fingerprint"] != marker["fingerprint"]:
                    paused = _pause_stale(customer_id, existing)
                    raise PublishError(
                        MSG_STALE.format(
                            name=str(existing.get("name") or earlier.get("name") or "")[:150],
                            paused=" och är pausad hos Google" if paused else "",
                        ),
                        record=True,
                    )

            auto_tagging = _prepare_account(account, customer_id)

            if existing is not None:
                resources = {
                    "campaign": str(existing["resourceName"]),
                    "budget": str(existing.get("campaignBudget") or ""),
                    "adopted": True,
                }
                if existing.get("status") != "ENABLED":
                    google_ads.mutate(
                        customer_id, [_status_operation(resources["campaign"], "ENABLED")]
                    )
            else:
                operations = build_operations(locked, customer_id)
                # Från och med anropet kan något finnas hos Google.
                nothing_at_google = False
                try:
                    payload = google_ads.mutate(customer_id, operations)
                except google_ads.GoogleAdsError as exc:
                    # Google svarade nej: allt eller inget, så inget skapades.
                    # Annars (inget svar, Googles eget fel) vet vi inte.
                    if exc.http_status is not None and 400 <= exc.http_status < 500:
                        nothing_at_google = True
                    else:
                        no_answer = True
                    raise
                resources = _resources_from(payload)
                if not resources.get("campaign"):
                    raise PublishError(MSG_NO_CAMPAIGN, record=True)

            locked.google_resources = resources
            locked.google_campaign_id = google_ads.resource_id(resources["campaign"])
            locked.google_synced_at = now
            locked.google_error = ""
            locked.status = Campaign.STATUS_LIVE
            locked.published_at = now
            locked.save(
                update_fields=[
                    "google_resources",
                    "google_campaign_id",
                    "google_synced_at",
                    "google_error",
                    "status",
                    "published_at",
                    "updated_at",
                ]
            )
    except google_ads.GoogleAdsError as exc:
        text = _error_text(exc, operations)
        if no_answer:
            text = f"{exc.message} {MSG_NO_ANSWER}"[:ERROR_MAX]
        _store_error(campaign, text)
        if nothing_at_google:
            campaign.release_google_publish()
        logger.warning(
            "Flamingo: kampanj %s publicerades inte hos Google: %s (request %s)",
            campaign.pk,
            exc.codes,
            exc.request_id or "-",
        )
        raise PublishError(text, google_error=exc) from None
    except _StartOver:
        campaign.release_google_publish()
        return go_live(campaign, user, now=now, _again=True)
    except BaseException as exc:
        if isinstance(exc, PublishError) and exc.record:
            _store_error(campaign, exc.message)
        if nothing_at_google:
            campaign.release_google_publish()
        raise

    for name in ("google_resources", "google_campaign_id", "status", "published_at"):
        setattr(campaign, name, getattr(locked, name))
    campaign.google_synced_at = now
    campaign.google_error = ""
    logger.info(
        "Flamingo: kampanj %s live hos Google som %s (%s, av %s)",
        campaign.pk,
        locked.google_campaign_id,
        "övertagen" if existing is not None else "skapad",
        getattr(user, "pk", None),
    )
    return PublishResult(
        campaign_id=locked.google_campaign_id,
        adopted=existing is not None,
        auto_tagging_enabled=auto_tagging,
    )


def _mark_attempt(campaign, now):
    Campaign.objects.filter(pk=campaign.pk).update(google_attempted_at=now)
    campaign.google_attempted_at = now


def _clear_error(campaign):
    """Inget försök gjordes hos Google: ett gammalt fel säger inte varför
    kampanjen inte är live nu."""
    if campaign.google_error:
        _store_error(campaign, "")


def publish_approved(campaign, user=None, now=None):
    """Kunden har godkänt (eller skickat in utan att be om granskning):
    publicera direkt när det går. Anropas utanför en transaktion, efter att
    godkännandet sparats. Kastar aldrig: kundens inskick ska inte fällas av
    Google.

    - Demokonto: inget anrop (OUTCOME_DEMO).
    - Kontot inte kopplat under ADX med ett eget id: OUTCOME_NOT_LINKED.
    - API:t inte inkopplat: OUTCOME_MANUAL, byrån publicerar för hand.
      I de två fallen töms ett gammalt google_error (inget försök gjordes).
    - Ett försök efter kunden misslyckades för mindre än RETRY_AFTER sedan,
      eller kontot har nått dagens gräns (limits.reserve_publish):
      OUTCOME_FAILED utan något anrop. Skyddar ADX kvot mot ett inskick i
      en slinga; byrån publicerar från kön utan gräns.
    - Annars go_live: OUTCOME_LIVE, eller OUTCOME_FAILED med orsaken. Orsaken
      sparas i google_error (Googles fel sparar go_live själv), så att
      byråns kö visar varför kampanjen inte är live.

    I alla fall utom live står kampanjen kvar som godkänd men inte
    publicerad, i byråns kö (manage_review.approved_not_live)."""
    account = campaign.account
    path = approval_path(account)
    if path == OUTCOME_DEMO:
        return Outcome(OUTCOME_DEMO, MSG_MANUAL_DEMO)
    if path == OUTCOME_NOT_LINKED:
        _clear_error(campaign)
        reason = MSG_SHARED_ID if account.google_id_shared else MSG_NOT_LINKED
        return Outcome(OUTCOME_NOT_LINKED, reason)
    if path == OUTCOME_MANUAL:
        _clear_error(campaign)
        return Outcome(OUTCOME_MANUAL, MSG_MANUAL_NOT_CONFIGURED)
    now = now or timezone.now()
    last = campaign.google_attempted_at
    if last is not None and now - last < RETRY_AFTER:
        return Outcome(OUTCOME_FAILED, MSG_COOLDOWN)
    if not limits.reserve_publish(account, now):
        _store_error(campaign, MSG_DAILY_LIMIT)
        return Outcome(OUTCOME_FAILED, MSG_DAILY_LIMIT)
    try:
        result = go_live(campaign, user)
    except PublishError as exc:
        # Googles fel (och de som markerats record) har go_live redan
        # sparat. "Pågår redan" sparas inte: någon publicerar just nu.
        if exc.message != MSG_BUSY:
            if exc.google_error is None and not exc.record:
                _store_error(campaign, exc.message)
            _mark_attempt(campaign, now)
        return Outcome(OUTCOME_FAILED, exc.message)
    except Exception:  # noqa: BLE001 - kundens godkännande får aldrig fällas här
        logger.exception(
            "Flamingo: kampanj %s kunde inte publiceras efter godkännandet", campaign.pk
        )
        _store_error(campaign, MSG_UNEXPECTED)
        _mark_attempt(campaign, now)
        return Outcome(OUTCOME_FAILED, MSG_UNEXPECTED)
    return Outcome(OUTCOME_LIVE, campaign_id=result.campaign_id)


def _set_status(campaign, *, live):
    """Pausa (live=False) eller återuppta kampanjen hos Google, och sedan här."""
    account = campaign.account
    if account.is_demo:
        raise PublishError(MSG_DEMO)
    want, google_status = (
        (Campaign.STATUS_LIVE, "ENABLED") if live else (Campaign.STATUS_PAUSED, "PAUSED")
    )
    need = Campaign.STATUS_PAUSED if live else Campaign.STATUS_LIVE
    try:
        with transaction.atomic():
            locked = _lock(campaign.pk)
            if locked.status != need:
                raise PublishError(
                    "Bara en pausad kampanj kan återupptas."
                    if live
                    else "Bara en kampanj som är live kan pausas."
                )
            resource = _campaign_resource(locked)
            if not resource:
                raise PublishError(MSG_MANUAL_PUBLISHED)
            google_ads.mutate(_customer_of(resource), [_status_operation(resource, google_status)])
            now = timezone.now()
            locked.status = want
            locked.google_synced_at = now
            locked.google_error = ""
            locked.save(update_fields=["status", "google_synced_at", "google_error", "updated_at"])
    except google_ads.GoogleAdsError as exc:
        text = _error_text(exc)
        _store_error(campaign, text)
        logger.warning(
            "Flamingo: kampanj %s kunde inte %s hos Google: %s",
            campaign.pk,
            "återupptas" if live else "pausas",
            exc.codes,
        )
        raise PublishError(text, google_error=exc) from None
    campaign.status = want
    campaign.google_synced_at = locked.google_synced_at
    campaign.google_error = ""
    return campaign


def pause(campaign):
    """Pausa kampanjen hos Google, sedan här (landningssidan stängs)."""
    return _set_status(campaign, live=False)


def resume(campaign):
    """Slå på kampanjen hos Google igen, sedan här (landningssidan öppnas)."""
    return _set_status(campaign, live=True)
