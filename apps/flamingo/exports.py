"""
Filerna byrån laddar ner när Google Ads API inte är inkopplat (README,
steg 8 och 11): kampanjen som Google Ads Editor-fil, och förfrågningar,
klick på numret och vunna affärer som offline-konverteringar.

Båda är ren text (str). Vyerna i manage_review.py gör svaret av dem; här
finns ingen databasskrivning och inget nätverk.

Skydd mot formelinjektion
-------------------------
Filerna öppnas ofta i Excel eller Google Kalkylark innan de laddas upp. En
cell som börjar med = + - @ (eller tabb/CR) tolkas där som en formel, och
texterna kommer delvis från kunden och kundens kunder (namn, sökord). Varje
cell som börjar så får en apostrof först (safe_cell). Det gäller alla
celler, även rubrikerna, så ingen väg runt skyddet finns kvar.

safe_cell är sista linjen och ska inte behöva slå till på riktiga texter:
Google Ads Editor läser filen rakt av, så apostrofen skulle följa med in i
annonsen. Därför stoppar checks.validate rubriker och beskrivningar som
börjar med = + - @ (kunden skriver om dem), och sökordens inledande + och -
tas bort här (_keyword).

Google Ads Editor (google_ads_editor_csv)
-----------------------------------------
Byggt efter Google Ads Editor-hjälpens "CSV file columns" och "Prepare a
CSV file" (support.google.com/google-ads/editor/answer/57747 och 56368),
oktober 2026. Antaganden, eftersom Editor inte kan provköras här:

- En rad per sak: kampanjen, annonsgruppen, varje sökord, varje negativt
  sökord och annonsen. Tomma celler betyder "ingen ändring" för Editor.
- Rubrikerna är engelska kolumnnamn som Editor känner igen utan mappning:
  Campaign, Campaign Type, Networks, Campaign Daily Budget (Editor godtar
  också Budget/Daily budget), Budget type, Languages, Bid Strategy Type,
  Campaign Status, Ad Group, Ad Group Status, Keyword, Criterion Type, Ad
  type, Headline 1-15, Description 1-4, Final URL, Status, Comment.
- Kampanjen skapas pausad (Campaign Status = Paused). Byrån slår på den i
  Editor eller i Google Ads efter importen; inget går live av en import.
- Nätverk: bara Google sök ("Google Search"), inte sökpartner eller display.
- Budgeten är per dag i kontots valuta (kronor, heltal).
- Budgetstrategi: "Maximize clicks". En ny lokal kampanj har inga
  konverteringar att optimera mot än; byrån byter i Editor vid behov.
- Språk: sv.
- Sökordens matchningstyp skrivs i Criterion Type: Phrase, Exact, Broad.
- Negativa sökord läggs på annonsgruppen (Criterion Type "Negative Broad",
  "Negative Phrase" eller "Negative Exact"). Kampanjen har en annonsgrupp,
  så det blir samma sak som negativa på kampanjnivå, och raderna blir
  entydiga för Editor. Ett negativt sökord som bara är text blir Negative
  Broad: frågan stoppas när alla orden finns med, i vilken ordning som helst.
- Platsinriktningen går inte att skriva som en radie utan koordinater, och
  områdets text ("Nacka + 15 km") är ingen plats Editor kan slå upp. Den
  står därför i Comment på kampanjraden och läggs in för hand.
- Annonsen är en responsiv sökannons (Ad type = "Responsive search ad")
  med upp till 15 rubriker och 4 beskrivningar och slutadressen
  https://adx.se/lp/<slug>/ (settings.FLAMINGO_LANDING_BASE_URL byter
  domänen när sidorna flyttar till kundens egen subdomän).
- Teckenkodning: UTF-8. Vyn lägger till en BOM så att Editor och Excel
  läser å, ä och ö rätt; funktionen här returnerar texten utan BOM.

Offline-konverteringar (offline_conversions_csv)
------------------------------------------------
Googles mall för import av konverteringar från annonsklick: första raden
"Parameters:TimeZone=Europe/Stockholm", sedan rubrikraden
"Google Click ID,Conversion Name,Conversion Time,Conversion Value,
Conversion Currency,Ad User Data". Tiden skrivs yyyy-MM-dd HH:mm:ss i svensk
tid (det tidszonen på första raden säger), värdet i hela kronor (bara
affärer har ett) och valutan SEK. Ad User Data är besökarens samtycke
(Granted/Denied, Googles mall för EU-samtycke), bara när Lead.ad_consent
har ett riktigt svar; annars är cellen tom. Sidan frågar inte i dag, så
cellen är tom och raderna köas ändå (beslut 2026-10-03).

En rad per konvertering, med namnet för sin sort (conversion_names()):
förfrågan "ADX Flamingo förfrågan", klick på numret "ADX Flamingo samtal"
och affären settings.FLAMINGO_CONVERSION_NAME, annars "ADX Flamingo affär".
Namnen måste finnas exakt så i kundens Google Ads-konto (med API:t skapar
google_conversions.ensure_conversion_actions dem). Tiden är densamma som
API:t skickar (conversion_moment), så Google känner igen en konvertering
som redan kommit in den andra vägen.
"""

import csv
import io
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from .models import (
    DESCRIPTION_COUNT,
    HEADLINE_COUNT,
    MATCH_BROAD,
    MATCH_EXACT,
    MATCH_PHRASE,
    ConversionUpload,
    Lead,
)

STOCKHOLM = ZoneInfo("Europe/Stockholm")

#: Tecken som får ett kalkylark att läsa cellen som en formel.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

DEFAULT_CONVERSION_NAME = "ADX Flamingo affär"
#: Konverteringarnas namn i kundens Google Ads-konto för förfrågan och klick
#: på numret (affären heter conversion_name()).
LEAD_CONVERSION_NAME = "ADX Flamingo förfrågan"
CALL_CONVERSION_NAME = "ADX Flamingo samtal"
DEFAULT_LANDING_BASE_URL = "https://adx.se"

CONVERSIONS_PARAMETERS = "Parameters:TimeZone=Europe/Stockholm"
CONVERSIONS_HEADER = [
    "Google Click ID",
    "Conversion Name",
    "Conversion Time",
    "Conversion Value",
    "Conversion Currency",
    "Ad User Data",
]
#: Lead.ad_consent som Googles mall skriver det.
CONSENT_CELLS = {Lead.CONSENT_GRANTED: "Granted", Lead.CONSENT_DENIED: "Denied"}
CONVERSION_CURRENCY = "SEK"
CONVERSION_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

EDITOR_HEADER = [
    "Campaign",
    "Campaign Type",
    "Networks",
    "Campaign Daily Budget",
    "Budget type",
    "Languages",
    "Bid Strategy Type",
    "Campaign Status",
    "Ad Group",
    "Ad Group Status",
    "Keyword",
    "Criterion Type",
    "Ad type",
    *[f"Headline {n}" for n in range(1, HEADLINE_COUNT + 1)],
    *[f"Description {n}" for n in range(1, DESCRIPTION_COUNT + 1)],
    "Final URL",
    "Status",
    "Comment",
]

#: Matchningstyp i databasen -> Criterion Type i Editor.
EDITOR_MATCH = {MATCH_PHRASE: "Phrase", MATCH_EXACT: "Exact", MATCH_BROAD: "Broad"}


def safe_cell(value):
    """Cellens text, med en apostrof först om den annars blir en formel."""
    text = "" if value is None else str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def _write(rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    for row in rows:
        writer.writerow([safe_cell(cell) for cell in row])
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Google Ads Editor
# ---------------------------------------------------------------------------


def landing_base_url():
    return getattr(settings, "FLAMINGO_LANDING_BASE_URL", "") or DEFAULT_LANDING_BASE_URL


def landing_page_url(campaign):
    """Annonsens slutadress: https://adx.se/lp/<slug>/."""
    return landing_base_url().rstrip("/") + campaign.landing_url


def landing_host():
    """Domänen annonserna visar ("adx.se"): landningssidornas."""
    return urlsplit(landing_base_url()).netloc.removeprefix("www.")


def _keyword(text):
    """Sökordets text utan inledande + eller - (Googles gamla skrivsätt för
    bred matchning och negativt sökord). Typen står i Criterion Type, och
    ett + eller - först skulle annars få en apostrof av safe_cell."""
    return str(text or "").strip().lstrip("+-").strip()


def keyword_rows(campaign):
    """Sökorden som (text, Criterion Type). Okänd matchningstyp blir Phrase,
    samma som generatorns standard."""
    rows = []
    for item in campaign.keywords or []:
        if isinstance(item, dict):
            text, match = _keyword(item.get("text")), item.get("match") or MATCH_PHRASE
        else:
            text, match = _keyword(item), MATCH_PHRASE
        if text:
            rows.append((text, EDITOR_MATCH.get(match, "Phrase")))
    return rows


def negative_rows(campaign):
    """De negativa sökorden som (text, Criterion Type)."""
    rows = []
    for item in campaign.negatives or []:
        if isinstance(item, dict):
            text, match = _keyword(item.get("text")), item.get("match") or MATCH_BROAD
        else:
            text, match = _keyword(item), MATCH_BROAD
        if text:
            rows.append((text, "Negative " + EDITOR_MATCH.get(match, "Broad")))
    return rows


def location_note(campaign):
    area = (campaign.area or "").strip()
    parts = ["Platsinriktning läggs in för hand."]
    if area:
        parts.append(f"Område: {area}.")
    if campaign.radius_km:
        parts.append(f"Radie: {campaign.radius_km} km.")
    return " ".join(parts)


def google_ads_editor_csv(campaign):
    """Kampanjen som en Google Ads Editor-fil (CSV-text). Se modulens
    docstring för kolumnerna och antagandena."""
    width = len(EDITOR_HEADER)
    col = {name: i for i, name in enumerate(EDITOR_HEADER)}
    name = campaign.name
    ad_group = campaign.service.name if campaign.service_id else campaign.name

    rows = [EDITOR_HEADER]
    campaign_row = [""] * width
    campaign_row[col["Campaign"]] = name
    campaign_row[col["Campaign Type"]] = "Search"
    campaign_row[col["Networks"]] = "Google Search"
    campaign_row[col["Campaign Daily Budget"]] = int(campaign.daily_budget_kr or 0)
    campaign_row[col["Budget type"]] = "Daily"
    campaign_row[col["Languages"]] = "sv"
    campaign_row[col["Bid Strategy Type"]] = "Maximize clicks"
    campaign_row[col["Campaign Status"]] = "Paused"
    campaign_row[col["Comment"]] = location_note(campaign)
    rows.append(campaign_row)

    group_row = [""] * width
    group_row[col["Campaign"]] = name
    group_row[col["Ad Group"]] = ad_group
    group_row[col["Ad Group Status"]] = "Enabled"
    rows.append(group_row)

    for text, criterion in keyword_rows(campaign) + negative_rows(campaign):
        keyword_row = [""] * width
        keyword_row[col["Campaign"]] = name
        keyword_row[col["Ad Group"]] = ad_group
        keyword_row[col["Keyword"]] = text
        keyword_row[col["Criterion Type"]] = criterion
        if not criterion.startswith("Negative"):
            keyword_row[col["Status"]] = "Enabled"
        rows.append(keyword_row)

    headlines = [h for h in (campaign.headlines or []) if str(h).strip()][:HEADLINE_COUNT]
    descriptions = [d for d in (campaign.descriptions or []) if str(d).strip()][:DESCRIPTION_COUNT]
    ad_row = [""] * width
    ad_row[col["Campaign"]] = name
    ad_row[col["Ad Group"]] = ad_group
    ad_row[col["Ad type"]] = "Responsive search ad"
    for n, text in enumerate(headlines, start=1):
        ad_row[col[f"Headline {n}"]] = text
    for n, text in enumerate(descriptions, start=1):
        ad_row[col[f"Description {n}"]] = text
    ad_row[col["Final URL"]] = landing_page_url(campaign)
    ad_row[col["Status"]] = "Enabled"
    rows.append(ad_row)
    return _write(rows)


# ---------------------------------------------------------------------------
# Offline-konverteringar
# ---------------------------------------------------------------------------


def conversion_name(kind=ConversionUpload.KIND_DEAL):
    """Konverteringens namn i kundens Google Ads-konto för sorten."""
    return conversion_names()[kind]


def conversion_names():
    """{sort: namn} för förfrågan, klick på numret och affär."""
    return {
        ConversionUpload.KIND_LEAD: LEAD_CONVERSION_NAME,
        ConversionUpload.KIND_CALL: CALL_CONVERSION_NAME,
        ConversionUpload.KIND_DEAL: getattr(settings, "FLAMINGO_CONVERSION_NAME", "")
        or DEFAULT_CONVERSION_NAME,
    }


def conversion_moment(upload):
    """När konverteringen hände: affären när den blev vunnen (annars när
    den köades), förfrågan och klicket när de kom in. Samma tid i filen och
    i API:t."""
    if upload.kind == ConversionUpload.KIND_DEAL:
        return upload.lead.won_at or upload.created_at
    return upload.lead.created_at


def conversion_time(upload):
    """conversion_moment i svensk tid, som filen vill ha den."""
    return timezone.localtime(conversion_moment(upload), STOCKHOLM).strftime(CONVERSION_TIME_FORMAT)


def offline_conversions_csv(uploads):
    """Konverteringarna i Googles importformat för klick-konverteringar.

    uploads är ConversionUpload-rader (med lead). En rad utan gclid kan
    Google inte koppla till ett klick och hoppas över."""
    names = conversion_names()
    rows = [[CONVERSIONS_PARAMETERS], CONVERSIONS_HEADER]
    for upload in uploads:
        gclid = (upload.lead.gclid or "").strip()
        if not gclid or upload.kind not in names:
            continue
        value = upload.value_kr if upload.kind == ConversionUpload.KIND_DEAL else None
        rows.append(
            [
                gclid,
                names[upload.kind],
                conversion_time(upload),
                "" if value is None else int(value),
                CONVERSION_CURRENCY,
                CONSENT_CELLS.get(upload.lead.ad_consent, ""),
            ]
        )
    return _write(rows)
