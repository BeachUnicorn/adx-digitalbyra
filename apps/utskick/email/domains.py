"""
Kundernas avsändardomäner (README B.3, J S3 "Domain flow", H.8, I.9).

Anspråk nekas för adx.se och varje underdomän, freemail- och
operatörsdomäner (freemail.py), publika suffix (PUBLIC_SUFFIXES här och
links.is_shared_host, inget beroende) och en domän som är samma som,
förälder till eller barn till ett annat kontos väntande eller verifierade
domän ("Domänen används redan. Kontakta ADX."). Ett andra anspråk på samma
namn larmar byrån. AlreadyExistsException från CreateEmailIdentity nekar
med samma text och ett larm: vi tar aldrig över en befintlig identitet,
och DeleteEmailIdentity anropas bara när ses_created är sant. En väntande
domän går ut efter SenderDomain.PENDING_DAYS dagar (byrån larmas och
identiteten tas bort); när ett konto verifierar en domän avbryts andra
kontons väntande anspråk på samma namn, föräldern eller barnen. Ett konto
har högst en domän i taget (väntande, verifierad eller misslyckad); en ny
kräver att den gamla tas bort.

Allt mot SES går genom aws.client("sesv2") (api-klienten med omförsök) i
UTSKICK_SES_REGION; DNS läses med dnspython (dns.resolver.resolve, mockas i
testerna).

    IN_USE_TEXT = "Domänen används redan. Kontakta ADX."
    class DomainRefused(ValueError)        .text för formuläret
    class Record(kind, name, value, purpose, short)
    normalize(raw) -> str                  gemener, IDNA, utan schema, sökväg, www. och
                                           punkt sist; "" när det inte är en domän
    claim_problem(account, domain) -> str  "" eller texten (reglerna ovan)
    claim(account, domain, *, from_local, from_name, user, now=None) -> SenderDomain
                                           CreateEmailIdentity (Easy DKIM, RSA 2048),
                                           PutEmailIdentityMailFromAttributes
                                           (studs.<domän>, USE_DEFAULT_VALUE), ses_created
    update_sender(row, *, from_local, from_name) -> SenderDomain
    records(row) -> list[Record]           tre DKIM-CNAME, MX och SPF-TXT för studs,
                                           DMARC-TXT "v=DMARC1; p=none" (en rekommendation)
    check(row, *, now=None, alert=True) -> SenderDomain
                                           dnspython och GetEmailIdentity; verified ->
                                           verified_at, andras anspråk avbryts; en
                                           verifierad som slutar fungera blir failed (larm);
                                           en väntande äldre än PENDING_DAYS går ut
    check_due(now=None, deadline=None) -> dict
                                           utskick_daily: väntande, verifierade och
                                           tidigare verifierade som misslyckats
    expire(row, now=None) -> bool
    delete_identity(row) -> bool          DeleteEmailIdentity, bara när ses_created
    remove(row, *, user, now=None) -> SenderDomain
                                           status removed; DeleteEmailIdentity bara när
                                           ses_created; raden ligger kvar (RESTRICT);
                                           utkast som använde domänen går till ADX-domänen
    current(account) -> SenderDomain | None
    sendable(account, domain_id) -> SenderDomain | None
                                           kontots verifierade domän, annars None
    summary(account, now=None) -> dict     Inställningar och Leveranshälsa (nycklarna
                                           står vid funktionen)

Kunden mejlas aldrig härifrån; larmen går till byrån och bär pk, inga
adresser. Domännamnen är kundens egna (inga personuppgifter) och står i
larmen.
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .. import alerts
from ..freemail import is_freemail
from ..models import SenderDomain, Utskick

logger = logging.getLogger(__name__)

IN_USE_TEXT = "Domänen används redan. Kontakta ADX."
INVALID_TEXT = "Skriv domänen, till exempel exempelror.se."
ADX_TEXT = "Det är ADX domän. Skriv domänen för din egen webbplats."
FREEMAIL_TEXT = (
    "Gratis e-post som Gmail och Outlook går inte att skicka från. "
    "Skriv domänen för din egen webbplats."
)
SUFFIX_TEXT = "Skriv hela domänen, till exempel exempelror.se."
OWN_TEXT = "Du har redan en domän. Ta bort den först om du vill byta."
LOCAL_TEXT = "Skriv det som står före @, till exempel hej."
NAME_TEXT = "Skriv avsändarnamnet, till exempel ditt företags namn."
SES_TEXT = "Domänen gick inte att lägga till just nu. Försök igen om en stund."
BUSY_TEXT = (
    "Ett utskick som är schemalagt eller skickas använder domänen. Vänta tills det är klart."
)
DEMO_TEXT = "Demokontot kan inte lägga till eller kontrollera en domän."

STATUS = SenderDomain.Status
#: Mejlen kommer bara från en domän i de här lägena (en i taget per konto).
CURRENT = (STATUS.PENDING, STATUS.VERIFIED, STATUS.FAILED)
#: Väntetiden för en DNS-fråga (dnspython).
DNS_SECONDS = 5
#: Hur ofta kunden får trycka på Kontrollera igen.
CHECK_EVERY = timedelta(minutes=1)
#: Raden med DMARC som rekommenderas när domänen saknar en.
DMARC_VALUE = "v=DMARC1; p=none"
SPF_VALUE = "v=spf1 include:amazonses.com ~all"
#: MX-postens prioritet för studs.<domän> (SES dokumentation).
MX_PRIORITY = 10
#: Posterna som måste stämma (DMARC är en rekommendation).
REQUIRED = ("dkim1", "dkim2", "dkim3", "mx", "spf")

#: Offentliga suffix utöver links.SHARED_HOSTS (ett urval ur Public Suffix
#: List: domäner där vem som helst registrerar under).
PUBLIC_SUFFIXES = frozenset(
    {
        "se",
        "nu",
        "com",
        "net",
        "org",
        "eu",
        "info",
        "io",
        "dk",
        "no",
        "fi",
        "de",
        "uk",
        "co.uk",
        "ax",
        "gov.se",
        "edu.se",
        "com.se",
        "co.se",
        "net.se",
        "go.se",
        "co.no",
        "priv.no",
        "com.de",
        "co.com",
        "us.com",
        "eu.com",
        "uk.com",
        "se.net",
        "eu.org",
        "us.org",
    }
)

_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_LOCAL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._+-]{0,62}[a-z0-9])?$")


class DomainRefused(ValueError):
    def __init__(self, text):
        super().__init__(text)
        self.text = text


@dataclass(frozen=True)
class Record:
    """En DNS-post att lägga in: kind (CNAME, MX, TXT), name (hela namnet),
    value, purpose (dkim1, dkim2, dkim3, mx, spf, dmarc), short (namnet
    utan domänen, som de flesta DNS-tjänster vill ha det; "@" för domänen)
    och priority (MX: ett eget fält hos de flesta DNS-tjänster, så värdet
    är bara värden)."""

    kind: str
    name: str
    value: str
    purpose: str = ""
    short: str = ""
    priority: int | None = None


# ---------------------------------------------------------------------------
# Namnet och reglerna
# ---------------------------------------------------------------------------


def normalize(raw):
    """Domänen i gemener och IDNA, utan schema, sökväg, port, användare,
    www. och punkt sist. En adress (hej@exempelror.se) ger domänen. "" när
    det inte är ett domännamn med minst två led."""
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    if "@" in text and "//" not in text:
        text = text.rsplit("@", 1)[1]
    if "//" not in text:
        text = "//" + text
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        return ""
    host = host.strip().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    host = host.removeprefix("www.")
    if not host or len(host) > 253 or "." not in host:
        return ""
    labels = host.split(".")
    if not all(_LABEL_RE.match(label) for label in labels):
        return ""
    if labels[-1].isdigit():
        return ""
    return host


def _under(host, parent):
    return host == parent or host.endswith("." + parent)


def is_public_suffix(domain):
    """Ett offentligt suffix eller en delad värd: ingen kund äger det."""
    from .. import links

    return domain in PUBLIC_SUFFIXES or links.is_shared_host(domain)


def _adx(domain):
    return _under(domain, "adx.se")


def _related(domain):
    """Q för rader vars domän är samma som, förälder till eller barn till domain."""
    labels = domain.split(".")
    parents = [".".join(labels[i:]) for i in range(1, len(labels) - 1)]
    return Q(domain=domain) | Q(domain__in=parents) | Q(domain__endswith="." + domain)


def _others(account, domain):
    return (
        SenderDomain.objects.filter(status__in=(STATUS.PENDING, STATUS.VERIFIED))
        .filter(_related(domain))
        .exclude(account_id=account.pk)
    )


def current(account):
    """Kontots domän just nu (väntande, verifierad eller misslyckad), eller None."""
    return (
        SenderDomain.objects.filter(account_id=account.pk, status__in=CURRENT)
        .order_by("-created_at", "-pk")
        .first()
    )


def claim_problem(account, domain):
    """ "" när kontot får göra anspråk på domänen, annars texten för
    formuläret. domain ska vara normaliserad (normalize)."""
    domain = normalize(domain)
    if not domain:
        return INVALID_TEXT
    if _adx(domain):
        return ADX_TEXT
    if is_freemail(domain):
        return FREEMAIL_TEXT
    if is_public_suffix(domain):
        return SUFFIX_TEXT
    if _others(account, domain).exists():
        return IN_USE_TEXT
    own = current(account)
    if own is not None and own.domain != domain:
        return OWN_TEXT
    return ""


def _clean_local(value):
    text = str(value or "").strip().lower() or "hej"
    if "@" in text:
        text = text.split("@", 1)[0]
    if not _LOCAL_RE.match(text) or ".." in text:
        raise DomainRefused(LOCAL_TEXT)
    return text


def _clean_name(value):
    text = " ".join(str(value or "").split())[:80]
    if not text:
        raise DomainRefused(NAME_TEXT)
    return text


def _client():
    from .. import aws

    return aws.client("sesv2")


def _code(exc):
    response = getattr(exc, "response", None) or {}
    return str((response.get("Error") or {}).get("Code") or type(exc).__name__)


def _alert_in_use(account, domain, why, now):
    alerts.agency(
        "Utskick: en kund vill skicka från en domän som redan används",
        [
            f"Konto {account.pk} försökte lägga till {domain} som avsändardomän ({why}).",
            "Kunden fick svaret att domänen används redan och att kontakta ADX.",
        ],
        once=f"domain_in_use:{account.pk}:{domain}"[:80],
        window="day",
        now=now,
    )


def _mail_from(client, row):
    client.put_email_identity_mail_from_attributes(
        EmailIdentity=row.domain,
        MailFromDomain=row.mail_from_domain,
        BehaviorOnMxFailure="USE_DEFAULT_VALUE",
    )


def claim(account, domain, *, from_local, from_name, user, now=None):
    """Kontots anspråk på en domän (J S3 "Domain flow"): reglerna, sedan
    CreateEmailIdentity med Easy DKIM och MAIL FROM studs.<domän>. Samma
    domän en gång till uppdaterar bara avsändaren. DomainRefused med
    texten när det inte går."""
    from botocore.exceptions import BotoCoreError, ClientError

    from .. import aws

    now = now or timezone.now()
    if getattr(account, "is_demo", False):
        # Demot når aldrig SES (D12): ingen identitet skapas.
        raise DomainRefused(DEMO_TEXT)
    name = normalize(domain)
    problem = claim_problem(account, name)
    if problem:
        if problem == IN_USE_TEXT:
            _alert_in_use(account, name, "ett annat konto", now)
        raise DomainRefused(problem)
    local = _clean_local(from_local)
    sender = _clean_name(from_name)
    own = current(account)
    if own is not None and own.domain == name:
        return update_sender(own, from_local=local, from_name=sender)
    row = SenderDomain.objects.create(
        account=account,
        domain=name,
        from_local=local,
        from_name=sender,
        status=STATUS.PENDING,
        created_by=user if getattr(user, "pk", None) else None,
        created_at=now,
    )
    try:
        client = _client()
        answer = client.create_email_identity(
            EmailIdentity=name,
            DkimSigningAttributes={"NextSigningKeyLength": "RSA_2048_BIT"},
        )
    except ClientError as exc:
        row.delete()
        if _code(exc) == "AlreadyExistsException":
            logger.warning("Utskick: domänen för konto %s finns redan hos SES", account.pk)
            _alert_in_use(account, name, "identiteten finns redan hos SES", now)
            raise DomainRefused(IN_USE_TEXT) from None
        logger.error("Utskick: CreateEmailIdentity gick inte (%s)", _code(exc))
        raise DomainRefused(SES_TEXT) from None
    except (aws.AwsNotConfigured, BotoCoreError) as exc:
        row.delete()
        logger.error("Utskick: ingen AWS för domänen (%s)", type(exc).__name__)
        raise DomainRefused(SES_TEXT) from None
    dkim = (answer or {}).get("DkimAttributes") or {}
    row.ses_created = True
    row.dkim_tokens = [str(t) for t in (dkim.get("Tokens") or [])][:3]
    row.ses_snapshot = _snapshot({"DkimAttributes": dkim, **(answer or {})})
    row.save(update_fields=["ses_created", "dkim_tokens", "ses_snapshot"])
    try:
        _mail_from(client, row)
    except (BotoCoreError, ClientError) as exc:
        # check() försöker igen: domänen fungerar ändå (amazonses.com som MAIL FROM).
        logger.warning("Utskick: MAIL FROM för domän %s gick inte (%s)", row.pk, _code(exc))
    logger.info("Utskick: konto %s lade till domän %s", account.pk, row.pk)
    return row


def update_sender(row, *, from_local, from_name):
    """Byt avsändaradressens första del och namnet på kontots domän."""
    row.from_local = _clean_local(from_local)
    row.from_name = _clean_name(from_name)
    row.save(update_fields=["from_local", "from_name"])
    return row


# ---------------------------------------------------------------------------
# Posterna och kontrollen
# ---------------------------------------------------------------------------


def _region():
    return getattr(settings, "UTSKICK_SES_REGION", "") or "eu-west-1"


def _short(name, domain):
    if name == domain:
        return "@"
    return name.removesuffix("." + domain)


def records(row):
    """Posterna kunden lägger in hos sin DNS (J S3): tre DKIM-CNAME, MX och
    SPF för studs.<domän> (MAIL FROM) och DMARC (rekommendation)."""
    domain = row.domain
    found = []
    for index, token in enumerate(list(row.dkim_tokens or [])[:3], start=1):
        name = f"{token}._domainkey.{domain}"
        found.append(
            Record(
                "CNAME", name, f"{token}.dkim.amazonses.com", f"dkim{index}", _short(name, domain)
            )
        )
    mail_from = row.mail_from_domain
    found.append(
        Record(
            "MX",
            mail_from,
            f"feedback-smtp.{_region()}.amazonses.com",
            "mx",
            _short(mail_from, domain),
            priority=MX_PRIORITY,
        )
    )
    found.append(Record("TXT", mail_from, SPF_VALUE, "spf", _short(mail_from, domain)))
    dmarc = f"_dmarc.{domain}"
    found.append(Record("TXT", dmarc, DMARC_VALUE, "dmarc", _short(dmarc, domain)))
    return found


def _lookup(name, rdtype):
    """Svaren som text, [] när posten saknas, None när DNS inte svarade."""
    import dns.exception
    import dns.resolver

    try:
        answer = dns.resolver.resolve(name, rdtype, lifetime=DNS_SECONDS)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return []
    except (dns.exception.Timeout, dns.exception.DNSException):
        return None
    return [rdata.to_text() for rdata in answer]


def _txt(value):
    """En TXT-post som text: segmenten ihop, utan citattecken."""
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', value)
    return "".join(parts) if parts else value.strip('"')


def _host(value):
    return str(value or "").strip().rstrip(".").lower()


def check_record(record):
    """{"state": "ok" | "missing" | "wrong" | "unknown", "seen": "..."} för en post."""
    seen = _lookup(record.name, record.kind)
    if seen is None:
        return {"state": "unknown", "seen": ""}
    if record.kind == "CNAME":
        hosts = [_host(v) for v in seen]
        if _host(record.value) in hosts:
            return {"state": "ok", "seen": record.value}
        return {"state": "wrong" if hosts else "missing", "seen": (hosts or [""])[0][:200]}
    if record.kind == "MX":
        want = _host(record.value.split()[-1])
        hosts = [_host(v.split()[-1]) for v in seen if v.split()]
        if want in hosts:
            return {"state": "ok", "seen": record.value}
        return {"state": "wrong" if hosts else "missing", "seen": (seen or [""])[0][:200]}
    texts = [_txt(v) for v in seen]
    if record.purpose == "spf":
        spf = [t for t in texts if t.lower().startswith("v=spf1")]
        if any("include:amazonses.com" in t.lower() for t in spf):
            return {"state": "ok", "seen": spf[0][:200]}
        return {"state": "wrong" if spf else "missing", "seen": (spf or [""])[0][:200]}
    dmarc = [t for t in texts if t.upper().startswith("V=DMARC1")]
    if dmarc:
        return {"state": "ok", "seen": dmarc[0][:200]}
    return {"state": "missing", "seen": ""}


def dns_ok(row, required=REQUIRED):
    """Stämmer posterna (senaste kontrollen)?"""
    checks = row.checks if isinstance(row.checks, dict) else {}
    return all((checks.get(key) or {}).get("state") == "ok" for key in required)


def _snapshot(answer):
    """Det vi sparar ur GetEmailIdentity (utan nycklar eller hemligheter)."""
    dkim = answer.get("DkimAttributes") or {}
    mail_from = answer.get("MailFromAttributes") or {}
    return {
        "VerifiedForSendingStatus": bool(answer.get("VerifiedForSendingStatus")),
        "VerificationStatus": str(answer.get("VerificationStatus") or ""),
        "DkimStatus": str(dkim.get("Status") or ""),
        "SigningEnabled": bool(dkim.get("SigningEnabled")),
        "MailFromDomain": str(mail_from.get("MailFromDomain") or ""),
        "MailFromDomainStatus": str(mail_from.get("MailFromDomainStatus") or ""),
    }


def _ses_verified(snapshot):
    return (
        bool(snapshot.get("VerifiedForSendingStatus")) and snapshot.get("DkimStatus") == "SUCCESS"
    )


def _next_status(row, snapshot):
    """Läget efter en kontroll. En ny domän verifieras bara när SES säger
    VerifiedForSendingStatus och DKIM SUCCESS. En verifierad domän fortsätter
    så länge SES skickar från den (VerifiedForSendingStatus): DKIM
    TEMPORARY_FAILURE är SES egen omprövning och pausar inget. Den blir
    failed när SES slutat skicka från den, eller när identiteten är borta.
    Ett fel i anropet ändrar inget."""
    if "error" in snapshot:
        return row.status
    if row.status != STATUS.VERIFIED:
        if _ses_verified(snapshot):
            return STATUS.VERIFIED
        if row.status == STATUS.FAILED and row.verified_at:
            return STATUS.FAILED
        return row.status
    if snapshot.get("missing"):
        return STATUS.FAILED
    if snapshot.get("VerifiedForSendingStatus"):
        return STATUS.VERIFIED
    if snapshot.get("DkimStatus") == "TEMPORARY_FAILURE":
        return STATUS.VERIFIED
    return STATUS.FAILED


def delete_identity(row):
    """DeleteEmailIdentity, bara när vi skapade identiteten (B.3)."""
    from botocore.exceptions import BotoCoreError, ClientError

    from .. import aws

    if not row.ses_created or _is_demo(row):
        return False
    try:
        _client().delete_email_identity(EmailIdentity=row.domain)
    except ClientError as exc:
        if _code(exc) != "NotFoundException":
            logger.error("Utskick: domän %s kunde inte tas bort hos SES (%s)", row.pk, _code(exc))
            return False
    except (aws.AwsNotConfigured, BotoCoreError) as exc:
        logger.error("Utskick: ingen AWS för domän %s (%s)", row.pk, type(exc).__name__)
        return False
    return True


def _is_demo(row):
    """Hör domänen till demokontot? Då anropas varken SES eller DNS (D12)."""
    from apps.flamingo.models import FlamingoAccount

    return FlamingoAccount.objects.filter(pk=row.account_id, is_demo=True).exists()


def _cancel_others(row, now):
    """Andra kontons väntande anspråk på samma namn, föräldern eller barnen
    avbryts (failed) när row verifieras. Deras identitet tas bort bara när
    den är en annan än row:s och vi skapade den."""
    others = list(
        SenderDomain.objects.filter(status=STATUS.PENDING)
        .filter(_related(row.domain))
        .exclude(account_id=row.account_id)
    )
    for other in others:
        changed = SenderDomain.objects.filter(pk=other.pk, status=STATUS.PENDING).update(
            status=STATUS.FAILED,
            checks={**(other.checks or {}), "cancelled": "verified_elsewhere"},
        )
        if changed and other.domain != row.domain:
            delete_identity(other)
        if changed:
            logger.warning(
                "Utskick: domän %s avbröts av en verifiering hos ett annat konto", other.pk
            )
    return len(others)


def expire(row, now=None):
    """En väntande domän som gått PENDING_DAYS dagar utan verifiering går ut:
    identiteten tas bort (så att kontot kan försöka igen) och byrån larmas."""
    now = now or timezone.now()
    if row.status != STATUS.PENDING:
        return False
    if now - row.created_at < timedelta(days=SenderDomain.PENDING_DAYS):
        return False
    changed = SenderDomain.objects.filter(pk=row.pk, status=STATUS.PENDING).update(
        status=STATUS.EXPIRED, checked_at=now
    )
    if not changed:
        return False
    row.status = STATUS.EXPIRED
    delete_identity(row)
    logger.warning("Utskick: domän %s gick ut utan verifiering", row.pk)
    alerts.agency(
        "Utskick: en avsändardomän gick ut",
        [
            f"Konto {row.account_id}: {row.domain} blev inte verifierad på "
            f"{SenderDomain.PENDING_DAYS} dagar.",
            "Kunden kan lägga till domänen igen under Avsändare och svar. Hjälp gärna till med "
            "DNS-posterna om kunden bett om det.",
        ],
        once=f"domain_expired:{row.pk}",
        window="day",
        now=now,
    )
    return True


def check(row, *, now=None, alert=True):
    """Läs DNS och SES för domänen och uppdatera raden (J S3): checks,
    ses_snapshot, checked_at och läget. Verifierad när SES säger
    VerifiedForSendingStatus och DKIM SUCCESS."""
    from botocore.exceptions import BotoCoreError, ClientError

    from .. import aws

    now = now or timezone.now()
    if row.status in (STATUS.REMOVED, STATUS.EXPIRED):
        return row
    if _is_demo(row):
        return row
    if expire(row, now):
        return row
    results = {record.purpose: check_record(record) for record in records(row)}
    snapshot = dict(row.ses_snapshot or {})
    try:
        client = _client()
        answer = client.get_email_identity(EmailIdentity=row.domain)
        snapshot = _snapshot(answer)
        if row.ses_created and snapshot.get("MailFromDomain") != row.mail_from_domain:
            _mail_from(client, row)
    except ClientError as exc:
        if _code(exc) == "NotFoundException":
            # Identiteten finns inte längre hos SES: inget går att skicka
            # från domänen (en verifierad blir failed med ett larm).
            snapshot = {**_snapshot({}), "missing": True}
        else:
            snapshot["error"] = _code(exc)
        logger.warning("Utskick: GetEmailIdentity för domän %s gick inte (%s)", row.pk, _code(exc))
    except (aws.AwsNotConfigured, BotoCoreError) as exc:
        snapshot["error"] = type(exc).__name__
    previous = row.status
    first = row.verified_at is None
    status = _next_status(row, snapshot)
    values = {"checks": results, "ses_snapshot": snapshot, "checked_at": now}
    if status == STATUS.VERIFIED and previous != STATUS.VERIFIED:
        values["verified_at"] = now
    try:
        with transaction.atomic():
            SenderDomain.objects.filter(pk=row.pk, status=previous).update(status=status, **values)
    except IntegrityError:
        # Ett annat konto hann verifiera samma domän (det unika villkoret).
        status = STATUS.FAILED
        values["checks"] = {**results, "cancelled": "verified_elsewhere"}
        values.pop("verified_at", None)
        SenderDomain.objects.filter(pk=row.pk, status=previous).update(status=status, **values)
    row.refresh_from_db()
    if row.status == STATUS.VERIFIED and previous != STATUS.VERIFIED:
        logger.info("Utskick: domän %s är verifierad", row.pk)
        _cancel_others(row, now)
        if first:
            adopt_drafts(row, now)
    if row.status == STATUS.FAILED and previous == STATUS.VERIFIED and alert:
        logger.error("Utskick: domän %s fungerar inte längre", row.pk)
        why = (
            "identiteten finns inte hos SES"
            if snapshot.get("missing")
            else f"DKIM {snapshot.get('DkimStatus') or 'okänt'}"
        )
        alerts.agency(
            "Utskick: en verifierad avsändardomän fungerar inte längre",
            [
                f"Konto {row.account_id}: {row.domain} är inte längre verifierad hos SES ({why}).",
                "Utskick från domänen pausas tills posterna stämmer igen.",
            ],
            once=f"domain_failed:{row.pk}",
            window="day",
            now=now,
        )
    return row


def adopt_drafts(row, now=None):
    """Kontots utkast utan avsändardomän skickas från domänen när den
    verifieras första gången (som remove flyttar utkasten till
    ADX-domänen). Ett utkast där kunden sedan väljer ADX-domänen under Från
    behåller det valet. Antalet."""
    now = now or timezone.now()
    moved = Utskick.objects.filter(
        account_id=row.account_id, sender_domain__isnull=True, status=Utskick.Status.DRAFT
    ).update(sender_domain=row, updated_at=now)
    if moved:
        logger.info("Utskick: %s utkast skickas nu från domän %s", moved, row.pk)
    return moved


def verified_for(account):
    """Kontots verifierade domän (en i taget), eller None. Nya utskick
    skickas från den (utskick_new)."""
    return (
        SenderDomain.objects.filter(account_id=account.pk, status=STATUS.VERIFIED)
        .order_by("-verified_at", "-pk")
        .first()
    )


def check_due(now=None, deadline=None):
    """utskick_daily (J S3): väntande domäner (de äldre än PENDING_DAYS går
    ut), verifierade och tidigare verifierade som misslyckats. Bara antal."""
    now = now or timezone.now()
    counts = {"checked": 0, "verified": 0, "expired": 0, "failed": 0}
    rows = (
        SenderDomain.objects.filter(
            Q(status__in=(STATUS.PENDING, STATUS.VERIFIED))
            | Q(status=STATUS.FAILED, verified_at__isnull=False)
        )
        .exclude(account__is_demo=True)
        .order_by("checked_at", "pk")
    )
    for row in rows[:500]:
        if deadline is not None and time.monotonic() > deadline:
            break
        before = row.status
        try:
            row = check(row, now=now)
        except Exception:  # noqa: BLE001 - en domän fäller inte de andra
            logger.exception("Utskick: kontrollen av domän %s gick inte", row.pk)
            counts["failed"] += 1
            continue
        counts["checked"] += 1
        if row.status == STATUS.EXPIRED and before != STATUS.EXPIRED:
            counts["expired"] += 1
        elif row.status == STATUS.VERIFIED and before != STATUS.VERIFIED:
            counts["verified"] += 1
    return {k: v for k, v in counts.items() if v}


def remove(row, *, user, now=None):
    """Kunden (eller byrån) tar bort domänen: status removed, identiteten tas
    bort hos SES bara när vi skapade den, och raden ligger kvar (skickade
    utskick pekar på den). Utkast som använde domänen skickas från
    ADX-domänen. DomainRefused när ett schemalagt, frysande, pågående eller
    pausat utskick använder den."""
    now = now or timezone.now()
    busy = Utskick.objects.filter(
        sender_domain_id=row.pk,
        status__in=(*Utskick.ACTIVE, *Utskick.PAUSED_STATES),
    ).exists()
    if busy:
        raise DomainRefused(BUSY_TEXT)
    if row.status == STATUS.REMOVED:
        return row
    previous = row.status
    changed = SenderDomain.objects.filter(pk=row.pk, status=previous).update(
        status=STATUS.REMOVED, checked_at=now
    )
    if not changed:
        row.refresh_from_db()
        return row
    row.status = STATUS.REMOVED
    if previous != STATUS.EXPIRED:
        delete_identity(row)
    Utskick.objects.filter(sender_domain_id=row.pk, status=Utskick.Status.DRAFT).update(
        sender_domain=None, updated_at=now
    )
    logger.warning("Utskick: domän %s togs bort av användare %s", row.pk, getattr(user, "pk", None))
    return row


def sendable(account, domain_id):
    """Kontots verifierade domän med id domain_id, annars None (ett annat
    kontos, väntande, borttagen). Sändningen prövar igen (D.6)."""
    try:
        pk = int(domain_id)
    except (TypeError, ValueError):
        return None
    return SenderDomain.objects.filter(pk=pk, account_id=account.pk, status=STATUS.VERIFIED).first()


def summary(account, now=None):
    """Det Inställningar och Leveranshälsa visar om e-postens avsändare:

    domain       kontots domän (SenderDomain) eller None
    verified     domänen är verifierad
    status_label "Väntar på DNS", "Verifierad", ...
    from_address hej@exempelror.example, eller exempelror@utskick.adx.se
    from_name    avsändarnamnet
    dns_ok       SPF, DKIM och MAIL FROM stämmer (DMARC en rekommendation)
    dns_text     "SPF, DKIM och DMARC OK", vad som saknas, eller "ADX-domänen"
    adx_used, adx_cap, adx_left, month     ADX-taket den här månaden
    adx_text     "utskick.adx.se · 1 240 av 2 000 mejl i oktober"
    reply_text   "svar till Inkorgen" eller "svar till hej@exempelror.example"
    card_text    adx_text plus reply_text (I.9)
    """
    from apps.flamingo.templatetags.flamingo_app import tal

    from ..access import settings_for
    from ..sending import email as email_loop

    now = now or timezone.now()
    row = current(account)
    settings_row = settings_for(account)
    used = email_loop.adx_month_count(account, now)
    cap = email_loop.adx_cap()
    month = email_loop._MONTHS[timezone.localtime(now).month - 1]
    reply = "svar till Inkorgen"
    if settings_row.email_reply_mode == settings_row.REPLY_OWN and email_loop.own_reply_ok(
        account, settings_row
    ):
        reply = f"svar till {settings_row.own_reply_to}"
    verified = bool(row and row.status == STATUS.VERIFIED)
    if verified:
        name, address = email_loop.from_for(account, sender_domain=row)
    else:
        name, address = email_loop.from_for(account)
    adx_text = f"{email_loop.adx_domain()} · {tal(used)} av {tal(cap)} mejl i {month}"
    if row is None:
        dns_text = "ADX-domänen"
    elif dns_ok(row, (*REQUIRED, "dmarc")):
        dns_text = "SPF, DKIM och DMARC OK"
    else:
        missing = []
        checks = row.checks if isinstance(row.checks, dict) else {}
        if any((checks.get(k) or {}).get("state") != "ok" for k in ("dkim1", "dkim2", "dkim3")):
            missing.append("DKIM")
        if any((checks.get(k) or {}).get("state") != "ok" for k in ("mx", "spf")):
            missing.append("SPF")
        if (checks.get("dmarc") or {}).get("state") != "ok":
            missing.append("DMARC")
        if not missing:
            dns_text = "Väntar på DNS"
        elif len(missing) == 1:
            dns_text = f"{missing[0]} saknas"
        else:
            dns_text = ", ".join(missing[:-1]) + f" och {missing[-1]} saknas"
    return {
        "domain": row,
        "verified": verified,
        "status_label": row.get_status_display() if row else "",
        "from_address": address,
        "from_name": name,
        "dns_ok": bool(row and dns_ok(row)),
        "dns_text": dns_text,
        "adx_used": used,
        "adx_cap": cap,
        "adx_left": max(0, cap - used),
        "adx_pct": min(100, round(used * 100 / cap)) if cap else 0,
        "month": month,
        "adx_text": adx_text,
        "reply_text": reply,
        "card_text": f"{adx_text} · {reply}",
    }
