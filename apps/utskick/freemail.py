"""
Gratis e-post och internetleverantörernas adresser (README H.5).

En adress på någon av de här domänerna är en privatpersons, även om den
står på ett företag. Statusen company (e-post till företag utan samtycke,
marknadsföringslagen 20 §) härleds därför aldrig för dem, och de får inte
heller vara en kunds egen avsändardomän (S3).
"""

FREEMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "hotmail.com",
        "hotmail.se",
        "outlook.com",
        "outlook.se",
        "live.se",
        "live.com",
        "msn.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "yahoo.com",
        "yahoo.se",
        "telia.com",
        "telia.se",
        "comhem.se",
        "bredband.net",
        "bahnhof.se",
        "tele2.se",
        "spray.se",
        "passagen.se",
        "glocalnet.net",
        "home.se",
        "protonmail.com",
        "proton.me",
    }
)


def is_freemail(value):
    """True för en adress eller domän hos en gratistjänst eller leverantör,
    också på en underdomän (mail.telia.com)."""
    text = str(value or "").strip().lower().rstrip(".")
    domain = text.rsplit("@", 1)[-1]
    if not domain:
        return False
    parts = domain.split(".")
    return any(".".join(parts[i:]) in FREEMAIL_DOMAINS for i in range(len(parts) - 1))
