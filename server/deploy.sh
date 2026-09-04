#!/usr/bin/env bash
#
# deploy.sh - update a running site to the latest code. Health-checked.
#
#   sudo ./deploy.sh <site_slug>            # deploy one site
#   sudo ./deploy.sh <site_slug> --seed     # ...and run the additive seeds
#   sudo ./deploy.sh --all                  # deploy every site in this repo
#
# Körs som root (systemctl kräver det); git, uv och manage.py körs som
# sajtens systemanvändare via run_as_app (lib.sh). Steg:
#   pull -> uv sync (locked) -> migrate -> collectstatic [-> seeds]
#   -> daemon-reload + graceful reload -> service aktiv OCH /healthz 200,
#   annars rollback till förra commit och fel.

source "$(dirname "$0")/lib.sh"

SEED=0
SLUG=""
for arg in "$@"; do
    case "$arg" in
        --seed) SEED=1 ;;
        --all)  SLUG="--all" ;;
        *)      SLUG="$arg" ;;
    esac
done

rollback() {
    local before="$1" reason="$2"
    warn "${reason}; rullar tillbaka till ${before}"
    run_as_app git reset --hard "$before" --quiet
    run_as_app uv sync --no-dev --frozen
    manage migrate --no-input || true
    systemctl restart "$SERVICE_NAME"
    die "deploy misslyckades för ${SITE_SLUG} (${reason}) - tillbakarullad"
}

deploy_one() {
    load_site "$1"
    require_cmd git
    [ "$(id -u)" -eq 0 ] || die "kör som root: sudo $0 $*"

    log "=== Deploy ${SITE_SLUG} ==="

    local before after
    before="$(run_as_app git rev-parse HEAD)"

    run_as_app git fetch --quiet origin "$GIT_BRANCH"
    run_as_app git pull --ff-only --quiet
    after="$(run_as_app git rev-parse HEAD)"
    if [ "$before" = "$after" ]; then
        log "Kod: ${after:0:8}"
    else
        log "Kod: ${before:0:8} -> ${after:0:8}"
        run_as_app git --no-pager log --oneline "${before}..${after}" | sed 's/^/    /'
    fi

    log "Beroenden (uv sync, låst)..."
    run_as_app uv sync --no-dev --frozen

    log "Migrerar..."
    manage migrate --no-input

    log "Samlar static..."
    manage collectstatic --no-input --verbosity 0

    if [ "$SEED" -eq 1 ]; then
        # Additiva seeds: skapar det som saknas, rör aldrig kundens ändringar.
        for spec in "seed_sokordssidor" \
                    "seed_sokordssidor --file seed_data/adx_tjanstesidor.json" \
                    "seed_produkter"; do
            log "Seed: ${spec}"
            # shellcheck disable=SC2086
            manage $spec
        done
    fi

    log "Laddar om ${SERVICE_NAME}..."
    systemctl daemon-reload
    systemctl reload "$SERVICE_NAME" || systemctl restart "$SERVICE_NAME"

    # --- hälsogrind -----------------------------------------------------------
    sleep 3
    systemctl is-active --quiet "$SERVICE_NAME" || rollback "$before" "tjänsten nere"

    if curl -fsS --max-time 10 --unix-socket "$SOCKET" \
        -H "Host: ${PRIMARY_DOMAIN}" -H "X-Forwarded-Proto: https" \
        "http://localhost/healthz/" >/dev/null; then
        log "${SITE_SLUG} frisk på ${after:0:8} ✔"
    else
        rollback "$before" "healthz svarade inte 200"
    fi
}

if [ "$SLUG" = "--all" ]; then
    for slug in $(list_sites); do
        deploy_one "$slug"
    done
    log "Alla sajter deployade."
else
    deploy_one "$SLUG"
fi
