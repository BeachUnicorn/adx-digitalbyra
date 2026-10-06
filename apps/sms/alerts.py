"""
Larm till byrån (INQUIRY_NOTIFICATION_EMAIL), aldrig till kunden:

- kunden har nått sitt kostnadstak: en gång per konto och månad;
- 46elks tog inte emot ett sms: högst en gång i timmen per konto;
- 46elks svar på en sändning var oklart (provider_unknown): samma gräns som
  ovan. Sms:en att stämma av listas alltid på /manage/sms/, så ett larm som
  hoppas över tappar inget.

Gränserna räknas i databasen med villkorliga UPDATE:s (samma mönster som
apps/flamingo/alerts.py), så att flera arbetare eller en kund som slår i
taket med hundra anrop i sekunden inte fyller byråns inkorg. Ett larm fäller
aldrig det som larmade.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.inquiries.emails import _as_list

from .models import SmsAccount

logger = logging.getLogger(__name__)

PROVIDER_ALERT_EVERY = timedelta(hours=1)


def _recipients():
    recipients = _as_list(getattr(settings, "INQUIRY_NOTIFICATION_EMAIL", ""))
    if not recipients:
        return []
    if "smtp" in settings.EMAIL_BACKEND and not getattr(settings, "EMAIL_HOST_USER", ""):
        logger.warning("SMS-larmet till byrån hoppades över: e-post är inte inkopplad.")
        return []
    return recipients


def _card_link(account):
    path = reverse("manage:customer_detail", args=[account.customer_id]) + "#sms"
    base = (getattr(settings, "SITE_BASE_URL", "") or "").rstrip("/")
    return f"{base}{path}"


def _send(subject, lines):
    recipients = _recipients()
    if not recipients:
        return False
    try:
        send_mail(
            subject[:200],
            "\n".join(lines),
            settings.DEFAULT_FROM_EMAIL,
            recipients,
            fail_silently=False,
        )
    except Exception:  # noqa: BLE001 - ett larm får aldrig fälla sändningen
        logger.exception("Kunde inte skicka SMS-larmet till byrån")
        return False
    return True


def cap_reached(account, period, cost_text, cap_text):
    """Kunden har slagit i taket. En gång per konto och månad."""
    try:
        if not _recipients():
            return False
        claimed = (
            SmsAccount.objects.filter(pk=account.pk)
            .filter(Q(cap_alerted_period__isnull=True) | ~Q(cap_alerted_period=period))
            .update(cap_alerted_period=period)
        )
        if not claimed:
            return False
        name = account.customer.name
        return _send(
            f"SMS: {name} har nått kostnadstaket ({period:%Y-%m})",
            [
                f"{name} har nått sitt kostnadstak för SMS i {period:%Y-%m}.",
                f"Hittills i månaden: {cost_text} kr av taket {cap_text} kr (utan moms).",
                "API:t stoppar nya sms med felet monthly_cap_reached tills kunden höjer",
                "taket i portalen eller månaden tar slut.",
                "",
                "Kunden har inte mejlats.",
                "",
                f"Kundkortet: {_card_link(account)}",
            ],
        )
    except Exception:  # noqa: BLE001
        logger.exception("SMS-larmet om taket misslyckades (konto %s)", account.pk)
        return False


def _claim_provider_alert(account, now):
    """Högst ett larm om 46elks i timmen per konto (villkorlig UPDATE)."""
    return (
        SmsAccount.objects.filter(pk=account.pk)
        .filter(
            Q(provider_alerted_at__isnull=True)
            | Q(provider_alerted_at__lt=now - PROVIDER_ALERT_EVERY)
        )
        .update(provider_alerted_at=now)
    )


def provider_failed(account, message, detail, now=None):
    """46elks tog inte emot ett sms. Högst en gång i timmen per konto."""
    try:
        if not _recipients():
            return False
        if not _claim_provider_alert(account, now or timezone.now()):
            return False
        name = account.customer.name
        return _send(
            f"SMS: 46elks tog inte emot ett sms från {name}",
            [
                f"Ett sms från {name} gick inte fram till 46elks och har inte debiterats.",
                f"Sms {message.pk} till {message.country or '?'}: {detail}",
                "",
                "Fler fel från samma kund larmas högst en gång i timmen.",
                "Kunden har inte mejlats; API:t svarade provider_error.",
                "",
                f"Kundkortet: {_card_link(account)}",
            ],
        )
    except Exception:  # noqa: BLE001
        logger.exception("SMS-larmet om 46elks misslyckades (konto %s)", account.pk)
        return False


def provider_unknown(account, message, detail, now=None):
    """46elks svar på en sändning var oklart: sms:et kan ha skickats och står
    kvar som reserverat. Högst en gång i timmen per konto."""
    try:
        if not _recipients():
            return False
        if not _claim_provider_alert(account, now or timezone.now()):
            return False
        name = account.customer.name
        base = (getattr(settings, "SITE_BASE_URL", "") or "").rstrip("/")
        return _send(
            f"SMS: kontrollera ett sms från {name} mot 46elks",
            [
                f"46elks svarade inte säkert på ett sms från {name}: det kan ha skickats.",
                f"Sms {message.pk} till {message.country or '?'}: {detail}",
                "",
                "Sms:et står kvar som reserverat med uppskattat pris, och kundens",
                "reference är låst så att inget skickas två gånger. En leveransrapport",
                "från 46elks avgör läget av sig själv; annars stäm av det i 46elks",
                "sms-historik och markera det på SMS-översikten.",
                "",
                "Fler fel från samma kund larmas högst en gång i timmen.",
                "Kunden har inte mejlats; API:t svarade 202 med status unknown.",
                "",
                f"Att stämma av: {base}{reverse('manage:sms_overview')}#kontrollera",
            ],
        )
    except Exception:  # noqa: BLE001
        logger.exception("SMS-larmet om oklart svar misslyckades (konto %s)", account.pk)
        return False


def sender_changed(account, old, new, user):
    """Kunden (eller byrån i kundvyn) bytte avsändarnamn. Larm till byrån, så
    att ett olämpligt namn upptäcks; kunden mejlas inte."""
    who = ""
    if user and user.is_authenticated:
        who = user.get_full_name() or user.email or user.get_username()
    return _send(
        f"SMS: {account.customer.name} bytte avsändare till {new}",
        [
            f"{account.customer.name} har bytt avsändarnamn för sms.",
            f"Förut: {old or '(inget)'}",
            f"Nu: {new}",
            f"Ändrat av: {who or 'okänd'}",
            "",
            f"Kundkortet: {_card_link(account)}",
        ],
    )
