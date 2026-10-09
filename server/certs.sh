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

# Länkvärdarna (apps/utskick, README C.5): en EGEN certifikatlinje, så att ett
# misslyckat HTTP-01 på k.adx.se eller klick.adx.se aldrig stoppar sajtens
# eget certifikat (det är redan klart här ovanför). Efter första gången: kör
# nginx-only.sh <slug> så att 443-blocket för länkvärdarna ritas.
if declare -p LINK_DOMAINS >/dev/null 2>&1 && [ "${#LINK_DOMAINS[@]}" -gt 0 ]; then
    link_cert="${LINK_CERT_NAME:-${SITE_SLUG}-links}"
    link_args=()
    for d in "${LINK_DOMAINS[@]}"; do
        link_args+=( -d "$d" )
    done
    log "Requesting certificate '${link_cert}' for: ${LINK_DOMAINS[*]}"
    if ! sudo certbot certonly --nginx --cert-name "$link_cert" --expand "${link_args[@]}"; then
        warn "Certifikatet '${link_cert}' kunde inte utfärdas. Sajtens eget certifikat påverkas inte."
    fi
fi

log "Validating and reloading nginx..."
sudo nginx -t
sudo systemctl reload nginx

log "Certificate '${CERT_NAME}' now covers: ${DOMAINS[*]}"
log "Renewal is handled automatically by certbot's systemd timer."
log "Inspect with: sudo certbot certificates"
