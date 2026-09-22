"""
Allt som pratar med AWS. Bara läsning, och bara via kundkontots läsroll.

session_for() antar rollen med ExternalId och KONTROLLERAR att vi hamnade i
rätt konto innan något hämtas - ett felskrivet konto-ID får aldrig leda
till att en kunds fakturor sparas på en annan kund.

Varje del hämtas för sig och får misslyckas för sig: saknar rollen en
rättighet blir just den delen {"error": ...} och resten av bilden gäller.
"""

import logging
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

logger = logging.getLogger(__name__)

#: Fakturor, kostnad och domäner är globala tjänster med hemvist i us-east-1.
GLOBAL_REGION = "us-east-1"
DEFAULT_REGION = "eu-north-1"
MAX_PDF_BYTES = 20 * 1024 * 1024
OLD_KEY_DAYS = 365
#: Portar som aldrig ska stå öppna mot hela internet.
SENSITIVE_PORTS = {22: "SSH", 3389: "RDP", 5432: "Postgres", 3306: "MySQL", 6379: "Redis"}

_CONFIG = Config(
    retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=8, read_timeout=25
)


class AwsError(Exception):
    """Ett fel värt att visa för byrån, utan stackspår."""


def _client(session, service, region=GLOBAL_REGION):
    return session.client(service, region_name=region, config=_CONFIG)


def base_session():
    profile = getattr(settings, "ADX_AWS_PROFILE", "") or None
    return boto3.Session(profile_name=profile)


def session_for(account, profile=None):
    """
    En session i kundens konto. profile= är utvecklingsvägen (en lokal
    AWS-profil som redan ÄR kundens konto); i drift antas läsrollen.
    """
    try:
        if profile:
            session = boto3.Session(profile_name=profile)
        else:
            creds = _client(base_session(), "sts").assume_role(
                RoleArn=account.role_arn,
                RoleSessionName="adx-se",
                ExternalId=account.external_id,
                DurationSeconds=1800,
            )["Credentials"]
            session = boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
            )
        actual = _client(session, "sts").get_caller_identity()["Account"]
    except (ClientError, BotoCoreError) as exc:
        raise AwsError(_explain(exc)) from exc
    if actual != account.account_id:
        raise AwsError(f"Hamnade i konto {actual}, väntade {account.account_id}. Inget hämtat.")
    return session


def _explain(exc):
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    if code == "AccessDenied":
        return "Åtkomst nekad. Finns rollen i kontot, med rätt ExternalId och rätt betrott konto?"
    return f"{code or type(exc).__name__}: {str(exc)[:200]}"


def _money(value):
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _section(name, build):
    try:
        return build()
    except (ClientError, BotoCoreError) as exc:
        logger.info("AWS %s: %s", name, _explain(exc))
        return {"error": _explain(exc)}


# ---- fakturor ---------------------------------------------------------------


def months_back(count, today=None):
    """[(år, månad)] bakåt från innevarande månad."""
    today = today or date.today()
    year, month, out = today.year, today.month, []
    for _ in range(count):
        out.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return out


def fetch_invoices(session, account_id, months):
    """Fakturasammanställningar för perioderna. API:t tar en månad i taget."""
    client = _client(session, "invoicing")
    rows = []
    for year, month in months:
        kwargs = {
            "Selector": {"ResourceType": "ACCOUNT_ID", "Value": account_id},
            "Filter": {"BillingPeriod": {"Month": month, "Year": year}},
        }
        while True:
            try:
                page = client.list_invoice_summaries(**kwargs)
            except (ClientError, BotoCoreError) as exc:
                raise AwsError(_explain(exc)) from exc
            for item in page.get("InvoiceSummaries", []):
                # Basbeloppet har alltid uppdelningen; betalvalutan saknas på
                # skattekopior (IINSE-numren) och är ändå samma valuta för oss.
                base = item.get("BaseCurrencyAmount") or {}
                amount = item.get("PaymentCurrencyAmount") or base
                breakdown = base.get("AmountBreakdown") or amount.get("AmountBreakdown") or {}
                taxes = breakdown.get("Taxes") or {}
                discounts = breakdown.get("Discounts") or {}
                period = item.get("BillingPeriod") or {}
                rows.append(
                    {
                        "invoice_id": item["InvoiceId"],
                        "invoice_type": item.get("InvoiceType", ""),
                        "entity": (item.get("Entity") or {}).get("InvoicingEntity", ""),
                        "period_year": period.get("Year", year),
                        "period_month": period.get("Month", month),
                        "issued_on": _as_date(item.get("IssuedDate")),
                        "due_on": _as_date(item.get("DueDate")),
                        "currency": amount.get("CurrencyCode") or base.get("CurrencyCode", ""),
                        "subtotal": _money(breakdown.get("SubTotalAmount")),
                        "credits": _money(discounts.get("TotalAmount")),
                        "tax": _money(taxes.get("TotalAmount")),
                        "total": _money(amount.get("TotalAmount") or base.get("TotalAmount")),
                    }
                )
            if not page.get("NextToken"):
                break
            kwargs["NextToken"] = page["NextToken"]
    return rows


def _as_date(value):
    return value.date() if hasattr(value, "date") else None


def download_pdf(session, invoice_id):
    """Fakturans PDF som bytes. Adressen är förhandssignerad och kortlivad."""
    try:
        url = _client(session, "invoicing").get_invoice_pdf(InvoiceId=invoice_id)["InvoicePDF"][
            "DocumentUrl"
        ]
    except (ClientError, BotoCoreError, KeyError) as exc:
        raise AwsError(_explain(exc)) from exc
    parsed = urlparse(url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".amazonaws.com"):
        raise AwsError("Oväntad adress för faktura-PDF, hämtar inte.")
    with urlopen(Request(url), timeout=30) as response:  # noqa: S310 - https + amazonaws.com ovan
        data = response.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES or not data.startswith(b"%PDF-"):
        raise AwsError("Svaret var ingen PDF (eller för stort).")
    return data


# ---- byråns bild ------------------------------------------------------------


def fetch_cost(session, today=None):
    """Kostnad per månad (6 st), per tjänst för senaste hela månaden, prognos, regioner."""
    today = today or date.today()
    client = _client(session, "ce")
    first_this = today.replace(day=1)
    year, month = months_back(6, today)[-1]
    start = date(year, month, 1)
    end = today + timedelta(days=1)

    # Bruttoanvändning: krediter och återbetalningar räknas bort ur summan
    # och redovisas för sig. Annars visar ett konto med gratiskrediter 0 i
    # månader där EC2 gick för fullt.
    no_credits = {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}}

    def usage(group_key=None, since=start, flt=no_credits):
        kwargs = {
            "TimePeriod": {"Start": since.isoformat(), "End": end.isoformat()},
            "Granularity": "MONTHLY",
            "Metrics": ["UnblendedCost"],
            "Filter": flt,
        }
        if group_key:
            kwargs["GroupBy"] = [{"Type": "DIMENSION", "Key": group_key}]
        return client.get_cost_and_usage(**kwargs)["ResultsByTime"]

    months = [
        {
            "month": r["TimePeriod"]["Start"][:7],
            "amount": round(float(r["Total"]["UnblendedCost"]["Amount"]), 2),
            "currency": r["Total"]["UnblendedCost"]["Unit"],
            "partial": r["TimePeriod"]["Start"] == first_this.isoformat(),
        }
        for r in usage()
    ]
    only_credits = {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}
    credits = {
        r["TimePeriod"]["Start"][:7]: round(-float(r["Total"]["UnblendedCost"]["Amount"]), 2)
        for r in usage(flt=only_credits)
    }
    for m in months:
        m["credits"] = credits.get(m["month"], 0.0)
    last_full = (first_this - timedelta(days=1)).replace(day=1)
    services, regions = [], set()
    for result in usage("SERVICE", since=last_full):
        if result["TimePeriod"]["Start"] != last_full.isoformat():
            continue
        for group in result["Groups"]:
            amount = round(float(group["Metrics"]["UnblendedCost"]["Amount"]), 2)
            if amount >= 0.01:
                services.append({"name": group["Keys"][0], "amount": amount})
    for result in usage("REGION", since=last_full):
        for group in result["Groups"]:
            region = group["Keys"][0]
            if float(group["Metrics"]["UnblendedCost"]["Amount"]) > 0 and "-" in region:
                regions.add(region)
    services.sort(key=lambda s: -s["amount"])

    forecast = None
    month_end = (first_this + timedelta(days=32)).replace(day=1)
    if (month_end - today).days > 1:
        try:
            result = client.get_cost_forecast(
                TimePeriod={
                    "Start": (today + timedelta(days=1)).isoformat(),
                    "End": month_end.isoformat(),
                },
                Metric="UNBLENDED_COST",
                Granularity="MONTHLY",
                Filter=no_credits,
            )
            so_far = next((m["amount"] for m in months if m["partial"]), 0)
            forecast = round(so_far + float(result["Total"]["Amount"]), 2)
        except (ClientError, BotoCoreError):
            forecast = None  # nya konton saknar underlag - inget fel
    return {
        "months": months,
        "services": services[:8],
        "forecast": forecast,
        "regions": sorted(r for r in regions if r != "global"),
    }


def fetch_resources(session, regions):
    out = {"ec2": [], "rds": [], "snapshots": {}}
    for region in regions:
        ec2 = _client(session, "ec2", region)
        for page in ec2.get_paginator("describe_instances").paginate():
            for reservation in page["Reservations"]:
                for i in reservation["Instances"]:
                    name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), "")
                    out["ec2"].append(
                        {
                            "id": i["InstanceId"],
                            "name": name,
                            "type": i["InstanceType"],
                            "state": i["State"]["Name"],
                            "region": region,
                            "launched": i["LaunchTime"].date().isoformat(),
                        }
                    )
        latest = None
        for page in ec2.get_paginator("describe_snapshots").paginate(OwnerIds=["self"]):
            for snap in page["Snapshots"]:
                if latest is None or snap["StartTime"] > latest:
                    latest = snap["StartTime"]
        if latest:
            out["snapshots"][region] = latest.date().isoformat()
        for db in _client(session, "rds", region).describe_db_instances()["DBInstances"]:
            out["rds"].append(
                {
                    "id": db["DBInstanceIdentifier"],
                    "engine": f"{db['Engine']} {db.get('EngineVersion', '')}".strip(),
                    "type": db["DBInstanceClass"],
                    "state": db["DBInstanceStatus"],
                    "region": region,
                    "backup_days": db.get("BackupRetentionPeriod", 0),
                    "public": db.get("PubliclyAccessible", False),
                }
            )
    out["s3_buckets"] = len(_client(session, "s3").list_buckets().get("Buckets", []))
    return out


def fetch_domains(session):
    """Route 53-registrerade domäner: utgång och auto-förnyelse, rakt från registraren."""
    client = _client(session, "route53domains")
    rows = []
    for page in client.get_paginator("list_domains").paginate():
        for d in page["Domains"]:
            rows.append(
                {
                    "name": d["DomainName"],
                    "expires": d["Expiry"].date().isoformat() if d.get("Expiry") else None,
                    "auto_renew": d.get("AutoRenew", False),
                }
            )
    return sorted(rows, key=lambda r: r["expires"] or "9999")


def fetch_security(session, regions):
    iam = _client(session, "iam")
    summary = iam.get_account_summary()["SummaryMap"]
    old_keys = []
    limit = date.today() - timedelta(days=OLD_KEY_DAYS)
    for page in iam.get_paginator("list_users").paginate():
        for user in page["Users"]:
            for key in iam.list_access_keys(UserName=user["UserName"])["AccessKeyMetadata"]:
                if key["Status"] == "Active" and key["CreateDate"].date() < limit:
                    old_keys.append(
                        {"user": user["UserName"], "created": key["CreateDate"].date().isoformat()}
                    )
    open_ports = []
    for region in regions:
        groups = _client(session, "ec2", region).describe_security_groups()["SecurityGroups"]
        for group in groups:
            for rule in group.get("IpPermissions", []):
                world = any(r.get("CidrIp") == "0.0.0.0/0" for r in rule.get("IpRanges", []))
                if not world:
                    continue
                low, high = rule.get("FromPort"), rule.get("ToPort")
                for port, label in SENSITIVE_PORTS.items():
                    if rule.get("IpProtocol") == "-1" or (
                        low is not None and high is not None and low <= port <= high
                    ):
                        open_ports.append(
                            {
                                "group": group.get("GroupName", group["GroupId"]),
                                "port": port,
                                "label": label,
                                "region": region,
                            }
                        )
    return {
        "root_mfa": bool(summary.get("AccountMFAEnabled")),
        "root_keys": bool(summary.get("AccountAccessKeysPresent")),
        "old_keys": old_keys,
        "open_ports": open_ports,
    }


def build_warnings(snapshot, today=None):
    """Det byrån bör titta på, i klartext. Tom lista = inget att göra."""
    today = today or date.today()
    out = []
    security = snapshot.get("security") or {}
    if security and "error" not in security:
        if not security.get("root_mfa"):
            out.append("Root-kontot saknar MFA.")
        if security.get("root_keys"):
            out.append("Root-kontot har åtkomstnycklar.")
        for key in security.get("old_keys", []):
            out.append(f"Åtkomstnyckel för {key['user']} är äldre än ett år ({key['created']}).")
        for port in security.get("open_ports", []):
            out.append(
                f"{port['label']} (port {port['port']}) är öppen mot hela internet "
                f"i {port['group']}, {port['region']}."
            )
    domains = snapshot.get("domains")
    if isinstance(domains, list):
        for d in domains:
            if not d.get("expires"):
                continue
            days = (date.fromisoformat(d["expires"]) - today).days
            if not d.get("auto_renew") and days < 60:
                out.append(f"{d['name']} går ut om {days} dagar och förnyas inte automatiskt.")
    resources = snapshot.get("resources") or {}
    for db in resources.get("rds", []) if "error" not in resources else []:
        if not db.get("backup_days"):
            out.append(f"Databasen {db['id']} har ingen automatisk backup.")
        if db.get("public"):
            out.append(f"Databasen {db['id']} är publikt åtkomlig.")
    cost = snapshot.get("cost") or {}
    full = [m for m in cost.get("months", []) if not m.get("partial")]
    if cost.get("forecast") and full and full[-1]["amount"] >= 5:
        if cost["forecast"] > full[-1]["amount"] * 1.5:
            out.append(
                f"Månadens prognos ({cost['forecast']:.0f} {full[-1]['currency']}) är över 50 % "
                f"högre än förra månaden ({full[-1]['amount']:.0f})."
            )
    return out


def fetch_snapshot(session):
    cost = _section("kostnad", lambda: fetch_cost(session))
    regions = (cost.get("regions") if "error" not in cost else None) or [DEFAULT_REGION]
    snapshot = {
        "cost": cost,
        "regions": regions,
        "resources": _section("resurser", lambda: fetch_resources(session, regions)),
        "domains": _section("domäner", lambda: fetch_domains(session)),
        "security": _section("säkerhet", lambda: fetch_security(session, regions)),
    }
    snapshot["warnings"] = build_warnings(snapshot)
    return snapshot
