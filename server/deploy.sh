#!/usr/bin/env bash
#
# deploy.sh - update a running site to the latest code. Health-checked.
#
#   ./deploy.sh <site_slug>           # deploy one site
#   ./deploy.sh --all                 # deploy every site in this repo
#
# What it does (per site):
#   pull -> uv sync (locked) -> migrate -> collectstatic -> graceful reload
#   -> verify the service is active AND /healthz returns 200, else fail loudly.

source "$(dirname "$0")/lib.sh"

deploy_one() {
    load_site "$1"
    require_cmd git

    log "=== Deploying ${SITE_SLUG} ==="

    local before
    before="$(git -C "$APP_DIR" rev-parse HEAD)"

    git -C "$APP_DIR" fetch --quiet origin "$GIT_BRANCH"
    git -C "$APP_DIR" pull --ff-only

    log "Syncing dependencies (locked)..."
    ( cd "$APP_DIR" && uv sync --no-dev --frozen )

    log "Migrating..."
    manage migrate --no-input

    log "Collecting static..."
    manage collectstatic --no-input

    # Graceful reload (gunicorn catches HUP). Restart only if reload fails.
    log "Reloading ${SERVICE_NAME}..."
    sudo systemctl reload "$SERVICE_NAME" || sudo systemctl restart "$SERVICE_NAME"

    # --- health gate --------------------------------------------------------
    sleep 2
    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
        warn "service not active; rolling back to ${before}"
        git -C "$APP_DIR" reset --hard "$before" --quiet
        ( cd "$APP_DIR" && uv sync --no-dev --frozen )
        sudo systemctl restart "$SERVICE_NAME"
        die "deploy failed for ${SITE_SLUG} (service down) - rolled back"
    fi

    if curl -fsS --max-time 10 --unix-socket "$SOCKET" \
        -H "Host: ${PRIMARY_DOMAIN}" "http://localhost/healthz/" >/dev/null; then
        log "${SITE_SLUG} healthy ✔"
    else
        warn "healthz failed; rolling back to ${before}"
        git -C "$APP_DIR" reset --hard "$before" --quiet
        ( cd "$APP_DIR" && uv sync --no-dev --frozen )
        manage migrate --no-input || true
        sudo systemctl restart "$SERVICE_NAME"
        die "deploy failed for ${SITE_SLUG} (healthz) - rolled back"
    fi
}

if [ "${1:-}" = "--all" ]; then
    for slug in $(list_sites); do
        deploy_one "$slug"
    done
    log "All sites deployed."
else
    deploy_one "${1:-}"
fi
