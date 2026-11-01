# ADR 0001: Use managed serverless services with explicit usage ceilings

## Status

Accepted

## Context

Sunroom needs account sign-in, private image intake, asynchronous moderation, image variants, a public approved gallery, and completion notifications. It also needs to avoid always-running compute and limit usage to available AWS free allowances.

## Decision

Use Cognito, API Gateway HTTP API, Lambda, EventBridge, Step Functions Standard, S3, Rekognition, DynamoDB, SNS, CloudFront, and WAF. Keep all runtime buckets private and serve only approved thumbnail objects through CloudFront. Use independent monthly upload and moderation counters, a 4 GiB reservation limit, a 2 MiB input limit, and low API throttling. Route abandoned upload cleanup from DynamoDB TTL and Streams instead of a scheduled or always-on service.

Activate the CloudFront Free plan before publishing the frontend. Use no external auth, image processing, storage, or analytics service.

## Consequences

- No EC2, long-running service, or production container is required.
- Images are processed only after a private S3 upload event.
- Public gallery items are indexed only after moderation and thumbnail creation succeed.
- Upload URL issuance and actual moderation have separate hard monthly counters.
- The Free plan does not cover every AWS service. AWS account eligibility and time-limited allowances still affect the final bill. Set the monthly photo limit to zero when moderation is not within an available free allowance.
- Cancelling the CloudFront Free plan may take effect at the end of the billing period, so full stack cleanup can be delayed.
