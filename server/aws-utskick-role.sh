#!/usr/bin/env bash
#
# aws-utskick-role.sh - rollen adx-utskick för utskickens post (apps/utskick,
# README H.8 och J S1 steg 3). S1-delen: skicka från utskick.adx.se i
# eu-west-1 och läsa kontots läge (ses:GetAccount). S3 lägger till
# kundernas domäner, konfigurationssetet, köerna och S3-hinken.
#
# Körs EN gång från en arbetsstation med AWS-behörighet (inte på servern),
# av den som leder bygget och efter frågan till Giovanni:
#   aws sso login --profile atlasholly-org
#   UTSKICK_AWS_EXTERNAL_ID=<32 slumptecken> AWS_PROFILE=atlasholly-org ./aws-utskick-role.sh
#
# External id: samma värde som UTSKICK_AWS_EXTERNAL_ID i produktionens .env
# (python -c "import secrets; print(secrets.token_hex(16))").
#
# Instansrollen django-ec2-instance-role får BARA sts:AssumeRole på den nya
# rollen, i en EGEN inline-policy (utskick-assume). aws-instance-role.sh
# skriver om policyn bedrock-and-backups i sin helhet; den här skriptet rör
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
EXTERNAL_ID="${UTSKICK_AWS_EXTERNAL_ID:?sätt UTSKICK_AWS_EXTERNAL_ID (samma som i .env)}"

if [ "${#EXTERNAL_ID}" -lt 32 ]; then
    echo "UTSKICK_AWS_EXTERNAL_ID ska vara minst 32 tecken." >&2
    exit 1
fi

TRUST=$(cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow",
  "Principal":{"AWS":"arn:aws:iam::${ACCOUNT}:role/${INSTANCE_ROLE}"},
  "Action":"sts:AssumeRole",
  "Condition":{"StringEquals":{"sts:ExternalId":"${EXTERNAL_ID}"}}}
]}
JSON
)

# S1: bekräftelsemejlen från bekrafta@utskick.adx.se, och kontots läge.
POLICY=$(cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"SendFromUtskickDomain","Effect":"Allow",
  "Action":["ses:SendEmail","ses:SendRawEmail"],
  "Resource":["arn:aws:ses:${SES_REGION}:${ACCOUNT}:identity/${MAIL_DOMAIN}"]},
 {"Sid":"ReadAccount","Effect":"Allow","Action":["ses:GetAccount"],"Resource":"*"}
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
    --description "ADX utskick: SES i eu-west-1 (bekräftelsemejl), antas av DJANGO-servern" \
    2>/dev/null || {
        echo "rollen finns redan; skriver om förtroendet"
        aws iam update-assume-role-policy --role-name "$ROLE" --policy-document "$TRUST"
    }
aws iam put-role-policy --role-name "$ROLE" \
    --policy-name utskick --policy-document "$POLICY"
aws iam put-role-policy --role-name "$INSTANCE_ROLE" \
    --policy-name utskick-assume --policy-document "$ASSUME"

echo "Klart. Lägg in i produktionens .env och starta om (systemctl restart adx):"
echo "  UTSKICK_AWS_ROLE_ARN=arn:aws:iam::${ACCOUNT}:role/${ROLE}"
echo "  UTSKICK_AWS_EXTERNAL_ID=<samma värde som ovan>"
echo "Sedan IMDSv2 (README C.5) och SES-identiteten utskick.adx.se i ${SES_REGION}."
