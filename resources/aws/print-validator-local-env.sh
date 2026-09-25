#!/usr/bin/env bash
# Print local-only validator configuration for direct insertion into the project's .env.

set -euo pipefail

AWS_REGION="ap-south-1"
VALIDATOR_NAME="nishrao-poc-data-seed-validator"
SECRET_NAME="${VALIDATOR_NAME}-auth"

for command in aws jq; do
  command -v "$command" >/dev/null || { echo "Missing required command: ${command}" >&2; exit 1; }
done

export AWS_REGION
API_ID="$(aws apigatewayv2 get-apis --query "Items[?Name=='${VALIDATOR_NAME}-api'].ApiId | [0]" --output text)"
[[ "$API_ID" != "None" ]] || { echo "Validator API was not found." >&2; exit 1; }
API_URL="$(aws apigatewayv2 get-api --api-id "$API_ID" --query ApiEndpoint --output text)"
HMAC_SECRET="$(aws secretsmanager get-secret-value --secret-id "$SECRET_NAME" --query SecretString --output text | jq -r .hmac_secret)"

printf 'SEED_VALIDATOR_URL=%s\n' "$API_URL"
printf 'SEED_VALIDATOR_HMAC_SECRET=%s\n' "$HMAC_SECRET"
printf 'SEED_VALIDATOR_TIMEOUT_SECONDS=24\n'