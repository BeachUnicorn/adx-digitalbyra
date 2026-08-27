#!/usr/bin/env bash
#
# lib.sh - shared helpers + single source of truth for all server scripts.
#
# Every script starts with:
#     source "$(dirname "$0")/lib.sh"
#     load_site "<site_slug>"
#
# It loads project.conf (repo-level) then sites.d/<site>.conf (per-site),
# then derives all paths. Nothing is hardcoded in the individual scripts.

set -euo pipefail

SERVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- logging helpers --------------------------------------------------------
log()  { printf '\033[1;32m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

# ---- config loading ---------------------------------------------------------
load_project_conf() {
    local conf="${SERVER_DIR}/project.conf"
    [ -f "$conf" ] || die "missing ${conf} (copy project.conf.example and edit it)"
    # shellcheck source=/dev/null
    source "$conf"
    : "${SYSTEM_USER:?set in project.conf}"
    : "${BASE_DIR:?set in project.conf}"
    DB_HOST="${DB_HOST:-localhost}"
    DB_PORT="${DB_PORT:-5432}"
    GUNICORN_WORKERS="${GUNICORN_WORKERS:-3}"
    GUNICORN_THREADS="${GUNICORN_THREADS:-2}"
    # WSGI är default; en sajt som behöver ASGI (t.ex. MCP-endpointen)
    # sätter dessa i sin sites.d/<slug>.conf. uvicorn måste då finnas i
    # sajtens beroenden.
    GUNICORN_WORKER_CLASS="${GUNICORN_WORKER_CLASS:-gthread}"
    APP_MODULE="${APP_MODULE:-config.wsgi:application}"
}

# List available site slugs (filenames in sites.d/ without .conf).
list_sites() {
    local f
    for f in "${SERVER_DIR}"/sites.d/*.conf; do
        [ -e "$f" ] || continue
        basename "$f" .conf
    done
}

# load_site <slug>: loads project + site config and derives all paths.
load_site() {
    local slug="${1:-}"
    [ -n "$slug" ] || die "usage: load_site <site_slug>  (available: $(list_sites | tr '\n' ' '))"

    load_project_conf

    local site_conf="${SERVER_DIR}/sites.d/${slug}.conf"
    [ -f "$site_conf" ] || die "no site config: ${site_conf}"
    # shellcheck source=/dev/null
    source "$site_conf"

    : "${SITE_SLUG:?set in sites.d/${slug}.conf}"
    : "${DJANGO_SETTINGS_MODULE:?set in sites.d/${slug}.conf}"
    : "${DB_NAME:?set in sites.d/${slug}.conf}"
    [ "${#DOMAINS[@]}" -ge 1 ] || die "DOMAINS must list at least one domain in ${slug}.conf"

    # Git source is always per site: each site names its own repo (and branch).
    : "${GIT_REPO_URL:?set GIT_REPO_URL in sites.d/${slug}.conf}"
    GIT_BRANCH="${GIT_BRANCH:-main}"

    # Certificate name is explicit and stable. Fall back to the slug if a site
    # config predates this setting, so existing sites keep working.
    CERT_NAME="${CERT_NAME:-$SITE_SLUG}"

    # Per-site override of DB_USER falls back to the repo-level one.
    DB_USER="${DB_USER:?set DB_USER in project.conf or the site conf}"

    # Derived paths - the ONLY place these are defined.
    PROJECT_DIR="${BASE_DIR}/${SITE_SLUG}"
    APP_DIR="${PROJECT_DIR}/app"
    VENV_DIR="${PROJECT_DIR}/.venv"
    ENV_FILE="${PROJECT_DIR}/.env"
    STATIC_DIR="${PROJECT_DIR}/collected-staticfiles"
    MEDIA_DIR="${PROJECT_DIR}/user-uploaded-media"
    SOCKET="${PROJECT_DIR}/${SITE_SLUG}.sock"
    SERVICE_NAME="${SITE_SLUG}"            # systemd unit: <slug>.service
    PRIMARY_DOMAIN="${DOMAINS[0]}"

    export DJANGO_SETTINGS_MODULE
}

# Run a manage.py command inside the site's venv, from the app dir.
manage() {
    ( cd "$APP_DIR" && "${VENV_DIR}/bin/python" manage.py "$@" )
}

# ---- config rendering (systemd + nginx) -------------------------------------
# Render the gunicorn unit from the template and (re)install it.
install_service() {
    local unit="/etc/systemd/system/${SERVICE_NAME}.service"
    log "Rendering systemd unit -> ${unit}"
    export SITE_SLUG SYSTEM_USER APP_DIR ENV_FILE VENV_DIR SOCKET \
        GUNICORN_WORKERS GUNICORN_THREADS GUNICORN_WORKER_CLASS APP_MODULE
    # Restrict substitution to OUR vars so systemd's $MAINPID is left literal.
    envsubst '$SITE_SLUG $SYSTEM_USER $APP_DIR $ENV_FILE $VENV_DIR $SOCKET $GUNICORN_WORKERS $GUNICORN_THREADS $GUNICORN_WORKER_CLASS $APP_MODULE' \
        < "${SERVER_DIR}/templates/gunicorn.service.template" \
        | sudo tee "$unit" >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
    sudo systemctl restart "$SERVICE_NAME"
}

# Render the nginx vhost from the template and (re)install + reload it.
install_nginx() {
    local conf="/etc/nginx/sites-available/${SITE_SLUG}.conf"
    local link="/etc/nginx/sites-enabled/${SITE_SLUG}.conf"

    # Build domain lists for the template.
    ALL_DOMAINS="${DOMAINS[*]}"
    local secondary=("${DOMAINS[@]:1}")
    SECONDARY_DOMAINS="${secondary[*]:-}"

    # The "redirect non-primary HTTPS domains to primary" server block only
    # makes sense when there ARE secondary domains. With a single domain (e.g.
    # the beta subdomain) we omit it entirely - an empty server_name is invalid.
    if [ -n "$SECONDARY_DOMAINS" ]; then
        SECONDARY_REDIRECT_BLOCK="$(cat <<NGINX

# Redirect non-primary HTTPS domains to the primary domain.
server {
    listen 443 ssl;
    server_name ${SECONDARY_DOMAINS};

    ssl_certificate /etc/letsencrypt/live/${CERT_NAME}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${CERT_NAME}/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    return 301 https://${PRIMARY_DOMAIN}\$request_uri;
}
NGINX
)"
    else
        SECONDARY_REDIRECT_BLOCK=""
    fi

    log "Rendering nginx vhost -> ${conf}"
    export SITE_SLUG PRIMARY_DOMAIN ALL_DOMAINS SECONDARY_DOMAINS \
        CERT_NAME SOCKET STATIC_DIR MEDIA_DIR SECONDARY_REDIRECT_BLOCK
    # Restrict substitution to OUR vars so nginx runtime vars ($host, $scheme,
    # $request_uri, $remote_addr, $proxy_add_x_forwarded_for) stay literal.
    envsubst '$SITE_SLUG $PRIMARY_DOMAIN $ALL_DOMAINS $CERT_NAME $SOCKET $STATIC_DIR $MEDIA_DIR $SECONDARY_REDIRECT_BLOCK' \
        < "${SERVER_DIR}/templates/nginx.conf.template" \
        | sudo tee "$conf" >/dev/null

    [ -L "$link" ] || sudo ln -s "$conf" "$link"

    # If certs aren't issued yet, nginx -t will fail on the ssl_certificate
    # lines. That's expected on first provision; certs.sh fixes it.
    if sudo nginx -t 2>/dev/null; then
        sudo systemctl reload nginx
        log "nginx reloaded"
    else
        warn "nginx config not valid yet (likely missing certs). Run certs.sh, it will reload."
    fi
}
