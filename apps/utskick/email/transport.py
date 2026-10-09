"""
E-posten ut från utskick (README D.6, D.8, H.8). S1: bara bekräftelsemejlet
(kind="doi"); utskicken, testmejlen och sonden kommer i S3.

    can_send() -> bool            går det att skicka alls i den här miljön?
    send(mail, kind=...) -> Sent  ett mejl
    class OutgoingMail            det som skickas
    class Sent                    hur det gick (ok, retry, stop, error)
    class FakeSes                 SES i testerna

Vägarna, i ordning:

1. UTSKICK_EMAIL_LIVE: SES v2 SendEmail med rå MIME (email/mime.py) i
   UTSKICK_SES_REGION genom aws.client("sesv2", send=True), som aldrig gör
   om ett anrop (SendEmail saknar idempotensnyckel). Det enda stället i
   apps/utskick som anropar send_email (vakten i test_s1_guards).
2. Av, men DEBUG: mejlet skrivs som en .eml-fil i
   PRIVATE_MEDIA_ROOT/utskick-mail/ (lokalt, för att se mejlet).
3. Av i drift: inget skickas och inget skrivs till disk (D.8).

Felen från SES (D.6):

    TooManyRequests, LimitExceeded, Throttling   retry (vänta, försök nästa tick)
    SendingPaused, AccountSuspended              stop: inget mer skickas, byrån larmas
    MailFromDomainNotVerified, AWS saknas        stop, byrån larmas
    läs-timeout, 5xx                             unknown: kanske skickat, görs aldrig om
    MessageRejected och andra 4xx                failed: just det här mejlet

Mejlets innehåll loggas aldrig, och inte adressen (bara slaget och
SES-id:t).
"""

import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from . import mime

logger = logging.getLogger(__name__)

DOI = "doi"
#: Slagen av mejl som går genom transporten i S1.
KINDS = (DOI,)

#: Mappen för .eml-filer lokalt (bara med DEBUG).
EML_FOLDER = "utskick-mail"

_RETRY_CODES = frozenset(
    {"TooManyRequestsException", "LimitExceededException", "Throttling", "ThrottlingException"}
)
_STOP_CODES = frozenset(
    {
        "SendingPausedException",
        "AccountSuspendedException",
        "MailFromDomainNotVerifiedException",
        "AccessDenied",
        "AccessDeniedException",
        "UnrecognizedClientException",
        "InvalidClientTokenId",
        "ExpiredTokenException",
    }
)


@dataclass
class OutgoingMail:
    to: str
    from_name: str
    from_addr: str
    subject: str
    text: str
    html: str = ""
    headers: dict = field(default_factory=dict)


@dataclass
class Sent:
    """Hur det gick. ok: skickat (eller skrivet som .eml). retry: inget
    skickades, försök igen senare. stop: inget mer ska skickas i den här
    körningen (byrån är larmad). unknown: kanske skickat, gör inte om."""

    ok: bool
    message_id: str = ""
    mode: str = ""
    error: str = ""
    retry: bool = False
    stop: bool = False
    unknown: bool = False


def _live():
    return _FAKE is not None or bool(getattr(settings, "UTSKICK_EMAIL_LIVE", False))


def can_send():
    """Kan transporten leverera något alls här? SES i drift, .eml lokalt."""
    return _live() or bool(settings.DEBUG)


def send(mail, *, kind, account_id=None):
    """Skicka ett mejl av slaget kind. Kastar aldrig för fel hos SES."""
    if kind not in KINDS:
        raise ValueError(f"Okänt slag av mejl: {kind!r}")
    raw = mime.build(mail)
    if _live():
        return _send_ses(mail, raw, kind, account_id)
    if settings.DEBUG:
        return _write_eml(raw, kind)
    return Sent(ok=False, error="email_off", stop=True)


def _tags(kind, account_id):
    tags = [{"Name": "k", "Value": kind}]
    if account_id:
        tags.append({"Name": "a", "Value": str(int(account_id))})
    return tags


def _send_ses(mail, raw, kind, account_id):
    from botocore.exceptions import BotoCoreError, ClientError, ReadTimeoutError

    from .. import aws

    try:
        client = _FAKE or aws.client("sesv2", send=True)
    except (aws.AwsNotConfigured, BotoCoreError, ClientError) as exc:
        logger.error("Utskick: ingen AWS-session för e-post (%s)", type(exc).__name__)
        _alert_stop("aws", type(exc).__name__)
        return Sent(ok=False, error="aws", stop=True)
    try:
        answer = client.send_email(
            FromEmailAddress=mail.from_addr,
            Destination={"ToAddresses": [mail.to]},
            Content={"Raw": {"Data": raw}},
            EmailTags=_tags(kind, account_id),
        )
    except ReadTimeoutError:
        logger.warning("Utskick: SES svarade inte i tid (%s), görs inte om", kind)
        return Sent(ok=False, error="timeout", unknown=True)
    except ClientError as exc:
        error = exc.response.get("Error", {}) if hasattr(exc, "response") else {}
        code = error.get("Code", "") or type(exc).__name__
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if code in _RETRY_CODES or status == 429:
            logger.info("Utskick: SES bromsar (%s)", code)
            return Sent(ok=False, error=code, retry=True)
        if code in _STOP_CODES:
            logger.error("Utskick: SES stoppar e-posten (%s)", code)
            _alert_stop(code, kind)
            return Sent(ok=False, error=code, retry=True, stop=True)
        if status >= 500:
            logger.warning("Utskick: SES fel %s (%s), görs inte om", status, code)
            return Sent(ok=False, error=code, unknown=True)
        logger.warning("Utskick: SES nekade mejlet (%s, %s)", kind, code)
        return Sent(ok=False, error=code)
    except BotoCoreError as exc:
        logger.warning("Utskick: anropet till SES föll (%s)", type(exc).__name__)
        return Sent(ok=False, error=type(exc).__name__, unknown=True)
    message_id = str(answer.get("MessageId", ""))
    logger.info("Utskick: %s skickat via SES (%s)", kind, message_id)
    return Sent(ok=True, message_id=message_id, mode="ses")


def _alert_stop(code, detail):
    from .. import alerts

    alerts.agency(
        "Utskick: e-posten via SES stannade",
        [
            f"SES eller AWS svarade {code} ({detail}).",
            "Bekräftelsemejlen väntar i kön tills felet är åtgärdat.",
            "Kontrollera rollen adx-utskick, SES i eu-west-1 och UTSKICK_* i .env.",
        ],
        once=f"ses_stop:{code}"[:80],
    )


def _write_eml(raw, kind):
    folder = Path(settings.PRIVATE_MEDIA_ROOT) / EML_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
    path = folder / f"{stamp}-{kind}-{secrets.token_hex(4)}.eml"
    path.write_bytes(raw)
    logger.info("Utskick: %s skrevs som .eml (DEBUG, inget skickat)", kind)
    return Sent(ok=True, message_id=path.name, mode="eml")


# ---------------------------------------------------------------------------
# Testerna
# ---------------------------------------------------------------------------

#: Klienten som står i för SES medan en FakeSes är aktiv.
_FAKE = None


class FakeSes:
    """Står i för SES v2 i testerna. Medan den är aktiv går transporten som
    med UTSKICK_EMAIL_LIVE på (testkörningen stänger av den, C.3), men till
    attrappen i stället för AWS. Sparar varje anrop; fail ger ett ClientError med den koden
    (till exempel "MessageRejected" eller "TooManyRequestsException").

        with FakeSes() as ses:
            optin.send_due()
        ses.messages[0]["Subject"]
    """

    def __init__(self, fail=None, status=400):
        self.fail = fail
        self.status = status
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            from botocore.exceptions import ClientError

            raise ClientError(
                {
                    "Error": {"Code": self.fail, "Message": "fake"},
                    "ResponseMetadata": {"HTTPStatusCode": self.status},
                },
                "SendEmail",
            )
        return {"MessageId": f"fake-{len(self.calls):04d}"}

    @property
    def messages(self):
        """De skickade mejlen som EmailMessage (bara lyckade anrop)."""
        if self.fail:
            return []
        return [mime.parse(call["Content"]["Raw"]["Data"]) for call in self.calls]

    def __enter__(self):
        global _FAKE
        self._previous = _FAKE
        _FAKE = self
        return self

    def __exit__(self, *exc):
        global _FAKE
        _FAKE = self._previous
        return False
