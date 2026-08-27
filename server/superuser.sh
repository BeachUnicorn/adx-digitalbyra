#!/usr/bin/env bash
#
# superuser.sh - create a Django admin user for a site (non-interactive-safe).
#
#   ./superuser.sh <site_slug>
#
# Uses Django's built-in createsuperuser. Username/email are prompted (or pass
# via DJANGO_SUPERUSER_USERNAME / DJANGO_SUPERUSER_EMAIL / DJANGO_SUPERUSER_PASSWORD).

source "$(dirname "$0")/lib.sh"
load_site "${1:-}"

# Load the site's env so DATABASE_URL etc. are present.
set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a

if [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
    log "Creating superuser non-interactively..."
    manage createsuperuser --no-input || warn "user may already exist"
else
    log "Creating superuser (interactive)..."
    manage createsuperuser
fi
