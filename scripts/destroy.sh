#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK_NAME="${SUNROOM_STACK_NAME:-sunroom}"
DEPLOY_REGION="${SUNROOM_REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
if [[ -z "$DEPLOY_REGION" ]]; then
  DEPLOY_REGION="$(aws configure get region 2>/dev/null || true)"
fi

fail() {
  printf 'Sunroom cleanup stopped: %s\n' "$1" >&2
  exit 1
}

command -v aws >/dev/null 2>&1 || fail "Install AWS CLI before cleanup."
command -v python3 >/dev/null 2>&1 || fail "Install Python 3 before cleanup."
[[ -n "$DEPLOY_REGION" ]] || fail "Set SUNROOM_REGION=us-east-1 before cleanup."
[[ "$DEPLOY_REGION" == "us-east-1" ]] || fail "Use the same us-east-1 region used for deployment."
AWS_PAGER="" aws pricing-plan-manager help >/dev/null 2>&1 || fail "Update AWS CLI to a version that includes pricing-plan-manager."

printf 'This permanently removes every uploaded photo and the Sunroom AWS stack.\n'
read -r -p 'Type DELETE to continue: ' confirmation
[[ "$confirmation" == "DELETE" ]] || fail "Confirmation did not match. Nothing was removed."

STACK_OUTPUTS_JSON="$(AWS_PAGER="" aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$DEPLOY_REGION" \
  --query 'Stacks[0].Outputs' \
  --output json)" || fail "Could not find the stack. Check SUNROOM_STACK_NAME and SUNROOM_REGION."

mapfile -t STACK_VALUES < <(python3 -c '
import json, sys
outputs = {entry["OutputKey"]: entry["OutputValue"] for entry in json.load(sys.stdin)}
keys = ["SiteBucketName", "UploadsBucketName", "MediaBucketName", "SiteDistributionId", "SiteDistributionArn", "SiteWebAclArn"]
print("\n".join(outputs[key] for key in keys))
' <<< "$STACK_OUTPUTS_JSON")

SITE_BUCKET="${STACK_VALUES[0]}"
UPLOADS_BUCKET="${STACK_VALUES[1]}"
MEDIA_BUCKET="${STACK_VALUES[2]}"
DISTRIBUTION_ID="${STACK_VALUES[3]}"
DISTRIBUTION_ARN="${STACK_VALUES[4]}"
WEB_ACL_ARN="${STACK_VALUES[5]}"

SUBSCRIPTIONS_JSON="$(AWS_PAGER="" aws pricing-plan-manager list-subscriptions \
  --region "$DEPLOY_REGION" \
  --output json)"
PLAN_JSON="$(SUNROOM_SUBSCRIPTIONS_JSON="$SUBSCRIPTIONS_JSON" python3 - "$DISTRIBUTION_ARN" <<'PY'
import json
import os
import sys

distribution_arn = sys.argv[1]
payload = json.loads(os.environ["SUNROOM_SUBSCRIPTIONS_JSON"])
matches = [
    item for item in payload.get("subscriptionSummaries", [])
    if distribution_arn in item.get("resourceArns", [])
]
if len(matches) > 1:
    print(json.dumps({"error": "More than one pricing plan includes this distribution."}))
elif matches:
    print(json.dumps(matches[0]))
else:
    print("{}")
PY
)"
PLAN_ERROR="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("error", ""))' <<< "$PLAN_JSON")"
[[ -z "$PLAN_ERROR" ]] || fail "$PLAN_ERROR"

if [[ "$PLAN_JSON" != "{}" ]]; then
  PLAN_TIER="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("planTier", ""))' <<< "$PLAN_JSON")"
  PLAN_STATUS="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("status", ""))' <<< "$PLAN_JSON")"
  PLAN_ARN="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("arn", ""))' <<< "$PLAN_JSON")"
  PLAN_ETAG="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("eTag", ""))' <<< "$PLAN_JSON")"
  PLAN_CHANGE="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("scheduledChange", {}).get("changeType", ""))' <<< "$PLAN_JSON")"
  PLAN_DATE="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("scheduledChange", {}).get("effectiveDate", ""))' <<< "$PLAN_JSON")"
  PLAN_HAS_WEB_ACL="$(SUNROOM_PLAN_JSON="$PLAN_JSON" SUNROOM_WEB_ACL_ARN="$WEB_ACL_ARN" python3 - <<'PY'
import json
import os

plan = json.loads(os.environ["SUNROOM_PLAN_JSON"])
print(str(os.environ["SUNROOM_WEB_ACL_ARN"] in plan.get("resourceArns", [])).lower())
PY
)"

  [[ "$PLAN_TIER" == "FREE" ]] || fail "This distribution is subscribed to the $PLAN_TIER plan. Cancel or move that plan manually before removing the stack."
  [[ "$PLAN_HAS_WEB_ACL" == "true" ]] || fail "The existing plan does not include this stack's WAF web ACL."

  if [[ "$PLAN_STATUS" == "ACTIVE" && "$PLAN_CHANGE" != "CANCELLATION" ]]; then
    cancel_result="$(AWS_PAGER="" aws pricing-plan-manager cancel-subscription \
      --region "$DEPLOY_REGION" \
      --arn "$PLAN_ARN" \
      --if-match "$PLAN_ETAG" \
      --output json)"
    PLAN_DATE="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("subscription", {}).get("scheduledChange", {}).get("effectiveDate", ""))' <<< "$cancel_result")"
  fi

  [[ -n "$PLAN_DATE" ]] || fail "The CloudFront Free plan is still active. Check its status, then rerun cleanup when it has ended."
fi

AWS_PAGER="" aws s3 rm "s3://$UPLOADS_BUCKET" --recursive --region "$DEPLOY_REGION"
AWS_PAGER="" aws s3 rm "s3://$MEDIA_BUCKET" --recursive --region "$DEPLOY_REGION"
AWS_PAGER="" aws s3 rm "s3://$SITE_BUCKET" --recursive --region "$DEPLOY_REGION"
AWS_PAGER="" aws cloudfront create-invalidation \
  --distribution-id "$DISTRIBUTION_ID" \
  --paths '/*' >/dev/null

if [[ "$PLAN_JSON" != "{}" ]]; then
  printf 'The CloudFront Free plan cancellation is scheduled for %s. Rerun this script after that date to remove the remaining stack resources.\n' "$PLAN_DATE"
  exit 0
fi

AWS_PAGER="" aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$DEPLOY_REGION"
AWS_PAGER="" aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$DEPLOY_REGION"
rm -f "$ROOT_DIR/frontend/config.js"
printf 'Sunroom AWS resources have been removed.\n'
