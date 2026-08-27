#!/usr/bin/env bash
#
# provision-site.sh - stand up ONE site end to end. Idempotent: safe to re-run.
#
#   ./provision-site.sh <site_slug>
#
# Steps:
#   1. Create the directory layout (app / static / media)
#   2. Clone or update the repo into app/
#   3. Create .env from .env.example if it doesn't exist (then you edit it)
#   4. Sync the locked venv with uv
#   5. migrate + collectstatic
#   6. Render & install the gunicorn systemd unit and nginx vhost
#   7. Remind you to issue certs (certs.sh) and create a superuser (superuser.sh)
#
# Run as the SYSTEM_USER (it uses sudo only where root is required).

source "$(dirname "$0")/lib.sh"
load_site "${1:-}"

require_cmd git
require_cmd envsubst

# 1) Directory layout -------------------------------------------------------
log "Creating directory layout under ${PROJECT_DIR}..."
mkdir -p "$APP_DIR" "$STATIC_DIR" "$MEDIA_DIR"

# 2) Clone or update --------------------------------------------------------
if [ -d "${APP_DIR}/.git" ]; then
    log "Repo present; fetching ${GIT_BRANCH}..."
    git -C "$APP_DIR" fetch --quiet origin "$GIT_BRANCH"
    git -C "$APP_DIR" checkout --quiet "$GIT_BRANCH"
    git -C "$APP_DIR" pull --ff-only --quiet
else
    log "Cloning ${GIT_REPO_URL} (branch ${GIT_BRANCH})..."
    git clone --branch "$GIT_BRANCH" "$GIT_REPO_URL" "$APP_DIR"
fi

# 3) .env (lives at project level, outside the git checkout) ----------------
if [ ! -f "$ENV_FILE" ]; then
    log "Creating ${ENV_FILE} from .env.example."
    cp "${APP_DIR}/.env.example" "$ENV_FILE"
    # Pre-fill what we already know from the site config.
    {
        echo ""
        echo "# --- filled by provision-site.sh ---"
        echo "DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE}"
        echo "SITE_SLUG=${SITE_SLUG}"
        echo "ALLOWED_HOSTS=${DOMAINS[*]}"
    } >> "$ENV_FILE"
    chmod 600 "$ENV_FILE"

    # Stop here on first run: migrate/collectstatic below need a real
    # DATABASE_URL and SECRET_KEY. Editing now, then re-running, is clean and
    # idempotent (this block is skipped once .env exists).
    warn "First run: created ${ENV_FILE} with placeholder values."
    warn "Edit it now, then run this script again to finish:"
    warn "    \$EDITOR ${ENV_FILE}        # set SECRET_KEY, DATABASE_URL, SENTRY_DSN"
    warn "    ./provision-site.sh ${SITE_SLUG}"
    warn "DATABASE_URL must match the role/db/password you set with ./postgres.sh ${SITE_SLUG}."
    exit 0
else
    log ".env present; continuing."
fi

# 4) Venv via uv (locked, reproducible) -------------------------------------
log "Syncing venv with uv (production deps only)..."
( cd "$APP_DIR" && uv sync --no-dev --frozen )
# uv creates .venv inside the app dir; expose it at the path the unit expects.
if [ ! -e "$VENV_DIR" ]; then
    ln -s "${APP_DIR}/.venv" "$VENV_DIR"
fi

# 5) Django ------------------------------------------------------------------
log "Running migrations..."
manage migrate --no-input
log "Collecting static files..."
manage collectstatic --no-input

# 6) systemd + nginx (rendered from templates) ------------------------------
install_service
install_nginx

log "Provision complete for '${SITE_SLUG}'."
log "Next:"
log "  1. Point DNS (Route 53) for: ${DOMAINS[*]}  ->  this server"
log "  2. ./certs.sh ${SITE_SLUG}        # issue Let's Encrypt certs"
log "  3. ./superuser.sh ${SITE_SLUG}    # create the Django admin user"
