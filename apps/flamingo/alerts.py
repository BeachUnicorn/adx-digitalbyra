"""
Larm till byrån om en kampanj (INQUIRY_NOTIFICATION_EMAIL), aldrig till
kunden: inskick, godkännande och när gränsen för klick på numret nås.

Samma larm (samma ämnesrad) om samma kampanj skickas högst en gång per
ALERT_EVERY. Det räknas i databasen (Campaign.agency_alerted_at och
agency_alert_subject) med en villkorlig UPDATE, så att ett inskick i en
slinga, eller flera gunicorn-arbetare samtidigt, inte fyller byråns inkorg.
Ett demokonto larmar aldrig. Ett larm fäller aldrig det som larmade.
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


def send_agency_alert(campaign, subject, lines, now=None):
    """Skicka larmet till byrån. True om det skickades; False för ett
    demokonto, utan mottagare eller e-post, när samma larm gick nyss, eller
    om sändningen misslyckades (loggas)."""
    if campaign.account.is_demo:
        return False
    recipients = _as_list(getattr(settings, "INQUIRY_NOTIFICATION_EMAIL", ""))
    if not recipients:
        return False
    if "smtp" in settings.EMAIL_BACKEND and not getattr(settings, "EMAIL_HOST_USER", ""):
        logger.warning("Flamingo-larmet till byrån hoppades över: e-post är inte inkopplad.")
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
    try:
        send_mail(
            subject, "\n".join(lines), settings.DEFAULT_FROM_EMAIL, recipients, fail_silently=False
        )
    except Exception:  # noqa: BLE001 - ett larm får aldrig fälla det som larmade
        logger.exception("Kunde inte skicka Flamingo-larmet till byrån")
        return False
    return True
