# ADX — digitalbyråns webbplats

Django-sajt för digitalbyrån ADX (adx.se): webbutveckling, automation,
managed content, hosting, domain management och e-post.

## Designen

Front-enden följer **`strict-design-guide.html` EXAKT** (mockupen ligger i
mappen ovanför repot). Kärnan: en WebGL-gradientbakgrund där **en hexfärg
per sida** driver hela paletten och textfärgen väljs automatiskt på
luminans. Rör du utseendet: ändra guiden först.

- `static/css/site.css` — extraherad ur guiden + minimala tillägg
  (formulär, a11y, redigeringsoverlay) dokumenterade i filens huvud
- `static/js/gradient.js` — shadern, exakt ur guiden; färgen läses ur
  `<body data-gradient>`
- `apps/website/theme.py` — Python-port av palettmatematiken (server-satt
  textfärg = ingen blink). Ändras härledningen: ändra BÅDA filerna
- Typsnitten är självhostade i `static/fonts/` (GDPR — inga anrop till
  tredjepartsdomäner)

## Innehållsmodellen

Allt publikt är **BlockPages** byggda av blocktyper ur designens
komponentbibliotek. En blocktyp = en rad i `BlockType` + en post i
`apps/manage/block_schema.py` + en mall i `templates/website/blocks/` —
synkvakten i `apps/website/tests.py` gör en missad registrering till ett
byggfel.

- Tjänsternas sidor är BlockPages med samma slug som `Service`-raden
  (`/webbutveckling/` …). Service-modellen driver navigation, tjänstelistan
  och förfrågningsformulärets ämnesval.
- Stadssidorna (`/digitalbyra/<stad>/`) ägs av areas-appen. **En sida per
  stad** som bär både "digitalbyrå" och "webbyrå" i copyn — aldrig separata
  sidor per sökord, aldrig mallad text (doorway-regeln).
- Förfrågningar: `apps/inquiries` — kvalificerande enkelstegsformulär med
  botskydd i lager (`apps/common/botcheck.py`) och **tyst fejkad framgång**
  för fällda botar.
- AI-redaktören (`apps/assistant`): chatt + MCP, allt blir utkast som
  godkänns i /manage/. Se `apps/assistant/README.md`.

## Kom igång

```bash
uv sync
cp .env.example .env          # fyll i SECRET_KEY och DATABASE_URL (adx_dev)
uv run python manage.py migrate
uv run python manage.py seed_site   # sidor, block, tjänster, städer ur seed_data/
uv run python manage.py createsuperuser
uv run uvicorn config.asgi:application --port 8770 --reload
```

`seed_site` är idempotent och läser spårade filer i `seed_data/`. Den körs
**aldrig** av deployen — efter första seeden är produktionen sanningskällan.

## Drift

`server/` provisionerar en Ubuntu-låda med sites.d-modellen: en conf-fil
per sajt, hälsogrindad deploy med automatisk rollback, certnamn frikopplat
från domänerna. Sajten kräver **ASGI** (uvicorn-worker) — MCP-endpointen
finns bara i `config.asgi`. Se `server/README.md`.
