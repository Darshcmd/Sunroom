#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AWS_DEFAULT_OUTPUT=json
export AWS_PAGER=""

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
[[ -n "$DEPLOY_REGION" ]] || fail "Set SUNROOM_REGION before cleanup."

STACK_JSON="$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$DEPLOY_REGION" \
  --output json)" || fail "Could not find the stack. Check SUNROOM_STACK_NAME and SUNROOM_REGION."

STACK_STATUS="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Stacks"][0]["StackStatus"])' <<< "$STACK_JSON")"
if [[ "$STACK_STATUS" == "ROLLBACK_COMPLETE" || "$STACK_STATUS" == "CREATE_FAILED" ]]; then
  printf 'This removes the failed Sunroom stack record.\n'
  read -r -p 'Type DELETE to continue: ' confirmation
  [[ "$confirmation" == "DELETE" ]] || fail "Confirmation did not match. Nothing was removed."
  aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$DEPLOY_REGION"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$DEPLOY_REGION"
  rm -f "$ROOT_DIR/frontend/config.js"
  printf 'The failed Sunroom stack record has been removed.\n'
  exit 0
fi

STACK_OUTPUTS_JSON="$(python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["Stacks"][0].get("Outputs", [])))' <<< "$STACK_JSON")"
BUCKETS=()
while IFS= read -r bucket; do
  BUCKETS[${#BUCKETS[@]}]="$bucket"
done < <(python3 -c '
import json, sys
outputs = {entry["OutputKey"]: entry["OutputValue"] for entry in json.load(sys.stdin)}
keys = ["SiteBucketName", "UploadsBucketName", "MediaBucketName"]
print("\n".join(outputs[key] for key in keys))
' <<< "$STACK_OUTPUTS_JSON")

printf 'This permanently removes the site and all uploaded photos.\n'
read -r -p 'Type DELETE to continue: ' confirmation
[[ "$confirmation" == "DELETE" ]] || fail "Confirmation did not match. Nothing was removed."

for bucket in "${BUCKETS[@]}"; do
  aws s3 rm "s3://$bucket" --recursive --region "$DEPLOY_REGION"
done

aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$DEPLOY_REGION"
aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$DEPLOY_REGION"
rm -f "$ROOT_DIR/frontend/config.js"
printf 'Sunroom AWS resources have been removed.\n'
