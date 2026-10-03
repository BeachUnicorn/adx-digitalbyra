# ADX Flamingo

Annonser i Google sök, en sida per tjänst och förfrågningar i en inkorg,
mätt hela vägen till affär. Kunden väljer vid inskicket om en person på ADX
ska granska kampanjen innan den publiceras (beslut 2026-10-03). Godkänd
kundresa: `adx-marketing/kundresa-mvp.html` (steg 02-12).

Tjänsten är stängd: bara kunder som byrån aktiverat på kundkortet ser den.
Kundens konto skapas av byrån (kontakt i kundportalen), inte av kunden själv.

## Beslut 2026-10-03 (Giovanni)

De här gäller före äldre beskrivningar:

1. **Betalningen stoppar aldrig publiceringen.** En kampanj kostar ADX
   ingenting innan annonserna visas, och ADX fakturerar i efterhand. En
   kampanj behöver kontot kopplat under ADX, inte betalningen. Utan
   betalning visar Google bara inte annonserna; det står som en påminnelse.
2. **Granskningen är kundens val.** Vid inskicket kan kunden bocka i "Jag
   vill att ADX granskar kampanjen innan den publiceras" (av från början).
   Utan rutan publiceras kampanjen direkt när kontrollerna är gröna. Ingen
   tvingad granskning av den första kampanjen.
3. **Konverteringarna går till Google utan fråga på landningssidan.**
   Samtycket hittas aldrig på: `consent` skickas bara när `Lead.ad_consent`
   faktiskt är "granted" eller "denied" (det är tomt i dag).
4. **Ett klick på numret eller ringknappen på /lp/ är en förfrågan och en
   konvertering.** Inga spårade eller vidarekopplade nummer.
5. **Demokunden finns i produktion** (`flamingo_demo --prod`), syns aldrig
   publikt, anropar aldrig Google och skickar aldrig något.
6. **ADX larmas med mejl vid inskick och godkännande**, med vad som hände.

## Delar

| Del | Adress | Design | Var |
|---|---|---|---|
| Sidorna (marknadsföring) | `/flamingo/`, `/flamingo/<slug>/` | Flamingo | Blocksidor med `design="flamingo"`, redigeras i /manage/, seed: `seed_flamingo` |
| Verktyget | `/flamingo/app/...` | Flamingo, appläge | `app_views/`, `templates/flamingo/app/` |
| Kundens landningssidor | `/lp/<slug>/`, `/lp/<slug>/tack/`, `/lp/<slug>/ring/` (POST) | Ren (sidbyggaren), kundens namn | `public_views.py`, `pagebuilder/`, `templates/flamingo/lp/ren/`, `static/css/flamingo-lp-ren.css`, `static/js/flamingo-lp.js` |
| Byråns sida | `/manage/flamingo/...` | Panelens | `manage_views.py`, `manage_review.py`, `manage_google.py`, `templates/manage/flamingo/` |
| Google Ads API och Data Manager API | (ingen adress) | | `google_ads.py` (den enda HTTP-klienten), `google_publish.py`, `google_accounts.py`, `google_conversions.py`, `google_reports.py`, `flamingo_google_sync` |

Verktygets sidor:

| Adress | Vad | Kundresan |
|---|---|---|
| `app/` | Översikten: siffror, bandet, "tre saker", senaste förfrågningar och kampanjer | 12 |
| `app/forslag/` | Förslaget: hemsidan läses av, kunden väljer tjänster, en exempelannons (mallarna, bara bekräftade uppgifter) och föreslagen start (formulärets förval) | 02 |
| `app/foretaget/` | Företaget: uppgifter med källa (bekräfta, rätta, stryk) och tjänsterna | 04 |
| `app/google/` | Google: kontots id eller "skapa ett åt mig", läget i klartext och en tidslinje | 05 |
| `app/kampanjer/`, `ny/`, `<pk>/` | Kampanjerna, ny kampanj, förslaget i flikarna Annonser, Sökord, Sidan och Granskning, och inskicket med valet om granskning. Fliken Sidan visar kampanjens sida i sidbyggaren och låter kunden välja egen eller delad sida | 06-08 |
| `app/sidor/`, `ny/`, `<pk>/` | Sidorna i sidbyggaren: listan, en ny sida ur mallarna för en tjänst (högst 50 sidor per konto), och redigeraren (sidan i en ram, blocken, versionerna, problemlistan, "Publicera ändringarna") | Sidbyggaren 01-09 |
| `app/sidor/<pk>/spara/`, `rita/`, `nytt-block/`, `installningar/`, `publicera/`, `kopiera/`, `ta-bort/` (POST) | Redigerarens anrop (JSON): spara med rev (409 när någon annan sparat), rita block, ett nytt block ur mallen med sidans tjänst och pris, namn och palett, publicera med rev, kopiera och ta bort (`app_views/pages.py`) | |
| `app/sidor/<pk>/ai/bygg/`, `ai/skriv-om/`, `konverteringskoll/` | "Bygg sidan åt mig", "Skriv om" och Konverteringskollen (JSON, `app_views/page_ai.py`, `pagebuilder/ai.py`, `koll.py`). Sparar ingenting | Sidbyggaren 05-07 |
| `app/media/`, `lista/`, `ladda-upp/` | Mediaarkivet: logotypen, uppladdade bilder och bilderna från hemsidan, färgerna ur logotypen (`app_views/media.py`, `media.py`) | Sidbyggaren 08 |
| `app/omdomen/` | Omdömen från Google: profilen, "Det här är vi", "Profilen är vår" och valet av omdömen (`app_views/reviews.py`, `reviews.py`). Under dem profilen på Reco (`#reco`): länken eller id:t, "Det här är vi", "Profilen är vår", "Hämta profilen igen" och "Koppla bort profilen" (`reco.py`) | Sidbyggaren 10 |
| `app/inkorg/`, `<pk>/` | Inkorgen och en förfrågan: varifrån, status och belopp | 10-11 |
| `app/installningar/` | Sms till kunden och autosvaret (båda av från början) | 10 |
| `app/kund/` (POST) | Kundväljaren för en kontakt i flera Flamingo-kunder | |

Kom-igång-stegen (`rules.onboarding_for`) visas ovanför Förslaget,
Företaget, Google och översikten tills alla fyra är klara: Förslaget
(hemsidan läst eller tjänster finns), Företaget (minst en uppgift och
ingen obekräftad), Google (klart när kontot är kopplat under ADX;
betalningen stoppar inte) och Första kampanjen (en kampanj som lämnat
utkastläget). Google-steget står "hos ADX" medan byrån kopplar, men när ADX
skickat en kopplingsförfrågan (`google_link_requested_at`) är det kundens
tur: steget säger "Godkänn ADX:s förfrågan i Google Ads" och samma sak står
bland "tre saker" (`FlamingoAccount.google_waiting_on_customer`).

Byråns sidor:

| Adress | Vad |
|---|---|
| `/manage/flamingo/` | Kunderna med Flamingo (demokunden sist, med etikett, utanför siffrorna), köns siffror, sidorna, läget för Google Ads API |
| `/manage/flamingo/granska/` | Kön: att granska (bara de där kunden bad om granskning), godkända som inte är publicerade (med orsaken), hos kunden, live, konverteringar. Demokunden bara med `?demo=1` ("Visa demokunden") |
| `/manage/flamingo/granska/<pk>/` | En kampanj: granska (rätta och skriv varför), "Kunden bad om granskning", publicera (API, eller för hand också med API:t), pausa, återuppta, "Tillbaka till granskning" för en godkänd kampanj som Google sagt nej till |
| `/manage/flamingo/kampanj/<pk>/editor.csv` | Kampanjen som Google Ads Editor-fil |
| `/manage/flamingo/konverteringar.csv` | Förfrågningar, klick på numret och vunna affärer i kö som Googles importfil (GET; filen tar sina rader från API:t), "Markera som exporterade" (POST, bara rader som varit i en fil). Aldrig demokundens rader |
| `/manage/flamingo/google/` | ADX:s inloggning hos Google: vad som saknas, adressen att registrera, koppla, testa, koppla från, konverteringarnas väg och om de får skickas ("Koppla om med Google för att skicka konverteringar" när behörigheten saknas, "Försök ladda upp igen"), alla kunders Google-läge |
| `/manage/flamingo/google/tillbaka/` | Googles omdirigering efter inloggningen (OAuth) |
| `/manage/kunder/<pk>/flamingo/google/api/` (POST) | Kundkortets knappar: kopplingsförfrågan, nytt konto, läget från Google |
| Kundkortet, `#flamingo` | Aktivera, "Visa Flamingo som kunden", Google-kopplingen (status, id, notering, knapparna med API:t), kampanjerna |

## Sidbyggaren (`pagebuilder/`)

Kundens landningssidor byggs av block (UX: `adx-marketing/sidbyggaren-mockup.html`).
Kontrakten (blockens JSON, registret, renderaren, hjälparna) står i
`pagebuilder/__init__.py` och modulernas docstrings. Beslut 2026-10-03:

- **En design, Ren** (Google-stil: bilden först, en överrubrik med tjänsten
  och orten över en rubrik om vad kunden får, luft efter innehållet, palettens
  ljusa ton på varannan sektion, märken direkt efter Toppen som en smal
  remsa), sex paletter: blå, grön, röd, orange, grafit (med mässing som
  accent) och färgerna från logotypen. Kontrasten prövas mot WCAG AA
  (`render.palette_vars`); en ljus logotypfärg står kvar på knapparna med
  mörk text. Typsnittet Figtree (OFL) är självhostat; inget
  hämtas från Googles typsnitt eller någon annan.
- **Block med varianter och versioner**, lite fri redigering, inga A/B-test.
  Pris, certifikat och garanti erbjuds bara med en bekräftad uppgift; mallarna
  använder bara bekräftade uppgifter. Ett pris används bara för sin egen
  tjänst (`pris-<tjänst>`, annars ett pris som inte hör till någon tjänst,
  som ett timpris): sidan om badrum får aldrig rörjourens pris
  (`generator.page_price`, `BuildContext.price`). Kontrollerna kräver samma
  uppgift också för ett block som kommit in på annat sätt (en kopia, AI), och
  i certifikat- och garantiblocken får kvalitetsord och behörigheter bara stå
  när de finns bland uppgifterna (`problems.py`).
- **Vem som skrev en version avgör servern.** Varje version som servern
  skapar eller sparar har en signatur (HMAC med en nyckel ur `SECRET_KEY`
  över id, källa, by, at och fälten, `blocks.sign_version`). En version som
  servern inte känner igen (i utkastet eller det publicerade) och som saknar
  en giltig signatur blir den inloggades, vad redigeraren än säger om
  källan. "Ångra" efter "Använd förslaget" behåller vem som skrev vad.
- **En sida per kampanj** (rekommenderas) **eller en delad sida**. Adressen är
  alltid kampanjens egen (`/lp/<page_slug>/`), så förfrågningarna räknas till
  rätt kampanj. `Campaign.landing_page` är RESTRICT: en sida som en kampanj
  visar kan inte tas bort. Högst 50 sidor per konto (`pages.MAX_PAGES`).
- **Ett nytt förslag rör bara sin egen orörda sida.** Sidan minns att
  förslaget byggde den, för vilken kampanj och vid vilket rev
  (`LandingPage.built_for`, `built_rev`). Ett nytt förslag bygger om den
  bara för den kampanjen och bara så länge ingen sparat något på den. En
  sida som kunden valt (en befintlig sida vid en ny kampanj eller på fliken
  Sidan), kopierat eller ändrat byggs aldrig om.
- **Ingen låsning.** En ändring på en sida som är live går live när
  kontrollerna (`pagebuilder.page_problems`) är gröna, och byrån larmas
  (`pagebuilder.alert_live_change`, `alerts.send_agency_alert`), en gång per
  publicering (sidans rev står i ämnesraden). Samma larm när paletten byts
  på en publicerad sida, när logotypen byts eller tas bort (sidhuvudet och
  paletten Från logotypen), och när en live- eller pausad kampanj byter
  sida; bytet kräver att den publicerade versionen klarar kontrollerna.
  Kunden mejlas aldrig. Publiceringen skickar redigerarens rev och nekas
  (409) om utkastet sparats från ett annat ställe sedan dess.
- **Problemen på den publicerade sidan** syns där kunden rättar dem: när
  utkastet redan är rättat står de i redigerarens problemlista och på
  fliken Sidan, märkta "på den publicerade sidan" med "Publicera det"
  (`pagebuilder.published_problems`). Inskicket prövar den publicerade
  versionen, med samma märkning.
- **När en kampanj går live** publiceras dess sida om den aldrig publicerats
  (`publish_for_campaign`, i `google_publish.go_live` och byråns publicering
  för hand); säger kontrollerna nej går kampanjen inte live.
- **Mediaarkivet** (`media.py`): högst 200 bilder per konto, längsta sida
  2400 px, WebP utan metadata, filerna under `MEDIA_ROOT/flamingo/<slump>/`.
  Bara JPEG, PNG, WebP och GIF. Minnet på servern (2 GB, delas av flera
  sajter) skyddas: storleken prövas i filens huvud innan något avkodas
  (högst 24 miljoner bildpunkter för en uppladdning, 8 för en bild från
  hemsidan, en WebP en tredjedel av det eftersom libwebp alltid avkodar
  allt; en JPEG räknas i den skala den avkodas i), bilden skalas ner direkt,
  en bild åt gången avkodas per process, och högst 60 uppladdningar per
  konto och timme. Bilderna från hemsidan hämtas i trådar men avkodas en i
  taget. Mätt med en förlustfri WebP på 2,4 kB (60 MP): 1 373 MB före, nekad
  efter 9 MB.
- **Google-omdömen** (`reviews.py`, `FlamingoAccount.google_*`): de valda
  omdömena visas oförändrade med Googles märkning, författarens namn och
  länken till profilen. Utan omdömen syns blocket inte. Profilen måste
  vara kundens: den prövas mot hemsidans domän, namnet och telefonnumret
  (`places.matches`). Liknar den inte kunden sparas betyget obekräftat, inget
  från profilen syns på sidorna eller i förslagen, och byrån larmas, tills
  kunden eller byrån intygat den ("Profilen är vår"). Länkarna till Google
  prövas när de sparas och igen när sidan ritas (bara https till Google,
  inga användaruppgifter). Cron (`flamingo_google_sync`) hämtar profilen
  igen efter 7 dagar; har en hämtning inte gått på 90 dagar tas omdömena,
  betyget och namnet bort (Giovannis beslut 2026-10-03, se villkoren i
  `reviews.py`). Kundens val står kvar också när ett omdöme saknas i en
  hämtning.
- **Omdömen från Reco** (`reco.py`, `FlamingoAccount.reco_*`, Giovannis
  beslut 2026-10-04): se avsnittet Omdömen från Reco nedan.
- **Redigeraren** (`static/js/flamingo-pb.js`) ritar sidan i en ram med
  srcdoc; dokumentet har `Content-Security-Policy: script-src 'none'`, så
  inget skript i sidan körs där.
- **De gamla kampanjsidorna** flyttades in automatiskt i Ren (migreringen
  0011, fryst mappning). `Campaign.page` står kvar som historik och läses inte.
  Granskningen rättar inte längre sidan: den länkar till sidbyggaren
  ("Visa Flamingo som kunden" öppnar sidan direkt).

## Omdömen från Reco

Kunden klistrar in länken till sin sida på Reco (`reco.se/cs-auto-ab`, med
eller utan www, med en sökväg eller frågor efter, en delningslänk, adressen
till Recos widget eller hela inbäddningskoden), eller Recos id (siffror).
`reco.parse_link` läser det utan anrop, och sidan frågar "Är det här ni?".
"Det här är vi" hämtar profilsidan (`https://www.reco.se/<slug>`; med bara
ett id först widgeten, som har adressen) med SSRF-skyddet i
`apps/tools/analyzer.fetch`, bara till `www.reco.se` och `widget.reco.se`
(också efter en omdirigering), högst 512 kB och 10 sekunder, och högst fem
hämtningar per konto och dag. Ett demokonto anropar aldrig Reco.

- **Id:t** står på profilsidan i `window.VenueData` (med namnet, betyget,
  antalet, hemsidan och telefonnumret), i `window.PaginationData` och i
  `data-venue-id`; JSON-LD har namnet, hemsidan, numret och betyget men
  inte id:t. `reco.parse_profile` läser dem i den ordningen och ger inget id
  när källorna säger olika. CS Auto AB har 5998572 (testdatan i
  `testdata/` är byggd ur deras riktiga sidor, omdömena utbytta).
- **Ägaren.** Profilen prövas mot kunden: samma domän som hemsidan (inte en
  delad värd som facebook.com) eller samma telefonnummer som en bekräftad
  uppgift. Namnet räcker inte. Liknar den inte kunden sparas den med
  `reco_unverified`, inget från Reco syns på sidorna, och byrån larmas, tills
  kunden eller byrån intygat den ("Profilen är vår"; byrån larmas med vem).
  En profil som ett annat riktigt konto redan har tas aldrig emot (och
  databasen har regeln `flamingo_reco_id_unique`); byrån larmas. En
  konkurrents profil kan alltså aldrig bli kundens av sig själv.
- **Blocket "Omdömen från Reco"** (`reviews_reco`, kräver en intygad profil)
  visar Recos egen ruta, en iframe från `widget.reco.se` som `reco.frames`
  bygger bara av siffrorna i id:t och Giovannis storlekar: Liggande stor
  (horizontal/xlarge, 225 px, alla skärmar, förvalet), Liggande medel
  (horizontal/large, 60 px, från 720 px; i mobilen vertical/medium, 300 x 150
  px), Liggande liten (horizontal/small, 27 px, utan rubrik, en smal remsa
  direkt efter Toppen) och Stående (vertical/medium, 300 x 150 px). Titeln är
  "Omdömen på Reco", `loading="lazy"` (CSS döljer den ruta som inte gäller
  skärmen, och en dold ruta laddas aldrig), `referrerpolicy` som bara skickar
  sidans domän (aldrig adressen med klick-id:t) och `sandbox` utan
  `allow-top-navigation` (rutan kan inte byta sidan besökaren står på).
  Utan en intygad profil syns blocket inte, och det stoppar inte
  publiceringen. Konverteringskollen räknar blocket som omdömen, och
  biblioteket i redigeraren länkar till Omdömen när profilen saknas.
- **Integriteten.** Rutan laddas från reco.se när besökaren ser den: Reco
  får besökarens IP-adress och räknar visningen (`widget/loaded`); inga
  kakor sattes 2026-10-04. Blockets "varför" och omdömessidan säger det.
- **Betyget** från Reco sparas bara för verktyget (kunden ser att profilen
  är rätt), blir aldrig en uppgift och används aldrig i annonserna eller
  förslagen (`RATING_SOURCES` är Google och ADX; "Reco" med en siffra är ett
  betyg för `is_rating_like`). Namnet, betyget och antalet tas bort efter 90
  dagar utan en ny hämtning (`reco.expire` i `flamingo_google_sync`); id:t och
  rutan står kvar. Cron hämtar ingenting från Reco.
- **Ingen egen ruta med utvalda omdömen.** Recos villkor (Medlemsvillkor för
  webbsöktjänsten reco.se, https://www.reco.se/info/terms, uppdaterade
  2026-09-22, avsnitt 7) säger att Reco äger rättigheterna till omdömena
  och att materialet inte får kopieras eller göras tillgängligt för andra i
  kommersiella sammanhang utan Recos skriftliga medgivande. Widgetarna är
  Recos sätt att visa omdömena på en annan sajt, och ett API finns bara som
  en skräddarsydd lösning. Därför hämtas och sparas inga omdömestexter.
  Med Recos skriftliga medgivande (eller deras API) kan en egen ruta byggas:
  profilsidans JSON-LD har de fem senaste omdömena med text, författarens
  förnamn och initial, datum, betyg och länk (`https://www.reco.se/r/<id>`),
  sidan har 50 till som HTML (`article.review-card-v2`, märkta "Omdöme från
  inbjuden kund" när företaget bjudit in), och widgeten har texterna
  avkortade. Märkningen "inbjuden" och att företaget valt ut omdömena måste
  då synas.
- **Vakten** i `apps/website/tests.py` (VvsLegacyGuardTests) fäller ordet
  reco i koden, eftersom det var ett arv från systersajten. Reco-mönstret
  lyfts bara i filerna i `RECO_ALLOWED_IN`; en ny fil som nämner Reco läggs
  till där med namn.

## Behörighet

All logik i `access.py`, en grind i `middleware.py`. 404 för alla utan
behörighet (aldrig omdirigering eller 403). Verktyget visar alltid
`request.flamingo.customer` och inget annat: varje vy går via `app_view`,
som slår upp kundens `FlamingoAccount`, och varje id ur adressen eller ett
formulär hämtas med `account=account`.

- Kontakt: sin Flamingo-kund (väljare om flera).
- Byrån i kundvyn: exakt som kunden, med samma formulär och knappar. Det
  byrån sparar gäller på riktigt och sparas i byråns namn (remsan överst
  säger det). Bara utkastförhandsvisningen i /manage/ är skrivskyddad.
  Kunden utan Flamingo-konto ger `app/no_account.html`.
- Byrån utan kundvy: verktyget visar Flamingo-kunderna med "Visa som
  kunden" (`app/staff_index.html`). Knappen skickar `next`, så kundvyn
  öppnas i verktyget; granskningens knapp öppnar kampanjen.
- Kundens landningssidor (`/lp/`) är publika när kampanjen är live; byrån
  kan förhandsvisa alla med en remsa överst. Ett demokonto har aldrig en
  publik sida.
- Byråns Google-sidor kräver byrån (`staff_required`). Nycklarna visas
  aldrig, bara namnen på inställningar som saknas.
- Ett Google Ads-konto hör till en kund: ett id som ett annat (riktigt)
  Flamingo-konto har tas aldrig emot, varken av kunden eller byrån, och
  databasen har en regel för det (`flamingo_google_id_unique`, demot
  undantaget). Kunden skriver själv sitt id, så id:t bevisar ingenting:
  kontot blir kopplat av sig självt bara efter ADX:s förfrågan från just det
  kontot (se Inloggningen och kundens konto).

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
   betyg och öppettider från Google till, också obekräftade (aldrig för ett
   demokonto).
2. **Företaget**: `Fact`-rader med källa (hemsidan, Google, kunden, ADX).
   Kunden bekräftar, rättar eller stryker. En vald tjänst får en tom
   prisrad; ett tomt pris skrivs aldrig som en gissning.
3. **Google**: kunden anger kontots id (aldrig ett som en annan kund har)
   eller ber om ett nytt. Med Google Ads API skickar byrån en
   kopplingsförfrågan från förvaltarkontot eller skapar ett konto åt kunden,
   och läget läses från Google (se Google Ads API nedan). Utan API:t bockar byrån av "kopplat" och "betalning klar" på
   kundkortet. Kunden ser läget och byråns notering. Kopplat räcker för att
   kampanjerna ska kunna gå live; betalningen är en påminnelse.
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
   budget, område, och landningssidan med `pagebuilder.page_problems`)
   stoppar inskicket och samma kontroller körs på granskarens version och
   före publiceringen. Sidans innehåll blir block i kampanjens egen
   LandingPage (ringer direkt: Toppen med ringknapp, eller med bild när
   kontot har bilder, och ringremsa; offert: Toppen med formulär och
   formulär med frågor; boka tid: formuläret för att
   boka tid).
6. **Inskicket, med eller utan granskning** (`app_views/campaigns.campaign_submit`):
   bara från utkast och bara när kontrollerna är gröna. Kunden väljer med
   kryssrutan "Jag vill att ADX granskar kampanjen innan den publiceras"
   (av från början, `Campaign.review_requested`). Texten under rutan och
   knappen säger vad som händer, med `:has()` i CSS (utan stöd står en
   allmän text och knappen "Skicka"):
   - **Utan rutan** är inskicket kundens godkännande: `approved_at` och
     `approved_by` sätts till kunden, ingen granskningsrunda skapas, och
     `google_publish.publish_approved` publicerar direkt (steg 8). Texten
     säger "publiceras direkt, eftersom kontrollerna är gröna" när API:t
     är inkopplat och kontot kopplat, annars att ADX publicerar, eller att
     den publiceras när kontot är kopplat. Google granskar också varje
     annons innan den visas.
   - **Med rutan**: som förut. Inskicket skapar en `Review`-runda med en
     ögonblicksbild och status "hos ADX". Byrån rättar i
     `/manage/flamingo/granska/<pk>/` och skriver varför per del;
     ändringarna sparas som diff och kampanjen går till kunden.
   Byrån larmas med mejl vid inskick och godkännande
   (`INQUIRY_NOTIFICATION_EMAIL`), med vad som hände: live hos Google,
   Googles nej med orsaken, kontot inte kopplat, eller API:t inte
   inkopplat. Kunden mejlas aldrig. Ett demokonto larmar inte.
7. **Godkännande** (efter granskning): kunden ser ändringarna och skälen i
   fliken Granskning och godkänner. Godkännandet publicerar direkt på samma
   sätt (steg 8). En ändring efter inskicket eller godkännandet gör
   kampanjen till ett utkast igen, och kunden skickar den på nytt.
8. **Publicering** (`google_publish.py`):
   - Efter kundens godkännande (eller inskicket utan granskning), med API:t
     inkopplat, kontot kopplat under ADX med ett id och inte demo:
     `go_live` direkt. Lyckas det blir kampanjen live och `/lp/<slug>/`
     öppnas. Säger Google eller kontrollerna nej står kampanjen kvar som
     godkänd men inte publicerad, med orsaken i `Campaign.google_error`,
     och kunden ser "ADX publicerar kampanjen, och du ser här när den är
     live" (inga tider).
   - Annars (API:t inte inkopplat, kontot inte kopplat, eller ett fel):
     kampanjen hamnar i byråns kö under "Godkända, ej publicerade" med
     orsaken, och byrån publicerar därifrån: "Publicera hos Google" med
     API:t, eller för hand med Editor-filen och "Markera som live" (kräver
     kundens godkännande och ett kopplat konto, inte betalningen). Vägen för
     hand finns också med API:t ("Publicera för hand i stället", öppen när
     Google sagt nej). Sa Google nej till innehållet (en policy för ett
     sökord eller en annons) tar byrån kampanjen "Tillbaka till granskning":
     en ny runda hos ADX, kundens godkännande nollställs, byrån rättar och
     kunden godkänner igen. Kunden mejlas inte.
   - Spärrar efter kunden: ett nytt inskick inom 15 minuter efter ett
     misslyckat försök anropar inte Google (`RETRY_AFTER`), högst 10 försök
     per konto och dygn (`limits.reserve_publish`), och samma larm om samma
     kampanj går till byrån högst en gång i timmen (`alerts.py`). Byrån
     publicerar från kön utan gräns.
   - Pausa och återuppta stänger och öppnar sidan, hos Google först för en
     kampanj som publicerats med API:t.
9. **Förfrågan** (`public_views.py`, `leads.py`): formuläret skapar en
   `Lead` med klick-id (gclid, gbraid, wbraid i egna fält) och utm ur
   adressen. Ett klick på numret eller ringknappen blir också en `Lead`
   (källa "Klick på telefonnumret", se Mätningen). Skydd: CSRF, honungsfält,
   högst 10 i timmen per besökare och kampanj och högst 30 i timmen per
   kampanj (`limits.py`, räknat ur Lead-raderna; besökarens IP sparas bara
   som HMAC i `Lead.ip_hash`). IP:n är X-Real-IP från nginx
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
    ett autosvar. Ett stoppat sms loggas som "disabled" med orsaken. Varje
    sms, eller varför det inte skickades, blir en `SmsLog`-rad som syns på
    förfrågan. Inga mejl. Ett klick på numret ger inget sms (ägaren får
    samtalet), och ett demokonto skickar aldrig sms.
11. **Inkorg**: filter per status, status (kontaktad, offert skickad,
    vunnen, förlorad, skräp) och belopp i hela kronor. Kunden kan lägga in
    en förfrågan själv (ett samtal). En förfrågan och ett klick på numret
    med gclid köas som konverteringar när de kommer in, och vunnen med
    belopp som en affär (`ConversionUpload`, sorterna lead, call och deal).
    Skräp tar bort en köad förfrågan eller ett köat klick. Med API:t skickar
    `flamingo_google_sync` dem med Data Manager API (se Konverteringarna);
    det som inte går fram, och allt utan API:t, exporterar byrån som CSV per
    kund.
12. **Översikten** (`app_views/overview.py`, `rules.py`): förfrågningar,
    affärer och affärsvärde för 30 dagar (kalenderdagar i svensk tid, i dag
    medräknad, samma dagar som Googles kostnad), bandet från förfrågan till affär,
    och högst tre saker ur regler: väntande förfrågningar, kampanjer att
    godkänna, förfrågningar utan status, kom-igång-steg (även "godkänn
    ADX:s förfrågan i Google Ads" och "lägg in betalning hos Google").
    Annonspengar, visningar, klick och kr per förfrågan/affär kommer från
    Googles rapporter (`CampaignDayStats`); innan kontot fått en rapport
    står "kopplas när Google-rapporterna är på".

## Google Ads API

### Grunden (`google_ads.py`)

`google_ads.py` är den enda modulen som pratar HTTP med Google Ads, Data
Manager API (konverteringarna) och Googles inloggning (OAuth). Den anropar
bara fasta adresser hos Google (`googleads.googleapis.com`,
`datamanager.googleapis.com` och `oauth2.googleapis.com`), alltid över https,
med en tidsgräns och ett tak för svarets storlek. Inloggningssidan
(`accounts.google.com`) anropas aldrig härifrån: byråns webbläsare skickas
dit.

Alla anrop görs som ADX: genom förvaltarkontot (headern
`login-customer-id`, bara siffror) och med en kortlivad nyckel som hämtas
med ADX:s långlivade nyckel (refresh token). Åtkomsten (Test, Explorer,
Basic, Standard) hör till Google Cloud-projektet som äger OAuth-klienten.
Utvecklartoken är avvecklad hos Google sedan 2026-09-09 (headern ignoreras):
den krävs inte och skickas bara om `GOOGLE_ADS_DEVELOPER_TOKEN` är satt.
Google aviserar att den nekas i en senare version, så låt den vara tom. Den långlivade nyckeln
kommer i första hand från `GOOGLE_ADS_REFRESH_TOKEN` i miljön; annars den
byrån fått genom att koppla Google i panelen, sparad krypterad (Fernet) i
`GoogleAdsConnection` (en rad, `get_solo()`). Nyckeln för krypteringen är
`FLAMINGO_TOKEN_KEY`, eller härleds ur `SECRET_KEY`; byts den kopplar byrån
Google igen. Den kortlivade nyckeln cachas under sin livstid minus fem
minuter (per process, ingen delad cache).

Nycklarna visas aldrig i admin, i loggar eller i ett felmeddelande. Varje
text från Google tvättas innan den sparas eller visas, och Sentry maskar
Googles nycklar (`apps/common/sentry.py`). Felen kommer som
`GoogleAdsError` med en svensk text för byrån (`message`), Googles koder
(`codes`, `errors` med operationens plats) och `request_id`. Fel i själva
kopplingen sparas som `GoogleAdsConnection.last_error`.

Demokonton anropar aldrig Google: `google_ads.ensure_not_demo` och varje
modul ovanpå prövar `is_demo` innan något anrop.

### Koppla ADX:s Google (en gång, i produktion)

1. **Google Cloud-projektet**: slå på Google Ads API och **Data Manager
   API** i projektet som ska äga OAuth-klienten (konverteringarna går genom
   Data Manager API; utan det går de som CSV). Ett nytt projekt har Test-åtkomst, som bara når
   testkonton (CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION mot riktiga
   konton). Ansök om **Explorer** på sidan Google Ads API Overview för
   projektet i Google Cloud Console, inte i förvaltarkontots API Center (där
   behandlas inga ansökningar längre). Explorer når riktiga konton men får
   inte skapa konton (CreateCustomerClient): för "Skapa ett konto åt
   kunden" krävs **Basic**, som kräver att projektets varumärke verifierats.
   Förvaltarkontots id (tio siffror): `GOOGLE_ADS_LOGIN_CUSTOMER_ID`.
2. **OAuth-klient** av typen Webbprogram i samma projekt:
   `GOOGLE_ADS_CLIENT_ID` och `GOOGLE_ADS_CLIENT_SECRET`. En API-nyckel
   (AIza...) duger inte: Google Ads kräver OAuth. Ingen utvecklartoken
   behövs.
3. **Godkänd omdirigering** på OAuth-klienten, exakt:
   `https://adx.se/manage/flamingo/google/tillbaka/`. Sidan
   `/manage/flamingo/google/` visar adressen som gäller för den sajt du är
   på (lokalt en annan).
4. **Samtyckesskärmen** (OAuth consent screen): användartypen Intern om
   Google-kontot hör till en Google Workspace-organisation. Annars Extern,
   och då måste appen ställas i produktion: i läget Testning går den
   långlivade nyckeln ut efter sju dagar, och kopplingen slutar fungera.
   Behörigheterna är `https://www.googleapis.com/auth/adwords`,
   `https://www.googleapis.com/auth/datamanager`, `openid` och `email`.
   Data Manager-behörigheten är känslig: med Extern ska appen verifieras av
   Google (OAuth app verification, kan ta veckor) innan den används i
   produktion; Intern behöver ingen verifiering.
5. Lägg in värdena i `../.env` på servern (aldrig i koden, aldrig i ett
   mejl), starta om tjänsten, öppna `/manage/flamingo/google/` och klicka
   "Koppla med Google" med det Google-konto som har åtkomst till
   förvaltarkontot, och låt rutorna för Google Ads och Data Manager vara
   ikryssade. "Testa kopplingen" visar hur många konton inloggningen
   når och om förvaltarkontot är ett av dem. En koppling som gjordes innan
   Data Manager API kom med kopplas om en gång ("Koppla om med Google" på
   sidan). `FLAMINGO_TOKEN_KEY` är
   valfri; sätts den, gör det innan kopplingen.

### Inloggningen och kundens konto (`manage_google.py`, `google_accounts.py`)

- "Koppla med Google": state slumpas och sparas i sessionen, sedan går
  webbläsaren till Googles inloggning. När Google skickar tillbaka prövas
  state en gång, i konstant tid, högst 15 minuter gammal och för samma
  person. Koden byts mot en långlivad nyckel som sparas krypterad och aldrig
  visas. Behörigheterna Google gav (svarets `scope`) sparas i
  `GoogleAdsConnection.granted_scopes`, med ett avtryck av nyckeln de gäller
  (`scopes_for`, HMAC, aldrig nyckeln), och förnyas varje gång den
  kortlivade nyckeln hämtas, också för nyckeln i miljön. Saknas Google Ads
  stoppas kopplingen; saknas bara Data Manager sparas den, och sidan säger
  "Koppla om med Google för att skicka konverteringar".
  "Koppla från" återkallar nyckeln hos Google och glömmer den här.
  Med `GOOGLE_ADS_REFRESH_TOKEN` i miljön används den nyckeln och knappen
  behövs inte.
- `request_link`: kopplingsförfrågan från förvaltarkontot, sparad med id:t
  den gällde (`google_link_requested_for`). Kontot står kvar som "Konto-id
  angivet" tills kunden godkänt den i Google Ads under Administratör >
  Åtkomst och säkerhet > Förvaltare; under tiden är det kundens tur i
  kom-igång-stegen. Knappen säger att Google kan mejla kontots
  administratörer om förfrågan. Ligger kontot redan under förvaltarkontot
  (ALREADY_MANAGED) sparas ingenting: byrån kontrollerar att kontot är
  kundens och bockar av det för hand.
- `create_client_account`: nytt konto under förvaltarkontot,
  "<kund> (ADX Flamingo)", SEK och svensk tid (kräver Basic-åtkomst).
  `emailAddress` och `accessRole` är bara för Googles tillåtelselista, så
  rutan för inbjudan finns bara med `GOOGLE_ADS_INVITE_ON_CREATE`; utan den
  bjuder byrån in kunden i Google Ads efteråt (Google mejlar kunden då).
  Kontoraden är låst under anropet, så ett dubbelklick ger ett konto.
- `sync_account_status`: läget från Google. ACTIVE blir kopplat bara när
  ADX skickade förfrågan från det här kontot till det här id:t (eller
  skapade kontot); annars står kontot kvar och byrån ser att det ska
  kontrolleras och bockas av för hand. PENDING är kundens tur (också en
  förfrågan som skickats från Google Ads), och ett besvarat läge (godkänt,
  nekat, tillbakadraget, avslutat) tömmer förfrågan; nekad eller avslutad
  ger en notering. Betalningen APPROVED blir "betalning klar"; automatisk
  taggning och en varning om kontot inte är i SEK. Allt sparas bara om
  kontots id är detsamma som det som lästes. Ett fel sparas i
  `google_sync_error`; ett fel i ADX:s koppling eller slut kvot kastas
  också. Utan API:t ändras ingenting. Byts kontots id töms det som gällde
  det förra kontot (`forget_previous_account`), även
  konverteringsåtgärderna och kampanjernas gamla fel från Google.

ADX mejlar aldrig kunden härifrån. Google meddelar kontots administratörer
om en kopplingsförfrågan, och mejlar en inbjudan bara när rutan är ibockad
(och den finns bara med tillåtelselistan).

### Publiceringen (`google_publish.py`)

`go_live` gör så här:

1. Kontrollerna (`checks.validate`) ska vara tomma, kontot kopplat under
   ADX med sitt eget id (inget annat Flamingo-konto har det), och området
   ska ge minst en ort. Betalningen krävs inte.
2. Spärren `google_publish_started_at` tas med en egen UPDATE innan Google
   anropas, tillsammans med vad försöket skickar (`google_publish_sent`:
   namnet och en hash av hela anropet), och kampanjraden låses (`FOR NO KEY
   UPDATE NOWAIT`) under anropet. Två klick, eller kundens godkännande och
   byråns klick samtidigt, ger aldrig två kampanjer: den andra får "pågår
   redan". Fick ett försök inget svar säger felet att kampanjen kan vara
   igång hos Google medan sidan är stängd, och nästa försök letar upp den
   på namnet (det gamla och det nya). Den tas över bara om innehållet är
   detsamma; har kunden ändrat något sedan dess pausas den gamla hos
   Google och byrån tar bort den där innan den publicerar igen. Finns inget
   hos Google börjar försöket om med en ny spärr.
3. Kundens konto måste ha valutan SEK; automatisk taggning slås på, så att
   klickets gclid når landningssidan.
4. Allt skapas i ett anrop (googleAds:mutate), allt eller inget: budget per
   dag, kampanjen "Flamingo: <namn> #<pk>" (Sök, bara Google sök, Maximera
   klick, platsinriktning på närvaro, svenska), en radie per ort, de
   negativa sökorden, en annonsgrupp med sökorden och en responsiv
   sökannons till landningssidan.
5. Kampanjen blir live och landningssidan öppnas först när Google svarat.

Ett fel ändrar inte kampanjen: felet sparas i `Campaign.google_error` och
visas i panelen och i kön med vilken ändring det gällde.
`publish_approved` är samma väg efter kundens godkännande, kastar aldrig,
och ger ett `Outcome` (live, failed, not_linked, manual, demo) som larmet
till byrån och kundens besked bygger på. Pausa och återuppta går till
Google först för kampanjer som publicerats med API:t; en kampanj som
publicerats för hand pausas för hand.

### Mätningen: klick på numret och konverteringarna

**Klicket** (`public_views.call_click`, `static/js/flamingo-lp.js`): varje
tel:-länk på landningssidan har `data-fl-call`. Skriptet skickar ett
sendBeacon till `POST /lp/<slug>/ring/` med CSRF, klick-id, utm och sökord,
och håller aldrig upp samtalet. Bara live-sidor räknas (inte
förhandsvisningen eller demot), svaret är 204. Det blir en Lead med källan
`call_click`, utan namn och nummer. Spärrar (`limits.create_call_click_lead`):
en gång per besökare, annonsklick och kampanj och timme (två personer bakom
operatörens gemensamma adress med var sitt gclid räknas båda), inte alls om
besökaren skickat formuläret från samma annonsklick den timmen, högst 5 i
timmen per besökare och 30 i timmen per kampanj. En IPv6-adress räknas som
sitt /64-nät (`limits.ip_bucket`), också för formulärets spärrar. Nås
kampanjens gräns får byrån ett larm, högst ett i timmen per kampanj. Byråns
klick räknas inte (remsan säger det), och tel:-länkarna på tacksidan räknas
inte. Utan skript räknas inget, och länken fungerar ändå. Förfrågningarna
räknas med ett lås per kampanj i Postgres (`pg_advisory_xact_lock`), inte
kampanjens rad, så ett klick väntar aldrig på en paus hos Google.

**Samtycket**: sidan frågar inte (beslut 2026-10-03), har ingen remsa,
inget dolt fält och ingen lagring i webbläsaren. Ett `ad_consent` i det som
postas läses aldrig. `Lead.ad_consent` finns kvar, tomt, för en fråga
senare.

**Konverteringarna** (`google_conversions.py`):

- Allt med gclid köas: förfrågan (lead), klick på numret (call) och vunnen
  affär med belopp (deal). Högst en av varje sort per förfrågan.
- **Vägen** väljs med `FLAMINGO_CONVERSIONS_UPLOAD`: `datamanager` (tomt,
  standard) skickar med Data Manager API; `googleads` med Google Ads API:s
  uploadClickConversions, som Google inte öppnar för nya användare sedan
  2026-06-15 (CUSTOMER_NOT_ALLOWLISTED_FOR_THIS_FEATURE); `off` bara CSV.
  Ett okänt värde räknas som `off`. Kön och Google-sidan säger vilken väg
  som gäller och, när den inte går, varför.
- **Data Manager API används bara när** Google Ads API är inkopplat,
  inloggningen har behörigheten `datamanager` (sparad vid kopplingen eller
  när nyckeln förnyas), kontot ligger under ADX förvaltarkonto, kontot inte
  är demo, och konverteringsåtgärden för radens sort finns i kundens konto.
  Annars går raden som CSV. `ensure_conversion_actions` skapar åtgärderna
  med Google Ads API.
- **Anropet** (developers.google.com/data-manager/api, läst 2026-10-03):
  `POST https://datamanager.googleapis.com/v1/events:ingest`, bara nyckeln
  som header (Google bortser från headers i ett ingest-anrop). En
  destination: `operatingAccount` kundens konto och `loginAccount`
  förvaltarkontot (båda `accountType` GOOGLE_ADS, tio siffror) och
  `productDestinationId` konverteringsåtgärdens id. En händelse:
  `transactionId` (`adx-flamingo-<förfrågan>-<sort>`, samma vid varje
  försök och samma som `orderId` på den gamla vägen), `eventTimestamp` (RFC
  3339 i svensk tid med offset, samma sekund som CSV-filen), `eventSource`
  WEB, `adIdentifiers.gclid`, `conversionValue` och `currency` SEK bara för
  affärer, och `consent.adUserData` (CONSENT_GRANTED eller CONSENT_DENIED)
  bara när `Lead.ad_consent` är "granted" eller "denied" (det är tomt i dag
  och hittas aldrig på). `validateOnly` false; `flamingo_google_sync
  --prova` skickar true och ändrar inget.
- **En konvertering per anrop.** Google tar högst 2 000 händelser och 10
  destinationer per anrop, men tar emot allt eller inget (fast-fail), och
  bearbetningens besked (`requestStatus:retrieve`) säger bara antal per
  orsak och destination, inte vilken händelse. Med en per anrop gäller
  varje besked exakt en rad. Högst 100 per konto och körning; Googles gräns
  är 300 anrop i minuten per Cloud-projekt.
- **Ingen konvertering tappas.** En rad står i kö, och i CSV-filen, tills
  Google tagit emot den (svaret har ett `requestId`). Ett nej för en
  händelse lämnar raden i kö med felet, som kön visar ("Senaste
  försöket"), och nästa rad skickas. Nästa försök väntar 1, 2, 4, 8, 16 och
  sedan 24 timmar; efter 8 försök (eller direkt, för ett fel som ett nytt
  försök inte ändrar) skickar API:t den inte själv längre, och den väntar
  på CSV-filen eller "Försök ladda upp igen". Ett fel för kundens konto
  (behörighet, villkor) eller ett tillfälligt fel hos Google stoppar
  kontots körning; inloggningen eller kvoten stoppar hela körningen. Ett
  nej till konverteringsåtgärden glömmer den, så skapas den igen.
- **Googles besked** läses efter minst 30 minuter (bearbetningen kan ta
  upp till ett dygn), oavsett vald väg: SUCCESS, eller bara dubbletter
  (DUPLICATE_TRANSACTION_ID, DUPLICATE_GCLID), är klart; FAILED lägger
  raden tillbaka i kön med orsaken (och den kan då exporteras); utan
  slutligt besked på tre dygn står raden kvar som skickad med en
  anteckning. Google-sidan visar hur många som väntar på besked.
- **Ingen konvertering räknas två gånger.** En rad går bara en väg: en
  nedladdad CSV-fil tar sina rader (`ConversionUpload.downloaded_at`), och
  API:t skickar dem aldrig; en rad som API:t skickat står inte i kö och
  kommer aldrig med i en fil. "Markera som exporterade" gäller bara rader
  som varit i en nedladdad fil. Raderna låses medan de skickas, och
  nedladdningen hoppar över en låst rad. Med API:t på visas därför inte
  länken "Alla kunder i en fil".
- **Nej till hela vägen**: Data Manager API avslaget i Cloud-projektet
  (SERVICE_DISABLED) eller NOT_ALLOWLISTED stoppar vägen för alla konton
  (`GoogleAdsConnection.conversion_upload_blocked_at`, med vägen i
  `conversion_upload_blocked_path`), raderna står orörda i kö för CSV-filen,
  och Google-sidan och kön säger varför en gång; kommandot räknar det inte
  som ett fel för kontot. "Försök ladda upp igen" häver stoppet.
  ACCESS_TOKEN_SCOPE_INSUFFICIENT markerar behörigheten som saknad, och
  sidan ber byrån koppla om. Den gamla vägen stoppas på samma sätt av
  CUSTOMER_NOT_ALLOWLISTED_FOR_THIS_FEATURE.
- **gbraid och wbraid** sparas men skickas inte. Data Manager API tar emot
  dem i `adIdentifiers`, men Flamingos åtgärder räknas en gång per klick
  (ONE_PER_CLICK, Googles råd för förfrågningar), och en sådan åtgärd tar
  inte emot braid-id:n: Data Manager API svarar
  PROCESSING_ERROR_REASON_ONE_PER_CLICK_CONVERSION_ACTION_NOT_PERMITTED_WITH_BRAID.
  Därför krävs gclid, och bara gclid skickas även när förfrågan har båda.
  Att stödja klick med bara ett braid-id kräver en andra uppsättning
  åtgärder som räknas flera gånger per klick. Inkorgen säger det till
  kunden.
- `ensure_conversion_actions` skapar "ADX Flamingo förfrågan", "ADX
  Flamingo samtal" och affären (`FLAMINGO_CONVERSION_NAME`) i kundens konto,
  som import av klick. En som redan finns med samma namn återanvänds.
  De skapas med Googles standard för primaryForGoal: förfrågan, samtal och
  affär kan alla räknas i kolumnen Konverteringar för samma klick. Gör
  affären sekundär, eller gå över till budgivning på värde, innan
  budgivningen styrs av konverteringar.
- Raderna väntar tills förfrågan är sex timmar gammal (Google tar inte
  emot för nya klick).
- CSV-exporten skriver alla tre sorterna med sina namn och kolumnen "Ad
  User Data" (tom när inget svar finns). Namnen måste då finnas i kundens
  konto som import av klick; med API:t skapar `flamingo_google_sync` dem.
- Demokundens rader skickas och exporteras aldrig.

**Rapporterna** (`google_reports.py`): `sync_stats` läser kostnad, visningar,
klick och konverteringar per kampanj och dag för de senaste 30 dagarna och
sparar dem som `CampaignDayStats`. Kontot måste ha valutan SEK.

### Kommandot `flamingo_google_sync` (cron)

För varje aktiverat konto (inte demo) hos en aktiv kund, med ett Google
Ads-id: `sync_account_status`, och för konton under ADX förvaltarkonto
konverteringsåtgärderna, kön den valda vägen (bara när uppladdningen är
på), Googles besked om det som skickats med Data Manager API (oavsett väg)
och rapporten.
Stegen körs var för sig: ett fel i konverteringarna (till exempel en
konvertering med samma namn som inte är en import av klick) stoppar inte
uppladdningen av de andra eller rapporten. Felen sparas tillsammans i
`google_sync_error` ("Konverteringarna: ...", "Rapporten: ...") och kontot
räknas som misslyckat, men de andra kontona körs. Ett fel i ADX:s egen
koppling eller slut kvot stoppar körningen, också från lägesläsningen.
Googles nej till hela vägen för konverteringarna skrivs en gång
("Konverteringarna stoppade: ...") och räknas inte som ett fel för kontot.
Utan API:t skriver kommandot en rad och gör inget. Inga mejl. Kommandot
publicerar inga kampanjer. `--prova` låter Google pröva raderna i kö
(`validateOnly`) och skriver vad Google sa, utan att något skickas.

Cron som djangouser, varje timme, samma mönster som de andra raderna i
djangousers crontab (lägg till de nycklar de raderna tar med, om de är
fler):

    17 * * * *  cd /home/djangouser/sites/adx/app && env $(grep -E "^(SECRET_KEY|DATABASE_URL|ALLOWED_HOSTS|SENTRY_DSN|GOOGLE_ADS_|FLAMINGO_)" ../.env | xargs) DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py flamingo_google_sync >> /home/djangouser/sites/adx/backups/flamingo-google.log 2>&1

Django läser också `../.env` själv (`config/settings/base.py`). Med
`--konto <id>` körs bara ett Flamingo-konto, med `--dagar 7` en kortare
rapport.

## Demokunden (`flamingo_demo`), också i produktion

    uv run python manage.py flamingo_demo           # lokalt, med DEBUG
    uv run python manage.py flamingo_demo --prod    # i produktion

Utan DEBUG vägrar kommandot om inte `--prod` anges. Det skapar eller
uppdaterar "Exempelrör AB (demo)", ett påhittat företag: hemsidan och
e-posten under den reserverade toppdomänen `.example`, telefonnumren ur PTS
serier för fiktiva nummer (08-465 004 00-99, 070-174 06 05-99) och
Google Ads-id 000-000-0000. Kontot har `FlamingoAccount.is_demo` på. Data på
varje sida: uppgifter från alla källor, tjänster, kampanjer i alla lägen
(en av dem skickad utan granskning),
granskningar med ändringar och skäl, förfrågningar i alla statusar och från
alla källor (klick på numret, en vunnen affär med klick-id), sms-rader och
Googles siffror per dag för 30 dagar. I sidbyggaren: fem sidor som
tillsammans har varje blocktyp och variant (de för kampanjer som är live
eller pausade publicerade), en logotyp och bilder i mediaarkivet, och en
påhittad Google-profil med omdömen (intygad som demots egen; den hämtas
aldrig från Google), och en påhittad profil på Reco (id:t 0000000, ingen
länk till reco.se): blocket Omdömen från Reco ritar en exempelruta i stället
för Recos och laddar ingenting från Reco. Kör det igen så byggs innehållet
om; inget dubbleras.
En annan kund med samma namn rörs aldrig.

I produktion finns ingen användare som kan logga in på demokunden. Byrån
öppnar den med "Visa Flamingo som kunden" på kundkortet. Lokalt finns
kontakten demo@exempelror.example, utan lösenord.

Ett demokonto skickar aldrig något: `/lp/` är 404 för alla utom byrån,
hemsidan läses aldrig av (`scan.demo_refusal`), Google Places frågas aldrig
(`places.update_from_google`, `reviews.refusal`), Reco anropas aldrig
(`reco.refusal`) och ingen ruta laddas från Reco, inga bilder hämtas från
någon hemsida (`media.DEMO_REFUSED`), inga sms (`sms.NOTE_DEMO`), inga anrop till
Google, inga larm till byrån, och affärerna exporteras eller laddas aldrig
upp. Demokundens kampanjer och konverteringar är inte med i byråns kö eller
siffror förrän byrån ber om det (`?demo=1`, "Visa demokunden" i kön);
kundkortet länkar ändå till dem. `test_demo.py` fäller bygget om en ny väg
i panelen eller en ny Google-modul glömmer `is_demo`.

## Integrationer och nycklar

Alla läses med `env()` i `config/settings/base.py`, tomma från början, och
finns i `.env.example`.

| Integration | Inställning | Läge nu | Utan nyckel |
|---|---|---|---|
| AI-texter och läsningen av hemsidan | samma som assistenten (`ASSISTANT_PROVIDER`, Bedrock) | Byggt, går i produktion | Mallar och regler |
| Google Places | `GOOGLE_PLACES_API_KEY` | Byggt, slås på av nyckeln | Uppgifter från hemsidan och kunden |
| Reco (omdömen) | Ingen nyckel: profilsidan och Recos widget är publika | Byggt: länken eller id:t, ägaren, Recos ruta i blocket | Recos egen ruta är det enda som visas; utvalda omdömen kräver Recos medgivande |
| 46elks sms | `ELKS_API_USERNAME`, `ELKS_API_PASSWORD`, `ELKS_SENDER` (alla tre) | Byggt, slås på av nycklarna | Inget sms, loggat som "inte inkopplat" |
| Google Ads API | `GOOGLE_ADS_LOGIN_CUSTOMER_ID`, `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET`, och inloggningen i panelen (eller `GOOGLE_ADS_REFRESH_TOKEN`); projektet behöver Explorer (Basic för nya konton) | Byggt: koppling, kundens konto, publicering direkt efter kundens godkännande, paus, konverteringsåtgärder och rapporter. Inte prövat mot ett riktigt konto | Editor-CSV och "Markera som live", koppling och "betalning klar" bockas av i panelen, konverteringar som CSV |
| Data Manager API (konverteringarna) | Samma inloggning; API:t påslaget i samma Cloud-projekt och behörigheten `datamanager` (koppla om en gång) | Byggt, standardvägen. Inte prövat mot ett riktigt konto | CSV-exporten |
| Utvecklartoken | `GOOGLE_ADS_DEVELOPER_TOKEN` | Valfri, avvecklad hos Google 2026-09-09 | Ingen header (det normala) |
| API-versionen | `GOOGLE_ADS_API_VERSION` | Byggt | v25 (den senaste 2026-10-03). Varje version har ett slutdatum hos Google (Deprecation and sunset): byt `DEFAULT_VERSION` och testernas `API` innan dess |
| Konverteringarnas väg | `FLAMINGO_CONVERSIONS_UPLOAD` | Byggt: `datamanager` (tomt), `googleads` (bara med Googles tillåtelse), `off` | CSV-exporten. `GOOGLE_ADS_UPLOAD_CONVERSIONS` läses inte längre |
| Inbjudan när ett konto skapas | `GOOGLE_ADS_INVITE_ON_CREATE` | Byggt, av | Byrån bjuder in kunden i Google Ads efteråt |
| Krypteringen av Google-nyckeln | `FLAMINGO_TOKEN_KEY` | Byggt, valfri | Härledd ur `SECRET_KEY` |
| Landningssidornas domän i Editor-filen och annonsen | `FLAMINGO_LANDING_BASE_URL` | Byggt | `https://adx.se` |
| Affärens konvertering i Google | `FLAMINGO_CONVERSION_NAME` | Byggt | "ADX Flamingo affär" (förfrågan och samtal har fasta namn) |

Inte prövat mot ett riktigt Google Ads-konto (inga nycklar i utvecklingen,
allt HTTP är attrapper i testerna): REST-fältens namn mot v25 (adresserna
stämmer med v25:s http-regler), radie per
ort på adress (`cityName` och SE), `languageConstants/1015`, budgetens namn
(samma som kampanjens; en gammal budget med samma namn ger
DUPLICATE_NAME), länken in i Google Ads (Google dokumenterar inga sådana),
att Google mejlar administratörerna om en kopplingsförfrågan, och att ADX
förvaltarkonto får skicka inbjudan med `createCustomerClient` (Google
anger en tillåtelselista). Gör den första publiceringen med byrån bredvid.

Data Manager API är byggt efter Googles referens och guider (events:ingest,
destinations, understand-errors, diagnostics, limits) men inte prövat:
felens exakta form (vilken ErrorInfo-orsak ett avslaget API eller en
saknad behörighet ger), att `eventSource` WEB passar en vunnen affär, och
att ett förvaltarkonto med Explorer-åtkomst får skicka. Kör
`flamingo_google_sync --prova` mot ett riktigt konto först.

## Inte byggt än

- Spårade eller vidarekopplade telefonnummer och riktiga samtal som
  förfrågningar (beslut: klicket på numret räcker tills vidare).
- Frågan om samtycke på landningssidan (`Lead.ad_consent` finns, tomt).
- Konverteringar för klick med bara gbraid eller wbraid (iOS): kräver
  åtgärder som räknas flera gånger per klick (se Konverteringarna).
- Tillbakadragning av en konvertering som redan skickats, när förfrågan
  sedan blir skräp (uploadConversionAdjustments).
- Publicering av sig själv när ett konto blir kopplat efter kundens
  godkännande: byrån publicerar då från kön.
- Landningssidor på kundens egen subdomän, bilduppladdning i formuläret.
- Sms-svaret "VANN 186000" från ägaren.
- En egen ruta med utvalda omdömen från Reco: kräver Recos skriftliga
  medgivande eller deras API (se Omdömen från Reco). "Bygg sidan åt mig"
  lägger aldrig till blocket Omdömen från Reco själv; ett som redan står på
  sidan följer med oförändrat.
- "Skriv om" i sidbyggaren: förslaget blir en version som redigeraren
  skapar, så den sparas som kundens (eller byråns), inte som "AI". Att
  behålla märkningen kräver att servern lämnar en signerad version.

## Driftsättning av den här ändringen

Sidan `/flamingo/` (och FAQ-sektionen `flamingo-fragor`) ligger i databasen
och byggs av `seed_flamingo`, som körs för hand, aldrig av `./deploy`.
Texterna ändrades 2026-10-03 (granskningen är kundens val, klick på numret
i stället för spårade nummer, ingen egen subdomän). Efter deploy, i
produktion:

    uv run python manage.py seed_flamingo

Det skriver över blocken och frågorna på Flamingos sidor, också ändringar
som gjorts i /manage/ sedan förra seeden; publiceringen rörs inte.

Sidbyggaren (migreringarna 0010-0014): `./deploy` kör migreringarna. 0010
skapar sidorna och mediaarkivet, 0011 gör varje kampanjs sida till en
LandingPage i Ren (publicerad för kampanjer som är live eller pausade; ett
nummer ur telefonuppgiften, frågornas nycklar omgjorda så att de klarar
schemat, etiketterna oförändrade), 0012 lägger till dagens räknare och
bildernas alt-text från hemsidan, och 0013 sidans ursprung (`built_for`,
`built_rev`) och Google-profilens intyg, och 0014 profilen på Reco
(`reco_*`, regeln `flamingo_reco_id_unique`). Kör sedan demot igen, så att
demokunden får sina sidor, bilder och sin påhittade profil på Reco:

    uv run python manage.py flamingo_demo --prod
Migreringen 0007 stoppar (med kontonas nummer) om två Flamingo-konton har
samma Google Ads-id: rätta det först.

Konverteringarna genom Data Manager API (migreringen 0008):

1. Slå på Data Manager API i samma Google Cloud-projekt som OAuth-klienten,
   och lägg till behörigheten för Data Manager API på samtyckesskärmen
   (känslig: Extern kräver Googles verifiering, Intern inte).
2. Ta bort `GOOGLE_ADS_UPLOAD_CONVERSIONS` ur `../.env` (läses inte
   längre). `FLAMINGO_CONVERSIONS_UPLOAD` behövs inte: tomt är
   `datamanager`.
3. Öppna `/manage/flamingo/google/` och "Koppla om med Google", med rutan
   för Data Manager ikryssad. Med `GOOGLE_ADS_REFRESH_TOKEN` i miljön:
   skapa en ny nyckel med båda behörigheterna.
4. Kör `flamingo_google_sync --prova` och se att Google godkänner raderna,
   sedan går cron som vanligt.

## Lokalt

    uv run python manage.py flamingo_demo           # demokunden, med DEBUG
    uv run python manage.py flamingo_demo --prod    # demokunden i produktion
    uv run python manage.py seed_flamingo           # Flamingos sidor
    uv run python manage.py flamingo_google_sync    # utan Google-nycklar: en rad, inget mer

Testerna når aldrig Google, AWS eller 46elks: Google byts mot `FakeGoogle`
(`test_google_ads.py`), AI stängs av med attrapper.

    TEST_DB_NAME=test_flamingo SENTRY_DSN= uv run python manage.py test apps.flamingo --noinput

## Hårda regler

Inga automatiska mejl till kunder (larm till byrån är fria; en knapp som
får Google att mejla någon säger det). Inga löften om tider. AI får bara
använda bekräftade fakta. Ingen extern ändring (Google, publicerad sida)
utan kundens godkännande: inskicket utan granskning eller godkännandet
efter ADX granskning. Demokontot pratar aldrig med Google och skickar
ingenting. Nycklar loggas, visas eller checkas aldrig in. 375 px utan
sidoscroll.
