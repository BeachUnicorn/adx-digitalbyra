"""
Email sending for inquiries.

Two emails are dispatched on submission:
1. Confirmation to the customer
2. Notification to ADX (with link to /manage/)

Images are NOT attached - the email mentions count only. Staff views images
in the manage panel.
"""

import logging
import threading

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)


def _email_configured():
    """Return True if SMTP credentials are actually set."""
    return bool(getattr(settings, "EMAIL_HOST_USER", ""))


def _as_list(value):
    """
    Normalise a recipient setting into a clean list of addresses.

    Accepts a list (from env.list) or a comma-separated string. Empty entries
    are dropped.
    """
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value).split(",")
    return [addr.strip() for addr in items if addr and addr.strip()]


def _send_in_thread(target, *args, **kwargs):
    """Fire-and-forget email sending in a background thread."""
    thread = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
    thread.start()


def send_inquiry_confirmation(inquiry, request=None):
    """Send confirmation email to the customer."""
    if not _email_configured():
        logger.warning(
            "Email not configured - skipping customer confirmation for %s",
            inquiry.reference,
        )
        return

    context = {
        "inquiry": inquiry,
        "image_count": inquiry.image_count,
        "site_name": _get_site_name(),
    }

    subject = _("Tack för din förfrågan - %(ref)s") % {"ref": inquiry.reference}
    body_text = render_to_string("inquiries/emails/confirmation_customer.txt", context)
    body_html = render_to_string("inquiries/emails/confirmation_customer.html", context)

    _send_in_thread(_do_send, subject, body_text, body_html, [inquiry.email], inquiry.reference)


def send_inquiry_notification(inquiry, request=None):
    """Send notification email to ADX staff."""
    if not _email_configured():
        logger.warning(
            "Email not configured - skipping staff notification for %s",
            inquiry.reference,
        )
        return

    manage_url = ""
    if request:
        manage_url = request.build_absolute_uri(inquiry.get_absolute_url())

    context = {
        "inquiry": inquiry,
        "image_count": inquiry.image_count,
        "manage_url": manage_url,
        "site_name": _get_site_name(),
    }

    subject = _("Ny förfrågan: %(ref)s - %(name)s") % {
        "ref": inquiry.reference,
        "name": inquiry.name,
    }
    body_text = render_to_string("inquiries/emails/notification_staff.txt", context)
    body_html = render_to_string("inquiries/emails/notification_staff.html", context)

    recipients = _as_list(settings.INQUIRY_NOTIFICATION_EMAIL)
    bcc = _as_list(getattr(settings, "INQUIRY_NOTIFICATION_BCC", ""))

    _send_in_thread(
        _do_send,
        subject,
        body_text,
        body_html,
        recipients,
        inquiry.reference,
        bcc,
    )


def _do_send(subject, body_text, body_html, recipients, reference, bcc=None):
    """Actually send the email (runs in background thread)."""
    if not recipients and not bcc:
        logger.warning("No recipients for %s - skipping send", reference)
        return
    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=body_text,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=recipients or None,
            bcc=bcc or None,
        )
        msg.attach_alternative(body_html, "text/html")
        msg.send(fail_silently=False)
    except Exception:
        logger.exception("Failed to send email for %s", reference)


def _get_site_name():
    """Get site name from SiteSettings, with fallback."""
    try:
        from apps.website.models import SiteSettings

        return SiteSettings.load().name or "ADX"
    except Exception:
        return "ADX"
