"""
Offertmejlen. Samma leveransväg som förfrågningsmejlen (SES via SMTP),
samma hållning: mejl får ALDRIG fälla en request - misslyckad sändning
loggas och rapporteras som False, aldrig som ett 500 mot kunden.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from apps.inquiries.emails import _as_list, _email_configured

logger = logging.getLogger(__name__)


def _base_url():
    return (getattr(settings, "SITE_BASE_URL", "") or "https://adx.se").rstrip("/")


def _send(subject, template, context, to, reply_to=None):
    if not _email_configured() or not to:
        logger.warning("Offertmejl hoppades över (e-post okonfigurerad eller mottagare saknas).")
        return False
    context = {**context, "base_url": _base_url()}
    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=render_to_string(f"offers/emails/{template}.txt", context),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=to,
            reply_to=reply_to or None,
        )
        msg.attach_alternative(
            render_to_string(f"offers/emails/{template}.html", context), "text/html"
        )
        msg.send(fail_silently=False)
        return True
    except Exception:
        logger.exception("Kunde inte skicka offertmejl (%s).", template)
        return False


def send_quote_to_customer(quote):
    """Offertlänken till kunden. Svar på mejlet går direkt till oss."""
    return _send(
        subject=f"Offert från ADX{': ' + quote.project_title if quote.project_title else ''}",
        template="quote_customer",
        context={"quote": quote},
        to=[quote.customer_email],
        reply_to=_as_list(settings.INQUIRY_NOTIFICATION_EMAIL),
    )


def send_accepted_notification(quote):
    """Till oss: kunden tryckte Acceptera. Det här är mejlet man vill få."""
    return _send(
        subject=f"ACCEPTERAD: {quote.customer_name} - {quote.project_title or 'offert'}",
        template="accepted_staff",
        context={"quote": quote, "totals": quote.totals_display()},
        to=_as_list(settings.INQUIRY_NOTIFICATION_EMAIL),
        reply_to=[quote.customer_email] if quote.customer_email else None,
    )


def send_question_to_staff(quote, message):
    """Kundens fråga från offertsidan, med reply-to satt till kunden."""
    return _send(
        subject=f"Fråga om offert: {quote.customer_name}",
        template="question_staff",
        context={"quote": quote, "message": message},
        to=_as_list(settings.INQUIRY_NOTIFICATION_EMAIL),
        reply_to=[quote.customer_email] if quote.customer_email else None,
    )
