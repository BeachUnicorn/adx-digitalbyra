# Övervakning: så kopplas en kund

Tre fall. Larm går alltid bara till byrån; kunden ser läget i portalen (/kund/status/).

## A. Sajten ligger inte på vår plattform (WordPress, annat webbhotell)

Inget installeras hos kunden. Allt mäts från adx.se: online-status, svarstid,
certifikat, registrar (RDAP, WHOIS för .se/.nu), e-postens DNS, PageSpeed,
säkerhetsheaders.

1. /manage/kunder/<id>/ -> Övervakning: skriv domänen, "vår plattform" tom, Lägg till.
2. Kryssa i det kunden ska se, Spara övervakning.
3. Kör dygnskontroll en gång så sidan får data direkt. Cron sköter resten
   (snabbkontroll var 5:e minut, dygnskontroll 06:30).

Server och Besök kan inte visas här - låt dem vara av.

## B. Pluskund med vår plattform på egen server (samma kodbas)

/status/adx/ finns i koden. Det som saknas är nyckeln.

1. Hämta den delade nyckeln: `ssh adx sudo grep ADX_STATUS_KEY /home/djangouser/sites/adx/.env`
2. På kundens server: `ADX_STATUS_KEY=<nyckeln>` i sajtens .env, starta om (deploy gör det).
3. Testa: `curl -H "X-ADX-Key: <nyckeln>" https://kundensdoman.se/status/adx/`
   -> JSON med "db": "ok", server, backup, deploy, visits. Utan nyckel: 403.
4. Kundkortet: skriv domänen, KRYSSA I "vår plattform", Lägg till
   (statusendpointet fylls i som https://domänen/status/adx/).
5. Slå på Server och Besök, spara, kör snabbkontroll.

## C. Annan Django-sajt vi driftar

Endpointet är en fil utan beroenden på resten av appen.

1. Kopiera apps/monitor/status_endpoint.py till projektet (t.ex. core/status_endpoint.py).
2. urls.py: `path("status/adx/", status_view)`.
3. settings.py: `ADX_STATUS_KEY = os.environ.get("ADX_STATUS_KEY", "")` + raden i .env.
4. Deploy, curl-testa, lägg till domänen med "vår plattform" ikryssad.

Filen läser bara settings.BASE_DIR. Backup visas om dumparna ligger i
<projektmapp>/backups/*.sql.gz, deploy om deployskriptet skriver
<projektmapp>/release.json (se server/deploy.sh), besök om sajten har vår
analytics-app. Saknas något blir bara den raden tom.

## Sentry (alla fall)

Kundens sajt rapporterar till ett projekt i byråns Sentry-organisation;
inget ändras hos kunden. På adx.se:s .env: SENTRY_ORG_SLUG och
SENTRY_API_TOKEN (project:read), starta om. På kundkortet: projektets slug
och rutan "Fel i Sentry".

## Google Business Profile och Search Console

Rutorna finns; panelerna säger "kopplas i nästa steg". OAuth per kund mot
Google är inte byggt.
