"""
Krokar för appar som skickar sms genom apps/sms utan att sms-appen känner
till dem (apps/utskick/README.md, C.1). apps/sms importerar inget härifrån
ut; den som vill veta något registrerar sig i sin AppConfig.ready():

    hooks.register_status_callback(func)   func(message) när en
                                           leveransrapport eller byråns
                                           avstämning ändrat sms:ets läge
    hooks.register_labeler(func)           func(messages) -> {pk: etikett}
                                           för portalens lista

    hooks.status_changed(message)          anropas av service.apply_delivery_report
                                           och service.resolve_check
    hooks.labels(messages)                 portalens etiketter för sms som
                                           inte kom från API:t

En krok får aldrig fälla det som anropade den: varje anrop går i en egen
savepoint, och ett fel loggas (Sentry) och sväljs. Leveransrapporten till
46elks får alltså sitt svar som förut, och ett fel i utskicken lämnar
mottagarens läge efter, aldrig sms:ets.
"""

import logging

from django.db import transaction

logger = logging.getLogger(__name__)

#: Anropas med sms:et när dess läge ändrats (utskick: sending.sms.sync_from_message).
STATUS_CALLBACKS = []
#: Anropas med en lista sms och ger {pk: etikett} ("Utskick: Höstservice").
LABELERS = []

#: Etiketten när ingen krok gav en, per källa (SmsMessage.Source).
FALLBACK_LABELS = {
    "utskick": "Utskick",
    "flow": "Flöde",
    "reply": "Svar i Inkorgen",
    "system": "Bekräftelse från Flamingo",
    "test": "Test",
}


def register_status_callback(func):
    """Lägg till func (en gång, också om ready() körs två gånger)."""
    if func not in STATUS_CALLBACKS:
        STATUS_CALLBACKS.append(func)
    return func


def register_labeler(func):
    if func not in LABELERS:
        LABELERS.append(func)
    return func


def status_changed(message):
    """Sms:ets läge har ändrats. Kör varje registrerad krok; ett fel fäller
    aldrig anroparen."""
    for func in list(STATUS_CALLBACKS):
        try:
            with transaction.atomic():
                func(message)
        except Exception:  # noqa: BLE001 - kroken får aldrig fälla leveransrapporten
            name = getattr(func, "__name__", func)
            logger.exception("SMS: kroken %s föll för sms %s", name, message.pk)


def labels(messages):
    """{pk: etikett} för sms som inte kom från API:t. Krokarna först, sedan
    FALLBACK_LABELS. API:ts sms får ingen etikett (portalen visar deras
    reference som förut)."""
    rows = [m for m in messages if getattr(m, "source", "api") != "api"]
    if not rows:
        return {}
    found = {}
    for func in list(LABELERS):
        try:
            found.update({pk: text for pk, text in (func(rows) or {}).items() if text})
        except Exception:  # noqa: BLE001 - en etikett får aldrig fälla portalen
            logger.exception("SMS: etiketterna föll (%s)", getattr(func, "__name__", func))
    return {m.pk: found.get(m.pk) or FALLBACK_LABELS.get(m.source, "") for m in rows}
