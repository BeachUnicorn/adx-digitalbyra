"""
Botskydd i lager för publika formulär - utan CAPTCHA, utan tredje part.
(Mönsterkatalogen §6: oberoende osynliga lager, servern håller facit,
fusk besvaras med TYST fejkad framgång så boten aldrig lär sig vilket
lager som tog den.)

Tre lager:
1. Honeypot: fältet ``bc_website`` är utflyttat ur skärmen (aldrig
   display:none - en del botar hoppar över dolda fält). Människor ser
   det aldrig; en bot som fyller alla fält fastnar.
2. Signerad tidsstämpel: ``bc_time`` utfärdas när formuläret renderas.
   En submit snabbare än MIN_SECONDS är ingen människa; äldre än
   MAX_AGE är en replay.
3. JS-bevis: ``bc_proof`` är tomt i HTML:en och fylls först av site.js
   vid verklig interaktion (pointerdown/keydown) med värdet ur formens
   data-attribut. En bot som POST:ar rå HTML lämnar det tomt.

Användning i mall::

    <form method="post" {% botcheck_attr %}> ... {% botcheck_fields %} ...

och i vyn::

    if not botcheck_passes(request):
        return <exakt samma svar som vid framgång, utan sidoeffekter>

Varje fällt lager loggas till loggern "security" så driften ser vågen.
"""

import logging
import time

from django.core import signing

logger = logging.getLogger("security")

MIN_SECONDS = 3
MAX_AGE_SECONDS = 24 * 3600
_SALT = "botcheck"


def issue_token():
    """Signerad tidsstämpel - renderas både som fält och som data-attribut."""
    return signing.dumps({"t": int(time.time())}, salt=_SALT)


def botcheck_passes(request):
    """True om alla tre lagren är nöjda. Loggar vilket lager som föll."""
    if request.POST.get("bc_website", ""):
        logger.warning("botcheck: honeypot ifylld (path=%s)", request.path)
        return False

    token = request.POST.get("bc_time", "")
    try:
        payload = signing.loads(token, salt=_SALT, max_age=MAX_AGE_SECONDS)
    except signing.BadSignature:
        logger.warning("botcheck: ogiltig/utgången tidsstämpel (path=%s)", request.path)
        return False
    if time.time() - payload.get("t", 0) < MIN_SECONDS:
        logger.warning("botcheck: för snabb submit (path=%s)", request.path)
        return False

    # Beviset är en EGEN signerad token (attributet och fälten renderas av
    # två olika taggar och kan inte dela instans) - det som bevisas är att
    # JS körde och kopierade ett äkta, färskt värde, inte att det är samma.
    try:
        signing.loads(request.POST.get("bc_proof", ""), salt=_SALT, max_age=MAX_AGE_SECONDS)
    except signing.BadSignature:
        logger.warning("botcheck: JS-bevis saknas eller ogiltigt (path=%s)", request.path)
        return False

    return True
