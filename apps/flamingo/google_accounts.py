"""
Kundens Google Ads-konto genom API:t (kundresan steg 5): kopplingsförfrågan
från ADX förvaltarkonto, ett nytt konto under förvaltarkontot, och läget hos
Google (kopplingen, betalningen och automatisk taggning).

    request_link(account)              förfrågan om koppling (PENDING) till kundens konto
    create_client_account(account)     nytt konto under förvaltarkontot, SEK och svensk tid
    sync_account_status(account)       läget hos Google till kontots fält
    forget_previous_account(account)   töm det som gällde ett tidigare konto-id

Rena funktioner över google_ads.request() och search(). Vyerna
(manage_google.py) och cron-kommandot flamingo_google_sync anropar dem.

- Demokonton (is_demo) anropar aldrig Google: funktionerna gör ingenting.
- Utan inställningarna (google_ads.is_configured()) ändras ingenting av
  sync_account_status: byråns avbockningar på kundkortet gäller.
- ADX mejlar aldrig kunden härifrån. Google meddelar kontots administratörer
  om en kopplingsförfrågan. En inbjudan när ett konto skapas kräver Googles
  tillåtelselista (GOOGLE_ADS_INVITE_ON_CREATE) och att byrån bockat i den.
- Betalningen stoppar ingenting: utan den visar Google bara inte annonserna.

Vems konto: kunden skriver själv sitt id, så ett id bevisar ingenting. Ett
id som ett annat Flamingo-konto har tas aldrig emot (databasen och
models.google_id_taken). Kontot blir kopplat av sig självt bara när ADX
skickat kopplingsförfrågan från just det här Flamingo-kontot till just det
id:t (google_link_requested_for) och kontots administratör godkänt den, eller
när ADX skapat kontot. Att kontot redan ligger under ADX förvaltarkonto
räcker inte: då kontrollerar byrån och bockar av det för hand. Allt som
skrivs efter ett anrop till Google sparas bara om id:t är detsamma som det
som lästes (_write), så ett id som byts under tiden aldrig blir kopplat.
"""

import logging

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from . import google_ads
from .google_ads import GoogleAdsError
from .models import FlamingoAccount, format_google_ads_id, google_id_taken

logger = logging.getLogger(__name__)

#: Kopplingens läge sett från förvaltarkontot (ManagerLinkStatus).
LINK_ACTIVE = "ACTIVE"
LINK_PENDING = "PENDING"
LINK_REFUSED = "REFUSED"
LINK_CANCELED = "CANCELED"
LINK_INACTIVE = "INACTIVE"

#: Betalningens läge (BillingSetupStatus), bäst först. NONE: Google svarade
#: men kontot har ingen betalning alls.
BILLING_APPROVED = "APPROVED"
BILLING_NONE = "NONE"
BILLING_ORDER = ("APPROVED", "APPROVED_HELD", "PENDING", "CANCELLED")
BILLING_LABELS = {
    "APPROVED": "Klar",
    "APPROVED_HELD": "Godkänd, väntar på första budgeten",
    "PENDING": "Google granskar betalningen",
    "CANCELLED": "Avbruten",
    BILLING_NONE: "Saknas",
}

#: Kontots namn hos Google: kundens namn och tjänsten.
ACCOUNT_NAME_SUFFIX = " (ADX Flamingo)"
ACCOUNT_NAME_MAX = 200
CURRENCY = "SEK"
TIME_ZONE = "Europe/Stockholm"

#: Där kunden godkänner förfrågan i Google Ads (svenska menyerna).
MANAGERS_PATH = "Administratör > Åtkomst och säkerhet > Förvaltare"

# Noteringarna nedan skrivs i google_note, som kunden ser i verktyget.
NOTE_LINK = (
    f"Godkänn ADX:s kopplingsförfrågan i Google Ads, under {MANAGERS_PATH}. "
    "Logga in med kontot som är administratör."
)
NOTE_REFUSED = (
    "Kopplingsförfrågan från ADX nekades i Google Ads. Säg till ADX om kontot ska kopplas ändå."
)
NOTE_CANCELED = "Kopplingsförfrågan från ADX drogs tillbaka och gäller inte längre."
NOTE_INACTIVE = "Kontot är inte längre kopplat till ADX förvaltarkonto."
NOTE_CREATED = "Kontot är skapat under ADX förvaltarkonto, i kronor och svensk tid."
#: Bara när Google tagit emot en inbjudan i samma anrop (tillåtelselistan).
NOTE_CREATED_INVITED = (
    "Kontot är skapat. Google tog emot ADX:s inbjudan till {email} som administratör: "
    "godkänn den när den kommer, så har du tillgång till kontot."
)
#: Till byrån (google_sync_error): kontot ligger under förvaltarkontot men
#: ingen förfrågan från det här Flamingo-kontot gällde id:t.
MSG_ACTIVE_UNVERIFIED = (
    "Kontot ligger under ADX förvaltarkonto, men kopplingen gjordes inte från Flamingo för "
    "den här kunden. Kontrollera att kontot är kundens och markera det som kopplat för hand."
)
MSG_ID_TAKEN = (
    "Id:t används redan av ett annat Flamingo-konto. Ett Google Ads-konto hör till en kund."
)
MSG_ID_CHANGED = (
    "Kontots id ändrades medan Google anropades, så inget sparades. Kontrollera id:t och "
    "försök igen."
)
MSG_INVITE_OFF = (
    "Inbjudan när kontot skapas kräver Googles tillåtelselista (GOOGLE_ADS_INVITE_ON_CREATE). "
    "Skapa kontot utan inbjudan och bjud in kunden i Google Ads."
)
MSG_CREATE_ACCESS = (
    "Google lät inte ADX skapa kontot. Att skapa konton kräver Basic-åtkomst för Google "
    "Cloud-projektet (Explorer räcker inte): ansök på sidan Google Ads API Overview i Google "
    "Cloud Console."
)
_ENDED_NOTES = {
    LINK_REFUSED: NOTE_REFUSED,
    LINK_CANCELED: NOTE_CANCELED,
    LINK_INACTIVE: NOTE_INACTIVE,
}
#: Noteringar som skrivs här och inte av byrån: kundens sida visar läget
#: själv, och de byts eller töms när läget ändras.
AUTO_NOTES = frozenset({NOTE_LINK, NOTE_REFUSED, NOTE_CANCELED, NOTE_INACTIVE, NOTE_CREATED})

#: Fält som gäller ett visst konto hos Google och blir fel när id:t byts.
ID_BOUND_FIELDS = (
    "google_billing_status",
    "google_auto_tagging",
    "google_synced_at",
    "google_sync_error",
    "google_link_requested_at",
    "google_link_requested_for",
    "google_conversion_actions",
)

_ALREADY_LINKED = frozenset({"ALREADY_MANAGED_BY_THIS_MANAGER", "ALREADY_MANAGED_IN_HIERARCHY"})
_CUSTOMER_STATES = {
    "CANCELED": "avslutat",
    "SUSPENDED": "avstängt av Google",
    "CLOSED": "stängt",
}


def is_auto_note(note):
    """Noteringen skrevs här (inte av byrån)."""
    return (note or "") in AUTO_NOTES or str(note or "").startswith("Kontot är skapat. Google")


def invite_allowed():
    """Får ADX be Google bjuda in kunden när kontot skapas? Bara när byrån
    slagit på GOOGLE_ADS_INVITE_ON_CREATE: emailAddress och accessRole i
    createCustomerClient är bara för Googles tillåtelselista."""
    return bool(getattr(settings, "GOOGLE_ADS_INVITE_ON_CREATE", False))


def billing_label(status):
    """Betalningens läge i klartext för panelen, eller ""."""
    return BILLING_LABELS.get(status or "", status or "")


def forget_previous_account(account):
    """Töm det som gällde ett tidigare konto hos Google (betalningen,
    taggningen, förfrågan och konverteringarna) när id:t byts. Sparar inte;
    returnerar fälten att spara."""
    account.google_billing_status = ""
    account.google_auto_tagging = None
    account.google_synced_at = None
    account.google_sync_error = ""
    account.google_link_requested_at = None
    account.google_link_requested_for = ""
    account.google_conversion_actions = {}
    return list(ID_BOUND_FIELDS)


def clear_campaign_errors(account):
    """Felen från Google på kontots opublicerade kampanjer gällde det förra
    kontot (eller läget innan): töm dem när id:t byts, så att byråns kö
    visar det som stoppar nu."""
    from .models import Campaign

    return (
        Campaign.objects.filter(account=account)
        .exclude(status__in=[Campaign.STATUS_LIVE, Campaign.STATUS_PAUSED])
        .exclude(google_error="")
        .update(google_error="")
    )


def _write(account, checked_id, values):
    """Spara values på kontot bara om kontots id fortfarande är checked_id
    (det som Google frågades om). True om det sparades; då är account
    uppdaterat. Ett id som byttes under anropet får aldrig det förra
    id:ts läge."""
    values = {**values, "updated_at": timezone.now()}
    updated = FlamingoAccount.objects.filter(
        pk=account.pk, google_ads_customer_id=checked_id
    ).update(**values)
    if updated:
        for name, value in values.items():
            setattr(account, name, value)
    return bool(updated)


def _require_configured():
    if not google_ads.is_configured():
        raise GoogleAdsError(google_ads.MSG_NOT_CONFIGURED, status="NOT_CONFIGURED")


def _client_id(account):
    """Kundens konto-id som tio siffror (inte förvaltarkontots eget)."""
    client = google_ads.digits(account.google_ads_customer_id)
    if len(client) != 10:
        raise GoogleAdsError(
            "Kunden har inget konto-id (tio siffror) att koppla.", status="INVALID_CUSTOMER_ID"
        )
    if client == google_ads.mcc_id():
        raise GoogleAdsError(
            "Id:t är ADX förvaltarkonto, inte kundens konto.", status="INVALID_CUSTOMER_ID"
        )
    return client


def _refuse_shared(account):
    """Ett id som ett annat Flamingo-konto har används aldrig."""
    if account.google_id_shared:
        raise GoogleAdsError(MSG_ID_TAKEN, status="ID_TAKEN")


def _linked_values(account):
    """Kontot ligger under förvaltarkontot: fälten att spara."""
    values = {}
    if not account.google_linked:
        values["google_status"] = FlamingoAccount.GOOGLE_LINKED
    if account.google_note in (NOTE_LINK, *_ENDED_NOTES.values()):
        values["google_note"] = ""
    return values


# ---------------------------------------------------------------------------
# Kopplingsförfrågan och nytt konto
# ---------------------------------------------------------------------------


def request_link(account, now=None):
    """Skicka en kopplingsförfrågan från förvaltarkontot till kundens konto
    (customerClientLinks, status PENDING). Kunden godkänner den i Google Ads
    under Förvaltare; till dess står kontot kvar som "Konto-id angivet".
    Förfrågan sparas med id:t den gällde (google_link_requested_for): bara
    då blir kontot kopplat när Google säger ACTIVE.

    Returnerar "pending", "managed" (kontot ligger redan under ADX
    förvaltarkonto: inget sparas, byrån kontrollerar att det är kundens och
    bockar av det för hand) eller None för ett demokonto. Kastar
    GoogleAdsError."""
    if account.is_demo:
        return None
    _require_configured()
    client = _client_id(account)
    _refuse_shared(account)
    checked = account.google_ads_customer_id
    mcc = google_ads.mcc_id()
    body = {
        "operation": {"create": {"clientCustomer": f"customers/{client}", "status": LINK_PENDING}}
    }
    try:
        google_ads.request("POST", f"customers/{mcc}/customerClientLinks:mutate", body)
    except GoogleAdsError as error:
        names = set(error.code_names)
        if names & _ALREADY_LINKED:
            return "managed"
        if "ALREADY_INVITED_BY_THIS_MANAGER" not in names:
            raise
    values = {
        "google_link_requested_at": now or timezone.now(),
        "google_link_requested_for": format_google_ads_id(client),
        "google_note": NOTE_LINK,
    }
    if not account.google_linked:
        values["google_status"] = FlamingoAccount.GOOGLE_ID_GIVEN
    if not _write(account, checked, values):
        raise GoogleAdsError(MSG_ID_CHANGED, status="ID_CHANGED")
    return "pending"


def _account_name(account):
    name = " ".join(str(account.customer.name or "").split())[:ACCOUNT_NAME_MAX]
    return f"{name}{ACCOUNT_NAME_SUFFIX}"


def create_client_account(account, invite_email=None):
    """Skapa ett nytt Google Ads-konto åt kunden under förvaltarkontot
    (createCustomerClient): "<kundens namn> (ADX Flamingo)", SEK och svensk
    tid. Kontot ligger under ADX direkt; betalningen lägger kunden in själv.

    invite_email skickas bara när Google satt ADX på tillåtelselistan
    (invite_allowed()) och byrån bockat i rutan som säger att Google mejlar
    en inbjudan till adressen: då bjuder Google in den som administratör.
    Utan den mejlar Google ingen.

    Ett konto som redan har ett id, eller är bockat som kopplat, får inget
    nytt (töm id:t på kundkortet först), och raden är låst medan Google
    anropas, så två klick ger inte två konton. Returnerar det nya id:t
    (123-456-7890), eller None för ett demokonto. Kastar GoogleAdsError."""
    if account.is_demo:
        return None
    _require_configured()
    invite_email = str(invite_email or "").strip() or None
    if invite_email and not invite_allowed():
        raise GoogleAdsError(MSG_INVITE_OFF, status="INVITE_OFF")
    if invite_email:
        try:
            validate_email(invite_email)
        except ValidationError:
            raise GoogleAdsError(
                "E-postadressen för inbjudan är inte giltig.", status="INVALID_EMAIL"
            ) from None
    body = {
        "customerClient": {
            "descriptiveName": _account_name(account),
            "currencyCode": CURRENCY,
            "timeZone": TIME_ZONE,
        }
    }
    if invite_email:
        body["emailAddress"] = invite_email
        body["accessRole"] = "ADMIN"
    path = f"customers/{google_ads.mcc_id()}:createCustomerClient"

    failure = None
    with transaction.atomic():
        locked = FlamingoAccount.objects.select_for_update().get(pk=account.pk)
        if locked.google_ads_customer_id:
            raise GoogleAdsError(
                f"Kunden har redan ett konto-id ({locked.google_ads_customer_id}). Töm id:t "
                "på kundkortet först om ett nytt konto ska skapas.",
                status="ALREADY_HAS_ACCOUNT",
            )
        if locked.google_linked:
            raise GoogleAdsError(
                "Kunden är redan bockad som kopplad. Skriv kontots id på kundkortet i stället "
                "för att skapa ett nytt konto.",
                status="ALREADY_HAS_ACCOUNT",
            )
        # Felet fångas här inne och kastas efter blocket: google_ads sparar
        # kopplingens fel (last_error) i samma transaktion.
        try:
            payload = google_ads.request("POST", path, body)
        except GoogleAdsError as error:
            failure = error
            if "ACTION_NOT_PERMITTED" in error.code_names:
                failure = GoogleAdsError(
                    MSG_CREATE_ACCESS,
                    status=error.status,
                    codes=error.codes,
                    errors=error.errors,
                    request_id=error.request_id,
                    http_status=error.http_status,
                )
        else:
            new_id = format_google_ads_id(google_ads.digits(payload.get("resourceName")))
            if not new_id:
                failure = GoogleAdsError(
                    "Google svarade utan kontots id. Titta i förvaltarkontot i Google Ads "
                    "innan du försöker igen, så att det inte blir två konton.",
                    status="NO_CUSTOMER_ID",
                )
            elif google_id_taken(new_id, exclude_pk=locked.pk):
                failure = GoogleAdsError(
                    f"Google skapade kontot {new_id}, men id:t finns redan på ett annat "
                    "Flamingo-konto. Inget sparades: titta i förvaltarkontot i Google Ads.",
                    status="ID_TAKEN",
                )
            else:
                fields = forget_previous_account(locked)
                locked.google_ads_customer_id = new_id
                locked.google_status = FlamingoAccount.GOOGLE_LINKED
                locked.google_note = (
                    NOTE_CREATED_INVITED.format(email=invite_email)[:300]
                    if invite_email
                    else NOTE_CREATED
                )
                fields += ["google_ads_customer_id", "google_status", "google_note"]
                locked.save(update_fields=[*fields, "updated_at"])
                for name in fields:
                    setattr(account, name, getattr(locked, name))
    if failure is not None:
        raise failure
    logger.info("Flamingo: Google Ads-konto %s skapat för konto %s", new_id, account.pk)
    return new_id


# ---------------------------------------------------------------------------
# Läget hos Google
# ---------------------------------------------------------------------------


def link_status(client):
    """Kopplingens läge sett från förvaltarkontot: "ACTIVE", "PENDING",
    "REFUSED", "CANCELED", "INACTIVE", eller "" när det inte finns någon
    koppling direkt under förvaltarkontot. Finns flera gäller ACTIVE, sedan
    PENDING, sedan den senaste."""
    query = (
        "SELECT customer_client_link.client_customer, customer_client_link.status, "
        "customer_client_link.manager_link_id FROM customer_client_link"
    )
    found = []
    for row in google_ads.search(google_ads.mcc_id(), query):
        link = row.get("customerClientLink") or {}
        if google_ads.digits(link.get("clientCustomer")) != client:
            continue
        try:
            link_id = int(link.get("managerLinkId") or 0)
        except (TypeError, ValueError):
            link_id = 0
        found.append((str(link.get("status") or ""), link_id))
    statuses = {status for status, _ in found}
    for preferred in (LINK_ACTIVE, LINK_PENDING):
        if preferred in statuses:
            return preferred
    if not found:
        return ""
    return max(found, key=lambda item: item[1])[0]


def read_client(client):
    """Kundens konto genom förvaltarkontot: {"auto_tagging", "currency",
    "status", "billing"}. billing är det bästa läget bland kontots
    betalningar (BILLING_ORDER) eller "NONE"."""
    info = {"auto_tagging": None, "currency": "", "status": "", "billing": BILLING_NONE}
    query = (
        "SELECT customer.id, customer.currency_code, customer.auto_tagging_enabled, "
        "customer.status FROM customer"
    )
    for row in google_ads.search(client, query):
        customer = row.get("customer") or {}
        info["auto_tagging"] = bool(customer.get("autoTaggingEnabled", False))
        info["currency"] = str(customer.get("currencyCode") or "")
        info["status"] = str(customer.get("status") or "")
    statuses = {
        str((row.get("billingSetup") or {}).get("status") or "")
        for row in google_ads.search(client, "SELECT billing_setup.status FROM billing_setup")
    }
    statuses.discard("")
    for status in BILLING_ORDER:
        if status in statuses:
            info["billing"] = status
            break
    else:
        if statuses:
            info["billing"] = sorted(statuses)[0][:20]
    return info


def _warnings(info):
    """Det byrån behöver veta om kontot, fast läsningen lyckades."""
    texts = []
    if info["currency"] and info["currency"] != CURRENCY:
        texts.append(
            f"Kontots valuta är {info['currency'][:3]}, inte SEK: budgetarna räknas i kronor "
            "och blir fel. Skapa ett konto i SEK."
        )
    state = _CUSTOMER_STATES.get(info["status"])
    if state:
        texts.append(f"Kontot är {state} hos Google.")
    return " ".join(texts)[:300]


def sync_account_status(account, now=None):
    """Läs läget hos Google till kontots fält: kopplingen från förvaltarkontot,
    betalningen och automatisk taggning från kundens konto, google_synced_at
    och google_sync_error.

    Kopplingen:
      ACTIVE    kopplat, men bara när ADX skickade förfrågan från det här
                kontot till det här id:t (google_link_requested_for). Annars
                står kontot kvar och byrån ser MSG_ACTIVE_UNVERIFIED.
      PENDING   "Konto-id angivet" och kundens tur: förfrågan räknas som
                skickad (google_link_requested_at) även om den skickades
                från Google Ads.
      nekad, tillbakadragen, avslutad
                "Konto-id angivet" med en notering, och förfrågan gäller
                inte längre (google_link_requested_at töms).

    Gör ingenting för ett demokonto, ett konto utan id eller när API:t inte
    är inkopplat (byråns avbockningar gäller då). Ett fel från Google sparas
    i google_sync_error och returneras (läget står kvar); ett fel i ADX:s
    egen koppling eller slut kvot sparas och kastas, så att
    flamingo_google_sync stoppar. Returnerar None när läsningen lyckades.
    Allt sparas bara om kontots id är detsamma som det som lästes."""
    if account.is_demo or not google_ads.is_configured():
        return None
    client = google_ads.digits(account.google_ads_customer_id)
    if len(client) != 10 or client == google_ads.mcc_id():
        return None
    checked = account.google_ads_customer_id
    now = now or timezone.now()
    values = {}
    hints = []
    try:
        link = link_status(client)
        linked = account.google_linked
        if link == LINK_ACTIVE:
            if linked:
                values.update(_linked_values(account))
            elif (
                account.google_link_requested_for == format_google_ads_id(client)
                and not account.google_id_shared
            ):
                values.update(_linked_values(account))
                linked = True
            else:
                hints.append(MSG_ACTIVE_UNVERIFIED)
        elif link == LINK_PENDING or link in _ENDED_NOTES:
            if account.google_status != FlamingoAccount.GOOGLE_ID_GIVEN:
                values["google_status"] = FlamingoAccount.GOOGLE_ID_GIVEN
                linked = False
            note = NOTE_LINK if link == LINK_PENDING else _ENDED_NOTES[link]
            if account.google_note != note and (
                not account.google_note or is_auto_note(account.google_note)
            ):
                values["google_note"] = note
        if link == LINK_PENDING:
            if account.google_link_requested_at is None:
                values["google_link_requested_at"] = now
        elif link:
            # Förfrågan är besvarad (godkänd, nekad, tillbakadragen): den
            # väntar inte längre på kunden.
            values["google_link_requested_at"] = None
            values["google_link_requested_for"] = ""
        warning = ""
        if linked:
            info = read_client(client)
            ready = info["billing"] == BILLING_APPROVED
            values["google_status"] = (
                FlamingoAccount.GOOGLE_BILLING_OK if ready else FlamingoAccount.GOOGLE_LINKED
            )
            values["google_billing_status"] = info["billing"]
            values["google_auto_tagging"] = info["auto_tagging"]
            warning = _warnings(info)
        elif link:
            values["google_billing_status"] = ""
            values["google_auto_tagging"] = None
    except GoogleAdsError as error:
        logger.info(
            "Flamingo: läget för konto %s kunde inte läsas från Google (%s)",
            account.pk,
            error.status or error.code_names,
        )
        _write(account, checked, {"google_sync_error": error.message[:300]})
        if error.is_auth_error or error.is_quota_error:
            raise
        return error
    values["google_synced_at"] = now
    values["google_sync_error"] = " ".join([*hints, warning]).strip()[:300]
    if not _write(account, checked, values):
        logger.info("Flamingo: konto %s bytte id under läsningen; inget sparades", account.pk)
        account.refresh_from_db()
    return None
