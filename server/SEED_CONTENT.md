# Seeding site content to the server

Your **content** (pages, blocks, menus, services, site settings) and your
**uploaded media** do not travel with `git pull` on their own:

- The database lives in Postgres, not in git.
- `user-uploaded-media/` is `.gitignore`d.

To get the content you built locally onto the production server, we ship it
through git in a tracked `seed_data/` folder, using two management commands.

## What gets exported

`export_site_data` writes everything under `<repo>/seed_data/`:

- `site_content.json` - a Django fixture of the **content models only**
  (`website` + `services`). Auth users, sessions and admin logs are
  deliberately **excluded**, so importing never touches the production
  superuser or anyone's login.
- `media/…` - a copy of every file referenced by a `MediaFile` row
  (e.g. `media/element.jpg`), so the image bytes ride along with git.

`element.jpg` at the repo root is the *source* placeholder; the media-library
copy is a `MediaFile` row pointing at `media/element.jpg` under `MEDIA_ROOT`.
The exporter copies that resolved file, so it lands correctly on the server.

## 1. Locally - export and push

Run this whenever your local DB holds the content you want live:

```bash
uv run python manage.py export_site_data
git add seed_data
git commit -m "Update site seed data"
git push
```

## 2. On the server - pull and import

After the normal deploy (or any time), from the site's app dir:

```bash
# as the system user, in BASE_DIR/<slug>/app
git pull --ff-only
../.venv/bin/python manage.py import_site_data            # asks for confirmation
# or, non-interactive:
../.venv/bin/python manage.py import_site_data --noinput
```

`import_site_data`:

1. Copies the bundled `seed_data/media/…` files into the server's
   `MEDIA_ROOT` (`user-uploaded-media/`). Files are **copied, never deleted**.
2. Runs `loaddata` on `site_content.json`.

The fixture preserves primary keys, so `loaddata` is an **upsert**: re-running
updates the same rows instead of creating duplicates.

> Tip: if your venv isn't at `../.venv`, use the project's `manage()` helper
> path. The deploy scripts run manage.py as
> `${VENV_DIR}/bin/python manage.py …` from `${APP_DIR}`.

## ⚠️ Important: this REPLACES live content

`import_site_data` overwrites the content rows with whatever is in the fixture.
That is exactly what you want for an **initial** production seed.

But once the customer starts editing their site through `/manage/` on
production, re-importing will **discard those live edits**. After the first
seed, treat production as the source of truth and avoid re-importing unless you
intend to overwrite it.

This is why importing is **not** wired into `deploy.sh` - deploys must be safe
to run repeatedly without clobbering customer content.

## Always back up first

Before importing on a server that already has real content, take a dump:

```bash
./server/backup.sh <slug>
```

A backup you have never restored is not a backup - test your restores.
