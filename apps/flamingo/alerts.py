"""
Larm till byrån (INQUIRY_NOTIFICATION_EMAIL), aldrig till kunden.

    send_agency_alert(campaign, subject, lines)
                    om en kampanj: inskick, godkännande, när gränsen för klick
                    på numret nås, och ändringar som syns direkt på en
                    live-sida (publiceringen, paletten, logotypen, ett byte
                    av sida; pagebuilder.alert_live_change)
    send_account_alert(account, subject, lines)
                    om ett konto utan en viss kampanj: en Google-profil som
                    inte liknar kunden (reviews.py)

Samma larm (samma ämnesrad) om samma kampanj skickas högst en gång per
ALERT_EVERY. Det räknas i databasen (Campaign.agency_alerted_at och
agency_alert_subject) med en villkorlig UPDATE, så att ett inskick i en
slinga, eller flera gunicorn-arbetare samtidigt, inte fyller byråns inkorg.
Ämnesraden ska därför säga vad som är nytt (sidans version, paletten), så
att två olika ändringar inom en timme båda larmar. Larmen om ett konto är
högst ACCOUNT_ALERTS_PER_DAY per konto och dag (limits.reserve_daily). Ett
demokonto larmar aldrig. Ett larm fäller aldrig det som larmade.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from apps.inquiries.emails import _as_list

from .models import Campaign

logger = logging.getLogger(__name__)

#: Samma larm om samma kampanj högst en gång på så här lång tid.
ALERT_EVERY = timedelta(hours=1)
SUBJECT_MAX = 200
#: Larm om ett konto (utan kampanj) per konto och svenskt dygn.
ACCOUNT_ALERTS_PER_DAY = 10
USAGE_ACCOUNT_ALERTS = "agency_alerts"


def _mail_ready():
    recipients = _as_list(getattr(settings, "INQUIRY_NOTIFICATION_EMAIL", ""))
    if not recipients:
        return []
    if "smtp" in settings.EMAIL_BACKEND and not getattr(settings, "EMAIL_HOST_USER", ""):
        logger.warning("Flamingo-larmet till byrån hoppades över: e-post är inte inkopplad.")
        return []
    return recipients


def _send(subject, lines, recipients):
    try:
        send_mail(
            subject, "\n".join(lines), settings.DEFAULT_FROM_EMAIL, recipients, fail_silently=False
        )
    except Exception:  # noqa: BLE001 - ett larm får aldrig fälla det som larmade
        logger.exception("Kunde inte skicka Flamingo-larmet till byrån")
        return False
    return True


def send_account_alert(account, subject, lines, now=None):
    """Larm om ett konto, utan en viss kampanj. True om det skickades;
    False för ett demokonto, utan mottagare eller e-post, när kontot redan
    larmat ACCOUNT_ALERTS_PER_DAY gånger i dag, eller om sändningen
    misslyckades (loggas)."""
    from . import limits

    if account.is_demo:
        return False
    recipients = _mail_ready()
    if not recipients:
        return False
    if not limits.reserve_daily(account, USAGE_ACCOUNT_ALERTS, ACCOUNT_ALERTS_PER_DAY, now=now):
        logger.info("Flamingo-larmen om konto %s är slut för i dag", account.pk)
        return False
    return _send(str(subject)[:SUBJECT_MAX], lines, recipients)


def send_agency_alert(campaign, subject, lines, now=None):
    """Skicka larmet till byrån. True om det skickades; False för ett
    demokonto, utan mottagare eller e-post, när samma larm gick nyss, eller
    om sändningen misslyckades (loggas)."""
    if campaign.account.is_demo:
        return False
    recipients = _mail_ready()
    if not recipients:
        return False
    now = now or timezone.now()
    subject = str(subject)[:SUBJECT_MAX]
    claimed = (
        Campaign.objects.filter(pk=campaign.pk)
        .exclude(agency_alert_subject=subject, agency_alerted_at__gte=now - ALERT_EVERY)
        .update(agency_alerted_at=now, agency_alert_subject=subject)
    )
    if not claimed:
        logger.info("Flamingo-larmet om kampanj %s skickades nyss; inte igen", campaign.pk)
        return False
    return _send(subject, lines, recipients)
