"""
Portalens mejl: inbjudan, inloggningskod, notiser till byrån - och de
kundmejl som bara går från en knapp (svar på ärende, loggsammanställning).

Mejl till kunder har en HTML-version i templates/emails/ (logga överst,
vitt kort, tydlig knapp) och en textversion för program som inte visar
HTML. Notiserna till byrån är bara text.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from apps.inquiries.emails import _as_list, _email_configured

logger = logging.getLogger(__name__)


def _base_url():
    return (getattr(settings, "SITE_BASE_URL", "") or "https://adx.se").rstrip("/")


def _send(subject, body, to, reply_to=None, html=None):
    if not _email_configured() or not to:
        logger.warning("Portalmejl hoppades över (okonfigurerad e-post eller ingen mottagare).")
        return False
    try:
        message = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=to,
            reply_to=reply_to or None,
        )
        if html:
            message.attach_alternative(html, "text/html")
        message.send(fail_silently=False)
        return True
    except Exception:
        logger.exception("Kunde inte skicka portalmejl.")
        return False


def _html(template, subject, heading, **context):
    """Kundmejlets HTML-version i den gemensamma ramen (emails/customer_base.html)."""
    return render_to_string(
        f"emails/{template}.html",
        {"subject": subject, "heading": heading, "base_url": _base_url(), **context},
    )


def _greeting(user):
    name = (getattr(user, "first_name", "") or "").strip()
    return f"Hej {name}!" if name else "Hej!"


INVITE_FEATURES = [
    ("Ärenden", "Beställ ändringar, bifoga skärmdumpar och följ vad som händer."),
    ("Övervakning", "Se hur webbplatsen mår, dygnet runt."),
    ("Logg", "Vad vi har gjort för er, datum för datum."),
]


def send_invite(user, customer):
    """Inbjudan: inget lösenord att sätta - man loggar in med sin e-post."""
    login_url = f"{_base_url()}/kund/logga-in/"
    subject = "Välkommen till er kundportal hos ADX"
    body = (
        f"{_greeting(user)}\n\n"
        f"Ni har nu en kundportal hos ADX för {customer.name}. Där har ni allt som rör "
        f"er webbplats samlat på ett ställe:\n\n"
        + "".join(f"- {title}: {text}\n" for title, text in INVITE_FEATURES)
        + f"\nDu loggar in med din e-postadress, {user.email}. Inget lösenord behövs: "
        f"varje gång får du en engångskod på mejl.\n\n"
        f"Logga in: {login_url}\n\n"
        f"Vänliga hälsningar\nADX"
    )
    html = _html(
        "invite",
        subject,
        "Välkommen" + (f", {user.first_name.strip()}" if user.first_name.strip() else ""),
        kicker="Kundportal",
        preheader=f"Er kundportal för {customer.name} är klar.",
        customer=customer,
        user=user,
        features=INVITE_FEATURES,
        login_url=login_url,
        reply_hint="Frågor? Svara på det här mejlet.",
    )
    return _send(
        subject,
        body,
        [user.email],
        reply_to=_as_list(settings.INQUIRY_NOTIFICATION_EMAIL),
        html=html,
    )


def send_login_code(user, code):
    subject = f"Din inloggningskod: {code}"
    body = (
        f"{_greeting(user)}\n\n"
        f"Din inloggningskod till ADX kundportal är:\n\n{code}\n\n"
        f"Koden gäller i tio minuter. Skriv in den på {_base_url()}/kund/kod/\n\n"
        f"Bad du inte om en kod? Då kan du bortse från det här mejlet.\n\nADX"
    )
    html = _html(
        "login_code",
        subject,
        "Din inloggningskod",
        kicker="Kundportal",
        preheader=f"Koden är {code}. Den gäller i tio minuter.",
        code=code,
    )
    return _send(subject, body, [user.email], html=html)


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
    issue_url = ""
    portal = ""
    if issue.visible_to_customer:
        issue_url = f"{_base_url()}/kund/arenden/{issue.pk}/"
        portal = f"\n\nFölj ärendet i portalen: {issue_url}"
    subject = f"{issue.key} {issue.title}"
    body = (
        f"Hej!\n\nDet finns nytt från ADX om {issue.key} {issue.title}:\n\n"
        f"{comment.body}{portal}\n\nVänliga hälsningar\nADX"
    )
    html = _html(
        "issue_update",
        subject,
        issue.title,
        kicker=f"Ärende {issue.key}",
        preheader=comment.body[:120],
        comment=comment,
        issue_url=issue_url,
        reply_hint="Svara gärna direkt på det här mejlet.",
    )
    return _send(
        subject,
        body,
        to,
        reply_to=_as_list(settings.INQUIRY_NOTIFICATION_EMAIL),
        html=html,
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
        "Hej!",
        "",
        f"Här är en sammanställning av vad vi gjort för {customer.name} ({period_label}):",
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
    subject = f"Vad vi gjort för {customer.name}: {period_label}"
    html = _html(
        "log_digest",
        subject,
        f"Vad vi gjort, {period_label}",
        kicker=customer.name,
        preheader=f"{len(entries)} saker vi gjort för er.",
        customer=customer,
        entries=sorted(entries, key=lambda e: (e.date, e.pk)),
        log_url=f"{_base_url()}/kund/logg/",
        reply_hint="Frågor om något av det här? Svara på mejlet.",
    )
    sent = _send(
        subject,
        body,
        to,
        reply_to=_as_list(settings.INQUIRY_NOTIFICATION_EMAIL),
        html=html,
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


def preview(name):
    """(ämne, html) med påhittade uppgifter - för /_mejl/<namn>/ under DEBUG."""
    from datetime import date
    from types import SimpleNamespace as NS

    customer = NS(name="Nordan Bygg AB")
    user = NS(first_name="Nina", email="nina@nordanbygg.se")
    if name == "inbjudan":
        return "Inbjudan", _html(
            "invite",
            "Välkommen till er kundportal hos ADX",
            "Välkommen, Nina",
            kicker="Kundportal",
            customer=customer,
            user=user,
            features=INVITE_FEATURES,
            login_url=f"{_base_url()}/kund/logga-in/",
            reply_hint="Frågor? Svara på det här mejlet.",
        )
    if name == "kod":
        return "Kod", _html(
            "login_code",
            "Din inloggningskod",
            "Din inloggningskod",
            kicker="Kundportal",
            code="482913",
        )
    if name == "arende":
        comment = NS(
            body="Nu är kontaktformuläret lagat.\nTesta gärna och säg till om något ser fel ut."
        )
        return "Ärende", _html(
            "issue_update",
            "NORD-3 Kontaktformuläret",
            "Kontaktformuläret skickar inte",
            kicker="Ärende NORD-3",
            comment=comment,
            issue_url=f"{_base_url()}/kund/arenden/3/",
            reply_hint="Svara gärna direkt på det här mejlet.",
        )
    if name == "logg":
        entries = [
            NS(
                date=date(2026, 9, 7), text="Skyddade sajten mot skannrar med begränsning av anrop."
            ),
            NS(
                date=date(2026, 9, 16),
                text="Uppdaterade utgånget SSL-cert och satte upp automatisk förnyelse.",
            ),
        ]
        return "Logg", _html(
            "log_digest",
            "Vad vi gjort",
            "Vad vi gjort, september 2026",
            kicker=customer.name,
            customer=customer,
            entries=entries,
            log_url=f"{_base_url()}/kund/logg/",
            reply_hint="Frågor om något av det här? Svara på mejlet.",
        )
    return None
