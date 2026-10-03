# SMS-API (apps/sms)

ADX kunder skickar sms från sina egna system via ett JSON-API hos ADX, som
skickar vidare med 46elks. Beställt av Giovanni 2026-10-03.

- **Aktivering:** byrån slår på SMS per kund på kundkortet (`#sms`). Först då
  fungerar API:t och kundens nycklar. Ingen mejlas, varken vid aktivering
  eller avstängning.
- **Kunden** ser allt under `/kund/sms/`: översikt (siffror, staplar per dag,
  länder, varje sms med hela texten), nycklar och tak, dokumentation och
  månadsunderlag. Byrån i kundvyn ser och kan göra exakt samma sak; det den
  gör gäller på riktigt och sparas i byråns namn.
- **Byrån** har översikten `/manage/sms/`: kunderna, månadens summor, sms att
  stämma av mot 46elks, månadsunderlagen, stängning av månaden och CSV för
  faktureringen.
- **Larm** går bara till byrån (`INQUIRY_NOTIFICATION_EMAIL`): när en kund når
  sitt tak (en gång per kund och månad) och när 46elks inte tar emot ett sms
  eller svarar oklart (högst en gång i timmen per kund). Kunden mejlas aldrig.

## Beslut

| Fråga | Beslut |
|---|---|
| Belopp | Heltal i 46elks enhet, tiotusendels krona (10 000 = 1 kr, 100 = 1 öre), på varje sms och underlag. Inga flyttal, inga avrundningar på vägen. Allt utan moms. |
| Pris per sms | 46elks `cost` + påslaget (standard 5 öre) gånger 46elks antal delar. Påslaget sparas på sms:et, så ett ändrat påslag ändrar inte gamla sms. |
| Årsavgift | Standard 999 kr, per kund. Står på underlaget för den månad tjänsteåret börjar (`service_year_start`, sätts till aktiveringsdagen) och samma månad varje år därefter, om SMS var aktiverat någon gång under månaden (`enabled_at` före månadens slut, och fortfarande aktiverat eller `disabled_at` efter månadens början) eller kunden skickat under den. Läget när månaden stängs spelar ingen roll. Räknas inte mot taket. |
| Kostnadstak | Standard 500 kr i månaden (knappt 900 svenska sms-delar). Kunden ändrar själv; 0 stoppar allt, också ett sms som skulle kosta 0. Gäller sms-kostnaden i svensk kalendermånad. Varje ändring sparar när och av vem (`monthly_cap_changed_at`/`_by`), och det syns på fliken Nycklar och tak och på kundkortet. |
| Länder | Standard bara Sverige. Byrån lägger till länder på kundkortet. Skydd mot sms-pumpning. |
| Avsändare | Kundens namn, 3-11 tecken a-z/A-Z/0-9, börjar med bokstav (46elks regel; "1Acme" ger 403 hos 46elks). Byrån sätter den vid aktivering. API:t godtar inget annat. |
| Längd | Högst 6 delar (918 GSM-tecken, 402 UCS-2). |
| Lagring | Allt sparas för alltid, texter och nummer inräknade. Raderna är skyddade mot kaskadradering. |
| Leveransfel | Ett sms som 46elks tog emot men operatören inte levererade (`failed`, felkod `delivery_failed`) behåller sitt pris: 46elks debiterar vid sändning. |
| Oklart svar | Tidsgräns, bruten förbindelse, 5xx eller ett svar som inte är JSON från 46elks: sms:et kan ha skickats. Det står kvar som `reserved` med uppskattat pris och kundens reference, felkod `provider_unknown`, och skickas aldrig igen. Se Avstämning nedan. |
| Provläge | Utan `SMS_SEND_LIVE=true` går varje sändning till 46elks med `dryrun=yes`: inget skickas, raden märks `test_mode` och kommer aldrig på underlaget. I provläge räknas den mot taket; när `SMS_SEND_LIVE` är på gör den det inte. Portalen visar dem som "varav testsändningar som inte faktureras" under Kostnad. |

## Så går en sändning till

`apps/sms/service.py`:

1. Nyckeln (`Authorization: Bearer adxsms_...`, bara SHA-256 sparas) och att
   kunden har SMS aktiverat.
2. Gränserna per nyckel (nedan, i cachen).
3. Fälten: numret tolkas med phonenumbers till E.164 och får sitt land;
   landet ska vara tillåtet, avsändaren kundens, texten högst 6 delar
   (GSM-7 eller UCS-2, `encoding.py`). Stopp blir en rad med status
   `rejected` och kostar inget.
4. Uppskattning: 46elks pris per del till landet från de senaste riktiga
   sms:en (7 dagar), annars 46elks provkörning (`dryrun=yes`).
5. Under radlås på kundens `SmsAccount` (`select_for_update`): minutgränsen
   per konto och för hela byrån (i databasen, nedan), sedan månadens summa
   plus uppskattningen mot taket. Över taket: raden blir `blocked_cap`,
   felet `monthly_cap_reached`, byrån larmas. Annars sparas raden som
   `reserved` med uppskattat pris.
6. Låset släpps, 46elks anropas med `whendelivered` (sms:ets egen adress).
7. Svaret stämmer av: 46elks `cost` och `parts` ersätter uppskattningen.
   Tar 46elks säkert inte emot sms:et (4xx, adressen går inte att slå upp,
   anslutningen nekas) blir raden `failed` med `provider_error`, pris 0, och
   byrån larmas.
8. Är svaret oklart (`ElksError.ambiguous` i `elks.py`: tidsgräns, bruten
   förbindelse, 5xx, inte JSON, inget id, eller ett oväntat fel) står raden
   kvar som `reserved` med felkod `provider_unknown` och `needs_check`, och
   byrån larmas. API:t svarar 202 med status `unknown`.

En `reference` från kunden gör anropet idempotent: samma reference och samma
nummer och text ger samma sms tillbaka (200, `"duplicate": true`); samma
reference med annat innehåll ger `reference_conflict` (409). Stoppade försök
och `provider_error` släpper sin reference, så att den kan användas igen.
Ett sms med `provider_unknown` håller sin reference: ett nytt försök ger samma
sms tillbaka och skickar aldrig igen.

## API:t

    POST /api/sms/v1/messages/         {to, message, from?, reference?, dryrun?}
    GET  /api/sms/v1/messages/<id>/
    GET  /api/sms/v1/usage/

Felkoder (stabila): `invalid_request` 400, `invalid_key` 401,
`sms_not_enabled` 403, `invalid_number` 400, `country_not_allowed` 400,
`sender_not_allowed` 400, `message_too_long` 400, `monthly_cap_reached` 402,
`not_found` 404, `method_not_allowed` 405, `reference_conflict` 409,
`rate_limited` 429 (med `Retry-After`), `internal_error` 500,
`provider_error` 502. Därtill `provider_unknown` på ett sms med status
`unknown` (svaret 202): inget fel i anropet, men läget är okänt. Kundens
dokumentation: `/kund/sms/dokumentation/`.

### Gränser

| Inställning | Standard | Räknas |
|---|---|---|
| `SMS_RATE_PER_SECOND` | 20 | alla anrop per nyckel, Djangos cache (per arbetare) |
| `SMS_RATE_PER_MINUTE` | 60 | per nyckel: sändningar och provkörningar, cache (per arbetare). Per konto: sms-rader de senaste 60 sekunderna utom `rejected`, i databasen under kontots lås (exakt, oavsett nycklar och arbetare) |
| `SMS_GLOBAL_PER_MINUTE` | 80 | alla kunders sms som gick till 46elks (inte `rejected`/`blocked_cap`) de senaste 60 sekunderna, i databasen |
| `SMS_DAILY_MAX_PER_KEY` | 5000 | sms per nyckel och svenskt dygn, i databasen (exakt) |

46elks släpper som standard igenom 100 sms i minuten för hela byråns konto,
och Flamingos sms går via samma konto. Gränsen per konto gör att en kund inte
ensam fyller den; byråns gräns håller alla kunder tillsammans på 80 och
lämnar 20 åt Flamingo. Byråns gräns kan passeras med högst så många sms som
skickas exakt samtidigt från olika kunder. Kostnaden skyddas av taket, inte
av gränserna.

### Leveransrapporter

46elks POST:ar `id`, `status` (`sent`, `delivered`, `failed`) och
`delivered` (UTC) till `/api/sms/46elks/dlr/<sms-id>/<signatur>/`.
Signaturen är en HMAC-SHA256 av sms:ets id med en nyckel härledd ur
`SECRET_KEY`: en adress går inte att gissa eller låna till ett annat sms, och
id:t i rapporten måste vara sms:ets eget 46elks-id. Status går bara framåt,
så en upprepad rapport (46elks försöker i minst sex timmar tills den får
200-204) ändrar inget. Fel signatur ger 404. Signaturer från
`SECRET_KEY_FALLBACKS` godtas också, så ett byte av `SECRET_KEY` gör inte
adresserna för sms på väg ogiltiga. En rapport för ett sms utan 46elks id får
409 (46elks försöker igen) om sms:et reserverades för mindre än två minuter
sedan: svaret är troligen på väg. Är det äldre och fortfarande reserverat
(eller `provider_unknown`) tas rapportens id över: sms:et får `provider_id`,
`sent_at`, priset blir det uppskattade (`provider_cost = estimated_cost`, en
rapport har inget pris), status enligt rapporten, och `needs_check` så att
byrån stämmer av priset. Valfritt: `SMS_DLR_ALLOWED_IPS`
spärrar rapporter från andra adresser än 46elks
(176.10.154.199, 85.24.146.132, 185.39.146.243, 2001:9b0:2:902::199, enligt
https://46elks.com/docs/verify-callback-origin).

Rapporten begärs bara när `SITE_BASE_URL` (eller `SMS_CALLBACK_BASE_URL`) är
https; lokalt finns ingen.

### Avstämning mot 46elks

`/manage/sms/#kontrollera` (och en rad på kundkortet) listar sms med
`needs_check` och reservationer som stått i mer än tio minuter. Leta upp
sms:et i 46elks sms-historik (mottagare och tid) och tryck:

- **Skickades**: ett reserverat sms blir `sent` med det uppskattade priset; ett
  som redan fått sitt id ur en leveransrapport markeras bara avstämt
  (**Avstämt**).
- **Skickades inte**: `failed`, `provider_error`, pris 0, och kundens
  reference blir fri.

Kunden mejlas inte. En månad stängs inte för en kund som har reserverade sms
från månaden kvar, så avstämningen behövs innan underlaget kan frysas.

### Sentry

`apps/common/sentry.py` maskar nycklarna (`adxsms_...`) och signaturen i
leveransadressen var de än står, spårar inte `/api/sms/46elks/`, maskar
variabler som heter `to`, `body`, `data` och `signature`, och skickar inga
lokala variabler alls från `apps.sms`: där bär nästan varje variabel ett
nummer, en text eller en nyckel.

## Kontrollerat mot 46elks (2026-10-03)

Mot https://46elks.com/docs/send-sms, /docs/sms-delivery-reports,
/docs/verify-callback-origin och 46elks OpenAPI-fil, och med `dryrun=yes` mot
kontot (inget skickades; numret +46701740605 är ur PTS serie för fiktiva):

- svaret har `status` "created", `parts`, och `estimated_cost` i stället för
  `cost` vid provkörning (inget `id`); Sverige 5200 per del (52 öre), Norge
  7000;
- 161 tecken gav 2 delar, 306 gav 2, 307 gav 3; 36 emojis 2; 80 par "{}" 3;
- fel är HTTP 403 med en rad text: USA ("disallowed by default"), avsändare
  som börjar med siffra, tomt meddelande;
- provkörningen prövar inte numret ("+4612" godtogs) eller avsändarens längd
  ("A" godtogs): därför prövas båda här först.

## Drift

Miljön (`../.env`):

    ELKS_API_USERNAME=...       # eller SMS_46ELKS_USER, samma 46elks-konto som Flamingo
    ELKS_API_PASSWORD=...       # eller SMS_46ELKS_PASSWORD
    SMS_SEND_LIVE=true          # annars skickas inget (provläge)
    SITE_BASE_URL=https://adx.se   # finns redan; ger leveransadressen
    # valfritt: SMS_DLR_ALLOWED_IPS=176.10.154.199,85.24.146.132,185.39.146.243,2001:9b0:2:902::199

Leveransadressen blir `https://adx.se/api/sms/46elks/dlr/<id>/<signatur>/`,
en per sms; inget behöver ställas in hos 46elks.

### Kommandot `sms_close_month` (cron)

Stänger förra månadens underlag för alla SMS-kunder; ett stängt underlag
ändras aldrig och körs kommandot igen hoppas det över. Varje konto stängs
för sig under radlås (två stängningar samtidigt ger ett underlag). En kund
med sms från månaden som fortfarande står som `reserved`, hur nya de än är,
hoppas över och rapporteras; kör igen efter avstämningen. Ett konto som
kastar ett fel loggas och hindrar inte de andra; kommandot avslutar då med
felkod. `--period 2026-09` för en viss månad, `--dry-run` för att bara visa
summorna. Knappen på `/manage/sms/` (och en per månad som inte är stängd)
gör samma sak med samma text. Inga mejl.

Cron som djangouser, den 1:a varje månad 03:10, samma mönster som de andra
raderna i djangousers crontab:

    10 3 1 * *  cd /home/djangouser/sites/adx/app && env $(grep -E "^(SECRET_KEY|DATABASE_URL|ALLOWED_HOSTS|SENTRY_DSN|SITE_BASE_URL)" ../.env | xargs) DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py sms_close_month >> /home/djangouser/sites/adx/backups/sms.log 2>&1

Fakturera sedan från CSV-filen på `/manage/sms/` (en rad per kund,
semikolon och decimalkomma, belopp utan moms). En textcell som börjar med
`= + - @`, tab eller vagnretur får en apostrof först, så att ett kundnamn
aldrig blir en formel i Excel.

### Lokalt

    uv run python manage.py sms_seed_demo --customer <id> [--clear]

Påhittade sms (nummer ur PTS fiktiva serie) för en kund med SMS aktiverat,
för att se sidorna. Vägrar utan DEBUG. Lokalt är `SMS_SEND_LIVE` av, så API:t
anropar 46elks bara med `dryrun=yes`.
