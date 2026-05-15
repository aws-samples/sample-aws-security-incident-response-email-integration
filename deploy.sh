#!/usr/bin/env bash
set -euo pipefail

# --- Configuration ---
STACK_NAME="sir-email-integration"
REGION="${AWS_REGION:-us-east-1}"
PROFILE="${AWS_PROFILE:-}"
SES_DOMAIN="${1:?Usage: ./deploy.sh <ses-domain> <recipient-address> [--profile <profile-name>]}"
RECIPIENT="${2:?Usage: ./deploy.sh <ses-domain> <recipient-address> [--profile <profile-name>]}"

# Parse optional --profile flag
shift 2
while [[ $# -gt 0 ]]; do
    case $1 in
        --profile) PROFILE="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

PROFILE_FLAG=""
if [ -n "${PROFILE}" ]; then
    PROFILE_FLAG="--profile ${PROFILE}"
fi

echo "=== Deploying SIR Email Integration ==="
echo "Region:    ${REGION}"
echo "Domain:    ${SES_DOMAIN}"
echo "Recipient: ${RECIPIENT}"
[ -n "${PROFILE}" ] && echo "Profile:   ${PROFILE}"
echo ""

# Verify SES domain is verified
echo "Checking SES domain verification..."
VERIFICATION=$(aws sesv2 get-email-identity \
    --email-identity "${SES_DOMAIN}" \
    --region "${REGION}" ${PROFILE_FLAG} \
    --query "VerifiedForSendingStatus" \
    --output text 2>/dev/null || echo "NOT_FOUND")

if [ "${VERIFICATION}" != "True" ] && [ "${VERIFICATION}" != "true" ]; then
    echo "ERROR: Domain '${SES_DOMAIN}' is not verified in SES (status: ${VERIFICATION})"
    echo "Verify the domain first via the SES console or:"
    echo "  aws sesv2 create-email-identity --email-identity ${SES_DOMAIN} --region ${REGION} ${PROFILE_FLAG}"
    exit 1
fi
echo "Domain verified."

# Build and deploy with SAM
echo ""
echo "Building..."
sam build --template-file template.yaml --region "${REGION}"

echo ""
echo "Deploying..."
sam deploy \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}" \
    --resolve-s3 \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides \
        SesDomain="${SES_DOMAIN}" \
        RecipientAddress="${RECIPIENT}" \
    --no-confirm-changeset \
    ${PROFILE_FLAG}

echo ""
echo "=== Deployment complete ==="
echo ""

# Remind about activating the receipt rule set
echo "IMPORTANT: Activate the SES receipt rule set:"
echo "  aws ses set-active-receipt-rule-set --rule-set-name sir-email-integration-rules --region ${REGION} ${PROFILE_FLAG}"
echo ""
echo "Ensure your domain has an MX record pointing to SES:"
echo "  ${SES_DOMAIN}  MX  10 inbound-smtp.${REGION}.amazonaws.com"
echo ""

# Show outputs
aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}" ${PROFILE_FLAG} \
    --query 'Stacks[0].Outputs' \
    --output table
