#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

STACK_NAME="${SUNROOM_STACK_NAME:-sunroom}"
PHOTO_LIMIT="${SUNROOM_MONTHLY_LIMIT:-350}"
DEPLOY_REGION="${SUNROOM_REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
if [[ -z "$DEPLOY_REGION" ]]; then
  DEPLOY_REGION="$(aws configure get region 2>/dev/null || true)"
fi

fail() {
  printf 'Sunroom deploy stopped: %s\n' "$1" >&2
  exit 1
}

for command_name in aws sam docker python3; do
  command -v "$command_name" >/dev/null 2>&1 || fail "Install $command_name, then run this script again."
done

[[ -n "$DEPLOY_REGION" ]] || fail "Set SUNROOM_REGION=us-east-1 before deployment."
[[ "$DEPLOY_REGION" == "us-east-1" ]] || fail "Use us-east-1 because CloudFront WAF web ACLs are created there."
[[ "$PHOTO_LIMIT" =~ ^[0-9]+$ ]] || fail "SUNROOM_MONTHLY_LIMIT must be an integer from 0 to 350."
(( PHOTO_LIMIT <= 350 )) || fail "SUNROOM_MONTHLY_LIMIT cannot exceed 350."

AWS_PAGER="" aws pricing-plan-manager help >/dev/null 2>&1 || fail "Update AWS CLI to a version that includes pricing-plan-manager. Sunroom requires the CloudFront Free plan before publishing."
AWS_PAGER="" aws sts get-caller-identity --region "$DEPLOY_REGION" >/dev/null || fail "AWS credentials are missing or cannot access the selected account."
docker info >/dev/null 2>&1 || fail "Start Docker for the local SAM build, then run this script again. The deployed app does not use Docker."

sam build --use-container --template-file template.yaml
sam deploy \
  --template-file .aws-sam/build/template.yaml \
  --stack-name "$STACK_NAME" \
  --resolve-s3 \
  --region "$DEPLOY_REGION" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides "MonthlyModerationLimit=$PHOTO_LIMIT"

STACK_OUTPUTS_JSON="$(AWS_PAGER="" aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$DEPLOY_REGION" \
  --query 'Stacks[0].Outputs' \
  --output json)"

mapfile -t STACK_VALUES < <(python3 -c '
import json, sys
outputs = {entry["OutputKey"]: entry["OutputValue"] for entry in json.load(sys.stdin)}
keys = ["SiteUrl", "ApiUrl", "SiteBucketName", "SiteDistributionId", "SiteDistributionArn", "SiteWebAclArn", "UserPoolClientId"]
print("\n".join(outputs[key] for key in keys))
' <<< "$STACK_OUTPUTS_JSON")

SITE_URL="${STACK_VALUES[0]}"
API_URL="${STACK_VALUES[1]}"
SITE_BUCKET="${STACK_VALUES[2]}"
DISTRIBUTION_ID="${STACK_VALUES[3]}"
DISTRIBUTION_ARN="${STACK_VALUES[4]}"
WEB_ACL_ARN="${STACK_VALUES[5]}"
USER_POOL_CLIENT_ID="${STACK_VALUES[6]}"

find_plan_subscription() {
  local subscriptions_json
  subscriptions_json="$(AWS_PAGER="" aws pricing-plan-manager list-subscriptions \
    --region "$DEPLOY_REGION" \
    --output json)"
  SUNROOM_SUBSCRIPTIONS_JSON="$subscriptions_json" python3 - "$DISTRIBUTION_ARN" "$WEB_ACL_ARN" <<'PY'
import json
import os
import sys

distribution_arn, web_acl_arn = sys.argv[1:]
payload = json.loads(os.environ["SUNROOM_SUBSCRIPTIONS_JSON"])
matches = [
    item for item in payload.get("subscriptionSummaries", [])
    if distribution_arn in item.get("resourceArns", [])
]
if not matches:
    print("{}")
    raise SystemExit(0)
if len(matches) != 1:
    print(json.dumps({"error": "More than one plan includes this distribution."}))
    raise SystemExit(0)
subscription = matches[0]
if web_acl_arn not in subscription.get("resourceArns", []):
    print(json.dumps({"error": "The existing plan does not include this stack's WAF web ACL."}))
    raise SystemExit(0)
print(json.dumps(subscription))
PY
}

PLAN_JSON="$(find_plan_subscription)"
PLAN_ERROR="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("error", ""))' <<< "$PLAN_JSON")"
[[ -z "$PLAN_ERROR" ]] || fail "$PLAN_ERROR"

if [[ "$PLAN_JSON" == "{}" ]]; then
  printf 'Subscribing the distribution to the CloudFront Free plan.\n'
  AWS_PAGER="" aws pricing-plan-manager create-subscription \
    --region "$DEPLOY_REGION" \
    --plan-family CloudFront \
    --plan-tier FREE \
    --usage-level DEFAULT \
    --resource-arns "$DISTRIBUTION_ARN" "$WEB_ACL_ARN" \
    --approval-mode IMMEDIATE \
    --client-token "$DISTRIBUTION_ID" \
    --output json >/dev/null
fi

PLAN_ACTIVE=false
for ((attempt = 1; attempt <= 30; attempt += 1)); do
  PLAN_JSON="$(find_plan_subscription)"
  PLAN_ERROR="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("error", ""))' <<< "$PLAN_JSON")"
  [[ -z "$PLAN_ERROR" ]] || fail "$PLAN_ERROR"
  PLAN_TIER="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("planTier", ""))' <<< "$PLAN_JSON")"
  PLAN_STATUS="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("status", ""))' <<< "$PLAN_JSON")"
  PLAN_CHANGE="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("scheduledChange", {}).get("changeType", ""))' <<< "$PLAN_JSON")"
  [[ "$PLAN_TIER" == "FREE" ]] || fail "The distribution is not attached to the CloudFront Free plan. No frontend files were published."
  [[ "$PLAN_CHANGE" != "CANCELLATION" ]] || fail "The CloudFront Free plan has a pending cancellation. No frontend files were published."
  if [[ "$PLAN_STATUS" == "ACTIVE" ]]; then
    PLAN_ACTIVE=true
    break
  fi
  [[ "$PLAN_STATUS" != "FAILED" ]] || fail "CloudFront Free plan activation failed. No frontend files were published."
  sleep 5
done
[[ "$PLAN_ACTIVE" == true ]] || fail "CloudFront Free plan did not become active in time. No frontend files were published."

SUNROOM_REGION="$DEPLOY_REGION" SUNROOM_API_URL="$API_URL" SUNROOM_CLIENT_ID="$USER_POOL_CLIENT_ID" python3 - <<'PY'
import json
import os
from pathlib import Path

config = {
    "apiUrl": os.environ["SUNROOM_API_URL"],
    "region": os.environ["SUNROOM_REGION"],
    "userPoolClientId": os.environ["SUNROOM_CLIENT_ID"],
}
Path("frontend/config.js").write_text(
    "window.SUNROOM_CONFIG = " + json.dumps(config, separators=(",", ":")) + ";\n",
    encoding="utf-8",
)
PY

AWS_PAGER="" aws s3 sync frontend/ "s3://$SITE_BUCKET" \
  --region "$DEPLOY_REGION" \
  --delete \
  --exclude config.js.example \
  --cache-control 'public,max-age=300'
AWS_PAGER="" aws cloudfront create-invalidation \
  --distribution-id "$DISTRIBUTION_ID" \
  --paths '/*' >/dev/null

printf '\nSunroom is published at %s\n' "$SITE_URL"
printf 'CloudFront Free plan is active for this distribution.\n'
