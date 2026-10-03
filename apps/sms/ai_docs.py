"""
/smsz/: SMS-API:ts dokumentation för AI-assistenter i andra projekt.

Giovanni länkar hit från andra projekt så att deras AI vet hur man bygger mot
ADX SMS-API, vilken adress som gäller och att nyckeln ska skapas av en
människa. Sidan är öppen (ingen inloggning, ingen kod), innehåller inga
hemligheter och läses som text (Markdown), som /aiz/. ?format=json ger samma
sak maskinläsbart.

Felkoderna, HTTP-statusarna, gränserna och teckenräkningen hämtas ur koden
(service, ratelimit, encoding), så att sidan inte kan glida isär från API:t.
Inga löften om leveranstider.
"""

from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.views.decorators.cache import cache_page
from django.views.decorators.http import require_GET

from . import encoding, ratelimit, service

#: Miljövariabeln projekten lägger nyckeln i (samma namn som portalens
#: dokumentation).
KEY_ENV = "ADX_SMS_KEY"

FIELDS = [
    (
        "to",
        "ja",
        "Mottagarens mobilnummer, helst E.164 (+46701234567). Utan landsnummer "
        "tolkas numret som svenskt (0701234567). Bara mobilnummer.",
    ),
    ("message", "ja", "Texten. Långa texter delas i flera sms-delar."),
    (
        "from",
        "nej",
        "Avsändaren. Utelämna den: kontots godkända avsändare används alltid, "
        "och ett annat värde ger sender_not_allowed.",
    ),
    (
        "reference",
        "nej",
        "Ert eget id för sms:et, 1-64 tecken a-z A-Z 0-9 . _ : - . Samma "
        "reference igen ger samma sms tillbaka i stället för ett nytt. Använd alltid en.",
    ),
    ("dryrun", "nej", "true: räkna land, delar och pris utan att skicka och utan kostnad."),
]

STATUSES = [
    ("reserved", "Platsen under taket är reserverad och sms:et är på väg till leverantören."),
    ("sent", "Leverantören har tagit emot sms:et."),
    ("delivered", "Mottagarens telefon har tagit emot sms:et."),
    ("failed", "Sms:et kunde inte levereras."),
    ("rejected", "Stoppat innan det skickades (se error.code). Ingen kostnad."),
    ("blocked_cap", "Stoppat av månadens kostnadstak. Ingen kostnad."),
    (
        "unknown",
        "Leverantören svarade inte säkert. Skicka inte igen med ny reference; "
        "läget avgörs av ADX och blir sent, delivered eller failed.",
    ),
]


def _base(request):
    return f"{request.scheme}://{request.get_host()}"


def _spec(request):
    base = _base(request)
    api = base + "/api/sms/v1"
    limits = {**ratelimit.limits(), "max_parts": service.MAX_PARTS}
    return {
        "service": "ADX SMS-API",
        "api_base": api,
        "auth": {
            "header": "Authorization: Bearer <nyckel>",
            "key_prefix": "adxsms_",
            "env_var": KEY_ENV,
        },
        "key_from": base + reverse("sms:keys"),
        "endpoints": [
            {
                "method": "POST",
                "path": "/messages/",
                "what": "Skicka ett sms, eller provkör med dryrun.",
            },
            {"method": "GET", "path": "/messages/<id>/", "what": "Ett sms och dess status."},
            {"method": "GET", "path": "/usage/", "what": "Månadens förbrukning, tak och gränser."},
        ],
        "fields": [{"name": n, "required": r == "ja", "what": w} for n, r, w in FIELDS],
        "statuses": [{"status": s, "what": w} for s, w in STATUSES],
        "errors": [
            {
                "code": code,
                "http": service.HTTP_STATUS[code],
                "what": service.ERROR_TEXTS.get(code, ""),
            }
            for code in service.HTTP_STATUS
        ],
        "limits": limits,
        "encoding": {
            "gsm7": {"single": encoding.GSM_SINGLE, "per_part": encoding.GSM_PART},
            "ucs2": {"single": encoding.UCS2_SINGLE, "per_part": encoding.UCS2_PART},
        },
    }


def _markdown(request):
    s = _spec(request)
    api = s["api_base"]
    lim = s["limits"]
    enc = s["encoding"]
    fields = "\n".join(f"| `{n}` | {r} | {w} |" for n, r, w in FIELDS)
    statuses = "\n".join(f"| `{st}` | {w} |" for st, w in STATUSES)
    errors = "\n".join(f"| `{e['code']}` | {e['http']} | {e['what']} |" for e in s["errors"])
    return f"""# ADX SMS-API

Du har hamnat hos digitalbyrån ADX (adx.se). Här står hur ett projekt skickar
sms via ADX SMS-API. Sidan är skriven för dig som AI-assistent i ett annat
projekt. Följ den, och gissa inte: står något inte här, fråga din användare.

API-adressen: `{api}`

## Innan du skriver kod: be om nyckeln

Du kan inte skapa en nyckel själv, och du ska inte försöka. Be din användare
om den så här:

1. SMS måste vara aktiverat för kunden hos ADX. Vet användaren inte om det
   är det, be hen fråga Giovanni på ADX. Utan aktivering svarar API:t
   `sms_not_enabled`.
2. Användaren skapar nyckeln i kundportalen: {s["key_from"]}
   (Nycklar och tak, "Skapa nyckel"). Nyckeln börjar med `adxsms_` och visas
   bara en gång.
3. Användaren lägger nyckeln i projektets miljö som `{KEY_ENV}` (till exempel
   i `.env` på servern). Be aldrig användaren klistra in nyckeln i chatten,
   skriv aldrig ut den, lägg den aldrig i koden och committa den aldrig.
4. Nyckeln hör hemma på servern. Den får aldrig ligga i JavaScript som körs i
   en webbläsare eller i en app: den som har nyckeln skickar sms på kundens
   bekostnad.

Fråga också vilken avsändare kontot har (den syns i portalen och i
`GET {api}/usage/`) och vilka länder kontot får skicka till.

## Inloggning

Varje anrop har huvudet:

    Authorization: Bearer <nyckeln ur {KEY_ENV}>

Kroppen är JSON (`Content-Type: application/json`). Svaren är JSON. Ett fel
har alltid formen `{{"error": {{"code": "...", "message": "..."}}}}`. Bygg
logiken på `code`, inte på texten.

## Skicka ett sms

    POST {api}/messages/

| Fält | Krävs | Betydelse |
|---|---|---|
{fields}

Exempel:

    curl -s -X POST {api}/messages/ \\
      -H "Authorization: Bearer ${KEY_ENV}" \\
      -H "Content-Type: application/json" \\
      -d '{{"to": "+46701234567", "message": "Hej! Bilen är klar.", "reference": "order-1042"}}'

Svar `201` (skickat), `200` med `"duplicate": true` (samma reference igen),
eller `202` med `"status": "unknown"` (se nedan). Exempel på svar:

    {{"id": 123, "status": "sent", "to": "+46701234567", "country": "SE",
      "from": "Avsandaren", "message": "...", "parts": 1, "encoding": "gsm7",
      "price_sek": "0.5700", "price_is_estimate": false, "reference": "order-1042",
      "error": null, "test_mode": false, "created_at": "...", "sent_at": "...",
      "delivered_at": null}}

`price_sek` är kundens pris i kronor exklusive moms, som text med fyra
decimaler. Med `"dryrun": true` får du i stället
`{{"dryrun": true, "to", "country", "from", "parts", "encoding", "price_sek"}}`
och inget skickas.

## Läget för ett sms

    GET {api}/messages/<id>/

Samma form som svaret ovan. Det finns ingen webhook till ert system:
fråga efter läget när ni behöver det, och inte oftare än gränserna tillåter.

| Status | Betydelse |
|---|---|
{statuses}

## Förbrukning och tak

    GET {api}/usage/

Ger månadens antal, kostnad (`cost_sek`), taket (`cap_sek`), vad som är kvar
(`remaining_sek`), `cap_reached`, avsändaren (`from`), tillåtna länder
(`allowed_countries`) och gränserna. Kunden sätter taket själv i portalen;
när det är nått svarar API:t `monthly_cap_reached` (402) tills nästa månad
eller tills taket höjs.

## Felkoder

| Kod | HTTP | Betydelse |
|---|---|---|
{errors}

## Så hanterar du fel och omförsök

- Skicka alltid med en `reference` som är unik för det ni vill skicka (till
  exempel order-id plus syfte). Då är ett omförsök ofarligt: samma reference
  ger samma sms tillbaka, aldrig ett till.
- `429 rate_limited`: vänta det antal sekunder som `Retry-After` säger, och
  försök igen med samma reference.
- `500 internal_error`, `502 provider_error` eller ett nätverksfel: försök
  igen senare med samma reference. `provider_error` har inte debiterats.
- `202` med `status: "unknown"`: skicka INTE igen med en ny reference. Sms:et
  kan ha gått iväg. Fråga efter läget senare med `GET /messages/<id>/`.
- `400`-felen (`invalid_number`, `country_not_allowed`, `sender_not_allowed`,
  `message_too_long`, `invalid_request`) blir inte bättre av omförsök: rätta
  anropet eller visa felet för den som skrev numret eller texten.
- `402 monthly_cap_reached`: skicka inte igen. Säg till användaren att taket
  är nått.

## Gränser

- Högst {lim["per_second"]} anrop per sekund och nyckel.
- Högst {lim["per_minute"]} sms per minut för kontot.
- Högst {lim["per_day"]} sms per dygn och nyckel (svensk tid).
- Högst {lim["max_parts"]} sms-delar per sms.
- Bara länder som ADX tillåtit för kontot (från början bara Sverige). Fler
  länder: be användaren fråga Giovanni.

## Längd och delar

Text med bara tecken ur GSM-alfabetet: {enc["gsm7"]["single"]} tecken i ett
sms, annars {enc["gsm7"]["per_part"]} per del. Ett enda tecken utanför (till
exempel en emoji) gör hela texten till UCS-2: {enc["ucs2"]["single"]} tecken i
ett sms, annars {enc["ucs2"]["per_part"]} per del. Varje del kostar. Använd
`dryrun` för att se antalet delar innan ni skickar.

## Exempel: Python (utan extra paket)

    import json, os, urllib.request, urllib.error

    API = "{api}"

    def skicka_sms(to, text, reference):
        body = json.dumps({{"to": to, "message": text, "reference": reference}}).encode()
        req = urllib.request.Request(
            API + "/messages/", data=body, method="POST",
            headers={{"Authorization": "Bearer " + os.environ["{KEY_ENV}"],
                     "Content-Type": "application/json"}},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            fel = json.load(err)["error"]
            raise RuntimeError(f"{{fel['code']}}: {{fel['message']}}") from None

## Exempel: Node (fetch)

    const API = "{api}";

    export async function skickaSms(to, message, reference) {{
      const res = await fetch(API + "/messages/", {{
        method: "POST",
        headers: {{
          Authorization: "Bearer " + process.env.{KEY_ENV},
          "Content-Type": "application/json",
        }},
        body: JSON.stringify({{ to, message, reference }}),
      }});
      const data = await res.json();
      if (!res.ok && res.status !== 202) throw new Error(data.error.code);
      return data;
    }}

## Bra att veta

- Testa med `"dryrun": true` först. Det kostar inget och skickar inget.
- Lägg utskicket i ett bakgrundsjobb eller en kö om ert system skickar många sms,
  och håll er under gränserna ovan.
- Spara `id` från svaret om ni vill fråga efter läget senare.
- Frågor om konto, avsändare, länder eller priser: Giovanni på ADX.

Maskinläsbart: `{_base(request)}/smsz/?format=json`
"""


@require_GET
@cache_page(300)
def smsz(request):
    """Öppen dokumentation för AI-assistenter. Inga hemligheter."""
    if request.GET.get("format") == "json":
        response = JsonResponse(_spec(request), json_dumps_params={"ensure_ascii": False})
    else:
        response = HttpResponse(_markdown(request), content_type="text/markdown; charset=utf-8")
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response
