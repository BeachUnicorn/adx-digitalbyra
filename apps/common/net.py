"""
Besökarens IP-adress bakom nginx.

nginx (server/templates/nginx.conf.template) sätter två huvuden på varje
anrop till appen:

    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

X-Real-IP skrivs över av nginx och är adressen som faktiskt anslöt.
X-Forwarded-For däremot BYGGS PÅ: nginx lägger till den riktiga adressen
sist i det klienten själv skickade. Den första posten är alltså vad som
helst en besökare hittar på, och får aldrig styra en spärr eller sparas som
"kundens IP". Därför, i den här ordningen:

    1. X-Real-IP
    2. den SISTA posten i X-Forwarded-For (den nginx lade dit)
    3. REMOTE_ADDR

Bara giltiga adresser släpps igenom. En ogiltig sista post faller tillbaka
på REMOTE_ADDR, aldrig på en tidigare post.
"""

import ipaddress


def _valid(value):
    try:
        return str(ipaddress.ip_address((value or "").strip()))
    except ValueError:
        return None


def client_ip(request):
    """Besökarens IP som text, eller None om ingen giltig adress finns."""
    meta = request.META
    real = _valid(meta.get("HTTP_X_REAL_IP", ""))
    if real:
        return real
    forwarded = [part for part in meta.get("HTTP_X_FORWARDED_FOR", "").split(",") if part.strip()]
    if forwarded:
        last = _valid(forwarded[-1])
        if last:
            return last
    return _valid(meta.get("REMOTE_ADDR", ""))
