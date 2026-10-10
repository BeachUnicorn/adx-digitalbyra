"""
Sidhuvudet och sidfoten på sidorna som en mottagare kan hamna på (README
E.5, "Mottagarens sidor"): kundens logga överst och ADX logga nederst.

Sidorna: templates/utskick/public/ (anmälan, tack, bekräfta e-post, Mina
utskick, integritet) på adx.se och templates/utskick/links/ (/s/, /p/, /b/
på k.adx.se, /a/ och /v/ på klick.adx.se). Startsidan, 404 och 429 på
länkvärdarna är inte någon kunds: där står "ADX Flamingo" som text överst.
Webbversionen /w/ är mejlet självt (sidhuvudet med loggan finns i mejlet)
och har inget eget skal.

    logo(account, company) -> dict | None
        {"url", "width", "height", "alt"} för kontots logga
        (MediaAsset is_logo), eller None utan logga (sidan visar då
        företagets namn som text). Bara kontots egen logga: kontot kommer ur
        koden eller token, aldrig ur en parameter (H.1).
    context(account, company) -> {"logga": ...}   det _base.html läser

Bilden är mejlens PNG (email.images.rendition med LOGO: på vitt, 80 px hög
fil), som byggs en gång och sedan återanvänds; retentionen tar aldrig den
logga som gäller. Den visas i samma mått som i mejlets sidhuvud
(images.display_size: högst 40 hög och 220 bred, aldrig större än filen),
och width och height i mallen är de måtten (utskick-public.css ändrar dem
inte, bara max-width:100% på en smal skärm). Den första visningen av en
logga utan rendition gör den i förfrågan, en tråd i taget per process
(images._making); en logga som inte går att göra om prövas inte igen på
BROKEN_SECONDS (cachen), så att varje visning inte läser filen på nytt.

Adressen är absolut på adx.se (SITE_BASE_URL + /media/...): länkvärdarna
har ingen /media/ och sätter inga kakor, och /media/ på adx.se lämnas av
nginx utan kakor. ADX logga nederst är static/images/adx-logo.png med en
länk till https://adx.se utan parametrar; den ritas av mallen
(templates/utskick/public/_base.html).

Loggan visas också när kundens utskick är avstängda: sidorna för att
avregistrera sig och ändra sina val fungerar då också (D.8), och företagets
namn står där ändå.
"""

import logging

from django.core.cache import cache

from . import optin

logger = logging.getLogger(__name__)

#: En logga som inte gick att göra om prövas igen först efter så här lång tid.
BROKEN_SECONDS = 600


def _absolute(url):
    if url.startswith(("https://", "http://")):
        return url
    return optin.absolute(url)


def _broken_key(asset):
    return f"utskick:logga-trasig:{asset.pk}"


def logo(account, company=""):
    """Kontots logga för sidhuvudet, eller None (utan logga, eller när filen
    inte går att läsa: sidan ska aldrig falla på loggan)."""
    from .email import images

    if account is None:
        return None
    asset = None
    try:
        asset = images.logo_asset(account)
        if asset is None or cache.get(_broken_key(asset)):
            return None
        row = images.rendition(asset, images.LOGO)
        if not row.file or not row.width or not row.height:
            return None
        url = _absolute(row.file.url)
    except Exception:  # noqa: BLE001 - namnet som text i stället för ett 500
        logger.warning("Utskick: loggan för konto %s gick inte att visa", account.pk)
        if asset is not None:
            cache.set(_broken_key(asset), True, BROKEN_SECONDS)
        return None
    width, height = images.display_size(row.width, row.height)
    return {"url": url, "width": width, "height": height, "alt": company or ""}


def context(account, company=""):
    return {"logga": logo(account, company)}
