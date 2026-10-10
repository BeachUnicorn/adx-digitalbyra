"""
Uppslag som sajtens ram (huvudet, mobilmenyn, sidfoten) och blocken gör om
och om igen, en gång per förfrågan (apps/common/request_memo.py). Vyerna och
kontextprocessorn site_chrome delar samma objekt i stället för att ladda var
sitt, och mallarna kan fråga hur många gånger som helst.

Objekten här delas inom förfrågan och är bara till för läsning.
"""

from apps.common.request_memo import memo


def menus():
    """(huvudmenyn eller None, sidfotens menyer i kolumnordning), med
    posterna och deras sidor hämtade."""
    from .models import Menu

    def load():
        header_menu = Menu.objects.filter(location="header").prefetch_related("items__page").first()
        footer_menus = list(
            Menu.objects.filter(location="footer")
            .order_by("order", "id")
            .prefetch_related("items__page")
        )
        return header_menu, footer_menus

    return memo("website:menus", load)


def active_services():
    """Aktiva tjänster i menyordning: mobilmenyn, 404-sidan, tjänstelistan,
    förfrågningsformulärets ämnen och Service-schemat läser samma lista."""
    from apps.services.models import Service

    return memo(
        "website:active_services",
        lambda: list(Service.objects.filter(is_active=True).order_by("order", "name")),
    )


def media_file(media_id):
    """MediaFile för ett id ur blockdata, eller None när id:t saknas, är
    ogiltigt eller pekar på en borttagen fil."""
    from .models import MediaFile

    if not media_id:
        return None

    def load():
        try:
            return MediaFile.objects.get(pk=media_id)
        except (MediaFile.DoesNotExist, ValueError, TypeError):
            return None

    return memo(("website:media", str(media_id)), load)
