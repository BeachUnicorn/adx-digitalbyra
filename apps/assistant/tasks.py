"""
Bakgrundskörning av en chattur.

Projektet har ingen kögivare, och för en enkundssajt är en tråd rätt
avvägning - men trådar har tre fällor som måste hanteras, annars blir de
sämre än inget:

* Databasanslutningen. Django städar per request; en tråd måste stänga sin
  egen, annars läcker anslutningar tills Postgres säger nej.
* Omstart. En deploy mitt i en körning lämnar en PENDING-rad som aldrig
  fylls i - gränssnittet skulle snurra för evigt. Därför städas gamla
  PENDING-rader vid varje ny tur.
* Tystnad. En tråd som dör tar med sig traceback:en om ingen fångar den.

Byts det här mot en riktig kö senare är `queue_turn` den enda funktion som
behöver ändras.
"""

import logging
import threading

from django.db import connection
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Så länge en tur får vara TYST innan den räknas som död. Mäts på
#: updated_at, inte created_at: en tur som fortfarande loggar steg lever,
#: hur länge den än hållit på. Måste vara KORTARE än chattens polltid,
#: annars slutar gränssnittet fråga innan sveparen hunnit döma ut turen -
#: och då står "Arbetar" kvar för evigt.
STALE_AFTER = 10 * 60


def sweep_stale():
    """
    Markera övergivna turer som misslyckade. Returnerar antalet.

    En tur körs i en tråd i webbprocessen. Startar processen om - deploy i
    produktion, autoreload i utveckling - dör tråden och lämnar en
    PENDING-rad som ingen någonsin fyller i.
    """
    from .models import ChatMessage

    cutoff = timezone.now() - timezone.timedelta(seconds=STALE_AFTER)
    stale = ChatMessage.objects.filter(status=ChatMessage.Status.PENDING, updated_at__lt=cutoff)
    return stale.update(
        status=ChatMessage.Status.FAILED,
        error=(
            "Körningen avbröts innan den blev klar, troligen av en omstart "
            "av servern. Skicka frågan igen."
        ),
    )


def _run(reply_id):
    from .chat import run_turn

    try:
        run_turn(reply_id)
    except Exception:  # noqa: BLE001 - en tråd som dör tyst är värre
        logger.exception("Bakgrundsturen %s kraschade", reply_id)
        from .models import ChatMessage

        ChatMessage.objects.filter(pk=reply_id, status=ChatMessage.Status.PENDING).update(
            status=ChatMessage.Status.FAILED,
            error="Något gick fel i bakgrundskörningen.",
        )
    finally:
        # Trådens egen anslutning stängs inte av Django.
        connection.close()


def queue_turn(reply_id):
    """Starta turen i bakgrunden och återvänd direkt."""
    sweep_stale()
    thread = threading.Thread(target=_run, args=(reply_id,), daemon=True)
    thread.start()
    return thread
