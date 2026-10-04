# Övervakning: så kopplas en kund

Tre fall. Larm går alltid bara till byrån; kunden ser läget i portalen (/kund/status/).

## A. Sajten ligger inte på vår plattform (WordPress, annat webbhotell)

Inget installeras hos kunden. Allt mäts från adx.se: online-status, svarstid,
certifikat, registrar (RDAP, WHOIS för .se/.nu), e-postens DNS, PageSpeed,
säkerhetsheaders.

1. /manage/kunder/<id>/ -> Övervakning: skriv domänen, "vår plattform" tom, Lägg till.
2. Kryssa i det kunden ska se, Spara övervakning.
3. Kör dygnskontroll en gång så sidan får data direkt. Cron sköter resten
   (snabbkontroll var 5:e minut, dygnskontroll 06:30, som också hämtar
   Googles data, se nedan).

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

## Googles data (PageSpeed, riktiga besökare, Search Console, Business Profile)

Allt hämtas i dygnskontrollen (`monitor_check --daily`), bara för det som är
påslaget på kundkortet. Byrån ser allt på /manage/overvakning/doman/<id>/google/
(länken "Google-data" på kundkortet och "Google" i driftöversikten). Kunden
ser en kort sammanfattning på /kund/status/. Larm går bara till byrån, i
dygnsmejlet; samma problem larmar en gång och påminns sedan högst en gång i
veckan (runner._once).

| Vad | Påslaget med | Hur | Kod |
|---|---|---|---|
| PageSpeed: prestanda, tillgänglighet, bästa praxis, SEO + fältdata | Prestanda | PAGESPEED_API_KEY | checks.check_performance |
| Riktiga besökare över tid (Chrome UX Report History API) | Prestanda | CRUX_API_KEY, annars PAGESPEED_API_KEY | google_checks.fetch_crux |
| Search Console: klick, visningar, CTR, position, toppfrågor och toppsidor, sitemaps, indexstatus | Sökpositioner | ADX:s Google-inloggning | google_checks.fetch_search |
| Google Business Profile: profilen, statistik, betyg | Google Business Profile | ADX:s Google-inloggning | google_checks.fetch_gbp |

### Cron

Ingen ny rad: dygnskontrollen som redan körs hämtar Googles data.

    30 6 * * * cd /home/djangouser/sites/adx && .venv/bin/python manage.py monitor_check --daily

Kvoter: CrUX 2 anrop per domän och dygn (150 per minut per projekt). Search
Console cirka 7 anrop plus högst 10 URL-inspektioner per domän och dygn
(Googles gräns är 2 000 per egendom). Business Profile 3 anrop per domän,
plus en lista över konton och platser per körning när någon domän matchas
automatiskt.

### Det som görs en gång i Google

1. **API-nyckeln (PageSpeed och CrUX).** Google Cloud-projektet som
   PAGESPEED_API_KEY hör till: slå på **Chrome UX Report API** (PageSpeed
   Insights API är redan på). Vill du ha en egen nyckel: CRUX_API_KEY i .env.
   Utan nyckel hämtas inte CrUX. Små sajter saknas hos Google (404): sidan
   säger då "För lite trafik för Googles mätning", det är inget fel.
2. **OAuth-projektet (samma som Flamingo).** Slå på **Google Search Console
   API**. Lägg till behörigheterna `webmasters.readonly` och `business.manage`
   på samtyckesskärmen. Koppla sedan om på /manage/flamingo/google/ (sidan säger
   "Koppla om för att läsa Search Console" tills det är gjort).
3. **Search Console per kund.** Kunden (ägaren av egendomen) lägger till
   ADX:s Google-konto (giovanni@palermo.se) som användare, behörighet
   Begränsad räcker. Egendomen hittas automatiskt (sc-domain:domänen eller
   https://domänen/) eller väljs på Google-sidan för domänen.
4. **Business Profile.** Googles API:er för Business Profile har kvoten 0
   tills Google godkänt en ansökan: https://support.google.com/business/contact/api_default
   ("Application for Basic API Access", med Cloud-projektets nummer). Kräver
   en verifierad profil som varit aktiv i 60 dagar och en webbplats. När
   kvoten i Cloud Console visar 300 per minut: slå på My Business Account
   Management API, My Business Business Information API, Business Profile
   Performance API och Google My Business API (omdömena). Kunden lägger till
   ADX:s Google-konto som ansvarig för profilen. Platsen matchas på
   webbadress eller väljs på Google-sidan för domänen.

Fel i inställningen (ingen inloggning, saknad behörighet, API:t avslaget,
kvoten 0) visas för byrån med vad som ska göras, larmar inte och visas
aldrig för kunden.
