# Seeding site content to the server

Your **content** (pages, blocks, menus, services, site settings) and your
**uploaded media** do not travel with `git pull` on their own:

- The database lives in Postgres, not in git.
- `user-uploaded-media/` is `.gitignore`d.

## The ADX way: `seed_site`

The tracked seed for this project is `seed_data/`:

- `adx_pages.json` - the eleven pages, transcribed from the design guide
  (blocks, colours, SEO meta).
- `adx_cities.json` - the unique city texts for `/digitalbyra/<stad>/`.

One command loads it all, idempotently (pages and blocks are replaced per
slug, services/cities upserted per slug):

```bash
uv run python manage.py seed_site
```

On the server, run the same command once from the app dir:

```bash
# as the system user, in BASE_DIR/<slug>/app
git pull --ff-only
../.venv/bin/python manage.py seed_site
```

Run it **once** for the initial seed. After the customer starts editing
through `/manage/` on production, production is the source of truth -
re-running `seed_site` replaces the seeded pages' blocks with the tracked
JSON and discards live edits to them. That is also why seeding is **not**
wired into `deploy.sh`: deploys must be safe to run repeatedly without
clobbering customer content.

## Optional: shipping a full content snapshot

`export_site_data` / `import_site_data` exist for moving a complete content
snapshot (DB rows + referenced media) between environments through git:

```bash
# locally, when your dev DB holds the content you want live
uv run python manage.py export_site_data
git add seed_data && git commit -m "Update site seed data" && git push

# on the server
git pull --ff-only
../.venv/bin/python manage.py import_site_data   # asks for confirmation
```

Notes:

- The export writes `seed_data/site_content.json` + `seed_data/media/…`.
  Neither exists in the repo until you run the export - `import_site_data`
  errors out clearly if the fixture is missing.
- The fixture covers the `website` + `services` content models only (no auth
  users or sessions, so the production superuser is never touched). Cities
  (`areas`) and FAQ are **not** included - they come from `seed_site` or are
  created in `/manage/`.
- `loaddata` preserves primary keys, so re-importing updates the same rows
  (idempotent) - but like `seed_site`, importing over live customer edits
  **replaces** them. Treat it as an initial-seed tool.

## Always back up first

Before importing on a server that already has real content, take a dump:

```bash
./server/backup.sh <slug>
```

A backup you have never restored is not a backup - test your restores.
