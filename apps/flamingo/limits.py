"""
Spärrarna: hur ofta något får hända per besökare, kampanj eller konto.

Allt räknas i databasen, inte i processens minne (LocMemCache är en per
gunicorn-arbetare och töms vid omstart), så en spärr gäller hela sajten.

    Förfrågningar på /lp/   högst LEADS_PER_IP i timmen från samma besökare
                            till samma kampanj, och högst LEADS_PER_CAMPAIGN i
                            timmen till en kampanj. Räknas ur Lead-raderna
                            (formuläret; klicken på numret räknas för sig).
    Klick på numret         ett per besökare, annonsklick och kampanj på
                            CALL_CLICK_DEDUPE (och inget alls om besökaren
                            redan skickat formuläret från samma annonsklick
                            då), högst CALL_CLICKS_PER_IP i timmen från samma
                            besökare och högst CALL_CLICKS_PER_CAMPAIGN i
                            timmen till en kampanj. Nås kampanjens gräns får
                            byrån ett larm (högst ett i timmen per kampanj).
    Publicering hos Google  efter kundens inskick eller godkännande högst
                            PUBLISH_DAILY_MAX försök per svenskt dygn och
                            konto (google_publish.publish_approved).
    Läsningen av hemsidan   en åt gången, högst en per SCAN_INTERVAL och
                            högst SCAN_DAILY_MAX per svenskt dygn och konto.
    AI-förslag              högst AI_DAILY_MAX per svenskt dygn och konto;
                            sedan skriver mallarna texterna (generator.py).
    Sms                     i sms.py: ett autosvar per nummer och dygn, och
                            högst SMS_DAILY_MAX sms per konto och dag.
    Per konto och dygn      reserve_daily: Google Places (reviews.py),
                            bilderna från hemsidan och larmen om ett konto
                            (alerts.send_account_alert).
    Per konto och timme     reserve_hourly: uppladdade bilder
                            (media.UPLOADS_PER_HOUR).

Utskick (apps/utskick, README C.2, E.4): en förfrågan eller ett klick på
numret med ett ut-klick från samma konto (utskick.attribution.resolve)
räknas mot UTSKICK_LEADS_PER_CAMPAIGN i stället för kampanjens vanliga
gräns (ett sms till 2 000 personer ger många förfrågningar samma timme),
högst attribution.LEADS_PER_CLICK_HOUR per klick och timme (sedan räknas
förfrågan som vilken som helst, utan spår). Förfrågningar via utskick
räknas inte mot de vanliga gränserna, så att ett utskick aldrig stänger
formuläret för besökare från Google. Klickets id ingår i dubblettnyckeln
för klick på numret.

Besökarens IP sparas aldrig. Lead.ip_hash är en HMAC av adressen med
SECRET_KEY som nyckel: samma adress ger samma värde, men värdet går inte att
vända tillbaka till en adress. En IPv6-adress räknas som sitt /64-nät (en
anslutning har ett helt /64 att välja adresser ur), en IPv4-adress som den är.

Förfrågningarna till en kampanj räknas en i taget med ett lås i Postgres
(pg_advisory_xact_lock) per kampanj, inte med kampanjens rad: publiceringen
låser raden medan Google anropas, och en förfrågan ska aldrig vänta på Google.
"""

import ipaddress
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import connection, transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.common.net import client_ip

from . import leads
from .models import FlamingoAccount, Lead

STOCKHOLM = ZoneInfo("Europe/Stockholm")

#: Förfrågningar i timmen från samma besökare till samma kampanj.
LEADS_PER_IP = 10
#: Förfrågningar i timmen till en kampanj, från alla besökare tillsammans.
LEADS_PER_CAMPAIGN = 30
LEAD_WINDOW = timedelta(hours=1)
LIMIT_IP = "ip"
LIMIT_CAMPAIGN = "campaign"
#: Klicket räknades inte: besökaren är redan en förfrågan på kampanjen.
LIMIT_DUPLICATE = "duplicate"

#: Ett klick på numret per besökare och kampanj på så här lång tid.
CALL_CLICK_DEDUPE = timedelta(minutes=60)
#: Klick på numret i timmen från samma besökare, alla kampanjer.
CALL_CLICKS_PER_IP = 5
#: Klick på numret i timmen till en kampanj, från alla besökare tillsammans.
CALL_CLICKS_PER_CAMPAIGN = 30
#: Förfrågningar och klick på numret i timmen till en kampanj via utskick
#: (ett ut-klick från samma konto), från alla mottagare tillsammans.
UTSKICK_LEADS_PER_CAMPAIGN = 200
#: Låsen per kampanj i Postgres: egen nyckelrymd ("FL") plus kampanjens id.
_LOCK_SPACE = 0x464C << 32

#: Publiceringar hos Google efter kundens inskick eller godkännande per
#: konto och dygn. Byrån publicerar från kön utan gräns.
PUBLISH_DAILY_MAX = 10

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


def ip_bucket(ip):
    """Det som räknas som en besökare: en IPv4-adress som den är, en
    IPv6-adress som sitt /64-nät ("2001:db8::/64"). En adress som inte går
    att läsa används som den är."""
    try:
        address = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return str(ip)
    if address.version == 6:
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def ip_hash(ip):
    """HMAC-SHA256 av besökaren (ip_bucket) med SECRET_KEY, 64 hextecken.
    Tomt utan adress."""
    if not ip:
        return ""
    return salted_hmac("flamingo.lead.ip", ip_bucket(ip), algorithm="sha256").hexdigest()


def _lock_campaign_leads(campaign_pk):
    """Förfrågningarna till kampanjen räknas en i taget (resten av
    transaktionen). Låser inte kampanjens rad."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_LOCK_SPACE + int(campaign_pk)])


# ---------------------------------------------------------------------------
# Förfrågningarna från landningssidan
# ---------------------------------------------------------------------------


def _utskick_click(click, now):
    """Klicket om det får ge spåret och de högre gränserna (högst
    attribution.LEADS_PER_CLICK_HOUR förfrågningar per klick och timme),
    annars None. Prövas under kampanjens lås."""
    if click is None:
        return None
    from apps.utskick import attribution

    return click if attribution.usable_for_lead(click, now) else None


def create_form_lead(campaign, data, request, now=None, click=None):
    """(förfrågan, "") eller (None, LIMIT_IP / LIMIT_CAMPAIGN).

    Kampanjens rad låses medan förfrågningarna räknas och den nya sparas, så
    samtidiga inskick till samma kampanj tas ett i taget och inte kan smita
    förbi gränsen. click är utskickets klick (samma konto som kampanjen,
    utskick.attribution.resolve) eller None."""
    now = now or timezone.now()
    hashed = ip_hash(client_ip(request))
    with transaction.atomic():
        _lock_campaign_leads(campaign.pk)
        click = _utskick_click(click, now)
        # Klicken på numret räknas för sig: en ström av klick får inte
        # stänga formuläret.
        recent = Lead.objects.filter(
            campaign_id=campaign.pk, created_at__gte=now - LEAD_WINDOW
        ).exclude(source=Lead.SOURCE_CALL_CLICK)
        if click is not None:
            if recent.count() >= UTSKICK_LEADS_PER_CAMPAIGN:
                return None, LIMIT_CAMPAIGN
        elif recent.filter(utskick__isnull=True).count() >= LEADS_PER_CAMPAIGN:
            return None, LIMIT_CAMPAIGN
        if hashed and recent.filter(ip_hash=hashed).count() >= LEADS_PER_IP:
            return None, LIMIT_IP
        return leads.create_lead(campaign, data, request, ip_hash=hashed, click=click), ""


def create_call_click_lead(campaign, data, request, now=None, click=None):
    """(förfrågan, "") eller (None, LIMIT_DUPLICATE / LIMIT_CAMPAIGN / LIMIT_IP).

    Samma låsning som create_form_lead. Samma besökare från samma
    annonsklick (samma klick-id, eller inget) som redan är en förfrågan på
    kampanjen den senaste CALL_CLICK_DEDUPE räknas inte igen. Två personer
    bakom samma adress (operatörens CGNAT) med var sitt annonsklick räknas
    båda. Utan IP-adress (ingen hash) gäller bara kampanjens gräns. Nås
    kampanjens gräns larmas byrån (cap_alert). Med ett utskicksklick (click)
    ingår klickets id i dubblettnyckeln och kampanjens gräns är
    UTSKICK_LEADS_PER_CAMPAIGN."""
    now = now or timezone.now()
    hashed = ip_hash(client_ip(request))
    tracking = leads.tracking_from(data)
    same_click = {key: tracking.get(key, "") for key in ("gclid", "gbraid", "wbraid")}
    with transaction.atomic():
        _lock_campaign_leads(campaign.pk)
        click = _utskick_click(click, now)
        if click is not None:
            same_click["attribution__click"] = click.pk
        if (
            hashed
            and Lead.objects.filter(
                campaign_id=campaign.pk,
                ip_hash=hashed,
                created_at__gte=now - CALL_CLICK_DEDUPE,
                **same_click,
            ).exists()
        ):
            return None, LIMIT_DUPLICATE
        clicks = Lead.objects.filter(
            source=Lead.SOURCE_CALL_CLICK, created_at__gte=now - LEAD_WINDOW
        )
        campaign_clicks = clicks.filter(campaign_id=campaign.pk)
        if click is not None:
            over = campaign_clicks.count() >= UTSKICK_LEADS_PER_CAMPAIGN
        else:
            over = campaign_clicks.filter(utskick__isnull=True).count() >= CALL_CLICKS_PER_CAMPAIGN
        if over:
            refused = LIMIT_CAMPAIGN
        elif hashed and clicks.filter(ip_hash=hashed).count() >= CALL_CLICKS_PER_IP:
            return None, LIMIT_IP
        else:
            lead = leads.create_call_click_lead(campaign, data, ip_hash=hashed, click=click)
            return lead, ""
    cap_alert(campaign, now)
    return None, refused


def cap_alert(campaign, now=None):
    """Larm till byrån (aldrig till kunden) när kampanjen nått gränsen för
    klick på numret: riktiga klick räknas inte resten av timmen. Högst ett
    larm per kampanj och timme (alerts.py). True om ett larm skickades."""
    from .alerts import send_agency_alert

    lines = [
        f"Kampanjen {campaign.name} ({campaign.account.customer.name}) fick "
        f"{CALL_CLICKS_PER_CAMPAIGN} klick på telefonnumret den senaste timmen.",
        "Fler klick räknas inte förrän timmen gått. Det kan vara påhittade klick: se "
        "förfrågningarna i inkorgen och klick-id:n innan de går till Google.",
    ]
    return send_agency_alert(
        campaign, f"Flamingo: gränsen för klick på numret nådd ({campaign.name})", lines, now
    )


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


def reserve_publish(account, now=None):
    """Får kontot ett publiceringsförsök hos Google till i dag (efter kundens
    inskick eller godkännande)? True betyder ja, och då är det bokfört.
    Skyddar ADX kvot hos Google mot ett inskick i en slinga."""
    today = stockholm_today(now)
    with transaction.atomic():
        row = (
            FlamingoAccount.objects.select_for_update()
            .only("id", "publish_day", "publish_count")
            .get(pk=account.pk)
        )
        count = row.publish_count if row.publish_day == today else 0
        if count >= PUBLISH_DAILY_MAX:
            return False
        FlamingoAccount.objects.filter(pk=row.pk).update(publish_day=today, publish_count=count + 1)
    return True


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


# ---------------------------------------------------------------------------
# Dagens räknare för det som kostar pengar eller bandbredd (mediaarkivet och
# Google-omdömena): FlamingoAccount.daily_usage, per svenskt dygn.
# ---------------------------------------------------------------------------


def daily_used(account, kind, now=None):
    """Hur många av sorten kind kontot använt i dag (läses, bokförs inte)."""
    usage = account.daily_usage if isinstance(account.daily_usage, dict) else {}
    if usage.get("day") != stockholm_today(now).isoformat():
        return 0
    value = usage.get(kind)
    return value if isinstance(value, int) and value > 0 else 0


def reserve_daily(account, kind, maximum, count=1, now=None):
    """Får kontot count till av sorten kind i dag? True betyder ja, och då är
    de bokförda. Kontots rad låses medan det avgörs, så två samtidiga klick
    kan inte båda smita förbi gränsen. Bokförs alltid före anropet (ett
    anrop som misslyckas har ändå kostat)."""
    today = stockholm_today(now).isoformat()
    with transaction.atomic():
        row = (
            FlamingoAccount.objects.select_for_update().only("id", "daily_usage").get(pk=account.pk)
        )
        usage = row.daily_usage if isinstance(row.daily_usage, dict) else {}
        if usage.get("day") != today:
            usage = {"day": today}
        used = usage.get(kind) if isinstance(usage.get(kind), int) else 0
        if count < 1 or used + count > maximum:
            return False
        usage[kind] = used + count
        FlamingoAccount.objects.filter(pk=row.pk).update(daily_usage=usage)
    account.daily_usage = usage
    return True


def reserve_hourly(account, kind, maximum, count=1, now=None):
    """Som reserve_daily, men per svensk timme (klockans timme, inte
    rullande): högst maximum av sorten kind mellan till exempel 14:00 och
    15:00. Räknas i samma rad (daily_usage), som nollställs varje dygn."""
    hour = timezone.localtime(now or timezone.now(), STOCKHOLM).strftime("%H")
    return reserve_daily(account, f"{kind}@{hour}", maximum, count=count, now=now)


def release_daily(account, kind, count=1, now=None):
    """Lämna tillbaka count av sorten kind (en bokning som aldrig blev ett
    anrop, till exempel en bild som inte hann hämtas)."""
    today = stockholm_today(now).isoformat()
    with transaction.atomic():
        row = (
            FlamingoAccount.objects.select_for_update().only("id", "daily_usage").get(pk=account.pk)
        )
        usage = row.daily_usage if isinstance(row.daily_usage, dict) else {}
        if usage.get("day") != today or not isinstance(usage.get(kind), int):
            return
        usage[kind] = max(0, usage[kind] - max(0, count))
        FlamingoAccount.objects.filter(pk=row.pk).update(daily_usage=usage)
    account.daily_usage = usage
