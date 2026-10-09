"""
Mejlen som rå MIME (README D.6): From med namnet kodat, To, Subject, Date,
multipart/alternative med text/plain före text/html, UTF-8 och
quoted-printable. Inga bilagor; bilder med absoluta adresser.

    build(mail) -> bytes      mail är transport.OutgoingMail
    parse(raw) -> EmailMessage    läsa tillbaka (testerna, FakeSes)

Det enda stället i apps/utskick som använder standardbibliotekets
email.message (vakten i test_s1_guards). S1 bygger bara bekräftelsemejlet;
List-Unsubscribe, Reply-To och resten kommer med utskicken i S3.
"""

from email import message_from_bytes
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from email.policy import default as default_policy
from email.utils import format_datetime, make_msgid

from django.conf import settings
from django.utils import timezone


def _one_line(value):
    """Ett huvudvärde på en rad: inga radbrytningar kan smyga in nya huvuden."""
    return " ".join(str(value or "").split())


def build(mail):
    """Mejlet som byte, redo för SES (Content.Raw) eller en .eml-fil."""
    msg = EmailMessage(policy=SMTP)
    msg["From"] = Address(
        display_name=_one_line(mail.from_name), addr_spec=_one_line(mail.from_addr)
    )
    msg["To"] = Address(addr_spec=_one_line(mail.to))
    msg["Subject"] = _one_line(mail.subject)
    msg["Date"] = format_datetime(timezone.now())
    domain = getattr(settings, "UTSKICK_ADX_MAIL_DOMAIN", "") or "utskick.adx.se"
    msg["Message-ID"] = make_msgid(domain=domain)
    for name, value in (mail.headers or {}).items():
        msg[_one_line(name)] = _one_line(value)
    msg.set_content(mail.text, subtype="plain", charset="utf-8", cte="quoted-printable")
    if mail.html:
        msg.add_alternative(mail.html, subtype="html", charset="utf-8", cte="quoted-printable")
    return msg.as_bytes()


def parse(raw):
    """Ett byggt mejl tillbaka som EmailMessage (för att läsa huvuden och delar)."""
    return message_from_bytes(raw, policy=default_policy)


def text_part(message, subtype="plain"):
    """Textdelens innehåll (plain eller html) ur ett parse()-at mejl."""
    part = message.get_body(preferencelist=(subtype,))
    return part.get_content() if part is not None else ""
