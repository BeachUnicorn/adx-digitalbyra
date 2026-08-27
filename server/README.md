# Server tooling

Deployment tooling for this customer's site(s). Everything is driven by config
files, so the scripts are **identical across customers** - only the config
differs.

## Mental model

```
project.conf            # machine-wide defaults: system user, base dir, DB role
sites.d/<slug>.conf     # one file PER SITE: git repo, slug, cert name, domains, DB
lib.sh                  # sources the config, derives all paths, shared helpers
```

A **site** is one git repo + one systemd service + one nginx vhost + one
Postgres database + one checkout under `BASE_DIR/<slug>/`. Each site is fully
described by one file in `sites.d/`, and that file names its own `GIT_REPO_URL`.
`project.conf` holds only machine-wide defaults (system user, base dir, DB role,
gunicorn tuning) shared by every site on the box.

Nothing is hardcoded in the scripts - change a path or user once in config.

### How this covers your cases

Because the repo is named **per site**, every layout is just "how many files in
`sites.d/` and which repo each points at":

| Case | `sites.d/` | Repos |
|---|---|---|
| **One site** (csauto-style) | one file | its own repo |
| **Many sites, separate repos, one box** | one file per site | each file a **different** `GIT_REPO_URL` |
| **Many sites, shared repo** (bdgroup-style, 6 sites) | one file per site | all point at the **same** `GIT_REPO_URL`, differ by `DJANGO_SETTINGS_MODULE` |
| **Many sites, separate boxes** (per-AWS-account model) | one file per box | each box's site file names its repo |

The "many sites, separate repos, one box" case lays out like this:

```
server/
├─ project.conf                 # system user, base dir, DB defaults (no git here)
└─ sites.d/
   ├─ alpha.conf                # GIT_REPO_URL=git@github.com:you/alpha.git
   ├─ bravo.conf                # GIT_REPO_URL=git@github.com:you/bravo.git
   └─ charlie.conf              # GIT_REPO_URL=git@github.com:you/charlie.git
```

On disk each site gets its own isolated checkout, venv, DB and service:

```
/home/djangouser/sites/
├─ alpha/    { app (git: alpha repo),   .venv, .env, *.sock }
├─ bravo/    { app (git: bravo repo),   .venv, .env, *.sock }
└─ charlie/  { app (git: charlie repo), .venv, .env, *.sock }
```

```bash
./provision-site.sh alpha     # clones alpha's repo
./provision-site.sh bravo     # clones bravo's repo
./deploy.sh --all             # deploys each site from ITS OWN repo
```

> One checkout acts as the "control checkout" that holds the `sites.d/` files;
> each site on disk pulls from whatever repo its own config names.

## Scripts

| Script | Scope | What it does |
|---|---|---|
| `bootstrap.sh` | once per server | Installs nginx, postgres, certbot, uv; creates the system user. Run as root. |
| `postgres.sh <slug>` | once per site | Creates the Postgres role + database. Idempotent. |
| `provision-site.sh <slug>` | once per site | Dirs → clone → `.env` → `uv sync` → migrate → static → systemd + nginx. Idempotent. |
| `certs.sh <slug>` | per site | Issues/renews the Let's Encrypt cert for all the site's domains. |
| `superuser.sh <slug>` | per site | Creates the Django admin user. |
| `deploy.sh <slug>` / `--all` | every update | Pull → `uv sync` → migrate → static → graceful reload → **health check + auto-rollback**. |
| `backup.sh <slug>` | cron | `pg_dump` → S3 (+ local retention). |

## First-time setup of a new server

```bash
# 0. On the server, get the repo (so you have the scripts + templates):
git clone git@github.com:youraccount/jungfru.git /tmp/bootstrap-checkout
cd /tmp/bootstrap-checkout/server

# 1. Configure the box defaults + this site:
cp project.conf.example project.conf
$EDITOR project.conf                 # system user, base dir, DB role (no git here)
$EDITOR sites.d/jungfru.conf         # GIT_REPO_URL, slug, CERT_NAME, domains, DB
                                     # (start with DOMAINS=("beta.<domain>") - see below)

# 2. Prepare the machine (root):
sudo ./bootstrap.sh

# 3. Per site: database, then provision:
./postgres.sh jungfru                # prompts for the DB password
./provision-site.sh jungfru
$EDITOR /home/djangouser/sites/jungfru/.env   # SECRET_KEY, DATABASE_URL, SENTRY_DSN, ALLOWED_HOSTS

# 4. Point Route 53 at this server, then issue certs:
./certs.sh jungfru

# 5. Admin user:
./superuser.sh jungfru
```

For more sites on this box, add another `sites.d/<slug>.conf` (with its own
`GIT_REPO_URL`) and repeat steps 3–5.

While the customer's old site is still live you typically start on a beta
subdomain and switch at launch - see "Going from beta to live" below.

## Day-to-day deploys

```bash
./deploy.sh jungfru     # one site
./deploy.sh --all       # every site in this repo
```

`deploy.sh` records the current commit, applies the update, then verifies the
service is active and `/healthz/` returns 200. If either check fails it resets
to the previous commit, re-syncs, restarts, and exits non-zero. No silent
broken deploys.

## Certificate naming

Each site sets `CERT_NAME` in its `sites.d/<slug>.conf`. This is the name you
choose for the Let's Encrypt certificate; it becomes:

- the certbot `--cert-name`, and
- the folder under `/etc/letsencrypt/live/<CERT_NAME>/` that nginx reads.

**Keep `CERT_NAME` stable and domain-agnostic** (the slug is a good default).
The nginx template points the `ssl_certificate` paths at `CERT_NAME`, *not* at a
domain - so the domain set can change without ever touching the cert path. That
is what makes the beta → live switch below painless.

## Going from beta to live

While building, the customer's old site is usually still on their real domain,
so you serve on a subdomain. At launch you move to the real domains and drop the
beta one. With this tooling that's a config edit plus two commands.

**During development** - `sites.d/jungfru.conf`:

```bash
CERT_NAME="jungfru"
DOMAINS=("beta.jungfru.se")
```

Point `beta.jungfru.se` at the server in Route 53, then:

```bash
./provision-site.sh jungfru   # renders nginx for beta + serves the app
./certs.sh jungfru            # issues cert "jungfru" covering beta.jungfru.se
```

Also set `ALLOWED_HOSTS=beta.jungfru.se` in the site's `.env`.

**At launch** - point `jungfru.se` and `www.jungfru.se` at the server in Route
53, edit the same file:

```bash
CERT_NAME="jungfru"                          # unchanged
DOMAINS=("jungfru.se" "www.jungfru.se")      # beta removed, real domains added
```

Update `ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS` in `.env` to the real domains, then:

```bash
./certs.sh jungfru            # see "What certbot does" below
./provision-site.sh jungfru   # re-renders nginx for the new domains
```

### What certbot does when you remove a domain

This is the part that's easy to be unsure about, so concretely:

- `certs.sh` always runs `certbot certonly --cert-name "$CERT_NAME" --expand -d <each domain>`.
- Because `--cert-name` stays `jungfru`, certbot updates the **same** certificate
  ("lineage") in place instead of creating a new one. The files under
  `/etc/letsencrypt/live/jungfru/` are replaced, so nginx needs no path change.
- `--expand` tells certbot it's fine that the domain list changed.
- A **removed** domain (beta) simply isn't on the new cert. Certbot does **not**
  need the old domain to still resolve in DNS to drop it - it only validates the
  domains you're *requesting now*. The old beta domain just stops being covered;
  nothing errors out.
- The previous certificate version is kept under
  `/etc/letsencrypt/archive/jungfru/` (harmless history). The automatic renewal
  timer renews only the current domain set going forward, so you won't get
  renewal failures for the dropped beta domain.

In short: removing the beta domain is safe. Re-run `certs.sh` then
`provision-site.sh`, and the beta name is gone from both nginx and the cert.

If you ever want to delete a cert entirely (e.g. retiring a site):

```bash
sudo certbot delete --cert-name jungfru
```

> DNS note: don't delete the beta DNS record until after you've re-issued the
> cert and confirmed the live site works, in case you need to fall back. Once
> live is confirmed, removing the beta record in Route 53 is all that's left.

## Why this differs from the old scripts

- **No hardcoded paths/users/sites** scattered across files - one config source.
- **`set -euo pipefail`** everywhere (the old `set -e` missed unset vars and
  pipe failures).
- **Templates + `envsubst`** instead of both committing *and* generating config
  (which had drifted apart).
- **`uv sync --frozen`** against a lock file instead of `pip install -U`, so
  installs are deterministic and upgrades are deliberate.
- **Graceful `reload`** instead of `restart`, and nginx is only touched when its
  own config changes.
- **A real health check with rollback** at the end of every deploy.
- **A real backup script** (the old one was only a comment in an example file).
- **`reset_migrations.sh` is gone** from server tooling - it was a foot-gun.
