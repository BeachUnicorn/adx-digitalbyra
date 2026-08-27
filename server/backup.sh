#!/usr/bin/env bash
#
# backup.sh - pg_dump a site's database to S3, with local retention as fallback.
#
#   ./backup.sh <site_slug>
#
# Intended to run from cron, e.g. (per site):
#   15 2 * * *  /home/djangouser/sites/jungfru/app/server/backup.sh jungfru >> /var/log/backup_jungfru.log 2>&1
#
# Requirements: pg_dump, and (optional) awscli configured for this account's
# S3 bucket. Set BACKUP_S3_BUCKET in the environment or project.conf.
#
# IMPORTANT: a backup you have never restored is not a backup. Test restores.

source "$(dirname "$0")/lib.sh"
load_site "${1:-}"
require_cmd pg_dump

# Load .env to get DATABASE_URL (and optionally BACKUP_S3_BUCKET).
set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a

BACKUP_S3_BUCKET="${BACKUP_S3_BUCKET:-}"
LOCAL_DIR="${PROJECT_DIR}/backups"
mkdir -p "$LOCAL_DIR"

stamp="$(date +%Y%m%d-%H%M%S)"
outfile="${LOCAL_DIR}/${SITE_SLUG}_${stamp}.sql.gz"

log "Dumping ${DB_NAME} -> ${outfile}"
# Prefer DATABASE_URL if present; fall back to local socket auth.
if [ -n "${DATABASE_URL:-}" ]; then
    pg_dump "$DATABASE_URL" | gzip > "$outfile"
else
    sudo -u postgres pg_dump "$DB_NAME" | gzip > "$outfile"
fi

if [ -n "$BACKUP_S3_BUCKET" ] && command -v aws >/dev/null 2>&1; then
    log "Uploading to s3://${BACKUP_S3_BUCKET}/${SITE_SLUG}/"
    aws s3 cp "$outfile" "s3://${BACKUP_S3_BUCKET}/${SITE_SLUG}/${SITE_SLUG}_${stamp}.sql.gz"
else
    warn "No S3 bucket/awscli; keeping local backup only."
fi

# Local retention: keep the 14 most recent dumps.
log "Pruning local backups (keep 14)..."
ls -1t "${LOCAL_DIR}/${SITE_SLUG}_"*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm -f

log "Backup done."
