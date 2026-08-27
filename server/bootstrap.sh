#!/usr/bin/env bash
#
# bootstrap.sh - prepare a fresh Ubuntu server. Run ONCE per machine, as root
# (or with sudo). Installs system packages, creates the system user, installs
# uv for that user. Safe to re-run.
#
#   sudo ./bootstrap.sh
#
# After this, run provision-site.sh for each site.

source "$(dirname "$0")/lib.sh"
load_project_conf

[ "$(id -u)" -eq 0 ] || die "run as root (sudo ./bootstrap.sh)"

log "Installing system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y \
    nginx \
    postgresql postgresql-contrib \
    certbot python3-certbot-nginx \
    git curl

log "Ensuring system user '${SYSTEM_USER}' exists..."
if ! id "$SYSTEM_USER" >/dev/null 2>&1; then
    adduser --disabled-password --gecos "" "$SYSTEM_USER"
    log "Created ${SYSTEM_USER}"
else
    log "${SYSTEM_USER} already exists"
fi

log "Creating sites base dir ${BASE_DIR}..."
install -d -o "$SYSTEM_USER" -g "$SYSTEM_USER" "$BASE_DIR"

log "Installing uv for ${SYSTEM_USER}..."
if ! sudo -u "$SYSTEM_USER" bash -lc 'command -v uv >/dev/null 2>&1'; then
    sudo -u "$SYSTEM_USER" bash -lc 'curl -LsSf https://astral.sh/uv/install.sh | sh'
    log "uv installed"
else
    log "uv already installed"
fi

log "Enabling services..."
systemctl enable --now nginx
systemctl enable --now postgresql

log "Bootstrap complete. Next: ./provision-site.sh <site_slug>"
