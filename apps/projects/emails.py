"""
Portalens mejl: inbjudan (sätt lösenord), notiser till byrån - och ETT
kundmejl, send_issue_update_to_customer, som bara vyn bakom knappen
"Svar + mejl till kunden" får anropa. Ingen annan kod mejlar en kund.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

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


def send_invite(user, customer):
    """Inbjudan: inget lösenord att sätta - man loggar in med sin e-post."""
    body = (
        f"Hej{(' ' + user.first_name) if user.first_name else ''},\n\n"
        f"ni har fått en kundportal hos ADX för {customer.name}. Där skapar ni ärenden, "
        f"bifogar skärmdumpar och dokument, och följer vad som pågår.\n\n"
        f"Logga in på {_base_url()}/kund/ med den här e-postadressen ({user.email}). "
        f"Inget lösenord behövs: du får en engångskod på mejl varje gång.\n\n"
        f"Vänliga hälsningar\nADX"
    )
    return _send(
        "Din kundportal hos ADX",
        body,
        [user.email],
        reply_to=_as_list(settings.INQUIRY_NOTIFICATION_EMAIL),
    )


def send_login_code(user, code):
    body = (
        f"Hej,\n\ndin inloggningskod till ADX kundportal är:\n\n{code}\n\n"
        f"Koden gäller i tio minuter. Skriv in den på {_base_url()}/kund/kod/\n\n"
        f"Bad du inte om en kod kan du bortse från det här mejlet.\n\nADX"
    )
    return _send(f"Din inloggningskod: {code}", body, [user.email])


def send_portal_issue_notice(issue, customer):
    body = (
        f"{customer.name} skapade ett ärende i portalen:\n\n"
        f"{issue.key}: {issue.title}\n\n{issue.description_text}\n\n"
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


def log_period_label(entries):
    """'september 2026' om alla poster är i samma månad, annars 'augusti till september 2026'."""
    from django.utils.formats import date_format

    dates = sorted(e.date for e in entries)
    first, last = dates[0], dates[-1]
    if (first.year, first.month) == (last.year, last.month):
        return date_format(first, "F Y")
    if first.year == last.year:
        return f"{date_format(first, 'F')} till {date_format(last, 'F Y')}"
    return f"{date_format(first, 'F Y')} till {date_format(last, 'F Y')}"


def log_digest_body(customer, entries, period_label):
    from django.utils.formats import date_format

    lines = [
        "Hej,",
        "",
        f"här är en sammanställning av vad vi gjort för {customer.name} ({period_label}):",
        "",
    ]
    for entry in sorted(entries, key=lambda e: (e.date, e.pk)):
        lines.append(f"- {date_format(entry.date, 'j F')}: {entry.text.strip()}")
    lines += [
        "",
        f"Hela historiken finns i portalen: {_base_url()}/kund/logg/",
        "",
        "Vänliga hälsningar",
        "ADX",
    ]
    return "\n".join(lines)


def send_log_digest(customer, entries, period_label):
    """
    Månadssammanställningen till kunden. Anropas BARA av
    manage_views.customer_log_send - knappen. Returnerar (skickat, mottagare, brödtext).
    """
    to = customer_recipients(customer)
    body = log_digest_body(customer, entries, period_label)
    if not to:
        return False, to, body
    sent = _send(
        f"Vad vi gjort för {customer.name}: {period_label}",
        body,
        to,
        reply_to=_as_list(settings.INQUIRY_NOTIFICATION_EMAIL),
    )
    return sent, to, body


def send_log_reminder(rows):
    """Till byrån: kunder med osända loggposter, en vecka före månadsskiftet."""
    lines = ["Om en vecka är månaden slut. Osända loggposter:", ""]
    for customer, count in rows:
        lines.append(f"- {customer.name}: {count} post{'' if count == 1 else 'er'}")
        lines.append(f"  {_base_url()}/manage/kunder/{customer.pk}/#logg")
    lines += ["", "Sammanställningen skickas bara när du trycker på knappen på kundens sida."]
    return _send(
        f"Påminnelse: skicka loggen till {len(rows)} kund{'' if len(rows) == 1 else 'er'}",
        "\n".join(lines),
        _as_list(settings.INQUIRY_NOTIFICATION_EMAIL),
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
