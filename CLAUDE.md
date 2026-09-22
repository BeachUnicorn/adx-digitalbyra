# adx.se

Django 6 + Postgres. Panelen ligger på `/manage/`, kundportalen på `/kund/`,
publika sajten i roten. Deploy: `./deploy` från den här mappen (kör tester,
ruff och migrationskoll innan något pushas).

## Smala skärmar är inget tillval

Panelen och kundportalen används från telefon. **Varje ändring som rör en
vy, en lista, en meny eller en formulärrad ska ses i 375 px bredd innan den
deployas** - inte bara i en bred webbläsare. Det här står här därför att det
gled tre gånger: kundregistret, driftöversikten och AWS-sektionen byggdes
alla i 1440 px, och portalens meny sköts utanför skärmen när Fakturor lades
till (2026-09-22).

Så här kontrolleras det (Claude Code har webbläsarpanelen inbyggd):

1. `preview_start` med `adx`, logga in som `claude-qa` / `qa-lokal-2026`.
2. `resize_window` med `preset: "mobile"`.
3. Gå igenom de vyer ändringen rör och mät överskott i sidled:

   ```js
   document.documentElement.scrollWidth - window.innerWidth   // ska vara 0
   ```

4. Titta på sidan. Noll i sidled räcker inte: en tabell som ryms för att
   den gömmer halva innehållet bakom en sidoscroll är inte löst.

Kundportalens vyer kräver kundvyn: knappen "Visa portalen som kunden" på
kundkortet, eller `/kund/lamna-kundvyn/` för att lämna den.

### Vad som redan är på plats

- **Tabeller blir kort under 760 px.** `static/js/manage-tables.js` stämplar
  kolumnrubriken på varje cell ur `<thead>`, och `manage-skin.css` ritar
  raderna som kort. En ny tabell får beteendet gratis - men den **måste ha
  en `<thead>`**, annars blir korten etikettlösa. Är tabellen redan
  etikett + värde: sätt `m-table--plain` så hoppas den över.
- **Vakterna i `apps/common/test_mobil.py`** fäller bygget om en tabell
  saknar rubrikrad, om kortläget kopplas bort, om portalens meny inte
  radbryter eller växer förbi sju länkar, eller om viewport-taggen försvinner.
  De ersätter inte ögat - de fångar bara de misstag som redan gjorts.

## Andra stående regler

- **Inga automatiska mejl till kunder.** Kunden mejlas bara från en knapp
  som uttryckligen säger att kunden mejlas. Larm till byrån är fria.
- **Lova aldrig svarstider** ("vi svarar inom en arbetsdag") i kod eller
  innehåll. `apps/common/tests.py` har en vakt.
- **Inga AI-typografitecken** (tankstreck, typografiska citattecken,
  ellipstecken) i kod, mallar eller seed-data. Vakt finns.
- **Inga `[ ]`-hakar som dekoration** i gränssnittet.
