# Architecture notes

## Request and media paths

API Gateway serves the static frontend over HTTPS and routes application requests to the API Lambda. The Lambda reads site files from a private S3 bucket. Cognito handles sign-up and sign-in; the API uses a Cognito JWT authorizer on owner routes.

A signed S3 PUT URL includes the exact content type and file length plus an `If-None-Match: *` condition. The browser can create one object at the unique incoming key. The upload bucket and original photos remain private.

S3 sends an Object Created event to EventBridge. A small Lambda starts a Standard Step Functions execution named with the photo ID. The workflow marks the upload as processing, uses Pillow to create three thumbnail sizes, copies the original to a private prefix, deletes the staged object, and adds the photo to the gallery index. No moderation service or manual approval step is used.

The API returns six-hour presigned S3 URLs for gallery thumbnails. Originals are never returned. Each user has an SNS topic derived from a one-way hash of the Cognito subject. The first upload subscribes the user's email address; the user must confirm that subscription to receive notifications.

## Data and status model

The DynamoDB table stores photo items under `PHOTO#{photoId}` and quota items under separate `QUOTA#...` keys. `PublicGallery` contains `READY` photo keys and display fields. `OwnerGallery` contains each user's uploads and status details.

```text
UPLOADING -> PROCESSING -> READY
                         -> FAILED
UPLOADING -> DELETING -> removed
PROCESSING -> DELETING -> removed after processing exits
READY     -> DELETING -> removed
FAILED    -> DELETING -> removed
```

An upload record receives a DynamoDB TTL one day after its signed URL expires. Processing removes that TTL. If no upload arrives, DynamoDB eventually deletes the stale record and a Streams-triggered Lambda deletes any staged object and releases its reservation. A short-lived marker prevents retries from releasing the same reservation twice.

## Operating limits

- Images are JPEG or PNG, up to 2 MiB and 40 million pixels.
- Thumbnails are at most 360, 960, and 1,600 pixels on the longer dimension.
- The default upload allowance is 350 photos per UTC month; set it to zero to pause uploads.
- A four GiB reservation ceiling bounds retained originals and thumbnails.
- API Gateway serves the site and API; S3, EventBridge, Lambda, Step Functions, DynamoDB, Cognito, and SNS handle requests and events without always-on compute.
- AWS Free Tier eligibility varies by account and service. Usage beyond applicable free allowances can be billed.
