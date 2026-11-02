# ADR 0001: Use managed serverless services with explicit usage ceilings

## Status

Accepted

## Context

Sunroom needs account sign-in, private image intake, asynchronous thumbnail generation, a public gallery, and completion notifications. It should avoid always-running compute and limit usage for a personal project.

## Decision

Use Cognito, API Gateway HTTP API, Lambda, EventBridge, Step Functions Standard, S3, DynamoDB, and SNS. Keep the site, upload, original, and thumbnail buckets private. API Gateway serves site files from S3; gallery responses contain temporary presigned URLs for generated thumbnails. Use one monthly upload counter, a 4 GiB storage reservation limit, a 2 MiB input limit, and API throttling. Use DynamoDB TTL and Streams for abandoned upload cleanup.

Do not run image moderation or require manual approval. A successful thumbnail workflow publishes the photo and notifies its owner.

## Consequences

- No EC2, long-running service, CloudFront distribution, or production container is required.
- Photos are processed after a private S3 upload event.
- Generated thumbnails become public through temporary signed links; original photos stay private.
- API Gateway and Lambda handle website file requests as well as application API requests.
- AWS Free Tier eligibility varies by account and service; usage above applicable allowances can be billed.
