"""
Byråns Google-sida för ADX Flamingo (/manage/flamingo/google/) och
kundkortets knappar mot Google Ads API:

    google_page        vad som saknas i miljön (bara namnen), adressen att
                       registrera i Google Cloud, vilket Google-konto som är
                       kopplat och sedan när, förvaltarkontot
    google_connect     "Koppla med Google" (POST): slumpad state i sessionen,
                       sedan till Googles inloggning
    google_callback    Google skickar tillbaka hit (GET): state prövas (en
                       gång, i konstant tid), koden byts mot en långlivad
                       nyckel som sparas krypterad (GoogleAdsConnection)
    google_test        "Testa kopplingen" (POST): kontona inloggningen når
    google_disconnect  "Koppla från" (POST): återkalla hos Google och glöm
    google_account     kundkortets knappar (POST): kopplingsförfrågan, nytt
                       konto åt kunden och läget från Google (google_accounts.py)

    google_uploads_retry  "Försök ladda upp igen" (POST): häver spärren när
                       Google inte tog emot konverteringarna

Alla vyer kräver byrån (staff_required). Nyckeln visas aldrig, inte heller
i ett felmeddelande. ADX mejlar aldrig kunden härifrån; Google mejlar
kunden bara när byrån bockat i inbjudan (som kräver Googles
tillåtelselista), och rutan säger det.
"""

import hmac
import logging
import secrets
import time

from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.projects.access import staff_required
from apps.projects.models import Customer

from . import google_accounts, google_ads, google_conversions
from .google_ads import GoogleAdsError
from .models import FlamingoAccount, GoogleAdsConnection, format_google_ads_id

logger = logging.getLogger(__name__)

#: Sessionsnyckeln för inloggningens state.
STATE_KEY = "flamingo_google_oauth"
#: Så länge får inloggningen hos Google ta.
STATE_MAX_AGE = 15 * 60
#: Googles egen sida för appar med åtkomst, om återkallelsen inte går fram.
PERMISSIONS_URL = "https://myaccount.google.com/permissions"


def _page():
    return redirect("manage:flamingo_google")


def _back_to_card(customer_pk):
    return redirect(reverse("manage:customer_detail", args=[customer_pk]) + "#flamingo-google")


def redirect_uri(request):
    """Adressen Google skickar tillbaka till. Exakt den registreras som
    "Authorized redirect URI" på OAuth-klienten i Google Cloud."""
    return request.build_absolute_uri(reverse("manage:flamingo_google_callback"))


def env_token_in_use():
    """GOOGLE_ADS_REFRESH_TOKEN i miljön vinner över den sparade kopplingen."""
    return bool(str(getattr(settings, "GOOGLE_ADS_REFRESH_TOKEN", "") or "").strip())


def api_state():
    """Läget för panelens mallar: {configured, missing, connection, env_token,
    invite_allowed}."""
    missing = google_ads.missing_settings()
    return {
        "configured": not missing,
        "missing": missing,
        "connection": GoogleAdsConnection.objects.filter(pk=GoogleAdsConnection.SOLO_PK).first(),
        "env_token": env_token_in_use(),
        "invite_allowed": google_accounts.invite_allowed(),
    }


def invite_choices(customer):
    """Adresserna en inbjudan från Google kan gå till: kundens kontakter med
    e-post och kundens egen adress, som [(adress, etikett)]."""
    choices, seen = [], set()
    for user in customer.users.order_by("first_name", "email"):
        email = (user.email or "").strip()
        if not email or email.casefold() in seen:
            continue
        seen.add(email.casefold())
        name = user.get_full_name().strip()
        choices.append((email, f"{name} ({email})" if name else email))
    own = (customer.email or "").strip()
    if own and own.casefold() not in seen:
        choices.append((own, f"{own} (kundens adress)"))
    return choices


# ---------------------------------------------------------------------------
# Byråns Google-sida
# ---------------------------------------------------------------------------


@staff_required
@require_GET
def google_page(request):
    missing = google_ads.missing_settings()
    connection = GoogleAdsConnection.objects.filter(pk=GoogleAdsConnection.SOLO_PK).first()
    accounts = (
        FlamingoAccount.objects.filter(is_enabled=True)
        .select_related("customer")
        .order_by("customer__name")
    )
    return render(
        request,
        "manage/flamingo/google.html",
        {
            "active": "flamingo",
            "title": "Google Ads",
            "missing": [(name, google_ads.SETTING_HELP.get(name, "")) for name in missing],
            "configured": not missing,
            "oauth_configured": google_ads.oauth_configured(),
            "env_token": env_token_in_use(),
            "connection": connection,
            "redirect_uri": redirect_uri(request),
            "mcc": format_google_ads_id(google_ads.mcc_id()) or "",
            "api_version": google_ads.api_version(),
            "accounts": accounts,
            "uploads_setting": bool(getattr(settings, "GOOGLE_ADS_UPLOAD_CONVERSIONS", False)),
            "uploads_via_api": google_conversions.upload_enabled(),
            "upload_blocked": google_conversions.upload_blocked(),
            "invite_allowed": google_accounts.invite_allowed(),
        },
    )


@staff_required
@require_POST
def google_uploads_retry(request):
    """Häv spärren efter att Google inte tog emot konverteringarna. Nästa
    körning av flamingo_google_sync försöker igen; svarar Google samma sak
    stoppas den igen. Raderna har stått kvar i kö hela tiden."""
    google_conversions.unblock_uploads()
    messages.success(
        request,
        "Uppladdningen försöks igen vid nästa körning av flamingo_google_sync. Säger Google "
        "nej igen stoppas den, och raderna står kvar för CSV-filen.",
    )
    return _page()


@staff_required
@require_POST
def google_connect(request):
    """Till Googles inloggning. state är slumpad, sparas i sessionen och
    prövas en gång när Google skickar tillbaka."""
    if not google_ads.oauth_configured():
        messages.error(request, google_ads.MSG_NO_CLIENT)
        return _page()
    state = secrets.token_urlsafe(32)
    request.session[STATE_KEY] = {"state": state, "at": int(time.time()), "user": request.user.pk}
    try:
        url = google_ads.authorization_url(state, redirect_uri(request))
    except GoogleAdsError as error:
        request.session.pop(STATE_KEY, None)
        messages.error(request, error.message)
        return _page()
    return redirect(url)


def _state_ok(request, saved):
    """state från Google stämmer med den sparade, är färsk och gäller samma
    person. Jämförs i konstant tid."""
    if not isinstance(saved, dict):
        return False
    expected = str(saved.get("state") or "")
    given = request.GET.get("state", "")
    try:
        fresh = 0 <= time.time() - int(saved.get("at") or 0) <= STATE_MAX_AGE
    except (TypeError, ValueError):
        fresh = False
    return bool(
        expected
        and fresh
        and saved.get("user") == request.user.pk
        and hmac.compare_digest(expected.encode(), given.encode())
    )


@staff_required
@require_GET
def google_callback(request):
    """Google skickar tillbaka hit med en kod (eller error). Koden byts mot en
    långlivad nyckel som sparas krypterad och aldrig visas. Sidan skickar
    vidare direkt, så att koden inte blir kvar i adressfältet."""
    # En gång: state tas bort ur sessionen vad som än händer nedan.
    saved = request.session.pop(STATE_KEY, None)
    if not _state_ok(request, saved):
        messages.error(
            request,
            "Svaret från Google kunde inte kopplas till en inloggning härifrån (gammal eller "
            "redan använd länk). Inget ändrades. Koppla igen från början.",
        )
        return _page()
    error = request.GET.get("error", "")
    if error:
        if error == "access_denied":
            messages.info(request, "Inloggningen hos Google avbröts. Inget ändrades.")
        else:
            code = "".join(ch for ch in error if ch.isalnum() or ch == "_")[:40]
            messages.error(request, f"Google avbröt inloggningen ({code}). Inget ändrades.")
        return _page()
    try:
        token, email = google_ads.exchange_code(request.GET.get("code", ""), redirect_uri(request))
    except GoogleAdsError as exc:
        messages.error(request, exc.message)
        return _page()
    GoogleAdsConnection.get_solo().set_refresh_token(token, email, request.user)
    token = None
    logger.info("Flamingo: ADX:s Google-konto kopplat av användare %s", request.user.pk)
    text = f"ADX:s Google-konto är kopplat{f' som {email}' if email else ''}."
    if env_token_in_use():
        text += " GOOGLE_ADS_REFRESH_TOKEN i miljön används ändå, eftersom den vinner."
    else:
        text += " Testa kopplingen för att se att förvaltarkontot nås."
    messages.success(request, text)
    return _page()


@staff_required
@require_POST
def google_test(request):
    """Kontona inloggningen når direkt. Sparar last_ok_at eller last_error."""
    try:
        ids = google_ads.list_accessible_customers()
    except GoogleAdsError as error:
        if error.status != "NOT_CONFIGURED":
            GoogleAdsConnection.objects.get_or_create(pk=GoogleAdsConnection.SOLO_PK)
            GoogleAdsConnection.objects.filter(pk=GoogleAdsConnection.SOLO_PK).update(
                last_error=error.message[:300]
            )
        messages.error(request, f"Kopplingen fungerar inte: {error.message}")
        return _page()
    mcc = google_ads.mcc_id()
    count = len(ids)
    accounts = f"{count} {'konto' if count == 1 else 'konton'}"
    problem = ""
    if not mcc:
        problem = "Förvaltarkontots id (GOOGLE_ADS_LOGIN_CUSTOMER_ID) saknas i miljön."
    elif mcc not in ids:
        problem = (
            f"Förvaltarkontot {format_google_ads_id(mcc)} är inte bland dem. Koppla med ett "
            "Google-konto som har åtkomst till förvaltarkontot, eller rätta "
            "GOOGLE_ADS_LOGIN_CUSTOMER_ID."
        )
    GoogleAdsConnection.objects.get_or_create(pk=GoogleAdsConnection.SOLO_PK)
    GoogleAdsConnection.objects.filter(pk=GoogleAdsConnection.SOLO_PK).update(
        last_ok_at=timezone.now(), last_error=problem[:300]
    )
    if problem:
        messages.warning(request, f"Google svarade och inloggningen når {accounts}. {problem}")
    else:
        messages.success(
            request,
            f"Kopplingen fungerar. Inloggningen når {accounts}, förvaltarkontot "
            f"{format_google_ads_id(mcc)} är ett av dem.",
        )
    return _page()


@staff_required
@require_POST
def google_disconnect(request):
    """Återkalla nyckeln hos Google och glöm kopplingen här. Glöms även om
    Google inte svarar."""
    connection = GoogleAdsConnection.get_solo()
    had = connection.is_connected
    token = connection.refresh_token()
    revoked = google_ads.revoke(token) if token else False
    token = None
    connection.clear()
    if revoked:
        text = "Kopplingen är borttagen och nyckeln återkallad hos Google."
    elif had:
        text = (
            "Kopplingen är borttagen här, men Google bekräftade inte återkallelsen. Ta bort "
            f"ADX under appar med åtkomst i Google-kontot: {PERMISSIONS_URL}"
        )
    else:
        text = "Det fanns ingen sparad koppling."
    if env_token_in_use():
        text += (
            " GOOGLE_ADS_REFRESH_TOKEN i miljön används fortfarande: ta bort den där för att "
            "koppla från helt."
        )
    (messages.success if revoked or not had else messages.warning)(request, text)
    return _page()


# ---------------------------------------------------------------------------
# Kundkortets knappar
# ---------------------------------------------------------------------------


def _sync_message(request, account):
    if account.google_sync_error:
        messages.warning(request, f"Läst från Google: {account.google_sync_error}")
        return
    parts = [f"Läst från Google: {account.get_google_status_display()}."]
    if account.google_billing_status:
        billing = google_accounts.billing_label(account.google_billing_status)
        parts.append(f"Betalningen: {billing}.")
    messages.success(request, " ".join(parts))


@staff_required
@require_POST
def google_account(request, pk):
    """Kundkortets knappar mot Google Ads API (action): link skickar en
    kopplingsförfrågan, create skapar ett konto åt kunden och sync hämtar
    läget. Kunden mejlas inte av ADX; Google mejlar bara en inbjudan när
    rutan för den är ibockad."""
    customer = get_object_or_404(Customer, pk=pk)
    account = FlamingoAccount.objects.filter(customer=customer).select_related("customer").first()
    if account is None:
        messages.error(request, "Aktivera ADX Flamingo för kunden först.")
        return _back_to_card(customer.pk)
    if account.is_demo:
        messages.info(request, google_ads.MSG_DEMO)
        return _back_to_card(customer.pk)
    if not google_ads.is_configured():
        messages.error(request, f"{google_ads.MSG_NOT_CONFIGURED} Bocka av för hand i stället.")
        return _back_to_card(customer.pk)

    action = request.POST.get("action", "")
    try:
        if action == "link":
            result = google_accounts.request_link(account)
            if result == "managed":
                messages.warning(
                    request,
                    "Kontot ligger redan under ADX förvaltarkonto, så ingen förfrågan skickades "
                    "och inget ändrades. Kontrollera att kontot är kundens och markera det som "
                    "kopplat under Ändra för hand. ADX har inte mejlat kunden.",
                )
            else:
                messages.success(
                    request,
                    "Kopplingsförfrågan är skickad. Kunden godkänner den i Google Ads under "
                    f"{google_accounts.MANAGERS_PATH}. Google kan meddela kontots "
                    "administratörer om förfrågan; ADX har inte mejlat kunden.",
                )
        elif action == "create":
            invite = request.POST.get("invite") == "1" and google_accounts.invite_allowed()
            email = ""
            if invite:
                email = request.POST.get("invite_email", "").strip()
                allowed = {address.casefold(): address for address, _ in invite_choices(customer)}
                if email.casefold() not in allowed:
                    messages.error(
                        request,
                        "Välj en av kundens adresser för inbjudan. Inget konto skapades.",
                    )
                    return _back_to_card(customer.pk)
                email = allowed[email.casefold()]
            new_id = google_accounts.create_client_account(account, invite_email=email or None)
            if email:
                text = (
                    f"Kontot {new_id} är skapat under förvaltarkontot. Google tog emot "
                    f"inbjudan till {email} och mejlar den."
                )
            else:
                text = (
                    f"Kontot {new_id} är skapat under förvaltarkontot. Ingen inbjudan skickades: "
                    "bjud in kunden som administratör i Google Ads (Administratör, Åtkomst och "
                    "säkerhet). Google mejlar kunden när du gör det."
                )
            messages.success(request, text)
        elif action == "sync":
            failure = google_accounts.sync_account_status(account)
            if failure is not None:
                messages.error(request, f"Läget kunde inte läsas från Google: {failure.message}")
            else:
                account.refresh_from_db()
                _sync_message(request, account)
        else:
            messages.error(request, "Okänd åtgärd. Inget ändrades.")
    except GoogleAdsError as error:
        messages.error(request, error.message)
    return _back_to_card(customer.pk)
