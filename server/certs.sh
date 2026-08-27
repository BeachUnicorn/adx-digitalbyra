#!/usr/bin/env bash
#
# certs.sh - issue/renew the Let's Encrypt certificate for a site's domains.
#
#   ./certs.sh <site_slug>
#
# Uses ONE certificate named CERT_NAME (from the site config) covering every
# domain in DOMAINS. DNS for each domain must already point at this server.
#
# Because the cert is addressed by CERT_NAME (not by domain), changing DOMAINS
# later - e.g. dropping beta.jungfru.se and adding jungfru.se + www - and
# re-running this script just updates the SAME cert in place. See README
# "Going from beta to live".

source "$(dirname "$0")/lib.sh"
load_site "${1:-}"

require_cmd sudo

domain_args=()
for d in "${DOMAINS[@]}"; do
    domain_args+=( -d "$d" )
done

log "Requesting certificate '${CERT_NAME}' for: ${DOMAINS[*]}"
# --cert-name pins the storage name; --expand lets the domain set on an existing
# cert of that name change (add/replace domains) without creating a new lineage.
sudo certbot certonly --nginx \
    --cert-name "$CERT_NAME" \
    --expand \
    "${domain_args[@]}"

log "Validating and reloading nginx..."
sudo nginx -t
sudo systemctl reload nginx

log "Certificate '${CERT_NAME}' now covers: ${DOMAINS[*]}"
log "Renewal is handled automatically by certbot's systemd timer."
log "Inspect with: sudo certbot certificates"
