# Architecture notes

## Request and media paths

CloudFront hosts the static frontend, proxies API routes, and serves approved thumbnails. Its Free plan includes the WAF web ACL and a small IP rate rule. The public gallery API is cached for 60 seconds. Owner routes and upload-ticket requests use a disabled-cache policy and forward the authorization header to API Gateway.

The API uses a Cognito JWT authorizer. A signed S3 PUT URL includes the exact content type and file length plus an `If-None-Match: *` condition. The browser can create one object at the unique incoming key. The original S3 bucket remains private.

S3 sends an Object Created event to EventBridge. A small Lambda starts a Standard Step Functions execution named with the photo ID. The deterministic execution name makes duplicate S3 events harmless. The workflow reserves a moderation count, calls Rekognition, and only creates thumbnails after the image passes the configured confidence threshold. It then copies the original to a private prefix, deletes the staged upload, and marks the DynamoDB item approved. Only the approved state adds the item to the public gallery index.

Each user has an SNS topic derived from a one-way hash of the Cognito subject. The first upload subscribes the verified email address. The user must confirm that SNS email subscription to receive later notifications.

## Data and status model

The DynamoDB table stores photo items under `PHOTO#{photoId}` and quota items under separate `QUOTA#...` keys. `PublicGallery` contains approved photo keys and display fields. `OwnerGallery` contains each user's uploads and status details.

The photo lifecycle is:

```text
UPLOADING -> PROCESSING -> APPROVED
                         -> REJECTED
                         -> QUOTA_PAUSED
                         -> FAILED
UPLOADING -> DELETING -> removed
APPROVED  -> DELETING -> removed
REJECTED  -> DELETING -> removed
FAILED    -> DELETING -> removed
```

An upload record receives a DynamoDB TTL one day after its signed URL expires. Processing removes that TTL. If no upload arrives, DynamoDB eventually deletes the stale record and a Streams-triggered Lambda deletes any staged object and releases its reservation. The stream cleanup writes a short-lived marker in the same transaction as the counter update so a retried stream record cannot release the same reservation twice.

## Decision record

### Use managed serverless services with explicit usage ceilings

The app has no EC2 instance, container service, or always-running backend. S3, EventBridge, Lambda, Step Functions, DynamoDB, Cognito, SNS, API Gateway, CloudFront, and WAF handle each request or event.

The upload and moderation quotas are independent. This avoids relying on workflow timing to protect the Rekognition count. A four GiB reservation ceiling bounds retained app assets, while a one-day S3 lifecycle rule and DynamoDB TTL handle abandoned staged uploads. A signed conditional PUT prevents replay from overwriting the same object key.

The gallery exposes only generated thumbnails. Full-size originals stay private. This keeps the public delivery path limited to approved media and lets CloudFront cache the small set of image variants.

## Operating limits

- Images are JPEG or PNG, up to 2 MiB and 40 million pixels.
- Thumbnails are 360, 960, and 1,600 pixels at most on the longer dimension.
- The default upload and moderation allowance is 350 photos per UTC month.
- New uploads can be disabled with a monthly limit of zero.
- The stack uses `us-east-1` for the CloudFront WAF web ACL and Free plan subscription.
- The CloudFront plan is activated by the deploy script after the stack exists and before it publishes the frontend.
- CloudFront plan cancellation may be scheduled for the end of a billing period. The cleanup script empties the buckets first and removes the remaining stack resources after cancellation takes effect.
