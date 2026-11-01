import json
import logging
import os
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError


logger = logging.getLogger()
logger.setLevel(logging.WARNING)
stepfunctions = boto3.client("stepfunctions")
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]


def start_workflow_handler(event, _context):
    detail = event.get("detail", {})
    bucket = detail.get("bucket", {}).get("name")
    key = unquote_plus(detail.get("object", {}).get("key", ""))

    if not bucket or not key.startswith("incoming/"):
        return {"started": False, "reason": "not-an-upload"}

    photo_id = key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if len(photo_id) != 36:
        logger.warning("Ignoring upload with an invalid photo id")
        return {"started": False, "reason": "invalid-photo-id"}

    try:
        stepfunctions.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=photo_id,
            input=json.dumps({"bucket": bucket, "key": key, "photoId": photo_id}),
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ExecutionAlreadyExists":
            return {"started": False, "reason": "duplicate-event"}
        raise

    return {"started": True, "photoId": photo_id}
