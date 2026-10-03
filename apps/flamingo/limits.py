"""
Spärrarna: hur ofta något får hända per besökare, kampanj eller konto.

Allt räknas i databasen, inte i processens minne (LocMemCache är en per
gunicorn-arbetare och töms vid omstart), så en spärr gäller hela sajten.

    Förfrågningar på /lp/   högst LEADS_PER_IP i timmen från samma besökare
                            till samma kampanj, och högst LEADS_PER_CAMPAIGN i
                            timmen till en kampanj. Räknas ur Lead-raderna.
    Läsningen av hemsidan   en åt gången, högst en per SCAN_INTERVAL och
                            högst SCAN_DAILY_MAX per svenskt dygn och konto.
    AI-förslag              högst AI_DAILY_MAX per svenskt dygn och konto;
                            sedan skriver mallarna texterna (generator.py).
    Sms                     i sms.py: ett autosvar per nummer och dygn, och
                            högst SMS_DAILY_MAX sms per konto och dag.

Besökarens IP sparas aldrig. Lead.ip_hash är en HMAC av adressen med
SECRET_KEY som nyckel: samma adress ger samma värde, men värdet går inte att
vända tillbaka till en adress.
"""

from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.common.net import client_ip

from . import leads
from .models import Campaign, FlamingoAccount, Lead

STOCKHOLM = ZoneInfo("Europe/Stockholm")

#: Förfrågningar i timmen från samma besökare till samma kampanj.
LEADS_PER_IP = 10
#: Förfrågningar i timmen till en kampanj, från alla besökare tillsammans.
LEADS_PER_CAMPAIGN = 30
LEAD_WINDOW = timedelta(hours=1)
LIMIT_IP = "ip"
LIMIT_CAMPAIGN = "campaign"

#: En läsning som stått som "hämtas" längre än så har avbrutits.
SCAN_STALE_AFTER = timedelta(minutes=2)
#: Minsta tid mellan två läsningar av hemsidan.
SCAN_INTERVAL = timedelta(minutes=2)
SCAN_DAILY_MAX = 10
SCAN_BUSY = "Hemsidan läses av just nu. Ladda om sidan om en liten stund."
SCAN_TOO_SOON = (
    "Hemsidan lästes av alldeles nyss. Vänta ett par minuter innan du läser av den igen."
)
SCAN_DAILY_LIMIT = (
    f"Hemsidan har lästs av {SCAN_DAILY_MAX} gånger i dag, och det är gränsen. "
    "Fyll i det som saknas under Företaget, eller läs av den igen i morgon."
)

#: AI-förslag (ny kampanj och nytt förslag) per konto och dag.
AI_DAILY_MAX = 20


def stockholm_today(now=None):
    return timezone.localdate(now or timezone.now(), STOCKHOLM)


def ip_hash(ip):
    """HMAC-SHA256 av adressen med SECRET_KEY, 64 hextecken. Tomt utan adress."""
    if not ip:
        return ""
    return salted_hmac("flamingo.lead.ip", ip, algorithm="sha256").hexdigest()


# ---------------------------------------------------------------------------
# Förfrågningarna från landningssidan
# ---------------------------------------------------------------------------


def create_form_lead(campaign, data, request, now=None):
    """(förfrågan, "") eller (None, LIMIT_IP / LIMIT_CAMPAIGN).

    Kampanjens rad låses medan förfrågningarna räknas och den nya sparas, så
    samtidiga inskick till samma kampanj tas ett i taget och inte kan smita
    förbi gränsen."""
    now = now or timezone.now()
    hashed = ip_hash(client_ip(request))
    with transaction.atomic():
        Campaign.objects.select_for_update().only("id").get(pk=campaign.pk)
        recent = Lead.objects.filter(campaign_id=campaign.pk, created_at__gte=now - LEAD_WINDOW)
        if recent.count() >= LEADS_PER_CAMPAIGN:
            return None, LIMIT_CAMPAIGN
        if hashed and recent.filter(ip_hash=hashed).count() >= LEADS_PER_IP:
            return None, LIMIT_IP
        return leads.create_lead(campaign, data, request, ip_hash=hashed), ""


# ---------------------------------------------------------------------------
# Läsningen av hemsidan
# ---------------------------------------------------------------------------


def reserve_scan(account, now=None):
    """Får kontot läsa av hemsidan nu? "" betyder ja, och då är läsningen
    bokförd (starttid och dagens antal). Annars texten som säger varför inte.

    Kontots rad låses medan det avgörs, så två samtidiga klick ger en läsning."""
    now = now or timezone.now()
    today = stockholm_today(now)
    with transaction.atomic():
        row = FlamingoAccount.objects.select_for_update().get(pk=account.pk)
        started = row.scan_started_at
        running_since = started or row.updated_at
        if row.scan_status == FlamingoAccount.SCAN_RUNNING and (
            running_since and now - running_since < SCAN_STALE_AFTER
        ):
            return SCAN_BUSY
        if started and now - started < SCAN_INTERVAL:
            return SCAN_TOO_SOON
        count = row.scan_count if row.scan_day == today else 0
        if count >= SCAN_DAILY_MAX:
            return SCAN_DAILY_LIMIT
        FlamingoAccount.objects.filter(pk=row.pk).update(
            scan_started_at=now, scan_day=today, scan_count=count + 1
        )
    account.scan_started_at, account.scan_day, account.scan_count = now, today, count + 1
    return ""


# ---------------------------------------------------------------------------
# AI-förslagen
# ---------------------------------------------------------------------------


def reserve_ai(account, now=None):
    """Får kontot ett AI-förslag till? True betyder ja, och då är det
    bokfört. False: dagens AI_DAILY_MAX är använda, mallarna tar över."""
    today = stockholm_today(now)
    with transaction.atomic():
        row = (
            FlamingoAccount.objects.select_for_update()
            .only("id", "ai_day", "ai_count")
            .get(pk=account.pk)
        )
        count = row.ai_count if row.ai_day == today else 0
        if count >= AI_DAILY_MAX:
            return False
        FlamingoAccount.objects.filter(pk=row.pk).update(ai_day=today, ai_count=count + 1)
    return True
