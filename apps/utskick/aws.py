"""
AWS för utskick (README D14, H.8): SES i eu-west-1, och från S3 även SQS
och S3 för händelser och inkommande post.

    session()                 boto3-session för rollen adx-utskick
    client(service, send=False)   klient i UTSKICK_SES_REGION, återanvänd i processen
    forget()                  glöm sessionen och klienterna (testerna)

I drift antas rollen UTSKICK_AWS_ROLE_ARN med UTSKICK_AWS_EXTERNAL_ID från
cloud.aws.base_session() (instansrollen får bara sts:AssumeRole på den, i en
egen inline-policy). Sessionen återanvänds tills fem minuter före att
nycklarna går ut. Lokalt (DEBUG och ingen roll) används ADX_AWS_PROFILE som
den är. Utan roll i drift: AwsNotConfigured, och inget skickas.

boto3 importeras först här inne (och cloud.aws, som importerar boto3 direkt),
så att en tick utan något att göra aldrig laddar det.

Sändningsklienten (send=True) försöker bara en gång: SendEmail har ingen
idempotensnyckel, så ett nytt försök från botocore kan ge två mejl (D.6).
"""

import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Sessionen förnyas så här långt innan nycklarna går ut.
REFRESH_BEFORE = timedelta(minutes=5)
#: Hur länge en antagen session gäller (sekunder, rollens tak är en timme).
DURATION_SECONDS = 3600

_lock = threading.Lock()
_state = {"session": None, "expires": None, "clients": {}}


class AwsNotConfigured(RuntimeError):
    """Rollen för utskick saknas i drift."""


def _config(send):
    from botocore.config import Config

    if send:
        return Config(
            retries={"max_attempts": 1, "mode": "standard"}, connect_timeout=5, read_timeout=10
        )
    return Config(
        retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=20
    )


def _assume(role_arn):
    import boto3

    from apps.cloud.aws import base_session

    params = {
        "RoleArn": role_arn,
        "RoleSessionName": "adx-utskick",
        "DurationSeconds": DURATION_SECONDS,
    }
    external_id = getattr(settings, "UTSKICK_AWS_EXTERNAL_ID", "")
    if external_id:
        params["ExternalId"] = external_id
    sts = base_session().client(
        "sts", region_name=settings.UTSKICK_SES_REGION, config=_config(send=False)
    )
    creds = sts.assume_role(**params)["Credentials"]
    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=settings.UTSKICK_SES_REGION,
    )
    return session, creds.get("Expiration")


def session():
    """boto3-sessionen för utskick. AwsNotConfigured i drift utan roll."""
    role_arn = getattr(settings, "UTSKICK_AWS_ROLE_ARN", "")
    if not role_arn:
        if not settings.DEBUG:
            raise AwsNotConfigured("UTSKICK_AWS_ROLE_ARN saknas.")
        from apps.cloud.aws import base_session

        with _lock:
            if _state["session"] is None:
                _state.update(session=base_session(), expires=None, clients={})
            return _state["session"]
    with _lock:
        expires = _state["expires"]
        if _state["session"] is not None and (
            expires is None or expires - timezone.now() > REFRESH_BEFORE
        ):
            return _state["session"]
        new_session, expires = _assume(role_arn)
        _state.update(session=new_session, expires=expires, clients={})
        logger.info("Utskick: antog rollen för AWS")
        return new_session


def client(service, send=False):
    """En klient för tjänsten i UTSKICK_SES_REGION. Återanvänds så länge
    sessionen gäller (en anslutningspool per process)."""
    current = session()
    key = (service, bool(send), id(current))
    with _lock:
        cached = _state["clients"].get(key)
        if cached is not None:
            return cached
    made = current.client(service, region_name=settings.UTSKICK_SES_REGION, config=_config(send))
    with _lock:
        _state["clients"][key] = made
    return made


def forget():
    """Testerna: glöm sessionen och klienterna."""
    with _lock:
        _state.update(session=None, expires=None, clients={})
