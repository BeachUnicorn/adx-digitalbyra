"""
Larm till byrån (INQUIRY_NOTIFICATION_EMAIL), aldrig till kunden.

    agency(subject, lines, once=None, window="hour")

Det enda stället i apps/utskick som importerar django.core.mail (vakten i
test_s1_guards). Kundens personer får bara sitt eget bekräftelsemejl, och
det går via email/transport.py, aldrig härifrån. Kunden mejlas aldrig:
pauser och notiser syns i appen (README I.5).

Med once skickas samma larm högst en gång per fönster ("hour" eller "day",
svenskt dygn): först i processens cache, sedan exakt i databasen
(limits.hit med scope "alert"), så att flera arbetare eller en tick i taget
inte fyller byråns inkorg. Larmen
bär aldrig adresser eller namn, bara pk och antal. Ett larm fäller aldrig
det som larmade.
"""

import logging

from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail

logger = logging.getLogger(__name__)

SUBJECT_MAX = 200


def _recipients():
    from apps.inquiries.emails import _as_list

    recipients = _as_list(getattr(settings, "INQUIRY_NOTIFICATION_EMAIL", ""))
    if not recipients:
        return []
    if "smtp" in settings.EMAIL_BACKEND and not getattr(settings, "EMAIL_HOST_USER", ""):
        logger.warning("Utskickslarmet till byrån hoppades över: e-post är inte inkopplad.")
        return []
    return recipients


def agency(subject, lines, once=None, window="hour", now=None):
    """Skicka ett larm till byrån. True om det skickades.

    once är en nyckel för samma larm ("key_mismatch", "dpa_missing"): då
    skickas det högst en gång per fönster."""
    from . import limits

    try:
        recipients = _recipients()
        if not recipients:
            return False
        if once:
            # Först processens cache: ett larm inifrån en transaktion som
            # sedan rullas tillbaka (KeyMismatch) tar räknaren med sig.
            timeout = 24 * 3600 if window == "day" else 3600
            if not cache.add(f"utskick-alert:{once}", 1, timeout):
                return False
            start = limits.day_window(now) if window == "day" else limits.hour_window(now)
            if limits.hit("alert", str(once)[:80], start, 1):
                return False
        send_mail(
            str(subject)[:SUBJECT_MAX],
            "\n".join(str(line) for line in lines),
            settings.DEFAULT_FROM_EMAIL,
            recipients,
            fail_silently=False,
        )
    except Exception:  # noqa: BLE001 - ett larm får aldrig fälla det som larmade
        logger.exception("Kunde inte skicka utskickslarmet till byrån")
        return False
    return True


# ---------------------------------------------------------------------------
# S2: sändningsmotorns larm (README D.3, D.4, D.9, H.5). Bara pk och antal.
# ---------------------------------------------------------------------------


def _utskick_line(utskick):
    return f"Utskick {utskick.pk} hos konto {utskick.account_id}."


def breaker(until, now=None):
    """Nödbromsen drogs (D.4): tre oklara svar eller fel från 46elks inom två
    minuter. All sms-sändning från utskicken väntar till until."""
    from django.utils import timezone

    stamp = timezone.localtime(until).strftime("%H.%M")
    return agency(
        "Utskick: sms pausade efter fel hos 46elks",
        [
            "Tre sms fick ett oklart svar eller ett fel från 46elks inom två minuter.",
            f"All sms-sändning från utskicken väntar till {stamp}. Kundernas API påverkas inte.",
            "Stäm av oklara sms på /manage/sms/#kontrollera.",
        ],
        once="sms_breaker",
        now=now,
    )


def utskick_paused(utskick, reason, detail="", now=None):
    """Ett utskick pausades av en orsak byrån ska känna till (provider,
    stops). Högst ett larm per utskick och orsak i timmen."""
    lines = [_utskick_line(utskick), f"Orsak: {reason}."]
    if detail:
        lines.append(detail)
    return agency(
        f"Utskick: pausat ({reason})",
        lines,
        once=f"paused:{utskick.pk}:{reason}",
        now=now,
    )


def first_big_utskick(utskick, recipients, now=None):
    """Kontots första utskick med fler än 500 mottagare (D.9)."""
    return agency(
        "Utskick: första stora utskicket för en kund",
        [
            _utskick_line(utskick),
            f"{recipients} mottagare. Kontrollera att urvalet och samtyckena ser rimliga ut.",
        ],
        once=f"first_big:{utskick.account_id}",
        window="day",
        now=now,
    )


def information_utskick(utskick, recipients, recent, now=None):
    """Informationsutskick till fler än 200, eller fler än två på 30 dagar
    för samma konto (H.5)."""
    return agency(
        "Utskick: informationsutskick att granska",
        [
            _utskick_line(utskick),
            f"Skäl: {utskick.get_info_reason_display() or 'saknas'}.",
            f"{recipients} mottagare. Kontot har {recent} informationsutskick "
            "de senaste 30 dagarna.",
            "Information kräver inget samtycke och följer inte veckotaket.",
        ],
        once=f"information:{utskick.pk}",
        window="day",
        now=now,
    )


def low_disk(free_pct, now=None):
    """Frysningen vägrar starta när diskens lediga utrymme är under 8 % (D.3)."""
    return agency(
        "Utskick: disken är nästan full",
        [
            f"Ledigt på disken: {free_pct:.1f} %.",
            "Schemalagda utskick fryses inte förrän det finns mer än 8 % ledigt.",
        ],
        once="freeze_low_disk",
        now=now,
    )
