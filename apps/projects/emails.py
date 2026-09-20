"""
Portalens mejl: inbjudan (sätt lösenord), notiser till byrån - och ETT
kundmejl, send_issue_update_to_customer, som bara vyn bakom knappen
"Svar + mejl till kunden" får anropa. Ingen annan kod mejlar en kund.
"""

import logging

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.inquiries.emails import _as_list, _email_configured

logger = logging.getLogger(__name__)


def _base_url():
    return (getattr(settings, "SITE_BASE_URL", "") or "https://adx.se").rstrip("/")


def _send(subject, body, to, reply_to=None):
    if not _email_configured() or not to:
        logger.warning("Portalmejl hoppades över (okonfigurerad e-post eller ingen mottagare).")
        return False
    try:
        EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=to,
            reply_to=reply_to or None,
        ).send(fail_silently=False)
        return True
    except Exception:
        logger.exception("Kunde inte skicka portalmejl.")
        return False


def set_password_link(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return f"{_base_url()}/kund/aterstall/{uid}/{token}/"


def send_invite(user, customer):
    body = (
        f"Hej{(' ' + user.first_name) if user.first_name else ''},\n\n"
        f"ni har fått en kundportal hos ADX för {customer.name}. Där skapar ni ärenden, "
        f"bifogar skärmdumpar och dokument, och följer vad som pågår.\n\n"
        f"Välj ditt lösenord här (länken gäller i tre dagar):\n{set_password_link(user)}\n\n"
        f"Därefter loggar du in på {_base_url()}/kund/ med {user.email}.\n\n"
        f"Vänliga hälsningar\nADX"
    )
    return _send(
        "Din kundportal hos ADX",
        body,
        [user.email],
        reply_to=_as_list(settings.INQUIRY_NOTIFICATION_EMAIL),
    )


def send_portal_issue_notice(issue, customer):
    body = (
        f"{customer.name} skapade ett ärende i portalen:\n\n"
        f"{issue.key}: {issue.title}\n\n{issue.description}\n\n"
        f"Bilagor: {issue.attachments.count()}\n\n"
        f"Öppna: {_base_url()}/manage/arenden/{issue.pk}/"
    )
    return _send(
        f"Nytt ärende från {customer.name}: {issue.title}",
        body,
        _as_list(settings.INQUIRY_NOTIFICATION_EMAIL),
    )


def customer_recipients(customer):
    """Kundens adresser: portalkontakterna plus kundens egen, utan dubbletter."""
    seen, out = set(), []
    addresses = [u.email for u in customer.users.all()] + [customer.email]
    for address in addresses:
        address = (address or "").strip().lower()
        if address and address not in seen:
            seen.add(address)
            out.append(address)
    return out


def send_issue_update_to_customer(issue, comment):
    """
    Mejla kunden ett svar på ett ärende. Anropas BARA av
    manage_views.issue_email_customer - alltså bara när någon tryckt på
    knappen som uttryckligen säger att kunden mejlas.
    """
    customer = issue.effective_customer
    if customer is None:
        return False
    to = customer_recipients(customer)
    if not to:
        return False
    portal = ""
    if issue.visible_to_customer:
        portal = f"\n\nFölj ärendet i portalen: {_base_url()}/kund/arenden/{issue.pk}/"
    body = (
        f"Hej,\n\nnytt från ADX om {issue.key} {issue.title}:\n\n{comment.body}{portal}\n\n"
        f"Vänliga hälsningar\nADX"
    )
    return _send(
        f"{issue.key} {issue.title}",
        body,
        to,
        reply_to=_as_list(settings.INQUIRY_NOTIFICATION_EMAIL),
    )


def send_portal_comment_notice(comment, customer):
    body = (
        f"{customer.name} kommenterade {comment.issue.key} {comment.issue.title}:\n\n"
        f"{comment.body}\n\nÖppna: {_base_url()}/manage/arenden/{comment.issue.pk}/"
    )
    return _send(
        f"Kommentar från {customer.name} på {comment.issue.key}",
        body,
        _as_list(settings.INQUIRY_NOTIFICATION_EMAIL),
    )
