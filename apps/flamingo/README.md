# ADX Flamingo

Annonser i Google sök, en sida per tjänst och förfrågningar i en inkorg,
mätt hela vägen till affär. En person på ADX granskar allt innan något
publiceras. Godkänd kundresa: `adx-marketing/kundresa-mvp.html`.

Tjänsten är stängd: bara kunder som byrån aktiverat på kundkortet ser den.

## Delar

| Del | Adress | Design | Var |
|---|---|---|---|
| Sidorna (marknadsföring) | `/flamingo/`, `/flamingo/<slug>/` | Flamingo | Blocksidor med `design="flamingo"`, redigeras i /manage/ |
| Verktyget | `/flamingo/app/...` | Flamingo, appläge | `apps/flamingo/app_views/`, `templates/flamingo/app/` |
| Kundens landningssidor | `/lp/<slug>/` (senare kundens egen subdomän) | Neutral, kundens namn | `apps/flamingo/public_views.py`, `templates/flamingo/lp/` |
| Byråns sida | `/manage/flamingo/...` | Panelens | `apps/flamingo/manage_views.py`, `templates/manage/flamingo/` |

## Behörighet

All logik i `access.py`, en grind i `middleware.py`. 404 för alla utan
behörighet (aldrig omdirigering eller 403). Verktyget visar alltid
`request.flamingo.customer` och inget annat: varje fråga filtreras på
kunden, aldrig på ett id ur adressen utan att kunden kontrolleras.

- Kontakt: sin Flamingo-kund (väljare om flera).
- Byrån i kundvyn: som kunden, **skrivskyddat** (POST nekas).
- Byrån utan kundvy: verktyget visar en lista över Flamingo-kunder med
  "Visa som kunden"; arbete görs i /manage/flamingo/.
- Kundens landningssidor (`/lp/`) är publika: de är till för kundens kunder.

## Flödet (kundresan, steg 2-12)

1. **Förslaget**: kunden anger sin hemsida. Sidan hämtas med SSRF-skyddet i
   `apps/tools/analyzer.fetch`; tjänster, telefon och fakta läses ut
   (regler + AI via `apps/assistant/llm`). Inget publiceras.
2. **Företaget**: `Fact`-rader med källa (hemsidan, Google, kunden, ADX).
   Kunden bekräftar, rättar eller stryker. AI får bara använda bekräftade
   fakta. Tomt pris skrivs aldrig som en gissning.
3. **Google**: kunden äger kontot. Länkas under ADX:s förvaltarkonto (MCC).
   Utan API-nycklar: kunden anger konto-id eller ber om ett nytt, byrån
   bockar av "kopplat" och "betalning klar" i /manage/.
4. **Kampanj**: tjänst, sätt att sälja (ringer / offert / boka tid), område,
   budget per dag.
5. **Förslag**: strukturen kommer från regler (`generator.py`): annonsgrupp,
   sökord med matchningstyp, standardlista med negativa sökord, 15 rubriker
   (max 30 tecken) och 4 beskrivningar (max 90 tecken), landningssidans
   innehåll. Texterna skrivs av AI om den är konfigurerad, annars av
   mallar. Kontroller: teckengränser, inga siffror som inte finns bland
   fakta, inga förbjudna påståenden (billigast, garanti, löften om tider).
6. **Granskning**: en `Review`-runda per inskick. Byrån rättar i
   /manage/flamingo/ och skriver varför; ändringarna sparas som diff.
7. **Godkännande**: kunden ser vad som ändrades och godkänner.
8. **Publicering**: med Google Ads API (när nycklar finns) eller manuellt:
   byrån laddar ner en Google Ads Editor-fil (CSV) och markerar kampanjen
   live. Landningssidan blir live på `/lp/<slug>/`.
9. **Förfrågan**: formuläret på landningssidan skapar en `Lead` med källa
   och klick-id (gclid). Sms till ägaren och autosvar till den som frågade
   skickas via 46elks när det är konfigurerat och kunden slagit på det;
   annars loggas att inget skickades. Inga mejl skickas automatiskt.
10. **Inkorg**: status (ny, kontaktad, offert skickad, vunnen, förlorad,
    skräp) och belopp.
11. **Vunnen + belopp**: blir en `ConversionUpload`. Med API laddas den upp
    till Google, annars exporterar byrån en CSV för Googles import av
    offline-konverteringar.
12. **Översikten**: annonspengar (när rapporter finns), förfrågningar, kr per
    förfrågan, affärer, kr per affär, affärsvärde, och "tre saker" från
    enkla regler.

## Integrationer och nycklar

| Integration | Inställning | Utan nyckel |
|---|---|---|
| AI-texter | samma som assistenten (`ASSISTANT_PROVIDER`, Bedrock) | mallar |
| Google Ads API | `GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_LOGIN_CUSTOMER_ID` (MCC), OAuth-klient | Editor-CSV och manuell koppling |
| Google Places | `GOOGLE_PLACES_API_KEY` | fakta från hemsidan och kunden |
| 46elks | `ELKS_API_USERNAME`, `ELKS_API_PASSWORD`, `ELKS_SENDER` | inget sms, loggat |

## Hårda regler

Inga automatiska mejl till kunder. Inga löften om tider. AI får bara
använda bekräftade fakta. Ingen extern ändring (Google, publicerad sida)
utan att kunden godkänt och byrån granskat. 375 px utan sidoscroll.
