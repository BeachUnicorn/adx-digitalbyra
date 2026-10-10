#!/usr/bin/env bash
#
# aws-utskick-s3.sh - resurserna i eu-west-1 för utskickens e-post i S3
# (apps/utskick, README D.6, D.7, G.3, H.8 och J S3 steg 2 och 3).
#
# Skapar eller uppdaterar, i den här ordningen:
#   1. konfigurationssetet adx-utskick: ryktesmått på, kontots spärrlista bara
#      för studsar (SuppressedReasons BOUNCE: ett klagomål hos en kund spärrar
#      aldrig en annan kunds mejl; vår egen spärrlista per kund sköter dem);
#   2. händelserna: SNS-ämnet adx-utskick-events, SQS-kön adx-utskick-events
#      med DLQ:n adx-utskick-events-dlq (fem mottagningar, 14 dagar), rå
#      leverans till kön, och konfigurationssetets händelsemål med SEND,
#      REJECT, BOUNCE, COMPLAINT, DELIVERY, DELIVERY_DELAY och
#      RENDERING_FAILURE. INTE OPEN och INTE CLICK: med dem lägger SES in sin
#      egen pixel och skriver om länkarna i varje mejl, också hos mottagare
#      som inte sagt ja till spårning (H.5). Öppningar räknas av vår egen
#      pixel på klick.adx.se/o/;
#   3. svaren: identiteten svar.utskick.adx.se (Easy DKIM) med DKIM-posterna
#      och MX 10 inbound-smtp.eu-west-1.amazonaws.com i Route53 (zonen för
#      adx.se), en privat S3-hink med SSE-S3 och livscykeln 7 dagar på in/,
#      SNS-ämnet adx-utskick-inbound -> SQS adx-utskick-inbound med DLQ,
#      och en mottagningsregel med spam- och virusskanning och S3-åtgärden
#      (prefix in/) med ämnet. S3-åtgärden används för att SNS-åtgärden
#      studsar mejl över 150 kB (bilder från telefoner, G.3).
#
# Bara en regeluppsättning kan vara aktiv i en region. Finns en aktiv redan
# läggs regeln i den (den byts aldrig ut); annars skapas och aktiveras
# adx-utskick.
#
# Kör aws-utskick-role.sh först (rollens rättigheter till köerna och hinken).
# Körs från en arbetsstation med AWS-behörighet (inte på servern), av den som
# leder bygget och efter frågan till Giovanni:
#   aws sso login --profile atlasholly-org
#   AWS_PROFILE=atlasholly-org ./aws-utskick-s3.sh
#
# Idempotent: det som finns uppdateras, inget tas bort. Skriver sist ut
# raderna till produktionens .env (köernas adresser och hinken).
set -euo pipefail

ACCOUNT="500841883756"
REGION="eu-west-1"
ZONE_ID="Z2HKAATG1V4QEA"
REPLY_DOMAIN="svar.utskick.adx.se"
# Samma namn som i aws-utskick-role.sh och .env (test_s3_foundation jämför).
CONFIG_SET="adx-utskick"
EVENTS_QUEUE="adx-utskick-events"
INBOUND_QUEUE="adx-utskick-inbound"
EVENTS_TOPIC="adx-utskick-events"
INBOUND_TOPIC="adx-utskick-inbound"
INBOUND_BUCKET="adx-utskick-inbound-${ACCOUNT}"
INBOUND_PREFIX="in/"
EVENT_DESTINATION="adx-utskick-events"
DEFAULT_RULE_SET="adx-utskick"
RULE_NAME="adx-utskick-svar"
EVENT_TYPES='["SEND","REJECT","BOUNCE","COMPLAINT","DELIVERY","DELIVERY_DELAY","RENDERING_FAILURE"]'
RETENTION_SECONDS=1209600
MAX_RECEIVES=5

export AWS_DEFAULT_REGION="$REGION"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

say() { printf '\n== %s\n' "$*"; }

CALLER=$(aws sts get-caller-identity --query Account --output text)
if [ "$CALLER" != "$ACCOUNT" ]; then
    echo "Fel AWS-konto ($CALLER), väntade $ACCOUNT. Kontrollera AWS_PROFILE." >&2
    exit 1
fi

# --- 1. Konfigurationssetet ---------------------------------------------------
say "Konfigurationssetet $CONFIG_SET"
if ! aws sesv2 get-configuration-set --configuration-set-name "$CONFIG_SET" >/dev/null 2>&1; then
    aws sesv2 create-configuration-set --configuration-set-name "$CONFIG_SET" \
        --reputation-options ReputationMetricsEnabled=true \
        --sending-options SendingEnabled=true \
        --suppression-options SuppressedReasons=BOUNCE
fi
aws sesv2 put-configuration-set-reputation-options --configuration-set-name "$CONFIG_SET" \
    --reputation-metrics-enabled
aws sesv2 put-configuration-set-suppression-options --configuration-set-name "$CONFIG_SET" \
    --suppressed-reasons BOUNCE
aws sesv2 put-configuration-set-sending-options --configuration-set-name "$CONFIG_SET" \
    --sending-enabled

# --- Hjälpare: ämne, kö med DLQ, prenumeration ---------------------------------

# topic NAMN KÄLL-ARN -> ämnets ARN. SES får publicera bara från KÄLL-ARN.
topic() {
    local name="$1" source_arn="$2" arn
    arn=$(aws sns create-topic --name "$name" --query TopicArn --output text)
    cat > "$WORK/topic-$name.json" <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"Owner","Effect":"Allow","Principal":{"AWS":"arn:aws:iam::${ACCOUNT}:root"},
  "Action":["SNS:GetTopicAttributes","SNS:SetTopicAttributes","SNS:AddPermission",
            "SNS:RemovePermission","SNS:DeleteTopic","SNS:Subscribe",
            "SNS:ListSubscriptionsByTopic","SNS:Publish"],
  "Resource":"${arn}"},
 {"Sid":"SesPublishes","Effect":"Allow","Principal":{"Service":"ses.amazonaws.com"},
  "Action":"SNS:Publish","Resource":"${arn}",
  "Condition":{"StringEquals":{"AWS:SourceAccount":"${ACCOUNT}"},
               "ArnLike":{"AWS:SourceArn":"${source_arn}"}}}
]}
JSON
    aws sns set-topic-attributes --topic-arn "$arn" --attribute-name Policy \
        --attribute-value "file://$WORK/topic-$name.json" >/dev/null
    echo "$arn"
}

# queue NAMN ÄMNES-ARN -> köns adress. Kön får en DLQ (NAMN-dlq), 14 dagar,
# synlighet 60 s, SSE-SQS och en policy som bara släpper in ämnet.
queue() {
    local name="$1" topic_arn="$2" dlq_url dlq_arn url arn
    dlq_url=$(aws sqs get-queue-url --queue-name "${name}-dlq" --query QueueUrl --output text 2>/dev/null) \
        || dlq_url=$(aws sqs create-queue --queue-name "${name}-dlq" --query QueueUrl --output text)
    cat > "$WORK/dlq-$name.json" <<JSON
{"MessageRetentionPeriod":"${RETENTION_SECONDS}","SqsManagedSseEnabled":"true"}
JSON
    aws sqs set-queue-attributes --queue-url "$dlq_url" --attributes "file://$WORK/dlq-$name.json"
    dlq_arn=$(aws sqs get-queue-attributes --queue-url "$dlq_url" --attribute-names QueueArn \
        --query Attributes.QueueArn --output text)

    url=$(aws sqs get-queue-url --queue-name "$name" --query QueueUrl --output text 2>/dev/null) \
        || url=$(aws sqs create-queue --queue-name "$name" --query QueueUrl --output text)
    arn=$(aws sqs get-queue-attributes --queue-url "$url" --attribute-names QueueArn \
        --query Attributes.QueueArn --output text)
    cat > "$WORK/queue-$name.json" <<JSON
{"MessageRetentionPeriod":"${RETENTION_SECONDS}",
 "VisibilityTimeout":"60",
 "SqsManagedSseEnabled":"true",
 "RedrivePolicy":"{\"deadLetterTargetArn\":\"${dlq_arn}\",\"maxReceiveCount\":\"${MAX_RECEIVES}\"}",
 "Policy":"{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"TopicSends\",\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"sns.amazonaws.com\"},\"Action\":\"sqs:SendMessage\",\"Resource\":\"${arn}\",\"Condition\":{\"ArnEquals\":{\"aws:SourceArn\":\"${topic_arn}\"}}}]}"}
JSON
    aws sqs set-queue-attributes --queue-url "$url" --attributes "file://$WORK/queue-$name.json"

    local sub
    sub=$(aws sns subscribe --topic-arn "$topic_arn" --protocol sqs \
        --notification-endpoint "$arn" --attributes RawMessageDelivery=true \
        --return-subscription-arn --query SubscriptionArn --output text)
    aws sns set-subscription-attributes --subscription-arn "$sub" \
        --attribute-name RawMessageDelivery --attribute-value true
    echo "$url"
}

# --- 2. Händelserna ---------------------------------------------------------------
say "Händelserna: $EVENTS_TOPIC -> $EVENTS_QUEUE"
CONFIG_SET_ARN="arn:aws:ses:${REGION}:${ACCOUNT}:configuration-set/${CONFIG_SET}"
EVENTS_TOPIC_ARN=$(topic "$EVENTS_TOPIC" "$CONFIG_SET_ARN")
EVENTS_URL=$(queue "$EVENTS_QUEUE" "$EVENTS_TOPIC_ARN")
cat > "$WORK/destination.json" <<JSON
{"Enabled":true,"MatchingEventTypes":${EVENT_TYPES},"SnsDestination":{"TopicArn":"${EVENTS_TOPIC_ARN}"}}
JSON
EXISTING=$(aws sesv2 get-configuration-set-event-destinations --configuration-set-name "$CONFIG_SET" \
    --query "EventDestinations[?Name=='${EVENT_DESTINATION}'].Name" --output text)
if [ "$EXISTING" = "$EVENT_DESTINATION" ]; then
    aws sesv2 update-configuration-set-event-destination --configuration-set-name "$CONFIG_SET" \
        --event-destination-name "$EVENT_DESTINATION" --event-destination "file://$WORK/destination.json"
else
    aws sesv2 create-configuration-set-event-destination --configuration-set-name "$CONFIG_SET" \
        --event-destination-name "$EVENT_DESTINATION" --event-destination "file://$WORK/destination.json"
fi

# --- 3. Svaren --------------------------------------------------------------------
say "Identiteten $REPLY_DOMAIN och posterna i Route53"
if ! aws sesv2 get-email-identity --email-identity "$REPLY_DOMAIN" >/dev/null 2>&1; then
    aws sesv2 create-email-identity --email-identity "$REPLY_DOMAIN" \
        --dkim-signing-attributes NextSigningKeyLength=RSA_2048_BIT >/dev/null
fi
TOKENS=$(aws sesv2 get-email-identity --email-identity "$REPLY_DOMAIN" \
    --query 'DkimAttributes.Tokens' --output text)
CHANGES=""
for token in $TOKENS; do
    CHANGES="${CHANGES}{\"Action\":\"UPSERT\",\"ResourceRecordSet\":{\"Name\":\"${token}._domainkey.${REPLY_DOMAIN}\",\"Type\":\"CNAME\",\"TTL\":1800,\"ResourceRecords\":[{\"Value\":\"${token}.dkim.amazonses.com\"}]}},"
done
CHANGES="${CHANGES}{\"Action\":\"UPSERT\",\"ResourceRecordSet\":{\"Name\":\"${REPLY_DOMAIN}\",\"Type\":\"MX\",\"TTL\":1800,\"ResourceRecords\":[{\"Value\":\"10 inbound-smtp.${REGION}.amazonaws.com\"}]}}"
cat > "$WORK/records.json" <<JSON
{"Comment":"ADX utskick: svar på mejl (S3)","Changes":[${CHANGES}]}
JSON
aws route53 change-resource-record-sets --hosted-zone-id "$ZONE_ID" \
    --change-batch "file://$WORK/records.json" >/dev/null

say "Regeluppsättningen"
RULE_SET=$(aws ses describe-active-receipt-rule-set --query Metadata.Name --output text 2>/dev/null || true)
if [ -z "$RULE_SET" ] || [ "$RULE_SET" = "None" ]; then
    RULE_SET="$DEFAULT_RULE_SET"
    aws ses create-receipt-rule-set --rule-set-name "$RULE_SET" 2>/dev/null || true
    aws ses set-active-receipt-rule-set --rule-set-name "$RULE_SET"
else
    echo "En regeluppsättning är redan aktiv ($RULE_SET); regeln läggs i den."
fi
RULE_ARN="arn:aws:ses:${REGION}:${ACCOUNT}:receipt-rule-set/${RULE_SET}:receipt-rule/${RULE_NAME}"

say "Hinken $INBOUND_BUCKET"
if ! aws s3api head-bucket --bucket "$INBOUND_BUCKET" >/dev/null 2>&1; then
    aws s3api create-bucket --bucket "$INBOUND_BUCKET" \
        --create-bucket-configuration "LocationConstraint=${REGION}" >/dev/null
fi
aws s3api put-public-access-block --bucket "$INBOUND_BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-ownership-controls --bucket "$INBOUND_BUCKET" \
    --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'
aws s3api put-bucket-encryption --bucket "$INBOUND_BUCKET" --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
cat > "$WORK/lifecycle.json" <<JSON
{"Rules":[{"ID":"svar-7-dagar","Status":"Enabled","Filter":{"Prefix":"${INBOUND_PREFIX}"},
  "Expiration":{"Days":7},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":1}}]}
JSON
aws s3api put-bucket-lifecycle-configuration --bucket "$INBOUND_BUCKET" \
    --lifecycle-configuration "file://$WORK/lifecycle.json"
cat > "$WORK/bucket-policy.json" <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"SesPutsReplies","Effect":"Allow","Principal":{"Service":"ses.amazonaws.com"},
  "Action":"s3:PutObject","Resource":"arn:aws:s3:::${INBOUND_BUCKET}/${INBOUND_PREFIX}*",
  "Condition":{"StringEquals":{"AWS:SourceAccount":"${ACCOUNT}","AWS:SourceArn":"${RULE_ARN}"}}},
 {"Sid":"TlsOnly","Effect":"Deny","Principal":"*","Action":"s3:*",
  "Resource":["arn:aws:s3:::${INBOUND_BUCKET}","arn:aws:s3:::${INBOUND_BUCKET}/*"],
  "Condition":{"Bool":{"aws:SecureTransport":"false"}}}
]}
JSON
aws s3api put-bucket-policy --bucket "$INBOUND_BUCKET" --policy "file://$WORK/bucket-policy.json"

say "Svaren: $INBOUND_TOPIC -> $INBOUND_QUEUE"
INBOUND_TOPIC_ARN=$(topic "$INBOUND_TOPIC" "$RULE_ARN")
INBOUND_URL=$(queue "$INBOUND_QUEUE" "$INBOUND_TOPIC_ARN")

say "Mottagningsregeln $RULE_NAME"
cat > "$WORK/rule.json" <<JSON
{"Name":"${RULE_NAME}","Enabled":true,"TlsPolicy":"Optional","ScanEnabled":true,
 "Recipients":["${REPLY_DOMAIN}"],
 "Actions":[{"S3Action":{"BucketName":"${INBOUND_BUCKET}","ObjectKeyPrefix":"${INBOUND_PREFIX}",
             "TopicArn":"${INBOUND_TOPIC_ARN}"}}]}
JSON
if aws ses describe-receipt-rule --rule-set-name "$RULE_SET" --rule-name "$RULE_NAME" >/dev/null 2>&1; then
    aws ses update-receipt-rule --rule-set-name "$RULE_SET" --rule "file://$WORK/rule.json"
else
    aws ses create-receipt-rule --rule-set-name "$RULE_SET" --rule "file://$WORK/rule.json"
fi

say "Klart"
echo "Lägg in i produktionens .env och starta om (systemctl restart adx):"
echo "  UTSKICK_SES_CONFIGURATION_SET=${CONFIG_SET}"
echo "  UTSKICK_SQS_EVENTS_URL=${EVENTS_URL}"
echo "  UTSKICK_SQS_INBOUND_URL=${INBOUND_URL}"
echo "  UTSKICK_SES_INBOUND_BUCKET=${INBOUND_BUCKET}"
echo "Kontrollera sedan (README J S3 steg 6 och 7): aws sesv2 get-account --region ${REGION},"
echo "DKIM för ${REPLY_DOMAIN} (aws sesv2 get-email-identity) och provmejlet till dig själv."
