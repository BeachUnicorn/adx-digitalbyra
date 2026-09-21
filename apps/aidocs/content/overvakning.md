# ADX övervakning: lägg in statusendpointet /status/adx/

Du är en AI-assistent som arbetar i ett Django-projekt som ADX driftar.
Din uppgift: ge projektet ADX standardiserade statusrapport, så att
adx.se kan visa kunden hur servern mår. Följ stegen i ordning. Ändra
inte kontraktet (fältnamn, URL, header) - adx.se:s övervakning läser
exakt det här formatet.

**Version 2 (2026-09-21).** Har projektet redan endpointet från en äldre
version av guiden (svaret saknar fältet `endpoint_version`): gå till
avsnittet "Uppgradera från version 1" längst ned.

## Vad som ska finnas när du är klar

- `GET /status/adx/` svarar med JSON när headern `X-ADX-Key` bär rätt nyckel.
  Nyckeln tas BARA emot i headern, aldrig i adressen.
- Fel eller saknad nyckel ger 403. Saknas nyckeln i projektets miljö ger vyn 404
  (endpointet "finns inte" förrän det är konfigurerat).
- Nyckeln ligger BARA i projektets `.env` (eller motsvarande hemliga miljö).
  Den får aldrig committas, loggas eller skrivas ut i chatten.
- Projektets felrapportering (Sentry) maskar headern, se steg 4. Nyckeln är
  densamma på alla sajter ADX driftar, så en läcka i ett projekt gäller alla.

## Steg 1: skapa filen

Lägg filen i en app som redan finns i projektet, t.ex. `core/status_endpoint.py`
(platsen är valfri). Kopiera källkoden nedan OFÖRÄNDRAD. Den har inga
beroenden utöver Django och standardbiblioteket.

```python
{{ status_endpoint_source }}```

Anmärkningar om filen:

- `_visits()` försöker importera `apps.analytics.models` (ADX egen
  besöksstatistik). Finns inte den appen returneras `null` för `visits` -
  det är korrekt, rör det inte. Har projektet en EGEN besökslogg får du
  gärna fylla `visits` med samma fyra fält: `sessions_7d`, `sessions_30d`,
  `pageviews_7d`, `top_pages_7d` (lista av `{"path", "n"}`).
- `_backup()` letar efter `*.sql.gz` i `<projektmapp>/backups/`, där
  projektmappen är katalogen OVANFÖR `settings.BASE_DIR`. Ligger projektets
  databasdumpar någon annanstans: ändra bara sökvägen i `_backup()`.
- `_deploy()` läser `<projektmapp>/release.json` och faller annars tillbaka
  på `git rev-parse`. Se steg 5.
- Nyckeln jämförs i `_authorized()`, en egen funktion, och på bytes. Det är
  avsiktligt: felrapporteringen skickar lokala variabler ur stackramarna, så
  nyckeln får aldrig vara en lokal variabel i en ram som kan kasta. Flytta
  inte in kontrollen i `status_view` och lägg inte till `?key=`.
- `settings.SITE_SLUG` och `settings.SENTRY_DSN` läses med `getattr` och
  får saknas.

## Steg 2: koppla in URL:en

I projektets rot-`urls.py`, FÖRE eventuella catch-all-mönster:

```python
from core.status_endpoint import status_view  # anpassa sökvägen

urlpatterns = [
    path("status/adx/", status_view, name="adx_status"),
    # ... resten
]
```

Adressen ska vara exakt `/status/adx/`.

## Steg 3: nyckeln

I `settings.py` (anpassa till hur projektet läser miljövariabler):

```python
ADX_STATUS_KEY = os.environ.get("ADX_STATUS_KEY", "")
```

I projektets `.env` på SERVERN (inte i repot):

```
ADX_STATUS_KEY={{ status_key }}
```

Det är samma nyckel på alla sajter ADX driftar. Kontrollera att `.env`
står i `.gitignore`. Har projektet en `.env.example`: lägg raden
`ADX_STATUS_KEY=` där UTAN värde.

## Steg 4: felrapporteringen får inte se nyckeln

Gäller om projektet använder Sentry (sök efter `sentry_sdk.init`). Annars:
hoppa över steget, men säg det till Giovanni.

Sentry skickar request-headers med varje felrapport OCH med varje spårad
request (`traces_sample_rate`), alltså även när inget gått fel. adx.se
anropar endpointet var femte minut. Sentrys inbyggda skydd känner inte igen
`X-ADX-Key`, så utan det här steget hamnar den delade nyckeln hos Sentry.

I `settings.py`, utanför eventuellt `if SENTRY_DSN:`-block så att testet
nedan kan läsa listan:

```python
from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber

SENTRY_DENYLIST = DEFAULT_DENYLIST + ["X-ADX-Key"]
```

Och i anropet till `sentry_sdk.init(...)`, lägg till:

```python
    event_scrubber=EventScrubber(denylist=SENTRY_DENYLIST),
```

Stavningen måste vara exakt `X-ADX-Key`. Scrubbern jämför headernamnet i
gemener med bindestreck; `x_adx_key` eller `HTTP_X_ADX_KEY` träffar ALDRIG.
Behåll projektets övriga init-argument som de är. Har projektet redan en
egen `event_scrubber` eller denylista: lägg till `"X-ADX-Key"` i den.

Headern är det enda stället nyckeln finns i en request, eftersom vyn inte
tar emot den i adressen och inte håller den i en lokal variabel. Därför
räcker den här raden; ingen `before_send` behövs för endpointets skull.

## Steg 5: deploy-stämpel (valfritt men önskat)

Låt deployskriptet skriva revision och tidpunkt efter varje lyckad deploy,
så visar adx.se "senast uppdaterad" för kunden:

```sh
printf '{"rev": "%s", "at": "%s"}\n' "$(git rev-parse --short HEAD)" \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > ../release.json
```

Körs från projektets kodkatalog, så att filen hamnar i katalogen ovanför
`BASE_DIR`. Filen ska inte committas.

## Steg 6: testa

Lokalt (sätt nyckeln i miljön först):

```sh
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/status/adx/          # 403
curl -s -H "X-ADX-Key: $ADX_STATUS_KEY" http://localhost:8000/status/adx/ | python3 -m json.tool
```

Förväntat svar (värdena varierar; `db` MÅSTE vara `"ok"`):

```json
{
  "app": "adx-platform",
  "endpoint_version": 2,
  "site": "kundslug",
  "time": "2026-09-21T08:00:00+00:00",
  "db": "ok",
  "server": {
    "cpu_count": 2,
    "uptime_seconds": 1234567,
    "load": [0.12, 0.10, 0.09],
    "mem": {"total_mb": 3900, "available_mb": 2100, "used_pct": 46.2},
    "disk": {"total_gb": 30.0, "used_gb": 12.4, "used_pct": 41.3}
  },
  "deploy": {"rev": "a1b2c3d", "at": "2026-09-20T18:49:37Z"},
  "backup": {"latest_at": "2026-09-21T02:15", "size_mb": 18.4, "age_hours": 5.7},
  "visits": null,
  "sentry": {"configured": true}
}
```

På macOS saknas `/proc`, så `uptime_seconds` och `mem` uteblir lokalt. Det
är väntat; på Linux-servern finns de.

Lägg gärna till ett test i projektets testsvit:

```python
from django.test import TestCase, override_settings

@override_settings(ADX_STATUS_KEY="test")
class AdxStatusTests(TestCase):
    def test_requires_key(self):
        self.assertEqual(self.client.get("/status/adx/").status_code, 403)
        r = self.client.get("/status/adx/", HTTP_X_ADX_KEY="test")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["db"], "ok")

    def test_key_in_the_address_is_refused(self):
        self.assertEqual(self.client.get("/status/adx/?key=test").status_code, 403)

    def test_odd_header_is_a_403_not_a_crash(self):
        r = self.client.get("/status/adx/", HTTP_X_ADX_KEY="\xe5\xe4\xf6")
        self.assertEqual(r.status_code, 403)

    def test_error_reports_never_carry_the_key(self):
        # Bara om projektet använder Sentry (steg 4).
        from django.conf import settings
        from sentry_sdk.scrubber import EventScrubber

        event = {"request": {"headers": {"X-Adx-Key": "hemlig-nyckel"}}}
        EventScrubber(denylist=settings.SENTRY_DENYLIST).scrub_event(event)
        self.assertNotIn("hemlig-nyckel", str(event))
```

Det sista testet är läckvakten: det faller om någon stavar om headern i
denylistan eller tar bort den.

## Steg 7: efter deploy

Verifiera mot produktionen:

```sh
curl -s -H "X-ADX-Key: <nyckeln>" https://<kundens-domän>/status/adx/
```

Säg sedan till Giovanni, ordagrant:

> Statusendpointet är live på https://<kundens-domän>/status/adx/. I
> adx.se/manage/: öppna kundens kort -> Övervakning -> lägg till domänen
> med rutan "vår plattform" ikryssad, slå på Server och Besök, och kör
> snabbkontroll.

## Felsökning

| Symptom | Orsak |
|---|---|
| 404 på /status/adx/ | `ADX_STATUS_KEY` saknas i processens miljö (starta om efter .env-ändringen), eller URL:en ligger efter ett catch-all-mönster |
| 403 med rätt nyckel | Blanksteg/radbrytning i .env-värdet, eller en proxy framför Django som tar bort headern (nginx släpper igenom `X-ADX-Key` som standard; lägg annars till den i proxyns lista). Lös det i proxyn - nyckeln får inte flyttas till adressen |
| `"db": "error: ..."` | Databasen svarar inte - det är ett riktigt fel, inte ett endpointfel |
| `backup` är `{}` eller `latest_at: null` | Inga `*.sql.gz` i `<projektmapp>/backups/` - peka om sökvägen i `_backup()` |
| `deploy.at` är `null` | `release.json` skrivs inte - se steg 5 |
| En del är `null` och `errors` finns i svaret | Den delen kastade ett undantag; `errors` säger vilken typ. Resten av rapporten gäller ändå |

## Regler

- Ändra inte fältnamn, URL eller header. Lägg hellre till nya fält än att byta gamla.
- Inga nya beroenden. Filen ska gå att kopiera mellan projekt.
- Endpointet ska vara billigt: inga tunga frågor, inga externa anrop.
- Exponera aldrig hemligheter, kunddata eller personuppgifter i svaret.
- Nyckeln i den här guiden är hemlig. Skriv den i `.env`, ingen annanstans.
- Nyckeln får inte hamna i loggar eller felrapporter: inte i adressen, inte i
  `print`/`logger`-rader, inte i en lokal variabel i `status_view`.

## Sentry (om kunden ska se antal fel)

Inget ändras i kundens projekt. Felen räknas centralt från ADX
Sentry-organisation. Det enda som behövs är att projektet redan
rapporterar till ett projekt i den organisationen; säg projektets slug
till Giovanni så lägger han in den på kundkortet.

## Uppgradera från version 1

Version 1 (före 2026-09-21) tog emot nyckeln även som `?key=` i adressen,
höll den som lokal variabel i `status_view` och jämförde strängar, vilket
gav ett 500-fel på en header med icke-ASCII-tecken. Alla tre kunde föra ut
nyckeln till projektets felrapportering.

1. Ersätt filen med källkoden i steg 1, oförändrad. Har du anpassat
   `_backup()` eller `_visits()`: för över just de ändringarna igen.
2. Sök i projektet efter `?key=` och `GET.get("key")` mot endpointet och ta
   bort dem (skript, dokumentation, cron).
3. Gör steg 4 (Sentry) och lägg till de tre nya testerna i steg 6.
4. Verifiera efter deploy med headern, som i steg 7: svaret ska innehålla
   `"endpoint_version": 2`. Prova INTE den riktiga nyckeln i adressen för att
   se att den nekas - då står den i accessloggen. Testet i steg 6 bevisar det.

Säg sedan till Giovanni att sajten kör version 2.
