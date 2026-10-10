"""
Byråns leverantörer vid namn: de syns inte för kunder, mottagare eller
allmänheten (Giovanni 2026-10-10: "What providers I am using is private.
Also same with AWS. Unless absolutely necessary, you DO NOT just hand away
information about my infrastructure partners and choices.").

I texten heter de "sms-tjänsten", "e-posttjänsten", "vår driftleverantör",
"molntjänsten" och så vidare.

    names_in(text)               namnen i en text, ["46elks", "aws", ...]
    names_in(text, strict=True)  också korta namn som är något annat i
                                 vanlig text ("Audi S3", RDS): för kod och
                                 byråns egna texter, inte för AI-texter om
                                 kundens affär
    warning(*texts)              byråns varning när en kundsynlig text nämner
                                 en leverantör, eller ""

Används av vakttestet (apps/common/test_leverantorer.py), av assistentens
verktyg som skriver det kunden ser (assistant/operations/arenden_ops.py),
av /manage/ när byrån skriver till kunden (projects/manage_views.py,
monitor/manage_views.py), av statussidan (monitor/status_areas.py) och av
AI-texternas kontroll (flamingo/pagebuilder/ai.py, Guard).

Namnen letas utan hänsyn till skiftläge och bara som hela ord, så att
"elks_id" eller "ses_suppressed" i kod inte räknas. Undantag:

  * SES, SNS och SQS letas med versaler ("ses" är svenska: "vi ses"), eller
    som värdnamn (sqs.eu-north-1...) och sesv2.
  * S3 som lagringstjänsten (S3-bucket, s3://) alltid; bara "S3" med strict.
  * Amazonas är en flod.
  * Outlook, Gmail och Microsoft 365 letas inte: de är nästan alltid
    mottagarens eller kundens egen e-post. Bedöms för hand.
"""

import re

_I = re.IGNORECASE

#: Leverantörerna, som (namn, mönster). Namnet står i felen och i undantagen.
NAMES = (
    ("46elks", re.compile(r"\b(?:46\s?)?elks\b", _I)),
    ("aws", re.compile(r"\baws\b", _I)),
    ("amazon", re.compile(r"amazon(?!as)", _I)),
    ("ses", re.compile(r"\bSES(?:v2)?\b|\b(?i:sesv2)\b")),
    ("sns", re.compile(r"\bSNS\b|\bsns\.(?=[a-z])")),
    ("sqs", re.compile(r"\bSQS\b|\bsqs\.(?=[a-z])")),
    ("s3", re.compile(r"\bS3[- ](?:bucket|hink|lagring)|\bs3://", _I)),
    ("ec2", re.compile(r"\bec2\b", _I)),
    ("route 53", re.compile(r"\broute\s?53\b|awsdns", _I)),
    ("cloudfront", re.compile(r"\bcloudfront\b", _I)),
    ("lightsail", re.compile(r"\blightsail\b", _I)),
    ("bedrock", re.compile(r"\bbedrock\b", _I)),
    ("anthropic", re.compile(r"anthropic", _I)),
    ("claude", re.compile(r"\bclaude\b", _I)),
    ("sentry", re.compile(r"\bsentry\b", _I)),
    ("let's encrypt", re.compile(r"\blet'?s\s?encrypt\b", _I)),
    ("postgres", re.compile(r"\bpostgres(?:ql)?\b", _I)),
    ("psycopg", re.compile(r"\bpsycopg\d?\b", _I)),
    ("nginx", re.compile(r"\bnginx\b", _I)),
    ("ubuntu", re.compile(r"\bubuntu\b", _I)),
    ("1password", re.compile(r"\b1password\b", _I)),
    (
        "region",
        re.compile(
            r"\b(?:eu|us|ap|sa|ca|me|af|il|mx)-"
            r"(?:west|east|north|south|central|northeast|southeast|northwest|southwest)-\d\b",
            _I,
        ),
    ),
)

#: Bara med strict: korta namn som i vanlig text kan vara annat (en bilmodell,
#: en förkortning). I kod och i byråns texter till kunden är de leverantören.
STRICT_NAMES = (
    ("s3", re.compile(r"\bS3\b")),
    ("rds", re.compile(r"\bRDS\b")),
)


def names_in(text, *, strict=False):
    """Leverantörernas namn i texten, i NAMES ordning, ett per namn."""
    text = text or ""
    found = [name for name, pattern in NAMES if pattern.search(text)]
    if strict:
        found += [
            name for name, pattern in STRICT_NAMES if name not in found and pattern.search(text)
        ]
    return found


def label(names):
    """Namnen som de skrivs i en varning till byrån: "AWS och 46elks"."""
    shown = [_LABELS.get(name, name) for name in names]
    if len(shown) < 2:
        return "".join(shown)
    return ", ".join(shown[:-1]) + " och " + shown[-1]


_LABELS = {
    "aws": "AWS",
    "amazon": "Amazon",
    "ses": "SES",
    "sns": "SNS",
    "sqs": "SQS",
    "s3": "S3",
    "ec2": "EC2",
    "route 53": "Route 53",
    "cloudfront": "CloudFront",
    "lightsail": "Lightsail",
    "bedrock": "Bedrock",
    "anthropic": "Anthropic",
    "claude": "Claude",
    "sentry": "Sentry",
    "let's encrypt": "Let's Encrypt",
    "postgres": "Postgres",
    "nginx": "nginx",
    "ubuntu": "Ubuntu",
    "1password": "1Password",
    "region": "ett regionnamn (som eu-north-1)",
    "rds": "RDS",
}

#: Vad byrån skriver i stället.
INSTEAD = "Skriv 'sms-tjänsten', 'e-posttjänsten' eller 'vår driftleverantör' i stället"


def warning(*texts):
    """Varningen till byrån när en text kunden ser nämner en leverantör, eller
    "" när den inte gör det. strict: byråns egna texter."""
    found = []
    for text in texts:
        found += [name for name in names_in(text, strict=True) if name not in found]
    if not found:
        return ""
    return (
        f"Texten nämner {label(found)}, och kunden ser den. {INSTEAD}, om det inte måste stå där."
    )
