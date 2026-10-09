"""
Kopplingen till apps/sms (README C.1, D.4): apps/sms importerar inget från
utskick, utan anropar krokarna i apps/sms/hooks.py som UtskickConfig.ready()
registrerar härifrån.

    sync_from_message(message)   leveransrapporten eller byråns avstämning
                                 flyttar mottagarna och trådens meddelanden
                                 som bär sms:et framåt, aldrig bakåt
    labels_for(messages)         {pk: "Utskick: Höstservice värmepump"} för
                                 kundportalens sms-lista

Mottagarens läge följer sms:et så här (Recipient.RANK, D.5):

    sms sent        sending, unknown          -> sent (sent_at om det saknas)
    sms delivered   sending, sent, unknown    -> delivered (delivered_at)
    sms failed      sending, sent, unknown    -> failed (med felet)

Trådens utgående meddelande: sent/delivered gör sending till sent, failed
gör sending och sent till failed. Villkoren ligger i UPDATE:arna, så en
rapport som kommer före slingans egen skrivning eller två gånger ändrar
inget i onödan. Inga personuppgifter i loggen.
"""

from django.db.models import Q
from django.db.models.functions import Coalesce

from .models import Recipient, ThreadMessage

#: Texten på en mottagare vars sms inte kom fram.
FAILED_TEXT = "Operatören kunde inte leverera sms:et."
NOT_SENT_TEXT = "Sms:et skickades inte."


def sync_from_message(message):
    """Krok för apps.sms.hooks.status_changed. Returnerar antalet ändrade
    mottagare (för testerna)."""
    status = message.status
    rows = Recipient.objects.filter(sms_message_id=message.pk)
    threads = ThreadMessage.objects.filter(sms_message_id=message.pk)
    R, T = Recipient.Status, ThreadMessage.Status
    changed = 0
    if status == "sent":
        changed = rows.filter(status__in=(R.SENDING, R.UNKNOWN)).update(
            status=R.SENT,
            sent_at=Coalesce("sent_at", message.sent_at or message.created_at),
        )
        threads.filter(status=T.SENDING).update(status=T.SENT)
    elif status == "delivered":
        changed = rows.filter(status__in=(R.SENDING, R.SENT, R.UNKNOWN)).update(
            status=R.DELIVERED,
            delivered_at=message.delivered_at or message.updated_at,
            sent_at=Coalesce("sent_at", message.sent_at or message.created_at),
        )
        threads.filter(status=T.SENDING).update(status=T.SENT)
    elif status == "failed":
        text = NOT_SENT_TEXT if message.error_code == "provider_error" else FAILED_TEXT
        changed = rows.filter(status__in=(R.SENDING, R.SENT, R.UNKNOWN)).update(
            status=R.FAILED, error=text
        )
        threads.filter(Q(status=T.SENDING) | Q(status=T.SENT)).update(status=T.FAILED)
    return changed


def labels_for(messages):
    """Etiketterna i kundportalens sms-lista för sms från utskicken: namnet
    på utskicket ("Utskick: Höstservice värmepump", flöden "Flöde: ...").
    En fråga för hela sidan. Sms utan mottagare (svar, bekräftelser, test)
    får apps/sms egna etiketter (hooks.FALLBACK_LABELS)."""
    pks = [m.pk for m in messages if m.source in ("utskick", "flow")]
    if not pks:
        return {}
    names = dict(
        Recipient.objects.filter(sms_message_id__in=pks).values_list(
            "sms_message_id", "utskick__name"
        )
    )
    prefix = {"utskick": "Utskick", "flow": "Flöde"}
    return {
        m.pk: f"{prefix[m.source]}: {names[m.pk]}"
        for m in messages
        if m.pk in names and names[m.pk] and m.source in prefix
    }
