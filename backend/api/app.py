import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import boto3
from boto3.dynamodb.types import TypeSerializer
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError


logger = logging.getLogger()
logger.setLevel(logging.WARNING)
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["TABLE_NAME"])
ddb = dynamodb.meta.client
s3 = boto3.client("s3")
sns = boto3.client("sns")
serializer = TypeSerializer()

TABLE_NAME = os.environ["TABLE_NAME"]
UPLOADS_BUCKET = os.environ["UPLOADS_BUCKET"]
MEDIA_BUCKET = os.environ["MEDIA_BUCKET"]
MAX_UPLOAD_BYTES = int(os.environ["MAX_UPLOAD_BYTES"])
ASSET_SLOT_BYTES = int(os.environ["ASSET_SLOT_BYTES"])
ASSET_STORAGE_LIMIT_BYTES = int(os.environ["ASSET_STORAGE_LIMIT_BYTES"])
MONTHLY_LIMIT = int(os.environ["MONTHLY_MODERATION_LIMIT"])
SNS_TOPIC_PREFIX = os.environ["SNS_TOPIC_PREFIX"]
PUBLIC_INDEX = "PublicGallery"
OWNER_INDEX = "OwnerGallery"
ALLOWED_TYPES = {"image/jpeg": "jpg", "image/png": "png"}
REMOVABLE_STATUSES = {"UPLOADING", "APPROVED", "REJECTED", "QUOTA_PAUSED", "FAILED", "DELETING"}


def api_handler(event, _context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("rawPath", "")
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})

    try:
        if method == "GET" and path == "/photos":
            return response(200, {"photos": list_public_photos()}, "public, max-age=60")
        if method == "GET" and path == "/my/photos":
            owner_id = require_owner(claims)
            return response(200, {"photos": list_owner_photos(owner_id)})
        if method == "POST" and path == "/uploads":
            owner_id = require_owner(claims)
            if claims.get("email_verified", "false").lower() != "true":
                return response(403, {"message": "Verify your email before sharing a photo."})
            return create_upload(event, owner_id, claims)
        if method == "DELETE" and path.startswith("/my/photos/"):
            owner_id = require_owner(claims)
            photo_id = path.rsplit("/", 1)[-1]
            return delete_photo(owner_id, photo_id)
        return response(404, {"message": "That page is not here."})
    except ValueError as error:
        return response(400, {"message": str(error)})
    except PermissionError as error:
        return response(401, {"message": str(error)})
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code in ("ConditionalCheckFailedException", "TransactionCanceledException"):
            return response(409, {"message": "The photo could not be changed. Refresh and try again."})
        logger.exception("AWS request failed")
        return response(503, {"message": "Sunroom is having trouble reaching AWS. Try again shortly."})
    except Exception:
        logger.exception("Unexpected API error")
        return response(500, {"message": "Something went wrong. Try again shortly."})


def response(status_code, body=None, cache_control="no-store"):
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json; charset=utf-8", "cache-control": cache_control},
        "body": json.dumps(body or {}),
    }


def require_owner(claims):
    owner_id = claims.get("sub", "")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", owner_id):
        raise PermissionError("Sign in to continue.")
    return owner_id


def clean_text(value, limit):
    text = "".join(char for char in str(value or "") if char.isprintable()).strip()
    return text[:limit]


def photo_urls(photo_id):
    base = f"/photos/{quote(photo_id, safe='')}"
    return {size: f"{base}/{size}.jpg" for size in ("small", "medium", "large")}


def list_public_photos():
    result = table.query(
        IndexName=PUBLIC_INDEX,
        KeyConditionExpression=Key("GSI1PK").eq("GALLERY#APPROVED"),
        Limit=700,
        ScanIndexForward=False,
        ProjectionExpression="photoId, caption, displayName, createdAt",
    )
    photos = []
    for item in result.get("Items", []):
        photos.append({
            "photoId": item["photoId"],
            "caption": item.get("caption", ""),
            "displayName": item.get("displayName", "Member"),
            "createdAt": item["createdAt"],
            "urls": photo_urls(item["photoId"]),
        })
    return photos


def list_owner_photos(owner_id):
    result = table.query(
        IndexName=OWNER_INDEX,
        KeyConditionExpression=Key("GSI2PK").eq(f"USER#{owner_id}"),
        Limit=1000,
        ScanIndexForward=False,
        ProjectionExpression="photoId, #status, caption, displayName, createdAt, uploadExpiresAt, moderationReason, reservationBytes",
        ExpressionAttributeNames={"#status": "status"},
    )
    photos = []
    for item in result.get("Items", []):
        photo = {
            "photoId": item["photoId"],
            "status": item["status"],
            "caption": item.get("caption", ""),
            "displayName": item.get("displayName", "Member"),
            "createdAt": item["createdAt"],
            "uploadExpiresAt": item.get("uploadExpiresAt", ""),
            "moderationReason": item.get("moderationReason", ""),
            "canRemove": item["status"] in REMOVABLE_STATUSES,
        }
        if item["status"] == "APPROVED":
            photo["urls"] = photo_urls(item["photoId"])
        photos.append(photo)
    return photos


def marshal(item):
    return {key: serializer.serialize(value) for key, value in item.items()}


def monthly_upload_quota_key(now):
    month = now.strftime("%Y-%m")
    return {"PK": f"QUOTA#UPLOADS#{month}", "SK": "META"}


def ensure_user_topic(owner_id, email):
    suffix = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:24]
    topic_arn = sns.create_topic(Name=f"{SNS_TOPIC_PREFIX}{suffix}")["TopicArn"]
    token = None
    while True:
        args = {"TopicArn": topic_arn}
        if token:
            args["NextToken"] = token
        page = sns.list_subscriptions_by_topic(**args)
        for subscription in page.get("Subscriptions", []):
            if subscription.get("Protocol") == "email" and subscription.get("Endpoint", "").lower() == email.lower():
                return topic_arn, subscription.get("SubscriptionArn") == "PendingConfirmation"
        token = page.get("NextToken")
        if not token:
            break
    sns.subscribe(TopicArn=topic_arn, Protocol="email", Endpoint=email, ReturnSubscriptionArn=True)
    return topic_arn, True


def create_upload(event, owner_id, claims):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError as error:
        raise ValueError("Send a photo and try again.") from error

    content_type = str(body.get("contentType", "")).lower()
    extension = ALLOWED_TYPES.get(content_type)
    if not extension:
        raise ValueError("Choose a JPEG or PNG photo.")
    try:
        size = int(body.get("fileSize", 0))
    except (TypeError, ValueError) as error:
        raise ValueError("The photo size could not be read.") from error
    if size < 1 or size > MAX_UPLOAD_BYTES:
        raise ValueError("Photos must be 2 MiB or smaller.")

    email = clean_text(claims.get("email", ""), 254)
    if not email or "@" not in email:
        raise ValueError("Add a verified email address to your account before uploading.")
    if MONTHLY_LIMIT == 0:
        return response(503, {"message": "Uploads are paused while the automatic safety check limit is set to zero."})

    topic_arn, confirmation_requested = ensure_user_topic(owner_id, email)
    photo_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    next_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        next_month = next_month.replace(year=now.year + 1, month=1)
    else:
        next_month = next_month.replace(month=now.month + 1)
    upload_ttl = min(600, int((next_month - now).total_seconds()))
    if upload_ttl < 60:
        return response(429, {"message": "Photo uploads pause for the last minute of each month. Try again shortly."})
    created_at = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    upload_expires_at = (now + timedelta(seconds=upload_ttl)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    expires_at_epoch = int((now + timedelta(seconds=upload_ttl + 86400)).timestamp()) + 1
    upload_key = f"incoming/{owner_id}/{photo_id}.{extension}"
    original_key = f"originals/{owner_id}/{photo_id}.{extension}"
    caption = clean_text(body.get("caption", ""), 120)
    display_name = clean_text(claims.get("name", ""), 48) or "Member"
    photo = {
        "PK": f"PHOTO#{photo_id}",
        "SK": "META",
        "photoId": photo_id,
        "ownerId": owner_id,
        "status": "UPLOADING",
        "caption": caption,
        "displayName": display_name,
        "contentType": content_type,
        "createdAt": created_at,
        "uploadExpiresAt": upload_expires_at,
        "expiresAtEpoch": expires_at_epoch,
        "GSI2PK": f"USER#{owner_id}",
        "GSI2SK": f"{created_at}#{photo_id}",
        "uploadKey": upload_key,
        "originalKey": original_key,
        "snsTopicArn": topic_arn,
        "reservationBytes": ASSET_SLOT_BYTES,
    }

    upload_form = {
        "url": s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": UPLOADS_BUCKET,
                "Key": upload_key,
                "ContentType": content_type,
                "ContentLength": size,
                "IfNoneMatch": "*",
            },
            ExpiresIn=upload_ttl,
            HttpMethod="PUT",
        )
    }

    try:
        ddb.transact_write_items(TransactItems=[
            {
                "Update": {
                    "TableName": TABLE_NAME,
                    "Key": {"PK": {"S": "QUOTA#ASSETS"}, "SK": {"S": "META"}},
                    "UpdateExpression": "SET reservedBytes = if_not_exists(reservedBytes, :zero) + :slot",
                    "ConditionExpression": "attribute_not_exists(reservedBytes) OR reservedBytes <= :max",
                    "ExpressionAttributeValues": {
                        ":zero": {"N": "0"},
                        ":slot": {"N": str(ASSET_SLOT_BYTES)},
                        ":max": {"N": str(ASSET_STORAGE_LIMIT_BYTES - ASSET_SLOT_BYTES)},
                    },
                }
            },
            {
                "Update": {
                    "TableName": TABLE_NAME,
                    "Key": marshal(monthly_upload_quota_key(now)),
                    "UpdateExpression": "SET #uploads = if_not_exists(#uploads, :zero) + :one",
                    "ConditionExpression": "attribute_not_exists(#uploads) OR #uploads < :limit",
                    "ExpressionAttributeNames": {"#uploads": "uploads"},
                    "ExpressionAttributeValues": {
                        ":zero": {"N": "0"},
                        ":one": {"N": "1"},
                        ":limit": {"N": str(MONTHLY_LIMIT)},
                    },
                }
            },
            {
                "Put": {
                    "TableName": TABLE_NAME,
                    "Item": marshal(photo),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
        ])
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            reasons = error.response.get("CancellationReasons", [])
            if len(reasons) > 1 and reasons[1].get("Code") == "ConditionalCheckFailed":
                return response(429, {"message": "This month's free photo limit has been reached. Try again next month."})
            return response(409, {"message": "The photo wall has reached its storage limit. Remove a photo and try again."})
        raise

    return response(201, {
        "upload": upload_form,
        "photo": {
            "photoId": photo_id,
            "status": "UPLOADING",
            "caption": caption,
            "displayName": display_name,
            "createdAt": created_at,
            "uploadExpiresAt": upload_expires_at,
        },
        "notificationConfirmationRequested": confirmation_requested,
    })


def delete_photo(owner_id, photo_id):
    if not re.fullmatch(r"[0-9a-f-]{36}", photo_id):
        return response(404, {"message": "That photo could not be found."})
    item = get_photo(photo_id)
    if not item or item.get("ownerId") != owner_id:
        return response(404, {"message": "That photo could not be found."})
    if item.get("status") not in REMOVABLE_STATUSES:
        return response(409, {"message": "Wait for the safety check to finish before removing this photo."})

    table.update_item(
        Key={"PK": item["PK"], "SK": item["SK"]},
        UpdateExpression="SET #status = :deleting REMOVE GSI1PK, GSI1SK, expiresAtEpoch",
        ConditionExpression="ownerId = :owner AND #status <> :processing",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":owner": owner_id, ":processing": "PROCESSING", ":deleting": "DELETING"},
    )

    try:
        upload_keys = [item.get("uploadKey"), item.get("originalKey")]
        delete_objects(UPLOADS_BUCKET, upload_keys)
        delete_objects(MEDIA_BUCKET, [f"photos/{photo_id}/{size}.jpg" for size in ("small", "medium", "large")])
    except ClientError:
        logger.exception("S3 cleanup failed for %s", photo_id)
        return response(503, {"message": "Removal could not finish. Choose Remove again to retry."})

    reservation = int(item.get("reservationBytes", 0))
    transaction = []
    if reservation:
        transaction.append({
            "Update": {
                "TableName": TABLE_NAME,
                "Key": {"PK": {"S": "QUOTA#ASSETS"}, "SK": {"S": "META"}},
                "UpdateExpression": "SET reservedBytes = reservedBytes - :slot",
                "ConditionExpression": "reservedBytes >= :slot",
                "ExpressionAttributeValues": {":slot": {"N": str(reservation)}},
            }
        })
    transaction.append({
        "Delete": {
            "TableName": TABLE_NAME,
            "Key": {"PK": {"S": item["PK"]}, "SK": {"S": item["SK"]}},
            "ConditionExpression": "ownerId = :owner AND #status = :deleting",
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {":owner": {"S": owner_id}, ":deleting": {"S": "DELETING"}},
        }
    })
    ddb.transact_write_items(TransactItems=transaction)
    return {"statusCode": 204, "headers": {"cache-control": "no-store"}, "body": ""}


def get_photo(photo_id):
    result = table.get_item(Key={"PK": f"PHOTO#{photo_id}", "SK": "META"})
    return result.get("Item")


def delete_objects(bucket, keys):
    objects = [{"Key": key} for key in keys if key]
    if not objects:
        return
    result = s3.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
    if result.get("Errors"):
        raise ClientError({"Error": {"Code": "S3DeleteError", "Message": "Could not remove all photo objects."}}, "DeleteObjects")
