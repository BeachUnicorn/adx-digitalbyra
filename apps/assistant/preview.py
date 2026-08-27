"""
Förhandsgranskning av ett utkast på den riktiga sidan.

Diffen visar vilka fält som ändras. Den visar inte hur sidan blir - och det
är resultatet kunden ska ta ställning till, inte datan.

Tekniken: applicera utkastet inuti en transaktion, rendera den publika vyn
med den ändrade databasen, och rulla sedan tillbaka. Ingenting sparas.

Två saker gör det säkert:

* `transaction.set_rollback(True)` sätts i ett `finally`, så återställningen
  sker även om renderingen kastar. Ett utkast får ALDRIG kunna publiceras av
  att någon tittade på det.
* Den publika vyn anropas direkt via `resolve()`, inte genom hela
  middleware-stacken. Då körs varken besöksstatistik eller
  revisionsmiddleware - en förhandsgranskning ska inte synas i mätningen.

`op.apply` anropas direkt i stället för `draft.approve`: godkännandet
skriver revisioner och ändrar status, och det hör inte hemma i en titt.
"""

import logging

from django.contrib.auth.models import AnonymousUser
from django.db import transaction
from django.http import Http404
from django.test import RequestFactory
from django.urls import Resolver404, resolve

from .operations import REGISTRY, OperationError

logger = logging.getLogger(__name__)


class PreviewUnavailable(Exception):
    """Utkastet går inte att visa på en sida - med förklaring till kunden."""


def _public_url(obj):
    getter = getattr(obj, "get_absolute_url", None)
    if getter is None:
        raise PreviewUnavailable(
            "Den här ändringen hör inte till någon egen sida, så det finns "
            "inget att förhandsgranska. Diffen ovan visar hela ändringen."
        )
    return getter()


def _render_public(url):
    """Rendera den publika vyn för `url` utan middleware."""
    try:
        match = resolve(url)
    except Resolver404 as exc:
        raise PreviewUnavailable(f"Hittade ingen sida för {url}.") from exc

    request = RequestFactory().get(url)
    # ANONYM besökare, inte redaktören. Med en inloggad användare renderas
    # redigeringsdocken (pennorna) ovanpå sidan, och det är inte vad kunden
    # ska ta ställning till - frågan är hur BESÖKAREN kommer att se sidan.
    request.user = AnonymousUser()
    try:
        response = match.func(request, *match.args, **match.kwargs)
    except Http404 as exc:
        raise PreviewUnavailable(
            "Sidan går inte att visa med den här ändringen - den blir dold "
            "eller borttagen för besökare."
        ) from exc

    if hasattr(response, "render"):
        response.render()
    return response.content.decode(response.charset or "utf-8")


def render_draft(change, user):
    """
    HTML för hur sidan ser ut om utkastet godkänns.

    Höjer PreviewUnavailable om ändringen inte har någon publik sida.
    Ingenting sparas - transaktionen rullas alltid tillbaka.
    """
    op = REGISTRY.get(change.operation)
    if op is None or op.apply is None:
        raise PreviewUnavailable("Den här ändringen går inte att förhandsgranska.")

    with transaction.atomic():
        try:
            obj = op.apply(user, dict(change.payload), change.target)
            if obj is None:
                raise PreviewUnavailable("Ändringen gav inget objekt att visa.")
            return _render_public(_public_url(obj))
        except (PreviewUnavailable, OperationError):
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Förhandsgranskning av utkast %s misslyckades", change.pk)
            raise PreviewUnavailable(f"Kunde inte rendera sidan: {type(exc).__name__}.") from exc
        finally:
            # I finally, inte sist i blocket: rullas det inte tillbaka när
            # renderingen kastar har utkastet publicerats av en titt.
            transaction.set_rollback(True)
