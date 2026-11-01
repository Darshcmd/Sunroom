import io
import logging
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from PIL import Image, ImageOps, UnidentifiedImageError


logger = logging.getLogger()
logger.setLevel(logging.WARNING)
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["TABLE_NAME"])
ddb = dynamodb.meta.client
s3 = boto3.client("s3")
rekognition = boto3.client("rekognition")
sns = boto3.client("sns")
serializer = TypeSerializer()

TABLE_NAME = os.environ["TABLE_NAME"]
UPLOADS_BUCKET = os.environ["UPLOADS_BUCKET"]
MEDIA_BUCKET = os.environ["MEDIA_BUCKET"]
SLOT_BYTES = int(os.environ["ASSET_SLOT_BYTES"])
MONTHLY_LIMIT = int(os.environ["MONTHLY_MODERATION_LIMIT"])
MAX_UPLOAD_BYTES = int(os.environ["MAX_UPLOAD_BYTES"])
MIN_CONFIDENCE = float(os.environ["MODERATION_CONFIDENCE"])
ASSET_SIZES = {"small": (360, 360), "medium": (960, 960), "large": (1600, 1600)}
MAX_THUMBNAILS_BYTES = 4 * 1024 * 1024


def workflow_handler(event, _context):
    action = event.get("action")
    if action == "moderate":
        return moderate(event["photoId"])
    if action == "create_thumbnails":
        return create_thumbnails(event["photoId"])
    if action == "mark_approved":
        return mark_approved(event["photoId"])
    if action == "mark_failed":
        return mark_failed(event["photoId"], event.get("error", {}))
    if action == "notify":
        return notify(event["photoId"], event["status"])
    raise ValueError("Unknown workflow action")


def get_photo(photo_id):
    result = table.get_item(Key={"PK": f"PHOTO#{photo_id}", "SK": "META"})
    return result.get("Item")


def low_level(value):
    return {key: serializer.serialize(item) for key, item in value.items()}


def update_status_and_release(item, status, reason, expected_statuses=("PROCESSING",)):
    reservation = int(item.get("reservationBytes", 0))
    status_values = {f":expected{index}": {"S": value} for index, value in enumerate(expected_statuses)}
    status_condition = " OR ".join(f"#status = :expected{index}" for index in range(len(expected_statuses)))
    update = {
        "Update": {
            "TableName": TABLE_NAME,
            "Key": {"PK": {"S": "QUOTA#ASSETS"}, "SK": {"S": "META"}},
            "UpdateExpression": "SET reservedBytes = reservedBytes - :slot",
            "ConditionExpression": "reservedBytes >= :slot",
            "ExpressionAttributeValues": {":slot": {"N": str(reservation)}},
        }
    }
    photo_update = {
        "Update": {
            "TableName": TABLE_NAME,
            "Key": {"PK": {"S": item["PK"]}, "SK": {"S": item["SK"]}},
            "UpdateExpression": "SET #status = :status, moderationReason = :reason, reservationBytes = :zero REMOVE GSI1PK, GSI1SK, expiresAtEpoch",
            "ConditionExpression": status_condition,
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {
                ":status": {"S": status},
                ":reason": {"S": reason},
                ":zero": {"N": "0"},
                **status_values,
            },
        }
    }

    if reservation:
        ddb.transact_write_items(TransactItems=[update, photo_update])
    else:
        ddb.update_item(**photo_update["Update"])


def moderation_count_key():
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return {"PK": f"QUOTA#MODERATION#{month}", "SK": "META"}


def reserve_moderation_check():
    key = moderation_count_key()
    ddb.update_item(
        TableName=TABLE_NAME,
        Key=low_level(key),
        UpdateExpression="SET #checks = if_not_exists(#checks, :zero) + :one",
        ConditionExpression="attribute_not_exists(#checks) OR #checks < :limit",
        ExpressionAttributeNames={"#checks": "checks"},
        ExpressionAttributeValues={
            ":zero": {"N": "0"},
            ":one": {"N": "1"},
            ":limit": {"N": str(MONTHLY_LIMIT)},
        },
    )


def moderate(photo_id):
    item = get_photo(photo_id)
    if not item or item.get("status") != "UPLOADING":
        return {"photoId": photo_id, "decision": "SKIP"}

    try:
        table.update_item(
            Key={"PK": item["PK"], "SK": item["SK"]},
            UpdateExpression="SET #status = :processing REMOVE expiresAtEpoch",
            ConditionExpression="#status = :uploading",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":processing": "PROCESSING", ":uploading": "UPLOADING"},
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return {"photoId": photo_id, "decision": "SKIP"}
        raise

    try:
        reserve_moderation_check()
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        s3.delete_object(Bucket=UPLOADS_BUCKET, Key=item["uploadKey"])
        update_status_and_release(
            {**item, "status": "PROCESSING"},
            "QUOTA_PAUSED",
            "This month's automatic safety check limit has been reached.",
        )
        return {"photoId": photo_id, "status": "QUOTA_PAUSED"}

    result = rekognition.detect_moderation_labels(
        Image={"S3Object": {"Bucket": UPLOADS_BUCKET, "Name": item["uploadKey"]}},
        MinConfidence=MIN_CONFIDENCE,
    )
    labels = result.get("ModerationLabels", [])
    if labels:
        s3.delete_object(Bucket=UPLOADS_BUCKET, Key=item["uploadKey"])
        update_status_and_release(
            {**item, "status": "PROCESSING"},
            "REJECTED",
            "This photo did not pass the automatic safety check.",
        )
        return {"photoId": photo_id, "status": "REJECTED"}

    return {"photoId": photo_id, "decision": "SAFE"}


def render_thumbnails(source_bytes):
    try:
        with Image.open(io.BytesIO(source_bytes)) as opened:
            if opened.format not in ("JPEG", "PNG"):
                raise ValueError("Only JPEG and PNG photos are supported.")
            if opened.width * opened.height > 40000000:
                raise ValueError("This photo has too many pixels to process.")
            image = ImageOps.exif_transpose(opened)
            if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")

            outputs = {}
            for size_name, bounds in ASSET_SIZES.items():
                resized = image.copy()
                resized.thumbnail(bounds, Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                resized.save(buffer, format="JPEG", quality=82, optimize=True, progressive=True)
                outputs[size_name] = buffer.getvalue()
            if sum(map(len, outputs.values())) > MAX_THUMBNAILS_BYTES:
                raise ValueError("These thumbnails would exceed the per-photo storage allowance.")
            return outputs
    except (UnidentifiedImageError, OSError) as error:
        raise ValueError("The uploaded file is not a readable JPEG or PNG image.") from error


def create_thumbnails(photo_id):
    item = get_photo(photo_id)
    if not item or item.get("status") != "PROCESSING":
        return {"photoId": photo_id, "decision": "SKIP"}

    uploaded = []
    final_original = item["originalKey"]
    try:
        response = s3.get_object(Bucket=UPLOADS_BUCKET, Key=item["uploadKey"])
        body = response["Body"].read(MAX_UPLOAD_BYTES + 1)
        if len(body) > MAX_UPLOAD_BYTES:
            raise ValueError("The uploaded photo exceeds the size limit.")
        images = render_thumbnails(body)
        s3.copy_object(
            Bucket=UPLOADS_BUCKET,
            Key=final_original,
            CopySource={"Bucket": UPLOADS_BUCKET, "Key": item["uploadKey"]},
            MetadataDirective="COPY",
        )
        s3.delete_object(Bucket=UPLOADS_BUCKET, Key=item["uploadKey"])

        for size_name, image_bytes in images.items():
            key = f"photos/{photo_id}/{size_name}.jpg"
            s3.put_object(
                Bucket=MEDIA_BUCKET,
                Key=key,
                Body=image_bytes,
                ContentType="image/jpeg",
                CacheControl="public, max-age=60",
            )
            uploaded.append(key)

    except Exception:
        for key in uploaded:
            s3.delete_object(Bucket=MEDIA_BUCKET, Key=key)
        s3.delete_object(Bucket=UPLOADS_BUCKET, Key=final_original)
        raise

    return {"photoId": photo_id, "decision": "SAFE"}


def mark_approved(photo_id):
    item = get_photo(photo_id)
    if not item or item.get("status") != "PROCESSING":
        return {"photoId": photo_id, "status": item.get("status", "MISSING") if item else "MISSING", "decision": "SKIP"}

    table.update_item(
        Key={"PK": item["PK"], "SK": item["SK"]},
        UpdateExpression="SET #status = :approved, GSI1PK = :gallery, GSI1SK = :sort REMOVE moderationReason, expiresAtEpoch",
        ConditionExpression="#status = :processing",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":approved": "APPROVED",
            ":gallery": "GALLERY#APPROVED",
            ":sort": f"{item['createdAt']}#{photo_id}",
            ":processing": "PROCESSING",
        },
    )
    return {"photoId": photo_id, "status": "APPROVED"}


def mark_failed(photo_id, error):
    item = get_photo(photo_id)
    if not item or item.get("status") in ("DELETING", "APPROVED", "REJECTED", "QUOTA_PAUSED"):
        return {"photoId": photo_id, "status": item.get("status") if item else "MISSING", "decision": "SKIP"}

    cleanup_ok = True
    for bucket, key in (
        (UPLOADS_BUCKET, item.get("uploadKey")),
        (UPLOADS_BUCKET, item.get("originalKey")),
    ):
        if key:
            try:
                s3.delete_object(Bucket=bucket, Key=key)
            except Exception:
                cleanup_ok = False

    for size_name in ASSET_SIZES:
        try:
            s3.delete_object(Bucket=MEDIA_BUCKET, Key=f"photos/{photo_id}/{size_name}.jpg")
        except Exception:
            cleanup_ok = False

    reason = "This photo could not be processed. Remove it and try again."
    if cleanup_ok:
        try:
            update_status_and_release({**item, "status": item.get("status", "PROCESSING")}, "FAILED", reason, ("PROCESSING", "UPLOADING"))
        except ClientError as update_error:
            if update_error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    else:
        table.update_item(
            Key={"PK": item["PK"], "SK": item["SK"]},
            UpdateExpression="SET #status = :failed, moderationReason = :reason REMOVE GSI1PK, GSI1SK, expiresAtEpoch",
            ConditionExpression="#status = :processing OR #status = :uploading",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":failed": "FAILED",
                ":reason": reason,
                ":processing": "PROCESSING",
                ":uploading": "UPLOADING",
            },
        )

    logger.warning("Photo processing failed for %s: %s", photo_id, error.get("Error", "unknown"))
    return {"photoId": photo_id, "status": "FAILED"}


def notify(photo_id, status):
    item = get_photo(photo_id)
    if not item or not item.get("snsTopicArn"):
        return {"photoId": photo_id, "notified": False}

    title = item.get("caption") or "Your photo"
    if status == "APPROVED":
        message = f"{title} passed the safety check and is now in the public gallery. Open Sunroom to see it on the photo wall."
    elif status == "REJECTED":
        message = f"{title} did not pass the automatic safety check. It was not published."
    elif status == "QUOTA_PAUSED":
        message = "Your photo was not published because this month's automatic safety-check limit has been reached. Try again next month."
    else:
        message = f"{title} could not be processed. Remove it from My photos and try again."

    sns.publish(TopicArn=item["snsTopicArn"], Subject="Your Sunroom photo update", Message=message)
    return {"photoId": photo_id, "notified": True}
