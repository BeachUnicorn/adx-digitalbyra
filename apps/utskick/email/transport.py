"""
E-posten ut från utskick (README D.6, D.8, H.8): bekräftelsemejlen (S1),
utskicken, testmejlen, svaren från Inkorgen, byråns provmejl och länken
som bekräftar en egen svarsadress (S3).

    can_send() -> bool            går det att skicka alls i den här miljön?
    send(mail, *, kind, account_id=None, utskick_id=None, recipient_id=None) -> Sent
                                  ett mejl
    configuration_set() -> str    konfigurationssetet mejlen går med, eller ""
    class OutgoingMail            det som skickas
    class Sent                    hur det gick (ok, retry, stop, unknown, error)
    class FakeSes                 SES i testerna

Bara sending/email.py och optin.py anropar send; redigeraren, Inkorgen och
byråns sidor går genom sending/email.py (deliver, send_test).

Vägarna, i ordning:

1. UTSKICK_EMAIL_LIVE: SES v2 SendEmail med rå MIME (email/mime.py) i
   UTSKICK_SES_REGION genom aws.client("sesv2", send=True), som aldrig gör
   om ett anrop (SendEmail saknar idempotensnyckel). Det enda stället i
   apps/utskick som anropar send_email (vakten i test_s1_guards).
2. Av, men DEBUG: mejlet skrivs som en .eml-fil i
   PRIVATE_MEDIA_ROOT/utskick-mail/ (lokalt, för att se mejlet).
3. Av i drift: inget skickas och inget skrivs till disk (D.8).

Demokontot nekas här längst ned, oavsett väg (DemoRefused, D.4): ett
account_id som är demokontots skickar aldrig.

Konfigurationssetet och taggarna (D.6, D.7): varje mejl, bekräftelsemejlen
inräknade, får taggarna k (slaget), a (kontot), u (utskicket) och r
(mottagaren) när de finns, så att SES-händelserna hittar tillbaka
(inbound/events.py). Konfigurationssetet UTSKICK_SES_CONFIGURATION_SET
skickas med först när händelsekön är satt (UTSKICK_SQS_EVENTS_URL):
server/aws-utskick-s3.sh skapar setet och kön i samma körning, och den
utökade rollen (aws-utskick-role.sh) ger rätten att skicka med setet.
Före J S3 steg 4 går mejlen alltså som i S1, utan set, i stället för att
nekas av SES för ett set som inte finns.

Felen från SES (D.6):

    TooManyRequests, LimitExceeded, Throttling, 429
                                  retry: inget skickades, försök senare
    SendingPaused, AccountSendingPaused, AccountSuspended
                                  stop: kontot hos SES är pausat (sending/email.py
                                  slår av Switchboard.email_enabled), byrån larmas
    MailFromDomainNotVerified     stop: avsändardomänens MAIL FROM (utskicket pausas)
    AWS saknas, åtkomst nekad, setet finns inte
                                  stop, byrån larmas
    anslutningen kom aldrig fram  retry (inget skickades)
    läs-timeout, bruten anslutning efter anropet, 5xx
                                  unknown: kanske skickat, görs aldrig om
    MessageRejected och andra 4xx failed: just det här mejlet

Mejlets innehåll loggas aldrig, och inte adressen (bara slaget, pk och
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
#: S3: utskicket, testmejlet (F.8), svaret från Inkorgen (G.2), byråns
#: provmejl (J S3 steg 7) och länken som bekräftar en egen svarsadress (I.9).
UTSKICK = "utskick"
TEST = "test"
REPLY = "reply"
PROBE = "probe"
REPLY_CONFIRM = "replyconf"
S3_KINDS = (UTSKICK, TEST, REPLY, PROBE, REPLY_CONFIRM)
#: Slagen av mejl som går genom transporten.
KINDS = (DOI, *S3_KINDS)

#: Mappen för .eml-filer lokalt (bara med DEBUG).
EML_FOLDER = "utskick-mail"

_RETRY_CODES = frozenset(
    {"TooManyRequestsException", "LimitExceededException", "Throttling", "ThrottlingException"}
)
#: SES har pausat kontot (eller konfigurationssetet): inget mer går förrän
#: byrån tittat (sending/email.py slår av Switchboard.email_enabled).
PAUSED_CODES = frozenset(
    {
        "SendingPausedException",
        "AccountSendingPausedException",
        "AccountSuspendedException",
    }
)
#: Avsändardomänens MAIL FROM är inte verifierad (D.6): utskicket pausas.
MAIL_FROM_CODES = frozenset({"MailFromDomainNotVerifiedException", "MailFromDomainNotVerified"})
_STOP_CODES = frozenset(
    {
        *PAUSED_CODES,
        *MAIL_FROM_CODES,
        "AccessDenied",
        "AccessDeniedException",
        "UnrecognizedClientException",
        "InvalidClientTokenId",
        "ExpiredTokenException",
        "NotFoundException",
        "ConfigurationSetDoesNotExistException",
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
    körningen (byrån är larmad). unknown: kanske skickat, gör inte om.
    error är SES-koden eller en egen ("email_off", "aws", "timeout", ...)."""

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


def configuration_set():
    """Konfigurationssetet mejlen skickas med: UTSKICK_SES_CONFIGURATION_SET
    när händelsekön finns (modulens text), annars ""."""
    name = str(getattr(settings, "UTSKICK_SES_CONFIGURATION_SET", "") or "").strip()
    if not name:
        return ""
    from ..inbound import queues

    return name if queues.events_enabled() else ""


def _assert_not_demo(account_id):
    """Demokontot skickar aldrig (D12), också om något ovanför glömt det."""
    if not account_id:
        return
    from apps.flamingo.models import FlamingoAccount

    from ..sending.sms_wrapper import DEMO_TEXT, DemoRefused

    demo = FlamingoAccount.objects.filter(pk=account_id).values_list("is_demo", flat=True).first()
    if demo:
        raise DemoRefused(DEMO_TEXT)


def send(mail, *, kind, account_id=None, utskick_id=None, recipient_id=None):
    """Skicka ett mejl av slaget kind. Kastar aldrig för fel hos SES, bara
    DemoRefused för demokontot och ValueError för ett okänt slag."""
    if kind not in KINDS:
        raise ValueError(f"Okänt slag av mejl: {kind!r}")
    _assert_not_demo(account_id)
    raw = mime.build(mail)
    if _live():
        tags = _tags(kind, account_id, utskick_id, recipient_id)
        return _send_ses(mail, raw, kind, tags)
    if settings.DEBUG:
        return _write_eml(raw, kind)
    return Sent(ok=False, error="email_off", stop=True)


def _tags(kind, account_id=None, utskick_id=None, recipient_id=None):
    """SES-taggarna (D.6): k alltid, a, u och r när de finns. Värdena är
    slaget och heltal, som SES tillåter i en tagg."""
    tags = [{"Name": "k", "Value": kind}]
    for name, value in (("a", account_id), ("u", utskick_id), ("r", recipient_id)):
        if value:
            tags.append({"Name": name, "Value": str(int(value))})
    return tags


def _client_error(exc):
    response = getattr(exc, "response", None) or {}
    code = (response.get("Error") or {}).get("Code", "") or type(exc).__name__
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode", 0) or 0
    return code, int(status)


def _send_ses(mail, raw, kind, tags):
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        ConnectTimeoutError,
        EndpointConnectionError,
        NoCredentialsError,
        ParamValidationError,
        PartialCredentialsError,
        ReadTimeoutError,
    )

    from .. import aws

    try:
        client = _FAKE or aws.client("sesv2", send=True)
    except (aws.AwsNotConfigured, BotoCoreError, ClientError) as exc:
        logger.error("Utskick: ingen AWS-session för e-post (%s)", type(exc).__name__)
        _alert_stop("aws", type(exc).__name__)
        return Sent(ok=False, error="aws", retry=True, stop=True)
    params = {
        "FromEmailAddress": mail.from_addr,
        "Destination": {"ToAddresses": [mail.to]},
        "Content": {"Raw": {"Data": raw}},
        "EmailTags": tags,
    }
    config_set = configuration_set()
    if config_set:
        params["ConfigurationSetName"] = config_set
    try:
        answer = client.send_email(**params)
    except ReadTimeoutError:
        logger.warning("Utskick: SES svarade inte i tid (%s), görs inte om", kind)
        return Sent(ok=False, error="timeout", unknown=True)
    except (EndpointConnectionError, ConnectTimeoutError) as exc:
        # Anslutningen kom aldrig fram: inget skickades. Bara de här två; ett
        # SSLError (eller fel via en proxy) kan komma när svaret läses, efter
        # att SES fått mejlet, och blir unknown nedan (BotoCoreError).
        logger.warning("Utskick: SES gick inte att nå (%s)", type(exc).__name__)
        return Sent(ok=False, error="connect", retry=True)
    except (NoCredentialsError, PartialCredentialsError) as exc:
        logger.error("Utskick: nycklarna för SES saknas (%s)", type(exc).__name__)
        _alert_stop("aws", type(exc).__name__)
        return Sent(ok=False, error="aws", retry=True, stop=True)
    except ParamValidationError:
        logger.error("Utskick: anropet till SES var felaktigt (%s), skickades inte", kind)
        return Sent(ok=False, error="invalid")
    except ClientError as exc:
        code, status = _client_error(exc)
        if code in _RETRY_CODES or status == 429:
            logger.info("Utskick: SES bromsar (%s)", code)
            return Sent(ok=False, error=code, retry=True)
        if code in _STOP_CODES:
            logger.error("Utskick: SES stoppar e-posten (%s)", code)
            if code not in MAIL_FROM_CODES:
                # MAIL FROM gäller en kunds domän: sending/email.py larmar per utskick.
                _alert_stop(code, kind)
            return Sent(ok=False, error=code, retry=True, stop=True)
        if status >= 500:
            logger.warning("Utskick: SES fel %s (%s), görs inte om", status, code)
            return Sent(ok=False, error=code, unknown=True)
        logger.warning("Utskick: SES nekade mejlet (%s, %s)", kind, code)
        return Sent(ok=False, error=code)
    except BotoCoreError as exc:
        # Anslutningen bröts efter att anropet skickats: kanske skickat.
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
            "Mejlen (bekräftelser och utskick) väntar i kön tills felet är åtgärdat.",
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
    attrappen i stället för AWS. Sparar varje anrop (calls, med
    ConfigurationSetName och EmailTags som de skickades).

    fail ger ett ClientError med den koden och status (till exempel
    "MessageRejected" eller "TooManyRequestsException") på varje anrop.
    script är en lista, ett svar per anrop i tur och ordning (sedan går
    resten bra): None (skickat), en kod (ClientError med status 400), en
    tupel (kod, status) eller ett undantag som kastas som det är (till
    exempel botocores ReadTimeoutError).

        with FakeSes() as ses:
            optin.send_due()
        ses.messages[0]["Subject"]
    """

    def __init__(self, fail=None, status=400, script=None):
        self.fail = fail
        self.status = status
        self.script = list(script or [])
        self.calls = []
        self.sent = []

    def _client_error(self, code, status):
        from botocore.exceptions import ClientError

        return ClientError(
            {
                "Error": {"Code": code, "Message": "fake"},
                "ResponseMetadata": {"HTTPStatusCode": status},
            },
            "SendEmail",
        )

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise self._client_error(self.fail, self.status)
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, BaseException):
                raise step
            if isinstance(step, tuple):
                raise self._client_error(*step)
            if step:
                raise self._client_error(step, 400)
        self.sent.append(kwargs)
        return {"MessageId": f"fake-{len(self.calls):04d}"}

    @property
    def messages(self):
        """De skickade mejlen som EmailMessage (bara lyckade anrop)."""
        return [mime.parse(call["Content"]["Raw"]["Data"]) for call in self.sent]

    def tags(self, index=-1):
        """Taggarna i ett lyckat anrop som {namn: värde}."""
        return {t["Name"]: t["Value"] for t in self.sent[index].get("EmailTags", [])}

    def __enter__(self):
        global _FAKE
        self._previous = _FAKE
        _FAKE = self
        return self

    def __exit__(self, *exc):
        global _FAKE
        _FAKE = self._previous
        return False
