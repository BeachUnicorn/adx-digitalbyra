#!/usr/bin/env bash
#
# aws-utskick-role.sh - rollen adx-utskick för utskickens post (apps/utskick,
# README H.8, J S1 steg 3 och J S3 steg 1).
#
# Policyn utskick (hela H.8, skrivs om varje gång):
#   - skicka (ses:SendEmail, ses:SendRawEmail) från identiteterna i eu-west-1
#     (utskick.adx.se och kundernas verifierade domäner) och med
#     konfigurationssetet adx-utskick;
#   - kundernas domäner: ses:CreateEmailIdentity, GetEmailIdentity,
#     PutEmailIdentityMailFromAttributes och DeleteEmailIdentity på
#     identity/* i eu-west-1;
#   - ett uttryckligt Deny på DeleteEmailIdentity och PutEmailIdentity* för
#     adx.se, utskick.adx.se, svar.utskick.adx.se och varje identitet som
#     fanns före S3 (listan i aws-utskick-identities.txt, se nedan), och på
#     att skicka med en From-adress på någon av dem utom utskick.adx.se
#     (villkoret ses:FromAddress), så att rollen aldrig skickar som adx.se;
#   - ses:GetAccount;
#   - köerna adx-utskick-events och adx-utskick-inbound med sina DLQ:er
#     (ReceiveMessage, DeleteMessage, GetQueueAttributes, StartMessageMoveTask,
#     och SendMessage på de två köerna, som "Skicka tillbaka" ur DLQ:n behöver);
#   - hinken för inkommande svar, bara prefixet in/ (GetObject, DeleteObject,
#     ListBucket).
# Resurserna själva skapas av aws-utskick-s3.sh (kör det här skriptet först;
# en rättighet till en resurs som inte finns än gör ingenting).
#
# Körs från en arbetsstation med AWS-behörighet (inte på servern), av den som
# leder bygget och efter frågan till Giovanni:
#   aws sso login --profile atlasholly-org
#   UTSKICK_AWS_EXTERNAL_ID=<32 slumptecken> AWS_PROFILE=atlasholly-org ./aws-utskick-role.sh
#
# External id: samma värde som UTSKICK_AWS_EXTERNAL_ID i produktionens .env
# (python -c "import secrets; print(secrets.token_hex(16))").
#
# Skyddade identiteter: första gången (filen aws-utskick-identities.txt
# saknas) listar skriptet identiteterna i eu-west-1 och eu-north-1, lägger
# till de tre fasta namnen och skriver filen; checka in den. Senare körningar
# läser bara filen, så att kundernas domäner som appen skapat efter S3 aldrig
# hamnar i Deny (då kunde appen inte ta bort dem). En identitet läggs till för
# hand i filen.
#
# Instansrollen django-ec2-instance-role får BARA sts:AssumeRole på den nya
# rollen, i en EGEN inline-policy (utskick-assume). aws-instance-role.sh
# skriver om policyn bedrock-and-backups i sin helhet; det här skriptet rör
# den aldrig. Kör ändå först
#   aws iam list-role-policies --role-name django-ec2-instance-role
#   aws iam get-role-policy --role-name django-ec2-instance-role --policy-name <namn>
# och för in det som finns live men saknas i aws-instance-role.sh (till
# exempel sts:AssumeRole på ADXReadOnly för aws_sync) innan något annat körs.
#
# Idempotent: en befintlig roll återanvänds, policyerna skrivs om.
set -euo pipefail

ACCOUNT="500841883756"
ROLE="adx-utskick"
INSTANCE_ROLE="django-ec2-instance-role"
SES_REGION="eu-west-1"
MAIL_DOMAIN="utskick.adx.se"
REPLY_DOMAIN="svar.utskick.adx.se"
# Samma namn som i aws-utskick-s3.sh och .env (test_s3_foundation jämför).
CONFIG_SET="adx-utskick"
EVENTS_QUEUE="adx-utskick-events"
INBOUND_QUEUE="adx-utskick-inbound"
INBOUND_BUCKET="adx-utskick-inbound-${ACCOUNT}"
INBOUND_PREFIX="in/"
IDENTITIES_FILE="$(cd "$(dirname "$0")" && pwd)/aws-utskick-identities.txt"
EXTERNAL_ID="${UTSKICK_AWS_EXTERNAL_ID:?sätt UTSKICK_AWS_EXTERNAL_ID (samma som i .env)}"

if [ "${#EXTERNAL_ID}" -lt 32 ]; then
    echo "UTSKICK_AWS_EXTERNAL_ID ska vara minst 32 tecken." >&2
    exit 1
fi

CALLER=$(aws sts get-caller-identity --query Account --output text)
if [ "$CALLER" != "$ACCOUNT" ]; then
    echo "Fel AWS-konto ($CALLER), väntade $ACCOUNT. Kontrollera AWS_PROFILE." >&2
    exit 1
fi

# --- Skyddade identiteter (H.8) -----------------------------------------------
if [ ! -f "$IDENTITIES_FILE" ]; then
    echo "Skriver $IDENTITIES_FILE (identiteterna före S3); checka in den."
    {
        echo "adx.se"
        echo "$MAIL_DOMAIN"
        echo "$REPLY_DOMAIN"
        for region in eu-west-1 eu-north-1; do
            aws sesv2 list-email-identities --region "$region" \
                --query 'EmailIdentities[].IdentityName' --output text | tr '\t' '\n'
        done
    } | sed '/^[[:space:]]*$/d; /^None$/d' | sort -u > "$IDENTITIES_FILE"
fi

# Filens rader (utan kommentarer och tomrader) plus de tre fasta namnen, en gång var.
PROTECTED=$(
    { sed 's/#.*//' "$IDENTITIES_FILE"; printf 'adx.se\n%s\n%s\n' "$MAIL_DOMAIN" "$REPLY_DOMAIN"; } \
        | tr -d '[:blank:]' | sed '/^$/d' | sort -u
)
DENY_RESOURCES=""
while IFS= read -r identity; do
    DENY_RESOURCES="${DENY_RESOURCES}\"arn:aws:ses:*:${ACCOUNT}:identity/${identity}\","
done <<< "$PROTECTED"
DENY_RESOURCES="${DENY_RESOURCES%,}"
# Avsändare som rollen aldrig får skicka från: varje skyddad identitet utom
# utskick.adx.se (en domän blir *@domän, en adress står som den är).
DENY_FROM=""
while IFS= read -r identity; do
    [ "$identity" = "$MAIL_DOMAIN" ] && continue
    case "$identity" in
        *@*) pattern="$identity" ;;
        *) pattern="*@${identity}" ;;
    esac
    DENY_FROM="${DENY_FROM}\"${pattern}\","
done <<< "$PROTECTED"
DENY_FROM="${DENY_FROM%,}"

TRUST=$(cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow",
  "Principal":{"AWS":"arn:aws:iam::${ACCOUNT}:role/${INSTANCE_ROLE}"},
  "Action":"sts:AssumeRole",
  "Condition":{"StringEquals":{"sts:ExternalId":"${EXTERNAL_ID}"}}}
]}
JSON
)

SQS="arn:aws:sqs:${SES_REGION}:${ACCOUNT}"
POLICY=$(cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"SendFromIdentities","Effect":"Allow",
  "Action":["ses:SendEmail","ses:SendRawEmail"],
  "Resource":["arn:aws:ses:${SES_REGION}:${ACCOUNT}:identity/*",
              "arn:aws:ses:${SES_REGION}:${ACCOUNT}:configuration-set/${CONFIG_SET}"]},
 {"Sid":"CustomerDomains","Effect":"Allow",
  "Action":["ses:CreateEmailIdentity","ses:GetEmailIdentity",
            "ses:PutEmailIdentityMailFromAttributes","ses:DeleteEmailIdentity"],
  "Resource":"arn:aws:ses:${SES_REGION}:${ACCOUNT}:identity/*"},
 {"Sid":"ProtectAdxIdentities","Effect":"Deny",
  "Action":["ses:DeleteEmailIdentity","ses:PutEmailIdentity*"],
  "Resource":[${DENY_RESOURCES}]},
 {"Sid":"NoSendFromProtected","Effect":"Deny",
  "Action":["ses:SendEmail","ses:SendRawEmail"],"Resource":"*",
  "Condition":{"StringLike":{"ses:FromAddress":[${DENY_FROM}]}}},
 {"Sid":"ReadAccount","Effect":"Allow","Action":["ses:GetAccount"],"Resource":"*"},
 {"Sid":"ReadQueues","Effect":"Allow",
  "Action":["sqs:ReceiveMessage","sqs:DeleteMessage","sqs:GetQueueAttributes",
            "sqs:StartMessageMoveTask","sqs:ListMessageMoveTasks"],
  "Resource":["${SQS}:${EVENTS_QUEUE}","${SQS}:${EVENTS_QUEUE}-dlq",
              "${SQS}:${INBOUND_QUEUE}","${SQS}:${INBOUND_QUEUE}-dlq"]},
 {"Sid":"RedriveToQueues","Effect":"Allow","Action":["sqs:SendMessage"],
  "Resource":["${SQS}:${EVENTS_QUEUE}","${SQS}:${INBOUND_QUEUE}"]},
 {"Sid":"InboundObjects","Effect":"Allow",
  "Action":["s3:GetObject","s3:DeleteObject"],
  "Resource":"arn:aws:s3:::${INBOUND_BUCKET}/${INBOUND_PREFIX}*"},
 {"Sid":"InboundListing","Effect":"Allow","Action":["s3:ListBucket"],
  "Resource":"arn:aws:s3:::${INBOUND_BUCKET}",
  "Condition":{"StringLike":{"s3:prefix":["${INBOUND_PREFIX}","${INBOUND_PREFIX}*"]}}}
]}
JSON
)

ASSUME=$(cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"AssumeUtskick","Effect":"Allow","Action":"sts:AssumeRole",
  "Resource":"arn:aws:iam::${ACCOUNT}:role/${ROLE}"}
]}
JSON
)

aws iam create-role --role-name "$ROLE" \
    --assume-role-policy-document "$TRUST" \
    --description "ADX utskick: SES, SQS och S3 i eu-west-1, antas av DJANGO-servern" \
    2>/dev/null || {
        echo "rollen finns redan; skriver om förtroendet"
        aws iam update-assume-role-policy --role-name "$ROLE" --policy-document "$TRUST"
    }
aws iam put-role-policy --role-name "$ROLE" \
    --policy-name utskick --policy-document "$POLICY"
aws iam put-role-policy --role-name "$INSTANCE_ROLE" \
    --policy-name utskick-assume --policy-document "$ASSUME"

echo "Klart. Skyddade identiteter ($(echo "$PROTECTED" | wc -l | tr -d ' ')): $(echo "$PROTECTED" | tr '\n' ' ')"
echo "Lägg in i produktionens .env och starta om (systemctl restart adx):"
echo "  UTSKICK_AWS_ROLE_ARN=arn:aws:iam::${ACCOUNT}:role/${ROLE}"
echo "  UTSKICK_AWS_EXTERNAL_ID=<samma värde som ovan>"
echo "Sedan resurserna för S3: ./aws-utskick-s3.sh (konfigurationssetet, köerna, hinken)."
