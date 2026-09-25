#!/usr/bin/env bash
# Delete resources created by start-seed-validator.sh. Run from the same CloudShell home directory.

set -euo pipefail

# ------------------------------ Configuration ------------------------------
# Leave empty to auto-detect the only nishrao-*-lambda-state.json file in this directory.
# Set it only when more than one validator state file is present.
STATE_FILE=""
DELETE_VALIDATOR_ECR_REPOSITORY=false # Set true to permanently delete validator images.
DELETE_VALIDATOR_AUTH_SECRET=false # Set true to permanently delete the generated HMAC secret.

resolve_state_file() {
  if [[ -n "$STATE_FILE" ]]; then
    [[ -f "$STATE_FILE" ]] || { echo "State file $STATE_FILE not found." >&2; exit 1; }
    return
  fi

  local state_files=()
  shopt -s nullglob
  state_files=(./nishrao-*-lambda-state.json)
  shopt -u nullglob
  if [[ ${#state_files[@]} -ne 1 ]]; then
    echo "Expected exactly one nishrao-*-lambda-state.json file. Set STATE_FILE explicitly when needed." >&2
    exit 1
  fi
  STATE_FILE="${state_files[0]}"
}

for command in aws jq; do command -v "$command" >/dev/null || { echo "Missing $command" >&2; exit 1; }; done
resolve_state_file

AWS_REGION="$(jq -er .region "$STATE_FILE")"
FUNCTION_NAME="$(jq -r .function_name "$STATE_FILE")"
FUNCTION_ARN="$(jq -r .function_arn "$STATE_FILE")"
API_ID="$(jq -r .api_id "$STATE_FILE")"
LOG_GROUP_NAME="$(jq -r .log_group "$STATE_FILE")"
API_LOG_GROUP_NAME="$(jq -r '.api_log_group // empty' "$STATE_FILE")"
ROLE_NAME="$(jq -r .role_name "$STATE_FILE")"
VALIDATOR_NAME="$(jq -er .validator_name "$STATE_FILE")"
API_NAME="$(jq -er .api_name "$STATE_FILE")"
AUTH_SECRET_ARN="$(jq -er .auth_secret_arn "$STATE_FILE")"
AUTH_SECRET_NAME="$(jq -er .auth_secret_name "$STATE_FILE")"
ECR_REPOSITORY="$(jq -er .ecr_repository "$STATE_FILE")"
RECORDED_ACCOUNT_ID="$(cut -d: -f5 <<<"$FUNCTION_ARN")"

for value in "$FUNCTION_NAME" "$ROLE_NAME" "$VALIDATOR_NAME" "$API_NAME" "$AUTH_SECRET_NAME" "$ECR_REPOSITORY"; do
  if [[ "$value" != nishrao-* ]]; then
    echo "State file contains a resource outside the required nishrao- namespace. Refusing teardown." >&2
    exit 1
  fi
done
if [[ "$LOG_GROUP_NAME" != "/aws/lambda/nishrao-"* ]]; then
  echo "State file contains a log group outside the required nishrao- namespace. Refusing teardown." >&2
  exit 1
fi
if [[ -n "$API_LOG_GROUP_NAME" && "$API_LOG_GROUP_NAME" != "/aws/apigateway/nishrao-"* ]]; then
  echo "State file contains an API log group outside the required nishrao- namespace. Refusing teardown." >&2
  exit 1
fi

export AWS_REGION
CURRENT_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
if [[ "$CURRENT_ACCOUNT_ID" != "$RECORDED_ACCOUNT_ID" ]]; then
  echo "Current AWS account does not match the state file. Refusing teardown." >&2
  exit 1
fi
printf 'Tearing down %s in account %s, region %s.\n' "$VALIDATOR_NAME" "$CURRENT_ACCOUNT_ID" "$AWS_REGION"

# Deleting the API removes its routes, integration, and stage.
CURRENT_API_NAME="$(aws apigatewayv2 get-api --api-id "$API_ID" --query Name --output text 2>/dev/null || true)"
if [[ "$CURRENT_API_NAME" != "$API_NAME" ]]; then
  echo "API name no longer matches the recorded nishrao- resource. Refusing teardown." >&2
  exit 1
fi
aws apigatewayv2 delete-api --api-id "$API_ID" 2>/dev/null || true
aws lambda delete-function --function-name "$FUNCTION_NAME" 2>/dev/null || true
aws logs delete-log-group --log-group-name "$LOG_GROUP_NAME" 2>/dev/null || true
if [[ -n "$API_LOG_GROUP_NAME" ]]; then
  aws logs delete-log-group --log-group-name "$API_LOG_GROUP_NAME" 2>/dev/null || true
fi

aws iam detach-role-policy --role-name "$ROLE_NAME" --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole 2>/dev/null || true
aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "${VALIDATOR_NAME}-runtime" 2>/dev/null || true
aws iam delete-role --role-name "$ROLE_NAME" 2>/dev/null || true

if [[ "$DELETE_VALIDATOR_ECR_REPOSITORY" == "true" ]]; then
  aws ecr delete-repository --repository-name "$ECR_REPOSITORY" --force
else
  echo "ECR repository retained: ${ECR_REPOSITORY} (set DELETE_VALIDATOR_ECR_REPOSITORY=true to remove it)."
fi

if [[ "$DELETE_VALIDATOR_AUTH_SECRET" == "true" ]]; then
  aws secretsmanager delete-secret --secret-id "$AUTH_SECRET_ARN" --force-delete-without-recovery
else
  echo "Auth secret retained: ${AUTH_SECRET_NAME} (set DELETE_VALIDATOR_AUTH_SECRET=true to remove it)."
fi

rm -f "$STATE_FILE" lambda-trust-policy.json validator-runtime-policy.json api-access-log-settings.json
echo "Teardown complete. Only IDs recorded by startup were targeted."
