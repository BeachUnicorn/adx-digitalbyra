#!/usr/bin/env bash
#
# nginx-only.sh - rita om och ladda om nginx för en sajt, inget annat.
#
#   ./nginx-only.sh <site_slug>
#
# Körs av den som har sudo (ubuntu), inte provision-site.sh: den kör git och
# uv direkt och skulle lämna filer ägda av fel användare. deploy.sh rör
# aldrig nginx. Används för länkvärdarna (apps/utskick, README C.5 och J S2
# steg 4): först en gång före certs.sh (bara port 80 för k.adx.se och
# klick.adx.se ritas, eftersom certifikatet inte finns), sedan igen efter
# certs.sh (då ritas 443-blocket).

source "$(dirname "$0")/lib.sh"
load_site "${1:-}"

require_cmd sudo
require_cmd envsubst

install_nginx
