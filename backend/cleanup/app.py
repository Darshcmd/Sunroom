import logging
import os
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError


logger = logging.getLogger()
logger.setLevel(logging.WARNING)
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["TABLE_NAME"])
ddb = dynamodb.meta.client
s3 = boto3.client("s3")
deserializer = TypeDeserializer()

TABLE_NAME = os.environ["TABLE_NAME"]
UPLOADS_BUCKET = os.environ["UPLOADS_BUCKET"]


def cleanup_handler(event, _context):
    cleaned = 0
    for record in event.get("Records", []):
        if record.get("eventName") != "REMOVE":
            continue
        identity = record.get("userIdentity", {})
        if identity.get("type") != "Service" or identity.get("principalId") != "dynamodb.amazonaws.com":
            continue

        attributes = record.get("dynamodb", {}).get("OldImage", {})
        if not attributes:
            continue
        item = {key: deserializer.deserialize(value) for key, value in attributes.items()}
        if item.get("status") != "UPLOADING" or not item.get("expiresAtEpoch"):
            continue

        reservation = int(item.get("reservationBytes", 0))
        if reservation < 1:
            continue

        photo_id = item["photoId"]
        if int(item["expiresAtEpoch"]) > int(time.time()):
            continue

        marker_key = {"PK": f"EXPIRY#{photo_id}", "SK": "META"}
        if table.get_item(Key=marker_key, ConsistentRead=True).get("Item"):
            continue

        s3.delete_object(Bucket=UPLOADS_BUCKET, Key=item["uploadKey"])
        quota_key = {"PK": "QUOTA#ASSETS#V4", "SK": "META"}
        quota_item = table.get_item(Key=quota_key, ConsistentRead=True).get("Item", {})
        previous = quota_item.get("assetBytesReservedV4")
        if not isinstance(previous, (int, Decimal)) or int(previous) < reservation:
            raise ValueError("Asset reservation counter is invalid.")
        previous = int(previous)
        table.update_item(
            Key=quota_key,
            UpdateExpression="SET #counter = :new",
            ConditionExpression="#counter = :previous",
            ExpressionAttributeNames={"#counter": "assetBytesReservedV4"},
            ExpressionAttributeValues={":new": previous - reservation, ":previous": previous},
        )
        try:
            table.put_item(
                Item={**marker_key, "expiresAtEpoch": int(time.time()) + 30 * 86400},
                ConditionExpression="attribute_not_exists(PK)",
            )
            cleaned += 1
        except ClientError as error:
            table.update_item(
                Key=quota_key,
                UpdateExpression="SET #counter = :previous",
                ConditionExpression="#counter = :current",
                ExpressionAttributeNames={"#counter": "assetBytesReservedV4"},
                ExpressionAttributeValues={":previous": previous, ":current": previous - reservation},
            )
            marker = table.get_item(Key=marker_key, ConsistentRead=True).get("Item")
            if marker and error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                continue
            logger.exception("Could not release expired photo reservation %s", photo_id)
            raise

    return {"cleaned": cleaned}
