#!/usr/bin/env bash
#
# aws-instance-role.sh - skapa och koppla instansrollen för DJANGO-servern.
#
# Körs EN gång från en arbetsstation med AWS-behörighet (inte på servern):
#   AWS_PROFILE=atlasholly-org ./aws-instance-role.sh
#
# Rollen ger boxen (i-00366e8f91f9ebc65, eu-north-1, konto 500841883756):
#   - bedrock:InvokeModel* på Anthropic-modeller/inferensprofiler
#     -> AI-assistenten (ASSISTANT_PROVIDER=bedrock, tom ASSISTANT_AWS_PROFILE)
#   - s3:PutObject till backupbucketen -> server/backup.sh laddar upp dit
#
# OBS: servern behöver också awscli för S3-uppladdningen:
#   sudo snap install aws-cli --classic
#
# Idempotent: befintlig roll/profil återanvänds, policyn skrivs om.
set -euo pipefail

ROLE="django-ec2-instance-role"
INSTANCE="i-00366e8f91f9ebc65"
REGION="eu-north-1"
BUCKET="atlasholly-db-backups-500841883756"

TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

POLICY=$(cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"BedrockInvokeAnthropic","Effect":"Allow",
  "Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],
  "Resource":["arn:aws:bedrock:*::foundation-model/anthropic.*",
              "arn:aws:bedrock:*:*:inference-profile/eu.anthropic.*",
              "arn:aws:bedrock:*:*:inference-profile/global.anthropic.*"]},
 {"Sid":"DbBackupsToS3","Effect":"Allow","Action":["s3:PutObject"],
  "Resource":"arn:aws:s3:::${BUCKET}/*"},
 {"Sid":"DbBackupsList","Effect":"Allow","Action":["s3:ListBucket"],
  "Resource":"arn:aws:s3:::${BUCKET}"}
]}
JSON
)

aws iam create-role --role-name "$ROLE" \
    --assume-role-policy-document "$TRUST" \
    --description "DJANGO EC2: Bedrock invoke + DB-backuper till S3" \
    2>/dev/null || echo "rollen finns redan"
aws iam put-role-policy --role-name "$ROLE" \
    --policy-name bedrock-and-backups --policy-document "$POLICY"
aws iam create-instance-profile --instance-profile-name "$ROLE" \
    2>/dev/null || echo "instansprofilen finns redan"
aws iam add-role-to-instance-profile --instance-profile-name "$ROLE" \
    --role-name "$ROLE" 2>/dev/null || echo "rollen redan i profilen"

# IAM är eventually consistent; ge profilen några sekunder innan associering.
sleep 10
aws ec2 associate-iam-instance-profile --region "$REGION" \
    --instance-id "$INSTANCE" \
    --iam-instance-profile "Name=$ROLE"

echo "Klart. Verifiera på servern (kan ta ~1 min):"
echo "  curl -s -H \"X-aws-ec2-metadata-token: \$(curl -sX PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')\" http://169.254.169.254/latest/meta-data/iam/security-credentials/"
