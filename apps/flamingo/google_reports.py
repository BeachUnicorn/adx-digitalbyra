"""
Googles rapporter för kampanjerna (README steg 12): kostnad, visningar,
klick och konverteringar per kampanj och dag, sparade som CampaignDayStats.
Översikten (app_views/overview.py) räknar annonspengarna och kr per
förfrågan och affär ur raderna.

    sync_stats(account, days=30)   läser de senaste dagarna från Google och
                                   skriver över dem i databasen

Bara kampanjer med google_campaign_id läses. Dagarna räknas i svensk tid
(kundkontona skapas med tidszonen Europe/Stockholm). Kostnaden sparas i
mikros som Google skickar den; kontot måste ha valutan SEK, annars sparas
inget (en kostnad i fel valuta visas aldrig som kronor).

Google skickar inga rader för dagar utan visningar. En dag i perioden som
finns i databasen men inte längre i Googles svar tas därför bort, så att
siffrorna alltid är Googles.

Demokonton och konton som inte får synkas (google_conversions.can_sync)
läses aldrig.
"""

import logging
import re
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from . import google_ads
from .google_ads import GoogleAdsError
from .google_conversions import can_sync, customer_id
from .models import CampaignDayStats

logger = logging.getLogger(__name__)

DAYS = 30
CURRENCY = "SEK"
MSG_CURRENCY = (
    "Kundens Google Ads-konto har valutan {currency}, inte SEK. Annonspengarna läses inte in."
)


def campaign_key(value):
    """Kampanjens id hos Google som siffror: '123', 'customers/1/campaigns/123'
    och ' 123 ' blir '123'. Inget giltigt id blir ""."""
    tail = google_ads.resource_id(str(value or "").strip())
    return tail if re.fullmatch(r"\d{1,20}", tail) else ""


def _int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _float(value):
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def stats_query(ids, start, end):
    """GAQL-frågan för kampanjerna (id:n som siffror) och dagarna."""
    return (
        "SELECT campaign.id, segments.date, metrics.cost_micros, metrics.impressions, "
        "metrics.clicks, metrics.conversions, customer.currency_code FROM campaign "
        f"WHERE campaign.id IN ({', '.join(sorted(ids, key=int))}) "
        f"AND segments.date BETWEEN '{start:%Y-%m-%d}' AND '{end:%Y-%m-%d}'"
    )


def sync_stats(account, days=DAYS, today=None):
    """Läs de senaste days dagarna (i dag medräknad) för kontots kampanjer
    och spara dem. Returnerar antalet dagar som sparades. Kastar
    GoogleAdsError vid fel från Google eller fel valuta."""
    if not can_sync(account):
        return 0
    campaigns = {}
    for campaign in account.campaigns.exclude(google_campaign_id=""):
        key = campaign_key(campaign.google_campaign_id)
        if key:
            campaigns[key] = campaign
    if not campaigns:
        return 0
    end = today or timezone.localdate(timezone.now(), google_ads.STOCKHOLM)
    start = end - timedelta(days=max(1, int(days)) - 1)

    rows = list(google_ads.search(customer_id(account), stats_query(campaigns, start, end)))
    now = timezone.now()
    days_found = {}
    for row in rows:
        currency = str((row.get("customer") or {}).get("currencyCode") or CURRENCY)
        if currency != CURRENCY:
            raise GoogleAdsError(MSG_CURRENCY.format(currency=currency[:10]), status="CURRENCY")
        campaign = campaigns.get(campaign_key((row.get("campaign") or {}).get("id")))
        try:
            day = date.fromisoformat(str((row.get("segments") or {}).get("date") or ""))
        except ValueError:
            day = None
        if campaign is None or day is None or not start <= day <= end:
            continue
        metrics = row.get("metrics") or {}
        stat = days_found.setdefault(
            (campaign.pk, day),
            CampaignDayStats(campaign=campaign, date=day, updated_at=now),
        )
        # En kampanj och dag kommer en gång; skulle Google dela upp den
        # läggs delarna ihop.
        stat.cost_micros += _int(metrics.get("costMicros"))
        stat.impressions += _int(metrics.get("impressions"))
        stat.clicks += _int(metrics.get("clicks"))
        stat.conversions += _float(metrics.get("conversions"))

    with transaction.atomic():
        if days_found:
            CampaignDayStats.objects.bulk_create(
                list(days_found.values()),
                update_conflicts=True,
                unique_fields=["campaign", "date"],
                update_fields=["cost_micros", "impressions", "clicks", "conversions", "updated_at"],
            )
        stale = [
            pk
            for pk, campaign_pk, day in CampaignDayStats.objects.filter(
                campaign__in=campaigns.values(), date__range=(start, end)
            ).values_list("pk", "campaign_id", "date")
            if (campaign_pk, day) not in days_found
        ]
        if stale:
            CampaignDayStats.objects.filter(pk__in=stale).delete()
    logger.info(
        "Google Ads: %s dagar i rapporten för konto %s (%s kampanjer)",
        len(days_found),
        account.pk,
        len(campaigns),
    )
    return len(days_found)
