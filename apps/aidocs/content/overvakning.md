# ADX övervakning: lägg in statusendpointet /status/adx/

Du är en AI-assistent som arbetar i ett Django-projekt som ADX driftar.
Din uppgift: ge projektet ADX standardiserade statusrapport, så att
adx.se kan visa kunden hur servern mår. Följ stegen i ordning. Ändra
inte kontraktet (fältnamn, URL, header) - adx.se:s övervakning läser
exakt det här formatet.

## Vad som ska finnas när du är klar

- `GET /status/adx/` svarar med JSON när headern `X-ADX-Key` bär rätt nyckel.
- Fel eller saknad nyckel ger 403. Saknas nyckeln i projektets miljö ger vyn 404
  (endpointet "finns inte" förrän det är konfigurerat).
- Nyckeln ligger BARA i projektets `.env` (eller motsvarande hemliga miljö).
  Den får aldrig committas, loggas eller skrivas ut i chatten.

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
  på `git rev-parse`. Se steg 4.
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

Om projektet kör bakom en proxy som tar bort okända headers finns en
reservväg: `?key=<nyckeln>` i adressen. Använd headern när det går.

## Steg 4: deploy-stämpel (valfritt men önskat)

Låt deployskriptet skriva revision och tidpunkt efter varje lyckad deploy,
så visar adx.se "senast uppdaterad" för kunden:

```sh
printf '{"rev": "%s", "at": "%s"}\n' "$(git rev-parse --short HEAD)" \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > ../release.json
```

Körs från projektets kodkatalog, så att filen hamnar i katalogen ovanför
`BASE_DIR`. Filen ska inte committas.

## Steg 5: testa

Lokalt (sätt nyckeln i miljön först):

```sh
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/status/adx/          # 403
curl -s -H "X-ADX-Key: $ADX_STATUS_KEY" http://localhost:8000/status/adx/ | python3 -m json.tool
```

Förväntat svar (värdena varierar; `db` MÅSTE vara `"ok"`):

```json
{
  "app": "adx-platform",
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
```

## Steg 6: efter deploy

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
| 403 med rätt nyckel | Proxyn tar bort headern - prova `?key=`; eller blanksteg/radbrytning i .env-värdet |
| `"db": "error: ..."` | Databasen svarar inte - det är ett riktigt fel, inte ett endpointfel |
| `backup` är `{}` eller `latest_at: null` | Inga `*.sql.gz` i `<projektmapp>/backups/` - peka om sökvägen i `_backup()` |
| `deploy.at` är `null` | `release.json` skrivs inte - se steg 4 |

## Regler

- Ändra inte fältnamn, URL eller header. Lägg hellre till nya fält än att byta gamla.
- Inga nya beroenden. Filen ska gå att kopiera mellan projekt.
- Endpointet ska vara billigt: inga tunga frågor, inga externa anrop.
- Exponera aldrig hemligheter, kunddata eller personuppgifter i svaret.
- Nyckeln i den här guiden är hemlig. Skriv den i `.env`, ingen annanstans.

## Sentry (om kunden ska se antal fel)

Inget ändras i kundens projekt. Felen räknas centralt från ADX
Sentry-organisation. Det enda som behövs är att projektet redan
rapporterar till ett projekt i den organisationen; säg projektets slug
till Giovanni så lägger han in den på kundkortet.
