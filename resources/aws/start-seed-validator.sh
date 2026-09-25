#!/usr/bin/env bash
# Provision a tagged Lambda seed validator behind an API Gateway HTTP API.
# Run from AWS CloudShell with this script beside ../validator/ source.

set -euo pipefail

# ------------------------------ Configuration ------------------------------
AWS_REGION="ap-south-1"
RESOURCE_PREFIX="nishrao-"
VALIDATOR_NAME="${RESOURCE_PREFIX}poc-data-seed-validator"
VALIDATOR_ECR_REPOSITORY="${RESOURCE_PREFIX}poc-data-seed-validator"
VALIDATOR_IMAGE_TAG="1.0.11"
VALIDATOR_AUTH_SECRET_NAME="${RESOURCE_PREFIX}poc-data-seed-validator-auth"
# CloudShell runs x86_64 Docker builders. Keep the Lambda architecture aligned
# with the image build to avoid an ARM emulation requirement.
LAMBDA_ARCHITECTURE="x86_64"
LAMBDA_MEMORY_MB=4096
LAMBDA_TIMEOUT_SECONDS=25 # Keep below API Gateway's synchronous integration limit.
LAMBDA_EPHEMERAL_STORAGE_MB=10240
VALIDATION_TEST_FORCE_IMPLEMENTATION_FAILURE="${VALIDATION_TEST_FORCE_IMPLEMENTATION_FAILURE:-false}"

OWNER="nishit.rao"
PURPOSE="training"
EXPIRE_ON="2027-12-31"
TAGS="Key=owner,Value=${OWNER} Key=purpose,Value=${PURPOSE} Key=expire-on,Value=${EXPIRE_ON}"
TAGS_MAP="owner=${OWNER},purpose=${PURPOSE},expire-on=${EXPIRE_ON}"

ROLE_NAME="${VALIDATOR_NAME}-lambda-role"
FUNCTION_NAME="${VALIDATOR_NAME}-function"
API_NAME="${VALIDATOR_NAME}-api"
LOG_GROUP_NAME="/aws/lambda/${FUNCTION_NAME}"
API_LOG_GROUP_NAME="/aws/apigateway/${API_NAME}"
STATE_FILE="${VALIDATOR_NAME}-lambda-state.json"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VALIDATOR_SOURCE_DIR="${SCRIPT_DIR}/../validator"

require_value() {
  local name="$1"
  local value="$2"
  if [[ -z "$value" ]]; then
    printf 'Set %s at the top of this script before running it.\n' "$name" >&2
    exit 1
  fi
}

require_command() {
  command -v "$1" >/dev/null || { printf 'Missing required command: %s\n' "$1" >&2; exit 1; }
}

create_lambda_function() {
  local attempt
  for attempt in {1..12}; do
    if FUNCTION_ARN="$(aws lambda create-function \
      --function-name "$FUNCTION_NAME" \
      --package-type Image \
      --code "ImageUri=${VALIDATOR_IMAGE}" \
      --role "$ROLE_ARN" \
      --architectures "$LAMBDA_ARCHITECTURE" \
      --memory-size "$LAMBDA_MEMORY_MB" \
      --timeout "$LAMBDA_TIMEOUT_SECONDS" \
      --ephemeral-storage "Size=${LAMBDA_EPHEMERAL_STORAGE_MB}" \
      --environment "Variables={VALIDATOR_AUTH_SECRET_ARN=${VALIDATOR_AUTH_SECRET_ARN},VALIDATION_TEST_FORCE_IMPLEMENTATION_FAILURE=${VALIDATION_TEST_FORCE_IMPLEMENTATION_FAILURE}}" \
      --tags "$TAGS_MAP" \
      --query 'FunctionArn' --output text 2>&1)"; then
      return
    fi
    if [[ "$FUNCTION_ARN" != *"cannot be assumed by Lambda"* ]]; then
      printf '%s\n' "$FUNCTION_ARN" >&2
      exit 1
    fi
    printf 'Waiting for IAM role propagation before creating Lambda (%s/12).\n' "$attempt" >&2
    sleep 5
  done
  printf '%s\n' "$FUNCTION_ARN" >&2
  exit 1
}

for command in aws jq openssl docker; do require_command "$command"; done
docker buildx version >/dev/null || { echo "Docker Buildx is required to build the Lambda image." >&2; exit 1; }
for variable in VALIDATOR_ECR_REPOSITORY VALIDATOR_IMAGE_TAG VALIDATOR_AUTH_SECRET_NAME; do
  require_value "$variable" "${!variable}"
done
[[ -f "${VALIDATOR_SOURCE_DIR}/Dockerfile" ]] || { echo "Validator source directory not found: ${VALIDATOR_SOURCE_DIR}" >&2; exit 1; }
[[ -f "${VALIDATOR_SOURCE_DIR}/package.json" ]] || { echo "Validator package.json not found: ${VALIDATOR_SOURCE_DIR}" >&2; exit 1; }

export AWS_REGION="$AWS_REGION"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR_REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
VALIDATOR_IMAGE="${ECR_REGISTRY}/${VALIDATOR_ECR_REPOSITORY}:${VALIDATOR_IMAGE_TAG}"
ECR_REPOSITORY_URI="$(aws ecr describe-repositories --repository-names "$VALIDATOR_ECR_REPOSITORY" --query 'repositories[0].repositoryUri' --output text 2>/dev/null || true)"
if [[ -z "$ECR_REPOSITORY_URI" || "$ECR_REPOSITORY_URI" == "None" ]]; then
  ECR_REPOSITORY_URI="$(aws ecr create-repository \
    --repository-name "$VALIDATOR_ECR_REPOSITORY" \
    --image-scanning-configuration scanOnPush=true \
    --tags $TAGS \
    --query 'repository.repositoryUri' --output text)"
fi
aws ecr tag-resource --resource-arn "arn:aws:ecr:${AWS_REGION}:${ACCOUNT_ID}:repository/${VALIDATOR_ECR_REPOSITORY}" --tags $TAGS

VALIDATOR_AUTH_SECRET_ARN="$(aws secretsmanager describe-secret --secret-id "$VALIDATOR_AUTH_SECRET_NAME" --query ARN --output text 2>/dev/null || true)"
if [[ -z "$VALIDATOR_AUTH_SECRET_ARN" || "$VALIDATOR_AUTH_SECRET_ARN" == "None" ]]; then
  GENERATED_HMAC_SECRET="$(openssl rand -hex 32)"
  VALIDATOR_AUTH_SECRET_ARN="$(aws secretsmanager create-secret \
    --name "$VALIDATOR_AUTH_SECRET_NAME" \
    --secret-string "{\"hmac_secret\":\"${GENERATED_HMAC_SECRET}\"}" \
    --tags $TAGS \
    --query ARN --output text)"
  unset GENERATED_HMAC_SECRET
fi
aws secretsmanager tag-resource --secret-id "$VALIDATOR_AUTH_SECRET_ARN" --tags $TAGS
HMAC_SECRET_LENGTH="$(aws secretsmanager get-secret-value \
  --secret-id "$VALIDATOR_AUTH_SECRET_ARN" \
  --query SecretString --output text | jq -er '.hmac_secret | length')"
if (( HMAC_SECRET_LENGTH < 32 )); then
  echo "Validator HMAC secret must contain at least 32 characters." >&2
  exit 1
fi
unset HMAC_SECRET_LENGTH

aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$ECR_REGISTRY"
docker buildx build \
  --platform linux/amd64 \
  --build-arg "LAMBDA_ARCH=${LAMBDA_ARCHITECTURE}" \
  --push \
  --tag "$VALIDATOR_IMAGE" \
  "$VALIDATOR_SOURCE_DIR"
printf 'Using account %s in %s.\n' "$ACCOUNT_ID" "$AWS_REGION"
printf 'Using validator image %s.\n' "$VALIDATOR_IMAGE"

if ! aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP_NAME" --query "logGroups[?logGroupName=='${LOG_GROUP_NAME}'] | [0].logGroupName" --output text | grep -qx "$LOG_GROUP_NAME"; then
  aws logs create-log-group --log-group-name "$LOG_GROUP_NAME" --tags "$TAGS_MAP"
fi
aws logs tag-log-group --log-group-name "$LOG_GROUP_NAME" --tags "$TAGS_MAP"
aws logs put-retention-policy --log-group-name "$LOG_GROUP_NAME" --retention-in-days 14
if ! aws logs describe-log-groups --log-group-name-prefix "$API_LOG_GROUP_NAME" --query "logGroups[?logGroupName=='${API_LOG_GROUP_NAME}'] | [0].logGroupName" --output text | grep -qx "$API_LOG_GROUP_NAME"; then
  aws logs create-log-group --log-group-name "$API_LOG_GROUP_NAME" --tags "$TAGS_MAP"
fi
aws logs tag-log-group --log-group-name "$API_LOG_GROUP_NAME" --tags "$TAGS_MAP"
aws logs put-retention-policy --log-group-name "$API_LOG_GROUP_NAME" --retention-in-days 14
API_LOG_GROUP_ARN="$(aws logs describe-log-groups --log-group-name-prefix "$API_LOG_GROUP_NAME" --query "logGroups[?logGroupName=='${API_LOG_GROUP_NAME}'] | [0].arn" --output text)"
API_LOG_GROUP_ARN="${API_LOG_GROUP_ARN%:*}"

cat > lambda-trust-policy.json <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document file://lambda-trust-policy.json --tags $TAGS >/dev/null
fi
aws iam tag-role --role-name "$ROLE_NAME" --tags $TAGS
aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam wait role-exists --role-name "$ROLE_NAME"
cat > validator-runtime-policy.json <<EOF
{"Version":"2012-10-17","Statement":[
  {"Effect":"Allow","Action":["secretsmanager:GetSecretValue"],"Resource":"${VALIDATOR_AUTH_SECRET_ARN}"}
]}
EOF
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name "${VALIDATOR_NAME}-runtime" --policy-document file://validator-runtime-policy.json
ROLE_ARN="$(aws iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text)"

FUNCTION_ARN="$(aws lambda get-function --function-name "$FUNCTION_NAME" --query 'Configuration.FunctionArn' --output text 2>/dev/null || true)"
if [[ -z "$FUNCTION_ARN" || "$FUNCTION_ARN" == "None" ]]; then
  create_lambda_function
else
  aws lambda update-function-code --function-name "$FUNCTION_NAME" --image-uri "$VALIDATOR_IMAGE" >/dev/null
  aws lambda wait function-updated-v2 --function-name "$FUNCTION_NAME"
  aws lambda update-function-configuration \
    --function-name "$FUNCTION_NAME" \
    --memory-size "$LAMBDA_MEMORY_MB" \
    --timeout "$LAMBDA_TIMEOUT_SECONDS" \
    --ephemeral-storage "Size=${LAMBDA_EPHEMERAL_STORAGE_MB}" \
    --environment "Variables={VALIDATOR_AUTH_SECRET_ARN=${VALIDATOR_AUTH_SECRET_ARN},VALIDATION_TEST_FORCE_IMPLEMENTATION_FAILURE=${VALIDATION_TEST_FORCE_IMPLEMENTATION_FAILURE}}" >/dev/null
  aws lambda wait function-updated-v2 --function-name "$FUNCTION_NAME"
  aws lambda tag-resource --resource "$FUNCTION_ARN" --tags "$TAGS_MAP"
fi
aws lambda wait function-active-v2 --function-name "$FUNCTION_NAME"
FUNCTION_ARN="$(aws lambda get-function --function-name "$FUNCTION_NAME" --query 'Configuration.FunctionArn' --output text)"

API_ID="$(aws apigatewayv2 get-apis --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text)"
if [[ "$API_ID" == "None" ]]; then
  API_ID="$(aws apigatewayv2 create-api --name "$API_NAME" --protocol-type HTTP --tags "$TAGS_MAP" --query ApiId --output text)"
fi
API_ENDPOINT="$(aws apigatewayv2 get-api --api-id "$API_ID" --query ApiEndpoint --output text)"
API_ARN="arn:aws:apigateway:${AWS_REGION}::/apis/${API_ID}"
aws apigatewayv2 tag-resource --resource-arn "$API_ARN" --tags "$TAGS_MAP" 2>/dev/null || true
LAMBDA_INTEGRATION_URI="arn:aws:apigateway:${AWS_REGION}:lambda:path/2015-03-31/functions/${FUNCTION_ARN}/invocations"

INTEGRATION_ID="$(aws apigatewayv2 get-integrations --api-id "$API_ID" --query "Items[?IntegrationUri=='${LAMBDA_INTEGRATION_URI}'].IntegrationId | [0]" --output text)"
if [[ "$INTEGRATION_ID" == "None" ]]; then
  INTEGRATION_ID="$(aws apigatewayv2 create-integration \
    --api-id "$API_ID" \
    --integration-type AWS_PROXY \
    --integration-uri "$LAMBDA_INTEGRATION_URI" \
    --payload-format-version 2.0 \
    --timeout-in-millis 29000 \
    --query IntegrationId --output text)"
fi

for route_key in 'POST /v1/validations/direct' 'GET /health'; do
  route_id="$(aws apigatewayv2 get-routes --api-id "$API_ID" --query "Items[?RouteKey=='${route_key}'].RouteId | [0]" --output text)"
  if [[ "$route_id" == "None" ]]; then
    aws apigatewayv2 create-route --api-id "$API_ID" --route-key "$route_key" --target "integrations/${INTEGRATION_ID}" >/dev/null
  fi
done
for route_key in 'POST /v1/validations' 'GET /v1/validations/{validation_id}' 'POST /v1/artifacts/presign'; do
  route_id="$(aws apigatewayv2 get-routes --api-id "$API_ID" --query "Items[?RouteKey=='${route_key}'].RouteId | [0]" --output text)"
  if [[ "$route_id" != "None" ]]; then
    aws apigatewayv2 delete-route --api-id "$API_ID" --route-id "$route_id"
  fi
done

STAGE_NAME='$default'
if ! aws apigatewayv2 get-stage --api-id "$API_ID" --stage-name "$STAGE_NAME" >/dev/null 2>&1; then
  aws apigatewayv2 create-stage --api-id "$API_ID" --stage-name "$STAGE_NAME" --auto-deploy --tags "$TAGS_MAP" >/dev/null
fi
ACCESS_LOG_FORMAT='{"requestId":"$context.requestId","routeKey":"$context.routeKey","status":"$context.status"}'
jq -n --arg destination "$API_LOG_GROUP_ARN" --arg format "$ACCESS_LOG_FORMAT" \
  '{DestinationArn:$destination,Format:$format}' > api-access-log-settings.json
aws apigatewayv2 update-stage \
  --api-id "$API_ID" \
  --stage-name "$STAGE_NAME" \
  --default-route-settings 'ThrottlingBurstLimit=10,ThrottlingRateLimit=5' \
  --access-log-settings file://api-access-log-settings.json >/dev/null
STAGE_ARN="arn:aws:apigateway:${AWS_REGION}::/apis/${API_ID}/stages/${STAGE_NAME}"
aws apigatewayv2 tag-resource --resource-arn "$STAGE_ARN" --tags "$TAGS_MAP" 2>/dev/null || true

SOURCE_ARN="arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_ID}/*"
if ! aws lambda get-policy --function-name "$FUNCTION_NAME" --query Policy --output text 2>/dev/null | grep -q "${API_ID}"; then
  aws lambda add-permission --function-name "$FUNCTION_NAME" --statement-id "${VALIDATOR_NAME}-api-invoke" --action lambda:InvokeFunction --principal apigateway.amazonaws.com --source-arn "$SOURCE_ARN" >/dev/null
fi

jq -n \
  --arg region "$AWS_REGION" \
  --arg function_name "$FUNCTION_NAME" \
  --arg function_arn "$FUNCTION_ARN" \
  --arg api_id "$API_ID" \
  --arg api_name "$API_NAME" \
  --arg api_endpoint "$API_ENDPOINT" \
  --arg log_group "$LOG_GROUP_NAME" \
  --arg api_log_group "$API_LOG_GROUP_NAME" \
  --arg role "$ROLE_NAME" \
  --arg auth_secret_arn "$VALIDATOR_AUTH_SECRET_ARN" \
  --arg auth_secret_name "$VALIDATOR_AUTH_SECRET_NAME" \
  --arg ecr_repository "$VALIDATOR_ECR_REPOSITORY" \
  --arg validator_name "$VALIDATOR_NAME" \
  '{region:$region,function_name:$function_name,function_arn:$function_arn,api_id:$api_id,api_name:$api_name,api_endpoint:$api_endpoint,log_group:$log_group,api_log_group:$api_log_group,role_name:$role,auth_secret_arn:$auth_secret_arn,auth_secret_name:$auth_secret_name,ecr_repository:$ecr_repository,validator_name:$validator_name}' > "$STATE_FILE"

printf 'Provisioned Lambda validator endpoint: %s\n' "$API_ENDPOINT"
printf 'State saved to %s for the teardown script.\n' "$STATE_FILE"
printf 'API Gateway routes/integrations and Lambda permissions do not support independent tags; their tagged parent API/function owns their lifecycle.\n'
