# ADX Flamingo

Annonser i Google sök, en sida per tjänst och förfrågningar i en inkorg,
mätt hela vägen till affär. En person på ADX granskar allt innan något
publiceras. Godkänd kundresa: `adx-marketing/kundresa-mvp.html` (steg 02-12).

Tjänsten är stängd: bara kunder som byrån aktiverat på kundkortet ser den.
Kundens konto skapas av byrån (kontakt i kundportalen), inte av kunden själv.

## Delar

| Del | Adress | Design | Var |
|---|---|---|---|
| Sidorna (marknadsföring) | `/flamingo/`, `/flamingo/<slug>/` | Flamingo | Blocksidor med `design="flamingo"`, redigeras i /manage/, seed: `seed_flamingo` |
| Verktyget | `/flamingo/app/...` | Flamingo, appläge | `app_views/`, `templates/flamingo/app/` |
| Kundens landningssidor | `/lp/<slug>/`, `/lp/<slug>/tack/` | Neutral, kundens namn | `public_views.py`, `templates/flamingo/lp/` |
| Byråns sida | `/manage/flamingo/...` | Panelens | `manage_views.py`, `manage_review.py`, `templates/manage/flamingo/` |

Verktygets sidor:

| Adress | Vad | Kundresan |
|---|---|---|
| `app/` | Översikten: siffror, bandet, "tre saker", senaste förfrågningar och kampanjer | 12 |
| `app/forslag/` | Förslaget: hemsidan läses av, kunden väljer tjänster, en exempelannons (mallarna, bara bekräftade uppgifter) och föreslagen start (formulärets förval) | 02 |
| `app/foretaget/` | Företaget: uppgifter med källa (bekräfta, rätta, stryk) och tjänsterna | 04 |
| `app/google/` | Google: kontots id eller "skapa ett åt mig", läget i en tidslinje | 05 |
| `app/kampanjer/`, `ny/`, `<pk>/` | Kampanjerna, ny kampanj, förslaget i flikarna Annonser, Sökord, Sidan och Granskning | 06-08 |
| `app/inkorg/`, `<pk>/` | Inkorgen och en förfrågan: varifrån, status och belopp | 10-11 |
| `app/installningar/` | Sms till kunden och autosvaret (båda av från början) | 10 |
| `app/kund/` (POST) | Kundväljaren för en kontakt i flera Flamingo-kunder | |

Kom-igång-stegen (`rules.onboarding_for`) visas ovanför Förslaget,
Företaget, Google och översikten tills alla fyra är klara: Förslaget
(hemsidan läst eller tjänster finns), Företaget (minst en uppgift och
ingen obekräftad), Google (kopplat och betalningen klar, "hos ADX" medan
byrån kopplar) och Första kampanjen (en kampanj som lämnat utkastläget).

Byråns sidor:

| Adress | Vad |
|---|---|
| `/manage/flamingo/` | Kunderna med Flamingo, köns siffror, sidorna |
| `/manage/flamingo/granska/` | Kön: att granska, godkända som inte är publicerade, hos kunden, live, konverteringar |
| `/manage/flamingo/granska/<pk>/` | En kampanj: granska (rätta och skriv varför), publicera, pausa, återuppta |
| `/manage/flamingo/kampanj/<pk>/editor.csv` | Kampanjen som Google Ads Editor-fil |
| `/manage/flamingo/konverteringar.csv` | Vunna affärer som Googles importfil (GET), "Markera som exporterade" (POST) |
| Kundkortet, `#flamingo` | Aktivera, "Visa Flamingo som kunden", Google-kopplingen (status, id, notering), kampanjerna |

## Behörighet

All logik i `access.py`, en grind i `middleware.py`. 404 för alla utan
behörighet (aldrig omdirigering eller 403). Verktyget visar alltid
`request.flamingo.customer` och inget annat: varje vy går via `app_view`,
som slår upp kundens `FlamingoAccount`, och varje id ur adressen eller ett
formulär hämtas med `account=account`.

- Kontakt: sin Flamingo-kund (väljare om flera).
- Byrån i kundvyn: som kunden, **skrivskyddat** (grinden skickar tillbaka
  varje POST, mallarna döljer formulären). Kunden utan Flamingo-konto ger
  `app/no_account.html`.
- Byrån utan kundvy: verktyget visar Flamingo-kunderna med "Visa som
  kunden" (`app/staff_index.html`). Knappen skickar `next`, så kundvyn
  öppnas i verktyget; granskningens knapp öppnar kampanjen.
- Kundens landningssidor (`/lp/`) är publika när kampanjen är live; byrån
  kan förhandsvisa alla med en remsa överst.

## Flödet (kundresan, steg 2-12)

1. **Förslaget** (`scan.py`, `places.py`): startsidan och upp till fem
   undersidor hämtas med SSRF-skyddet i `apps/tools/analyzer.fetch` (varje
   omdirigering prövas och anslutningen går till den IP som prövades), högst
   512 kB per sida och med en hård tidsgräns. Kunden ser aldrig hämtningens
   eget fel, bara en allmän text. En läsning åt gången, högst en per två
   minuter och tio per dag (`limits.reserve_scan`). Telefon, e-post,
   adress och öppettider läses med regler, tjänster och övriga uppgifter
   med AI (`apps/assistant/llm`, ett verktyg med fast schema, varje förslag
   prövas mot sidtexten) eller regler; betyg och omdömen tas aldrig från
   hemsidan. Allt sparas obekräftat. Med `GOOGLE_PLACES_API_KEY` läggs adress, telefon,
   betyg och öppettider från Google till, också obekräftade.
2. **Företaget**: `Fact`-rader med källa (hemsidan, Google, kunden, ADX).
   Kunden bekräftar, rättar eller stryker. En vald tjänst får en tom
   prisrad; ett tomt pris skrivs aldrig som en gissning.
3. **Google**: kunden anger kontots id eller ber om ett nytt. Byrån kopplar
   kontot under förvaltarkontot och bockar av "kopplat" och "betalning
   klar" på kundkortet. Kunden ser läget och byråns notering.
4. **Kampanj** (`app/kampanjer/ny/`): tjänst, sätt att sälja (ringer /
   offert / boka tid), ort och radie, budget per dag (50-5 000 kr).
5. **Förslag** (`generator.py`): sökord (tjänst och verbform gånger orterna,
   fras och exakt), standardlistan med negativa sökord, upp till 15
   rubriker och 4 beskrivningar, och landningssidans innehåll efter sättet
   att sälja. Texterna skrivs av AI när den är konfigurerad, inom
   dygnsbudgeten och inom kontots 20 AI-förslag per dag
   (`limits.reserve_ai`), annars av mallar, och bara ur bekräftade
   uppgifter. Ett betyg används bara om det kommer från Google eller ADX,
   vad uppgiften än heter ("Trustpilot: 4,9"): `models.is_rating_like` och
   `Fact.is_usable`, som `confirmed_facts()` filtrerar på. Företaget visar
   kunden varför ett sådant betyg inte används.
   `checks.validate` (teckengränser, inga siffror som inte finns bland
   uppgifterna, inga förbjudna påståenden eller löften om tider, ingen
   text eller tjänst som börjar med = + - @ (Editor-filen), sökord mot negativa,
   budget, område) stoppar inskicket och samma kontroller
   körs på granskarens version.
6. **Granskning**: inskicket skapar en `Review`-runda med en
   ögonblicksbild. Byrån rättar i `/manage/flamingo/granska/<pk>/` och
   skriver varför per del; ändringarna sparas som diff och kampanjen går
   till kunden. Byrån larmas med mejl vid inskick och godkännande
   (`INQUIRY_NOTIFICATION_EMAIL`); kunden mejlas aldrig.
7. **Godkännande**: kunden ser ändringarna och skälen i fliken Granskning
   och godkänner. En ändring efter granskningen gör kampanjen till ett
   utkast igen (ny runda).
8. **Publicering**: för hand. Byrån laddar ner Editor-filen, importerar den
   i Google Ads Editor, och markerar kampanjen som live (kräver kundens
   godkännande och "betalning klar"). Då öppnas `/lp/<slug>/`. Pausa och
   återuppta stänger och öppnar sidan.
9. **Förfrågan** (`public_views.py`, `leads.py`): formuläret skapar en
   `Lead` med klick-id (gclid, gbraid, wbraid) och utm ur adressen. Skydd:
   CSRF, honungsfält, högst 10 i timmen per besökare och kampanj och högst
   30 i timmen per kampanj (`limits.py`, räknat ur Lead-raderna; besökarens
   IP sparas bara som HMAC i `Lead.ip_hash`). IP:n är X-Real-IP från nginx
   (`apps/common/net.py`), aldrig den första posten i X-Forwarded-For. Ett
   betyg på sidan måste vara bekräftat och komma från Google (eller ADX).
   Sidorna räknas inte i adx.se:s besöksstatistik och får inga ADX-kakor.
   Sidan visar kundens namn utan bolagsform, samma som annonserna.
10. **Sms** (`sms.py`): till kunden om en ny förfrågan och autosvar till den
    som frågade, bara om kunden slagit på dem och 46elks är konfigurerat
    (autosvaret inte 21-07). Besökarens text går aldrig rakt in: {namn}
    bara om det ser ut som ett förnamn, namnet till ägaren utan adresser.
    Högst ett autosvar per nummer och dygn och högst 50 sms per konto och
    dag. Gränserna prövas med kontot låst och raden sparas som "sending"
    innan 46elks anropas, så två förfrågningar samtidigt kan inte båda få
    ett autosvar. Ett stoppat sms loggas som "disabled" med orsaken. Varje sms, eller varför det inte skickades,
    blir en `SmsLog`-rad som syns på förfrågan. Inga mejl.
11. **Inkorg**: filter per status, status (kontaktad, offert skickad,
    vunnen, förlorad, skräp) och belopp i hela kronor. Kunden kan lägga in
    en förfrågan själv (ett samtal). Vunnen med belopp och klick-id blir en
    `ConversionUpload` i kö; byrån exporterar CSV per kund för Googles
    import av offline-konverteringar.
12. **Översikten** (`app_views/overview.py`, `rules.py`): förfrågningar,
    affärer och affärsvärde för 30 dagar, bandet från förfrågan till affär,
    och högst tre saker ur regler: väntande förfrågningar, kampanjer att
    godkänna, förfrågningar utan status, kom-igång-steg (även "lägg in
    betalning hos Google"). Annonspengar och kr per förfrågan/affär står
    som "kopplas när Google-rapporterna är på" tills de finns.

## Integrationer och nycklar

Alla läses med `env()` i `config/settings/base.py`, tomma från början, och
finns i `.env.example`.

| Integration | Inställning | Läge nu | Utan nyckel |
|---|---|---|---|
| AI-texter och läsningen av hemsidan | samma som assistenten (`ASSISTANT_PROVIDER`, Bedrock) | Byggt, går i produktion | Mallar och regler |
| Google Places | `GOOGLE_PLACES_API_KEY` | Byggt, slås på av nyckeln | Uppgifter från hemsidan och kunden |
| 46elks sms | `ELKS_API_USERNAME`, `ELKS_API_PASSWORD`, `ELKS_SENDER` (alla tre) | Byggt, slås på av nycklarna | Inget sms, loggat som "inte inkopplat" |
| Google Ads API | `GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | **Inte byggt.** Token ändrar bara en text på publiceringen | Editor-CSV, koppling och "betalning klar" bockas av i panelen, konverteringar som CSV |
| Landningssidornas domän i Editor-filen | `FLAMINGO_LANDING_BASE_URL` | Byggt | `https://adx.se` |
| Konverteringens namn i Google | `FLAMINGO_CONVERSION_NAME` | Byggt | "ADX Flamingo affär" |

## Inte byggt än

- Google Ads API: publicering, rapporter (annonspengar, visningar, klick,
  kr per förfrågan och affär), uppladdning av konverteringar och
  Google-inloggning (OAuth) för kopplingen.
- Landningssidor på kundens egen subdomän, spårade telefonnummer och samtal
  som förfrågningar, bilduppladdning i formuläret.
- Sms-svaret "VANN 186000" från ägaren.

## Lokalt

    uv run python manage.py flamingo_demo     # demokund, bara med DEBUG
    uv run python manage.py seed_flamingo     # Flamingos sidor

## Hårda regler

Inga automatiska mejl till kunder. Inga löften om tider. AI får bara
använda bekräftade fakta. Ingen extern ändring (Google, publicerad sida)
utan att kunden godkänt och byrån granskat. 375 px utan sidoscroll.
