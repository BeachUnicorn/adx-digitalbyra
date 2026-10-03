"""
Google Places (kundresan steg 2): företagets adress, telefon, betyg och
öppettider hos Google, sparade som OBEKRÄFTADE uppgifter med källan "google".

Av utan GOOGLE_PLACES_API_KEY: då görs inget anrop alls, och uppgifterna
kommer från hemsidan och kunden (README, Integrationer). Med nyckeln görs en
textsökning i Places API (New), places:searchText, med kort timeout. Adressen
är fast (Googles), så inget av kundens indata styr vart anropet går.

Träffen måste likna kunden: samma domän som hemsidan, eller ett gemensamt
ord i namnet. En annan firma med liknande namn ska inte ge kunden sitt betyg.
Kunden bekräftar ändå varje uppgift under Företaget innan den används.
"""

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from django.conf import settings

from apps.common.security import normalize_typography

from .models import Fact, company_slug
from .scan import (
    FACT_LABELS,
    KEY_ADDRESS,
    KEY_HOURS,
    KEY_PHONE,
    KEY_RATING,
    VALUE_MAX,
    registrable,
    store_fact,
)

logger = logging.getLogger(__name__)

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join(
    "places." + name
    for name in (
        "displayName",
        "formattedAddress",
        "nationalPhoneNumber",
        "rating",
        "userRatingCount",
        "regularOpeningHours",
        # Bara för att pröva att träffen är kundens (samma domän som hemsidan).
        "websiteUri",
    )
)
TIMEOUT = 5
MAX_BYTES = 256 * 1024


@dataclass
class PlaceResult:
    found: bool
    facts: int = 0
    name: str = ""
    error: str = ""


def is_configured():
    return bool(getattr(settings, "GOOGLE_PLACES_API_KEY", ""))


def _query(account):
    """Företagets namn, och adressen om vi redan har en (bättre träff)."""
    query = account.customer.name
    address = (
        account.facts.filter(key=KEY_ADDRESS)
        .exclude(value="")
        .values_list("value", flat=True)
        .first()
    )
    return f"{query} {address}" if address else query


def search(query):
    """Första träffen för query, som Googles dict, eller None."""
    body = json.dumps(
        {"textQuery": query[:200], "languageCode": "sv", "regionCode": "SE", "pageSize": 1}
    ).encode("utf-8")
    request = Request(  # noqa: S310 - fast https-adress hos Google
        SEARCH_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": settings.GOOGLE_PLACES_API_KEY,
            "X-Goog-FieldMask": FIELD_MASK,
        },
    )
    with urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
        data = json.loads(response.read(MAX_BYTES).decode("utf-8"))
    places = data.get("places") if isinstance(data, dict) else None
    if isinstance(places, list) and places and isinstance(places[0], dict):
        return places[0]
    return None


def _tokens(name):
    return {word for word in company_slug(name).split("-") if len(word) >= 3}


def matches(place, account):
    """Är träffen kunden? Samma domän som hemsidan, eller namnen delar ett
    ord på minst fyra bokstäver (eller alla kundnamnets ord)."""
    website = place.get("websiteUri") or ""
    if account.website_url and website:
        ours = registrable(urlsplit(account.website_url).hostname)
        theirs = registrable(urlsplit(website).hostname)
        if ours and ours == theirs:
            return True
    display = place.get("displayName") or {}
    theirs = _tokens(display.get("text", "") if isinstance(display, dict) else "")
    ours = _tokens(account.customer.name)
    shared = ours & theirs
    return bool(ours) and (any(len(word) >= 4 for word in shared) or ours <= theirs)


def _rating(place):
    rating, count = place.get("rating"), place.get("userRatingCount")
    if not isinstance(rating, int | float) or not isinstance(count, int) or count < 1:
        return ""
    word = "omdöme" if count == 1 else "omdömen"
    return f"{rating:.1f}".replace(".", ",") + f" av {count} {word}"


def _hours(place):
    hours = place.get("regularOpeningHours")
    days = hours.get("weekdayDescriptions") if isinstance(hours, dict) else None
    if not isinstance(days, list):
        return ""
    # Googles tider har smala hårda blanksteg och tankstreck: "07:00 - 16:00"
    # blir "07:00-16:00". split() tar alla sorters blanktecken.
    lines = []
    for day in days[:7]:
        if isinstance(day, str):
            line = " ".join(normalize_typography(day).split())
            lines.append(re.sub(r"(\d)\s*-\s*(\d)", r"\1-\2", line))
    return "; ".join(line for line in lines if line)[:VALUE_MAX]


def _address(place):
    address = place.get("formattedAddress")
    if not isinstance(address, str):
        return ""
    return re.sub(r",\s*(Sverige|Sweden)$", "", address.strip())


def update_from_google(account):
    """Slå upp företaget hos Google och spara det som hittas, obekräftat.

    None när Places inte är konfigurerat eller kontot är ett demokonto
    (inget anrop görs: demot pratar aldrig med Google, och läsningen av
    hemsidan som anropar hit stoppas redan av scan.demo_refusal). Annars ett
    PlaceResult; fel loggas och ger found=False, aldrig ett undantag."""
    if not is_configured() or getattr(account, "is_demo", False):
        return None
    try:
        place = search(_query(account))
    except Exception as exc:  # noqa: BLE001 - Google nere ska inte stoppa förslaget
        logger.warning("Flamingo: Google Places svarade inte: %s", type(exc).__name__)
        return PlaceResult(found=False, error="Google svarade inte.")
    if place is None or not matches(place, account):
        return PlaceResult(found=False)
    values = (
        (KEY_ADDRESS, _address(place)),
        (KEY_PHONE, place.get("nationalPhoneNumber") or ""),
        (KEY_RATING, _rating(place)),
        (KEY_HOURS, _hours(place)),
    )
    stored = 0
    for key, value in values:
        if isinstance(value, str) and value:
            stored += store_fact(account, key, FACT_LABELS[key], value, Fact.SOURCE_GOOGLE)
    display = place.get("displayName") or {}
    name = display.get("text", "") if isinstance(display, dict) else ""
    return PlaceResult(found=True, facts=stored, name=name)
