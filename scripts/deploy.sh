#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export AWS_DEFAULT_OUTPUT=json
export AWS_PAGER=""

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

[[ -n "$DEPLOY_REGION" ]] || fail "Set SUNROOM_REGION to your AWS Region."
[[ "$PHOTO_LIMIT" =~ ^[0-9]+$ ]] || fail "SUNROOM_MONTHLY_LIMIT must be an integer from 0 to 350."
(( PHOTO_LIMIT <= 350 )) || fail "SUNROOM_MONTHLY_LIMIT cannot exceed 350."
aws sts get-caller-identity --region "$DEPLOY_REGION" >/dev/null || fail "AWS credentials are missing or cannot access the selected account."
docker info >/dev/null 2>&1 || fail "Start Docker for the local SAM build, then run this script again. The deployed app does not use Docker."

if STACK_JSON="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$DEPLOY_REGION" --output json 2>/dev/null)"; then
  STACK_STATUS="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Stacks"][0]["StackStatus"])' <<< "$STACK_JSON")"
  if [[ "$STACK_STATUS" == "ROLLBACK_COMPLETE" || "$STACK_STATUS" == "CREATE_FAILED" ]]; then
    printf 'Removing the empty failed CloudFormation stack before retrying.\n'
    aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$DEPLOY_REGION"
    aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$DEPLOY_REGION"
  fi
fi

sam build --use-container --template-file template.yaml
SAM_DEPLOY_LOG="$(mktemp)"
if sam deploy \
  --template-file .aws-sam/build/template.yaml \
  --stack-name "$STACK_NAME" \
  --resolve-s3 \
  --region "$DEPLOY_REGION" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides "MonthlyPhotoLimit=$PHOTO_LIMIT" >"$SAM_DEPLOY_LOG" 2>&1; then
  cat "$SAM_DEPLOY_LOG"
elif grep -q "No changes to deploy" "$SAM_DEPLOY_LOG"; then
  cat "$SAM_DEPLOY_LOG"
else
  cat "$SAM_DEPLOY_LOG" >&2
  rm -f "$SAM_DEPLOY_LOG"
  exit 1
fi
rm -f "$SAM_DEPLOY_LOG"

STACK_OUTPUTS_JSON="$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$DEPLOY_REGION" \
  --query 'Stacks[0].Outputs' \
  --output json)"

STACK_VALUES=()
while IFS= read -r value; do
  STACK_VALUES[${#STACK_VALUES[@]}]="$value"
done < <(python3 -c '
import json, sys
outputs = {entry["OutputKey"]: entry["OutputValue"] for entry in json.load(sys.stdin)}
keys = ["SiteUrl", "ApiUrl", "SiteBucketName", "UserPoolClientId"]
print("\n".join(outputs[key] for key in keys))
' <<< "$STACK_OUTPUTS_JSON")

SITE_URL="${STACK_VALUES[0]}"
API_URL="${STACK_VALUES[1]}"
SITE_BUCKET="${STACK_VALUES[2]}"
USER_POOL_CLIENT_ID="${STACK_VALUES[3]}"

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

aws s3 sync frontend/ "s3://$SITE_BUCKET" \
  --region "$DEPLOY_REGION" \
  --delete \
  --exclude config.js.example \
  --cache-control 'no-cache,no-store,must-revalidate'

aws s3 cp frontend/ "s3://$SITE_BUCKET" \
  --region "$DEPLOY_REGION" \
  --recursive \
  --exclude config.js.example \
  --cache-control 'no-cache,no-store,must-revalidate'

printf '\nSunroom is published at %s\n' "$SITE_URL"
printf 'Photos are processed automatically and published after thumbnail generation.\n'
