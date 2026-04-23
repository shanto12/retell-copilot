#!/usr/bin/env bash
# Deploy the Retell Copilot Lambda. Idempotent.
# NOTE: Uses HTTP API Gateway (g6o8u1x16j). Do NOT add Function URL — 403s on this account.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

FN_NAME="retell-copilot-api"
ROLE_NAME="retell-copilot-lambda-role"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
API_GW_URL="https://g6o8u1x16j.execute-api.us-east-1.amazonaws.com"
# PUBLIC_BASE_URL is what Lambda uses to build outbound URLs in responses.
# Defaults to the Netlify edge so short URLs stay on the clean domain; override
# via env for staging/dev deploys.
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://retell-copilot-demo.netlify.app}"
CODES_TABLE="retell-copilot-codes"
RETELL_API_KEY="${RETELL_API_KEY:?RETELL_API_KEY must be set}"

# 1. Ensure IAM role exists
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "→ Creating IAM role $ROLE_NAME"
  cat > /tmp/trust.json <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document file:///tmp/trust.json >/dev/null
  aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  echo "  waiting 10s for role to propagate…"
  sleep 10
fi
ROLE_ARN="$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)"
echo "✓ Role: $ROLE_ARN"

# 2. Package (handler.py only — boto3 is in the Lambda runtime)
echo "→ Packaging handler.py"
rm -f /tmp/retell-copilot.zip
(cd "$HERE" && zip -q /tmp/retell-copilot.zip handler.py)

# 3. Write env JSON
python3 - "$RETELL_API_KEY" "$PUBLIC_BASE_URL" "$CODES_TABLE" <<'PY' > /tmp/retell-copilot-env.json
import json, sys
print(json.dumps({"Variables": {
    "RETELL_API_KEY": sys.argv[1],
    "PUBLIC_BASE_URL": sys.argv[2],
    "CODES_TABLE": sys.argv[3],
}}))
PY

# 4. Create or update Lambda
if aws lambda get-function --function-name "$FN_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "→ Updating Lambda code"
  aws lambda update-function-code --function-name "$FN_NAME" --zip-file fileb:///tmp/retell-copilot.zip --region "$REGION" >/dev/null
  aws lambda wait function-updated --function-name "$FN_NAME" --region "$REGION"
  aws lambda update-function-configuration --function-name "$FN_NAME" --region "$REGION" \
    --environment file:///tmp/retell-copilot-env.json \
    --timeout 20 --memory-size 512 >/dev/null
  aws lambda wait function-updated --function-name "$FN_NAME" --region "$REGION"
else
  echo "→ Creating Lambda $FN_NAME"
  aws lambda create-function \
    --function-name "$FN_NAME" \
    --runtime python3.12 \
    --role "$ROLE_ARN" \
    --handler handler.handler \
    --timeout 20 \
    --memory-size 512 \
    --zip-file fileb:///tmp/retell-copilot.zip \
    --environment file:///tmp/retell-copilot-env.json \
    --region "$REGION" >/dev/null
  aws lambda wait function-active-v2 --function-name "$FN_NAME" --region "$REGION"
fi
echo "✓ Lambda deployed"

echo ""
echo "$PUBLIC_BASE_URL" > "$HERE/../artifacts/public_url.txt"
echo "🌐 Public URL: $PUBLIC_BASE_URL"
echo "   (API GW:     $API_GW_URL)"
