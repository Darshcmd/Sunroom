import io
import logging
import os
from decimal import Decimal

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
sns = boto3.client("sns")
serializer = TypeSerializer()

TABLE_NAME = os.environ["TABLE_NAME"]
UPLOADS_BUCKET = os.environ["UPLOADS_BUCKET"]
MEDIA_BUCKET = os.environ["MEDIA_BUCKET"]
SLOT_BYTES = int(os.environ["ASSET_SLOT_BYTES"])
MAX_UPLOAD_BYTES = int(os.environ["MAX_UPLOAD_BYTES"])
ASSET_SIZES = {"small": (360, 360), "medium": (960, 960), "large": (1600, 1600)}
MAX_THUMBNAILS_BYTES = 4 * 1024 * 1024


def workflow_handler(event, _context):
    action = event.get("action")
    if action == "begin_processing":
        return begin_processing(event["photoId"])
    if action == "create_thumbnails":
        return create_thumbnails(event["photoId"])
    if action == "mark_ready":
        return mark_ready(event["photoId"])
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


def update_status_and_release(item, status, expected_statuses=("PROCESSING",)):
    reservation = int(item.get("reservationBytes", 0))
    status_values = {f":expected{index}": value for index, value in enumerate(expected_statuses)}
    status_condition = " OR ".join(f"#status = :expected{index}" for index in range(len(expected_statuses)))
    quota_key = {"PK": "QUOTA#ASSETS#V4", "SK": "META"}
    previous_counter = None
    if reservation:
        quota_item = table.get_item(Key=quota_key, ConsistentRead=True).get("Item", {})
        previous_counter = quota_item.get("assetBytesReservedV4")
        if not isinstance(previous_counter, (int, Decimal)) or int(previous_counter) < reservation:
            raise ValueError("Asset reservation counter is invalid.")
        previous_counter = int(previous_counter)
        table.update_item(
            Key=quota_key,
            UpdateExpression="SET #counter = :new",
            ConditionExpression="#counter = :previous",
            ExpressionAttributeNames={"#counter": "assetBytesReservedV4"},
            ExpressionAttributeValues={":new": previous_counter - reservation, ":previous": previous_counter},
        )

    try:
        table.update_item(
            Key={"PK": item["PK"], "SK": item["SK"]},
            UpdateExpression="SET #status = :status, reservationBytes = :zero REMOVE GSI1PK, GSI1SK, expiresAtEpoch",
            ConditionExpression=status_condition,
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": status, ":zero": 0, **status_values},
        )
    except ClientError:
        if reservation:
            try:
                table.update_item(
                    Key=quota_key,
                    UpdateExpression="SET #counter = :previous",
                    ConditionExpression="#counter = :current",
                    ExpressionAttributeNames={"#counter": "assetBytesReservedV4"},
                    ExpressionAttributeValues={":previous": previous_counter, ":current": previous_counter - reservation},
                )
            except ClientError:
                logger.exception("Could not restore asset reservation after photo status update failed")
        raise


def begin_processing(photo_id):
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

    return {"photoId": photo_id, "decision": "PROCESS"}


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
                CacheControl="private, max-age=300",
            )
            uploaded.append(key)

    except Exception:
        for key in uploaded:
            s3.delete_object(Bucket=MEDIA_BUCKET, Key=key)
        s3.delete_object(Bucket=UPLOADS_BUCKET, Key=final_original)
        raise

    return {"photoId": photo_id, "decision": "CREATED"}


def mark_ready(photo_id):
    item = get_photo(photo_id)
    if not item or item.get("status") != "PROCESSING":
        return {"photoId": photo_id, "status": item.get("status", "MISSING") if item else "MISSING", "decision": "SKIP"}

    table.update_item(
        Key={"PK": item["PK"], "SK": item["SK"]},
        UpdateExpression="SET #status = :ready, GSI1PK = :gallery, GSI1SK = :sort REMOVE expiresAtEpoch",
        ConditionExpression="#status = :processing",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":ready": "READY",
            ":gallery": "GALLERY#READY",
            ":sort": f"{item['createdAt']}#{photo_id}",
            ":processing": "PROCESSING",
        },
    )
    return {"photoId": photo_id, "status": "READY"}


def mark_failed(photo_id, error):
    item = get_photo(photo_id)
    if not item or item.get("status") in ("DELETING", "READY"):
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

    if cleanup_ok:
        try:
            update_status_and_release(
                {**item, "status": item.get("status", "PROCESSING")},
                "FAILED",
                ("PROCESSING", "UPLOADING"),
            )
        except ClientError as update_error:
            if update_error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    else:
        table.update_item(
            Key={"PK": item["PK"], "SK": item["SK"]},
            UpdateExpression="SET #status = :failed REMOVE GSI1PK, GSI1SK, expiresAtEpoch",
            ConditionExpression="#status = :processing OR #status = :uploading",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":failed": "FAILED", ":processing": "PROCESSING", ":uploading": "UPLOADING"},
        )

    logger.warning("Photo processing failed for %s: %s", photo_id, error.get("Error", "unknown"))
    return {"photoId": photo_id, "status": "FAILED"}


def notify(photo_id, status):
    item = get_photo(photo_id)
    if not item or not item.get("snsTopicArn"):
        return {"photoId": photo_id, "notified": False}

    title = item.get("caption") or "Your photo"
    if status == "READY":
        message = f"{title} has finished processing and is now in the public gallery. Open Sunroom to see it on the photo wall."
    else:
        message = f"{title} could not be processed. Remove it from My photos and try again."

    sns.publish(TopicArn=item["snsTopicArn"], Subject="Your Sunroom photo update", Message=message)
    return {"photoId": photo_id, "notified": True}
