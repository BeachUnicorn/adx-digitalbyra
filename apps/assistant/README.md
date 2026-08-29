# AI-redaktören (apps/assistant)

Kundens egen Claude- eller ChatGPT-app kopplas till sajten som MCP-connector
och kan läsa innehåll och **föreslå** ändringar. Ingenting publiceras utan att
kunden godkänner det i /manage/.

Plan och beslut: `notes/ai-redaktoren-byggdokumentation.md`.

## Arkitektur

```
Claude-appen ──OAuth──┐
                      ├──▶ /mcp/ ──▶ operationsregistret
Claude Code ──nyckel──┘                 │
                        READ ───────────┤ körs direkt
                        TEXT/BUSINESS ──┴─▶ DraftChange (utkast)
                                                │
                                   kunden godkänner i /manage/ai/
                                                │
                                   samma formulär/sanering som manuellt
                                                │
                                        reversion-revision (källa: AI)
```

**Säkerhetsgränsen är strukturell.** MCP-verktygen kan bara skapa
`DraftChange`-rader. Även en helt vilseledd modell kan inte publicera - och
inte heller godkänna, eftersom godkännandet bara finns i /manage/.

| Fil | Ansvar |
|---|---|
| `revisions.py` | Vilka modeller som versioneras + middleware som fångar manuella /manage/-ändringar |
| `models.py` | `AIJob`, `DraftChange`, `RevisionMeta`, `AssistantToken` |
| `operations/` | Operationsregistret - AI:ns enda väg in. En modul per domän |
| `draft.py` | propose / approve / reject / undo_job. Enda stället som skriver AI-ändringar |
| `mcp_server.py` | MCP-verktyg genererade ur registret + serverinstruktioner |
| `asgi_app.py` | Router: /mcp/ till MCP, allt annat till Django. Autentisering |
| `oauth_models.py` | Klient, auktoriseringskod och tokens |
| `oauth_provider.py` | OAuth-provider mot MCP-SDK:t |
| `oauth_views.py` | Samtyckesvyn - enda stället en kod delas ut |
| `history_views.py` | Versionshistorik och återställning per objekt |
| `views.py` | Granskning, godkännande, nyckelhantering i /manage/ |
| `diffing.py` | Ordnivå-diff för historik och utkast |

## Riskklasser

| Klass | Exempel | Godkännande |
|---|---|---|
| `READ` | `hamta_sida`, `lista_omraden` | Inget - körs direkt |
| `TEXT` | `uppdatera_block`, `skapa_faq_fraga` | Utkast, får klumpgodkännas |
| `BUSINESS` | `skapa_tjanst`, `satt_omrade_aktiv`, `skapa_sida` | Utkast, **alltid en i taget** |

**Riskklassen begränsar inte längre markeringen** (kundens beslut
2026-08-21): den som granskar väljer själv vad hen godkänner, även
affärsdata. Klassen finns kvar som märkning i granskningen, och en
bekräftelseruta talar om hur många markerade rader som är affärsdata innan
de godkänns - informerat val, inte spärr. Endast vid godkännande; ett
felaktigt avslag går att göra om.

**Markeringen är gemensam för hela jobbet**: ETT formulär (`bulk-form`), en
verktygsrad med "Markera alla" och en räknare, plus ett kryss per grupp.
Tidigare fanns ett formulär per grupp - då hörde kryssrutor i olika grupper
till olika formulär, så klumpknappen skickade bara sin egen grupps rutor och
det såg ut som att ingenting hände. Nya objekt grupperas dessutom per
operation (`_NEW_GROUP_LABELS`); utan det fick varje nytt förslag en egen
grupp med en egen knapp.

`BUSINESS` har kryssruta som allt annat. Skillnaden är märkningen och
bekräftelserutan, inte vad som går att markera.

Radering finns inte i verktygsytan. AI:n kan avaktivera, aldrig ta bort.

## Lägga till en operation

1. Skriv `prepare` (validerar, returnerar `Prepared`) och `apply` (skriver).
   Båda ska gå genom befintliga ModelForms via `run_form()` - det är där
   saneringen av em-dash och HTML sitter.
2. `register(Operation(...))` med JSON Schema (`additionalProperties: False`).
3. Beskriv **när** verktyget ska användas, inte bara vad det gör.

Verktyget dyker upp i MCP automatiskt - registret är enda definitionen.

## Modellen: Bedrock

> **OBS (ADX):** styckena om kontoverifieringar och produktionsläge nedan
> ärvdes från systersajten och beskriver HENNES konto/servrar. För ADX
> gäller principerna men inte kontonumren - verifiera modelltillgången mot
> ADX eget AWS-konto innan driftsättning (mät, anta inte).

Servern kör i AWS, så den inbyggda assistenten går via Bedrock med
instansrollen - inga API-nycklar att distribuera eller rotera.

```
ASSISTANT_PROVIDER=bedrock            # "anthropic" = reservläge, kräver nyckel
ASSISTANT_BEDROCK_REGION=eu-central-1
ASSISTANT_BEDROCK_MODEL=eu.anthropic.claude-sonnet-4-6
adx-prod=                # tom på servern; lokalt en ~/.aws-profil
ASSISTANT_DAILY_BUDGET_USD=50.0   # nödbroms, inte kundgräns
```

**Serverrollen behöver tre rättigheter**, inte en:

```
bedrock:InvokeModel
aws-marketplace:ViewSubscriptions
aws-marketplace:Subscribe
```

De två sista är inte valfria. Utan dem nekas det FÖRSTA anropet mot varje ny
modell med "Model access is denied", och AWS felmeddelande nämner inte IAM -
det kostade Atlas-projektet en incident 2026-07-08. `llm._friendly()`
översätter felet till den här listan om det ändå inträffar.

Modellen måste dessutom vara aktiverad i regionen (Bedrock → Model access).

**Och formuläret om användningsfall måste vara inskickat** - en gång per
AWS-konto, i konsolen under Bedrock → Model access. Utan det svarar Bedrock
"You do not have access to this operation" och hänvisar till
aws-verification@amazon.com; den verkliga texten ("Model use case details have
not been submitted") ligger i svarskroppen och syns bara med `--debug`.
`llm._friendly()` översätter även det felet.

**Verifierat mot konto 200810847648 (2026-08-21).** Att en inference profile
listas som `ACTIVE` säger ingenting om att den går att anropa - alla 22 stod
ACTIVE medan varje anrop nekades.

| Modell | Status på kontot |
|---|---|
| `eu.anthropic.claude-sonnet-4-6` | svarar - **vår inställning**, samma som adx kör |
| `eu.anthropic.claude-opus-4-6-v1` | svarar |
| Opus 4.5, Sonnet 4.5, Haiku 4.5 | svarar, men spärrade av modellpolicyn |
| Opus 5, Sonnet 5, Fable 5, Opus 4.7, 4.8 | `not available for this account` (AWS Sales) |

Opus 5 och Fable 5 är alltså inte en region- eller konfigurationsfråga utan en
kontobegränsning - testat i eu-central-1, eu-north-1 och eu-west-1.

## Modellpolicy

Kundens regel 2026-08-21: **bara Sonnet eller Opus från 4.6 och uppåt, aldrig
Haiku.** Skälet är innehållskvalitet - modellen skriver text som går rakt ut
till besökare, och adx såg Haiku producera "fyllare dagar"-copy (2026-07-26).

Regeln sitter som en spärr i `llm.assert_model_allowed()` som körs före varje
anrop, inte bara som ett defaultvärde: en felsatt `ASSISTANT_BEDROCK_MODEL`
ska stoppa anropet, inte tyst byta modell. Versionen läses efter att
datumstämplar rensats - annars tolkas `claude-3-sonnet-20240229` som version
20240229 och den äldsta modellen passerar som den nyaste. Se
`ModelPolicyTests`.

## Lokal AWS-profil

`adx-prod=adx-prod` hämtar nycklarna ur 1Password via
`credential_process`. Låser appen sig ger AWS bara
`Expecting value: line 1 column 1` - det är inte Bedrock, det är att
`credential_process` returnerade tomt. `llm._friendly()` översätter även det.
På servern ska variabeln vara tom; där gäller instansrollen.

## Gränssnittet i /manage/ai/

Sektionen har tre sidor och en gemensam undermeny (`_subnav.html`):
**Chatt**, **Förslag** och **Anslutning**. Undermenyn hör till sektionen och
inkluderas på alla sidorna - tidigare låg länkarna som knappar i chattsidans
rubrik och försvann så fort man klickade vidare, vilket gjorde utkastlistan
till en återvändsgränd. `AISectionNavTests` skyddar det.

Ordet är **förslag**, inte "utkast" eller "AI-redaktören", överallt kunden
ser det. Tre namn på samma sak var en av anledningarna till att strukturen
kändes ogenomtänkt.

**Radering:** ett förslag/samtal kan raderas från förslagslistan, från
jobbsidan och från chattens sidolista. Versionshistoriken överlever -
`RevisionMeta.job` är SET_NULL, så genomförda ändringar finns kvar under
/manage/historik/ även när jobbet är borta. Det som försvinner är utkasten,
chattraderna och möjligheten att ångra jobbet som helhet; bekräftelserutan
säger det när det finns något att ångra.

Tomma chatten visar bara orben och skrivrutan. Exempelförslagen togs bort
2026-08-21 - de lästes som statiska exempel oavsett att de byggdes på verklig
data. Kostnadspanelen är också borta; kostnaden följs i `AICall` och
dygnstaket finns kvar som nödbroms, men syns inte i gränssnittet.

**Orben** (`_orb.html`) är ren CSS - tre suddade fläckar som roterar olika
fort bakom en rund mask. Olika hastighet är hela poängen; samma takt läser
som ett hjul. Den används liten i toppmenyn och vid assistentens
meddelanden, stor i tomma chatten, och snurrar fortare (`is-thinking`) medan
en tur pågår. Inga bilder, inga nätverksanrop, och den stannar helt vid
`prefers-reduced-motion`.

## @-omnämnanden och underförslag

**@-omnämnanden** (`mentions.py`): kunden skriver `@` i chatten och väljer ur
en sökmeny (tjänster, områden, sidor, FAQ, egna förslag). I texten landar en
token som `@tjanst:spolning`; vid modellanropet översätts den till ett
referensblock med exakt slug och rätt hämtverktyg. Blocket sparas ALDRIG i
meddelandet - kundens text förblir kundens. I loggen visas token som chip.
En token som inte träffar ger ingen referens alls: modellen ska aldrig få
påhittade objekt. Sökningen av förslag är per användare. Ren @ öppnar en bläddringslista -
en tom meny läses som att funktionen är trasig. Alla objekttyper är sökbara:
tjänster, kategorier, områden, sidor, FAQ-sektioner, enskilda
FAQ-frågor och egna förslag. @ mitt i ord (e-postadresser) triggar inte.

**Underförslag** (`suggestions.followups_for`): när en ändring godkänts visar
jobbsidan "Nästa steg"-chips för samma objekt - arbetsgång som saknas, FAQ
som inte finns, tom brödtext. Deterministiska: de speglar vad som saknas på
raden, inte vad modellen tycker. Klicket fyller chatten (?q=) med en prompt
som bär @-token; inget skickas förrän kunden trycker Skicka.

## Driftsättning av AI-delen

`.env` på servern:

```
ASSISTANT_PROVIDER=bedrock
ASSISTANT_BEDROCK_REGION=eu-central-1
ASSISTANT_BEDROCK_MODEL=eu.anthropic.claude-opus-4-6-v1
ASSISTANT_AWS_PROFILE=            # TOM på servern - instansrollen gäller
```

Lokalt sätts `ASSISTANT_AWS_PROFILE=adx-prod` (profilen i `~/.aws`).
**Profilnamnet är ett värde i `.env`, aldrig ett namn i koden** - byter man
namn på inställningen i `base.py` eller `llm.py` slutar Django starta.

Serverns instansroll behöver `bedrock:InvokeModel`,
`aws-marketplace:ViewSubscriptions` och `aws-marketplace:Subscribe`, och
formuläret om användningsfall måste vara inskickat för kontot (engångssteg).

**Släppt till kunden 2026-08-22**: chatt, förslag och anslutning är öppna
för inloggade användare. Kvar bakom superuser: personliga API-nycklar
(`token_create`/`token_revoke`) och nyckelsektionerna på anslutningssidan -
kunden ansluter via inloggning + samtycke, aldrig via kod. Statistikmodulen
är fortsatt AV i MCP (`ASSISTANT_FEATURES`), verifierad i produktion.

## Moduler (ASSISTANT_FEATURES)

En operation kan tillhöra en modul: `Operation(..., feature="statistik")`.
Är modulen avstängd filtreras operationen bort i `runtime.available_operations()`
- alltså för MCP, stdio och den inbyggda chatten samtidigt - och
`run_operation` vägrar dessutom köra den. Två spärrar, eftersom filtrering i
verktygslistan inte är en behörighetskontroll: en MCP-klient kan anropa vilket
namn som helst.

Operationer utan `feature` är alltid tillgängliga.

**Statistikmodulen är AV** - kunden har inte köpt den. Slå på med en rad i
`.env`, ingen kodändring:

```
ASSISTANT_FEATURE_STATISTIK=true
```

## Markdown i svaren

Modellen svarar i markdown; `rendering.message_html()` gör det till HTML.
Ordningen är säkerhetskritisk och får inte kastas om:

1. markdown -> HTML (modellens text är **inte** betrodd - den kan bära text
   som en webbsida matat in via ett läsverktyg)
2. `nh3.clean` mot en snäv taggista - samma boundary som resten av sajten
3. @-tokens ersätts sist, med `format_html` på redan sanerad HTML

Rubriker plattas till `<strong>`: h1-h3 i en chattbubbla konkurrerar med
sidans egen rubriknivå. Inga bilder, inga tabeller, ingen inbäddning -
assistenten ska svara, inte formge.

Kundens egen text renderas INTE som markdown (`mentions.as_html`) - den ska
visas som den skrevs.


## Återkoppling under turen

Tre saker, eftersom en tur kan ta minuter och "Arbetar" ensamt inte går att
skilja från en hängning:

* **Modellens egen berättelse.** Texten den skriver innan den anropar ett
  verktyg ("Nu hämtar jag alla fem tjänster för att kunna jämföra") sparas
  som ett berättarsteg. Den kastades tidigare bort och är den bästa
  feedbacken som finns - den kommer från modellen själv.
* **Pågående steg.** Steget sparas som `state: running` FÖRE anropet och
  uppdateras till `done`/`fail` efteråt. Sparas det först efteråt står
  gränssnittet stilla under hela verktygsanropet.
* **Klocka och objektnamn.** "Läser tjänst: byte-av-blandare" i stället för
  "Läser tjänst", plus förfluten tid.

Steg utan `state` (sparade före augusti 2026) visas som avklarade.

## Övergivna turer

En tur körs i en tråd i webbprocessen (`tasks.queue_turn`). Startar processen
om - deploy i produktion, autoreload i utveckling - dör tråden och lämnar en
`PENDING`-rad som ingen fyller i.

`sweep_stale()` dömer ut sådana rader och körs både när en ny tur startas och
när chatten **läses** (`chat_poll`). Bara det förstnämnda räckte inte: satt
man och tittade på en död tur blev den aldrig utdömd, och "Arbetar" stod kvar.

Två invarianter:

* Staleness mäts på `updated_at`, inte `created_at`. En tur som fortfarande
  loggar steg lever, hur länge den än hållit på.
* **Gränssnittet måste polla längre än `STALE_AFTER`.** Ger pollningen upp
  först hinner sveparen aldrig döma ut turen. `test_ui_polls_longer_than_the
  _sweeper_waits` låser fast ordningen.

Ger gränssnittet ändå upp säger det ifrån i bubblan - tystnad är det sämsta
svaret.

## Granskningslänken

I chatten är den en **knapp** ("Visa N förslag") under det sista svaret, inte
en URL i löptexten: svaret renderas som markdown, och en naken URL blir då
oklickbar text. Knappen ritas utifrån verkligt antal väntande förslag, så den
kan aldrig visa en föråldrad siffra. Gamla meddelanden som har den inklistrade
raden får den bortstädad vid rendering.

Över MCP gäller motsatsen - där finns ingen knapp att rita, och
serverinstruktionen ber modellen avsluta med länken från `runtime.review_url`.
Andra länkar i svaren behandlas inte särskilt; markdown-länkar renderas som
vanliga länkar.

## Nya tjänster skapas kompletta och synliga

`skapa_tjanst` kräver **namn, beskrivning och arbetsgång** i schemat - inte
som en uppmaning i verktygsbeskrivningen. Instruktionen fanns där först och
modellen följde den inte; ett krav i `required` går inte att hoppa över.

Tjänsten skapas **aktiv**. Den skapades tidigare inaktiv "tills innehållet är
klart", men det innebar att kunden godkände ett förslag och sedan fick leta
upp tjänsten för att tända den - ett dolt extrasteg av precis den sort vi
byggt bort på andra ställen. Eftersom förslaget nu innehåller både text och
arbetsgång är det en färdig tjänst som godkänns. Vill man ändå ha den dold
finns `satt_tjanst_aktiv`.

Säkerhetsgränsen är oförändrad: ingenting når sajten utan kundens
godkännande. Se `test_nothing_becomes_public_without_approval`.

**Områden (`skapa_omrade`) skapas fortfarande inaktiva** - de saknar ofta
både text och koordinater när de skapas, och en tom ortssida är sämre än
ingen. Ändras det bör samma resonemang som för tjänster gälla: gör innehållet
obligatoriskt först.

## Förhandsgranskning av utkast

Diffen visar vilka fält som ändras. Förhandsgranskningen visar **hur sidan
blir** - och det är resultatet kunden ska ta ställning till, inte datan.

`preview.render_draft()` applicerar utkastet inuti en transaktion, renderar
den publika vyn, och rullar tillbaka. Tre saker gör det säkert:

* `transaction.set_rollback(True)` ligger i ett **finally**. Utan det
  publicerar en misslyckad rendering utkastet - se
  `test_rollback_happens_even_when_rendering_crashes`.
* Vyn anropas direkt via `resolve()`, inte genom middleware-stacken. Då körs
  varken besöksstatistik eller revisionsmiddleware; en titt ska inte synas i
  mätningen.
* `op.apply` anropas direkt, inte `draft.approve` - godkännandet skriver
  revisioner och ändrar status, och det hör inte hemma i en titt.

Sidan renderas som **anonym besökare**. Med en inloggad användare ritas
redigeringsdocken ovanpå, och frågan är hur besökaren kommer att se sidan.

Ram-vyn bär `@xframe_options_sameorigin`. Sajtens globala X-Frame-Options är
`DENY`, så utan undantaget vägrar webbläsaren visa förhandsgranskningen.
Undantaget gäller **bara den vyn** och bara samma origin - resten av
/manage/ behåller DENY, vilket `test_other_manage_pages_keep_deny` bevakar.

Sidan visas i en iframe med `sandbox="allow-same-origin"` (inga skript):
innehållet är text som AI:n skrivit, och den texten kan i sin tur komma från
något den läst. Den ska visas, inte köras.

Ändringar utan egen publik sida - FAQ-frågor, metatexter - ger ett vänligt
meddelande i stället för ett fel.

## Beroenden mellan utkast

Ett förslag kan peka på något ett annat förslag ska skapa: en FAQ-fråga i en
sektion som bara finns som utkast. `Prepared.depends_on` sätter
`DraftChange.depends_on`, och `approve()` vägrar genomföra ett utkast vars
beroende inte är godkänt. `approve_many` sorterar beroendefria först, så
"Markera alla" fungerar oavsett i vilken ordning kunden kryssar.

Operationer som behöver se jobbet (för att hitta utkast som ännu inte
godkänts) markeras med `wants_job=True`; signaturen blir då
`prepare(job, user, **params)`. Övriga operationer rörs inte.

**Varför det behövdes:** `skapa_faq_fraga` krävde en sektion som redan fanns
i databasen. Sektionen är bara ett utkast tills den godkänts, så anropet
misslyckades med "Okänd FAQ-sektion" och inget utkast skapades - modellen
kunde alltså skapa sektioner men aldrig fylla dem. Tio tomma sektioner
2026-08-21. `payload` bär numera `section_slug`, inte `section_id`, och
sektionen slås upp vid apply.

## Produktionsläget (verifierat 2026-08-22)

Servern: EC2 `django` (i-08f4c9ff42b8a97c4, eu-north-1) i kundens konto,
`ssh adx`. Appen körs som `adx.service`:
gunicorn med **uvicorn-workern** mot `config.asgi:application` - ren WSGI
har ingen /mcp/-routning, vilket var felet när Claude Desktop inte kunde
registrera sig ("Couldn't register with sign-in service").
Backup av gamla unit-filen: `/etc/systemd/system/adx.service.bak-wsgi`.

`SITE_BASE_URL=https://www.adx.se` ligger i serverens `.env` - utan
den pekar OAuth-metadatan fel. Instansrollen **adx-ec2** (skapad 2026-08-23, inline-policyn
`bedrock-invoke`) ger serverns tre Bedrock-rättigheter - den saknades från
början och gav "Inga AWS-uppgifter hittades" i chatten trots korrekt kod.
Instansprofilen är kopplad till i-08f4c9ff42b8a97c4; ingen omstart krävdes
eftersom en ny Bedrock-klient skapas per anrop.

`createinitialrevisions` är körd (710
revisioner). Registreringen är verifierad utifrån: POST /register ger 201
med client_id + secret, /mcp/ svarar 401 utan auth.

## Ändra ett liggande förslag

Modellen kan inte mutera ett utkast på plats - `before`-ögonblicksbilden och
den diff kunden redan läst skulle sluta stämma. I stället finns
`dra_tillbaka_utkast`: dra tillbaka det gamla, lägg ett nytt. Bara EGNA
väntande utkast; godkända ändringar ångras i /manage/, aldrig av modellen.

Kunden kan peka ut ett liggande förslag med `@utkast:<id>` - sökmenyn listar
väntande utkast per användare, och referensblocket tipsar modellen om att
dra tillbaka i stället för att lägga ett motsägande förslag.

**FAQ-koppling:** `koppla_faq_till_omrade` och `koppla_faq_till_tjanst`
fäster en sektion där den ska visas. Operationerna saknades helt fram till
2026-08-23 - modellen kunde skapa en FAQ men aldrig fästa den, och
underförslagen lovade dessutom "koppla den till tjänsten", alltså något den
inte kunde göra. Sektionen får vara ett utkast från samma tur.

## Skrivguiden

`SiteSettings.ai_style_guide` redigeras under **AI → Skrivguide**
(`assistant.views.style_guide`). Fältet fanns på modellen men saknade
gränssnitt helt fram till 2026-08-23 - det gick alltså inte att ändra.
Tom ruta = `DEFAULT_STYLE_GUIDE` i `context_ops`. AI:n läser den via
`hamta_skrivguide` först i varje session.

## Vad AI:n kan ändra (efter fältrevisionen 2026-08-23)

| Objekt | Skapa | Ändra |
|---|---|---|
| Stad (område) | ja | texter, SEO, synlighet, FAQ-koppling, grannområden |
| Tjänst | ja (med steg) | texter, arbetsgång, synlighet, FAQ-koppling |
| FAQ-sektion | ja | titel, beskrivning, synlighet |
| FAQ-fråga | ja | fråga, svar |
| Blocksida | ja (med block) | titel, SEO, publicering |
| Block | ja | fält, listrader, ordning, synlighet |

Medvetet UTANFÖR verktygsytan: bilder (kräver mediebibliotek och
alt-texter), koordinater, slugs (bryter länkar), menyer, kategorier,
målgrupper som objekt, och skrivguiden själv - den är kundens instruktion
TILL modellen, inte något modellen ska kunna skriva om. Radering finns inte
alls; AI:n kan avaktivera, aldrig ta bort.

## Att bygga en hel sida (2026-08-29)

En blocksida är en ordnad stapel block. Tre saker gjorde att modellen inte
kunde bygga en sådan, och alla tre är åtgärdade.

**1. Sidan och blocken i samma tur.** `skapa_block` krävde en sida som redan
fanns i databasen - men en föreslagen sida är ett utkast tills kunden godkänt
den. Modellen fick "Okänd sida" och *inget utkast alls*: den kunde skapa sidor
men aldrig ge dem innehåll. Exakt samma fälla som de tomma FAQ-sektionerna,
och löst på samma sätt (`wants_job` + `depends_on` + slug-uppslag vid apply).
Sidan och dess block hamnar därmed i EN godkännandegrupp, och `approve_many`
sorterar sidan först.

**2. Listinnehållet var oåtkomligt.** `clean_block_values` läste bara schemats
`fields`, aldrig `lists`. Fjorton av tjugoen blocktyper har innehåll i listor
och tre (`chips`, `marquee`, `contact_cards`) har ALLT där - de gick bara att
skapa tomma. `clean_block_rows` täcker nu listorna med samma sanerare, och
operationerna tar en `listor`-parameter vid sidan av `falt`.

**3. Modellen visste inte hur block ser ut.** `hamta_blockkatalog` beskriver
varje blocktyp: utseende, fält, listornas radform, kompositionsreglerna (hero
först, bar sist) och vad som inte går att sätta. Beskrivningen bor i
`BLOCK_EDIT_SCHEMA["<typ>"]["purpose"]`, alltså på samma ställe som resten av
blocktypens registrering - synkvakten i `apps/website/tests.py` kräver den, så
en ny blocktyp utan beskrivning blir ett byggfel. Lärdomen från `har_bild`
gäller: modellen läser scheman och beskrivningar, inte payloads.

Nytt i verktygsytan: `hamta_blockkatalog`, `ordna_block` (hela sidans block-id
i ny ordning) och `satt_block_synligt`. Därmed är luckan "blockens ordning och
synlighet" stängd.

### Tre saker som tyst gick fel på vägen

* **Länkar sparades som råa strängar.** `clean_block_values` körde inte
  `link`-fält genom `_clean_link` som POST-vägen gör, så en AI-satt länk blev
  `"/kontakt/"` i stället för `{"kind": "page", "id": 11}` - alltså utanför
  hela länksystemet som gör att länken överlever ett slug-byte och att ett
  dött mål döljs. Rättat i schemat, med regressionstest.
* **Ogiltiga värden sparades tomma.** `_clean_value` svarar med tom sträng på
  en trasig url eller längd och med första alternativet på ett ogiltigt
  choice-värde. Modellen såg bara kvittot. `_assert_kept` gör det till ett
  verktygsfel, i samma anda som `assert_nothing_lost`.
* **Blockändringar gick inte att förhandsgranska.** `apply` returnerar ett
  `Block`, och ett block har ingen egen adress - så kunden fick "hör inte till
  någon egen sida" på den vanligaste ändringen som finns. `preview._SHOWN_ON`
  hoppar nu till blockets sida.

### Vad modellen får veta att den INTE kan

Katalogens `det_du_inte_kan` säger det rakt ut, eftersom en modell som inte
vet var gränsen går lovar kunden fel saker: bilder (den kan inte se dem),
sidans adress, menyplacering (**en ny sida hamnar inte i någon meny** - kunden
måste lägga in den), radering, och publicering.

## Varför AI:n inte kopplar tjänster till städer

Beslut 2026-08-23 (ärvt och fortsatt giltigt i ADX). En kombinationssida
per tjänst och stad renderar `service.body` - identisk för alla städer -
plus `area.body` - identisk för alla tjänster - med stadsnamnet inbytt i
rubriken. Tjänster gånger städer blir hundratals nästan identiska sidor,
alltså doorway pages som Google straffar.

Hela funktionen är borttagen, inte bara AI:ns del: matrisen i /manage/,
kombinations-URL:en, vyn och sitemap-posten. Byrån levererar alla tjänster
i alla städer, så en koppling per stad var dessutom fel modell från början
- stadssidan listar nu helt enkelt ALLA aktiva tjänster, utan något att
underhålla.

`DoorwayPageGuardTests` faller om någon återinför operationerna eller låter
en annan operation skriva `AreaService`-rader. Vill man ta upp funktionen
igen måste unikt innehåll per kombination lösas först - annars är sidorna
en risk oavsett vem som skapar dem.

## Bilder

Modellen kan inte SE bilder, men ska veta om de finns. Läsoperationerna
rapporterar `har_bild` (tjänster, kategorier, områden, block) och vid
enskild läsning även filnamn + alt-text - alt-texten är det enda modellen
kan bedöma om en bild.

**Fältet räckte inte.** Data fanns i svaret, men modellen svarade ändå "jag
har inte tillgång till bildinformation" utan att ens anropa verktyget - den
utgick från sin bild av verktygsytan, inte från utfallet. Först när
verktygens BESKRIVNINGAR nämnde har_bild började den använda det. Samma
lärdom som med obligatoriska fält: modellen läser scheman och beskrivningar,
inte payloads.

## Godkänna i chatten, och djuplänkar

**Chattkort:** väntande förslag renderas som kort direkt under det sista
svaret i den inbyggda chatten, med Godkänn/Avslå. Klicket är kundens, i en
inloggad session med CSRF - samma säkerhetsgräns som på granskningssidan,
bara på en annan plats. AI:n kan fortfarande inte godkänna något; över MCP
finns inget godkänn-verktyg och kommer inte att finnas, eftersom
samtyckesrutan lovar kunden motsatsen.

**Gruppering följer beroendekedjan.** En FAQ-sektion och dess frågor är EN
sak att godkänna, inte sex: `_grouped` följer `depends_on` upp till roten
och lägger allt i samma grupp. Granskningssidan får en "Godkänn alla N"-knapp
per grupp; `approve_many` sorterar ändå beroendefria först, så ordningen
håller.

**Djuplänk:** MCP-svaret returnerar `granska` = länk till ETT förslag
(`...#utkast-<id>`), inte till listan. Kunden sitter i Claude-appen och ska
inte behöva leta rätt på kortet bland tjugo andra. Kortet bär ankaret och
markeras med `:target`.

## Kan förslag godkännas inne i Claude-appen?

Nej - mätt, inte gissat. Servern loggar varje ny klients förmågor
(`mcp_server._log_client`), och Claude svarar 2026-08-22:

```
MCP-klient ansluten: Anthropic/ClaudeAI 1.0.0 | protokoll 2026-07-28
                     elicitation: nej | sampling: nej
```

Klienten deklarerar **ingen elicitation alls**, varken URL- eller
formulärläge. URL-elicitation (MCP 2025-11-25) skulle vara vägen till en
godkänn-prompt inne i appen - specen och SDK:t stödjer det, klienten inte.
Bygger man det ändå utlöses det tyst aldrig.

Därför är **djuplänken** det bästa som finns: MCP-svarets `granska` pekar på
ett enskilt förslag. Klientnamnet är `Anthropic/ClaudeAI` och inte något
Desktop-specifikt, så resultatet gäller sannolikt även claude.ai i
webbläsaren.

Loggningen står kvar just för att fånga dagen detta ändras - dyker
`elicitation: url` upp i loggen är det bara att bygga prompten. Kontrollera
med:

```bash
sudo journalctl -u adx.service | grep "MCP-klient ansluten"
```

Och för tydlighetens skull: ett `godkann_utkast`-verktyg i MCP är inte
alternativet. Samtyckesrutan lovar kunden att appen inte kan godkänna sina
egna förslag, och det löftet står.
