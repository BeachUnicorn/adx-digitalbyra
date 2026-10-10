"""
Mejlen som rå MIME (README D.6): From med namnet kodat, To, Subject, Date,
multipart/alternative med text/plain före text/html, UTF-8 och
quoted-printable. Inga bilagor; bilder med absoluta adresser.

    build(mail) -> bytes      mail är transport.OutgoingMail
    parse(raw) -> EmailMessage    läsa tillbaka (testerna, FakeSes)

Det enda stället i apps/utskick som använder standardbibliotekets
email.message (vakten i test_s1_guards). S1 bygger bara bekräftelsemejlet;
List-Unsubscribe, Reply-To och resten kommer med utskicken i S3.

S3 (foundation): huvudena för ett utskicksmejl (D.6) går in som
mail.headers och skrivs av build() som de andra:

    unsubscribe_headers(https_url, mailto_url) -> dict
        {"List-Unsubscribe": "<https://klick.adx.se/a/...>, <mailto:s+u...@svar...>",
         "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
        (links.unsubscribe_url och links.mailto_unsubscribe)

Reply-To (tokens.reply_address eller kundens bekräftade egna adress) och
In-Reply-To (svar från Inkorgen) läggs på samma sätt av sändnings-byggaren.

S3 (renderaren): MAIL_POLICY är SMTP-policyn, men huvudena med adresser
och meddelande-id (UNFOLDED: List-Unsubscribe, List-Unsubscribe-Post,
In-Reply-To, References) viks bara mellan posterna och kodas aldrig med
RFC 2047. Standardbibliotekets vikning kodar annars en adress som är längre
än 78 tecken som =?utf-8?q?...?=, och då hittar Gmail varken länken eller
ettklicket.
"""

from email import message_from_bytes
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import EmailPolicy
from email.policy import default as default_policy
from email.utils import format_datetime, make_msgid

from django.conf import settings
from django.utils import timezone


def _one_line(value):
    """Ett huvudvärde på en rad: inga radbrytningar kan smyga in nya huvuden."""
    return " ".join(str(value or "").split())


# -- S3 (renderaren): vikningen av adresshuvudena -----------------------------

#: Huvuden som aldrig kodas med RFC 2047 (de bär adresser och id).
UNFOLDED = frozenset({"list-unsubscribe", "list-unsubscribe-post", "in-reply-to", "references"})
#: RFC 5322 2.1.1: en rad får vara högst 998 tecken.
HARD_LINE_LIMIT = 998


class _MailPolicy(EmailPolicy):
    """email.policy.SMTP, men UNFOLDED viks bara efter ett kommatecken eller
    blanksteg mellan posterna (aldrig inne i en adress) och kodas aldrig."""

    def _plain_fold(self, name, value):
        text = " ".join(str(value).split())
        if not text.isascii() or len(name) + 2 + len(text) > HARD_LINE_LIMIT * 4:
            return None
        parts = text.split(" ")
        lines, line = [], f"{name}:"
        for part in parts:
            candidate = f"{line} {part}"
            if len(candidate) > self.max_line_length and line.strip() != f"{name}:":
                lines.append(line)
                line = f" {part}"
            else:
                line = candidate
        lines.append(line)
        if any(len(row) > HARD_LINE_LIMIT for row in lines):
            return None
        return self.linesep.join(lines) + self.linesep

    def fold(self, name, value):
        if name.lower() in UNFOLDED:
            folded = self._plain_fold(name, value)
            if folded is not None:
                return folded
        return super().fold(name, value)

    def fold_binary(self, name, value):
        if name.lower() in UNFOLDED:
            folded = self._plain_fold(name, value)
            if folded is not None:
                return folded.encode("ascii")
        return super().fold_binary(name, value)


MAIL_POLICY = _MailPolicy(linesep="\r\n")


# -- slut på S3-blocket ----------------------------------------------------------


def build(mail):
    """Mejlet som byte, redo för SES (Content.Raw) eller en .eml-fil."""
    msg = EmailMessage(policy=MAIL_POLICY)
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


#: Värdet i List-Unsubscribe-Post (RFC 8058): mejlprogrammet avregistrerar
#: med en POST utan att öppna sidan.
ONE_CLICK = "List-Unsubscribe=One-Click"


def unsubscribe_headers(https_url, mailto_url):
    """List-Unsubscribe och List-Unsubscribe-Post för ett utskicksmejl (D.6).
    Båda adresserna måste finnas: Gmail och Yahoo kräver https-adressen och
    ettklicket, äldre program använder mailto."""
    https_url, mailto_url = str(https_url or ""), str(mailto_url or "")
    # http bara lokalt (klick.localhost); i drift är basen https.
    if not https_url.startswith(("https://", "http://")):
        raise ValueError("List-Unsubscribe behöver en http(s)-adress.")
    if not mailto_url.startswith("mailto:"):
        raise ValueError("List-Unsubscribe behöver en mailto-adress.")
    return {
        "List-Unsubscribe": f"<{https_url}>, <{mailto_url}>",
        "List-Unsubscribe-Post": ONE_CLICK,
    }
