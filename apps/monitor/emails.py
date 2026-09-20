"""Larm från övervakningen. Bara till byrån - aldrig till kunden."""

from django.conf import settings

from apps.inquiries.emails import _as_list
from apps.projects.emails import _base_url, _send


def _manage_url(domain):
    return f"{_base_url()}/manage/kunder/{domain.customer_id}/#overvakning"


def alert_down(domain, error):
    return _send(
        f"NERE: {domain.name}",
        f"{domain.name} svarar inte ({error}).\n\nKund: {domain.customer.name}\n"
        f"{_manage_url(domain)}",
        _as_list(settings.INQUIRY_NOTIFICATION_EMAIL),
    )


def alert_up(domain, minutes):
    return _send(
        f"UPPE IGEN: {domain.name}",
        f"{domain.name} svarar igen efter {minutes} minuter.\n\nKund: {domain.customer.name}",
        _as_list(settings.INQUIRY_NOTIFICATION_EMAIL),
    )


def alert_daily(rows):
    """En sammanfattning per dygn av det som behöver ögon: cert, domän, disk, fel."""
    lines = ["Övervakningen behöver ögon:", ""]
    for domain, text in rows:
        lines.append(f"- {domain.name}: {text}")
        lines.append(f"  {_manage_url(domain)}")
    return _send(
        f"Övervakning: {len(rows)} sak{'' if len(rows) == 1 else 'er'} att titta på",
        "\n".join(lines),
        _as_list(settings.INQUIRY_NOTIFICATION_EMAIL),
    )
