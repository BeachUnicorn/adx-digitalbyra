#!/usr/bin/env bash
#
# postgres.sh - create the Postgres role and per-site database. Idempotent.
#
#   ./postgres.sh <site_slug>
#
# The DB password is taken from $DB_PASSWORD if set, otherwise prompted.
# It is never written into any tracked file.

source "$(dirname "$0")/lib.sh"
load_site "${1:-}"

require_cmd sudo

if [ -z "${DB_PASSWORD:-}" ]; then
    read -rsp "Postgres password for role '${DB_USER}': " DB_PASSWORD
    echo
fi
[ -n "$DB_PASSWORD" ] || die "empty password"

log "Creating/updating role '${DB_USER}'..."
sudo -u postgres psql -v ON_ERROR_STOP=1 \
    -v role="$DB_USER" -v pw="$DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'role', :'pw')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'role')
\gexec
-- Always (re)apply the password and sane Django defaults.
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'role', :'pw') \gexec
SELECT format('ALTER ROLE %I SET client_encoding TO ''utf8''', :'role') \gexec
SELECT format('ALTER ROLE %I SET default_transaction_isolation TO ''read committed''', :'role') \gexec
SELECT format('ALTER ROLE %I SET timezone TO ''UTC''', :'role') \gexec
SQL

log "Creating database '${DB_NAME}' (owner ${DB_USER}) if missing..."
sudo -u postgres psql -v ON_ERROR_STOP=1 \
    -v db="$DB_NAME" -v owner="$DB_USER" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER %I', :'db', :'owner')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db')
\gexec
SQL

log "Postgres ready: role=${DB_USER} db=${DB_NAME}"
