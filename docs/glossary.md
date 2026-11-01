# Glossary

| Term | Meaning in Sunroom |
| --- | --- |
| Incoming image | Private source file uploaded to the `incoming/` prefix while it waits for processing. |
| Original | Private copy of a source image retained after approval. |
| Thumbnail | Generated JPEG variant in the `small`, `medium`, or `large` size. Only approved thumbnails are served to the public. |
| Moderation check | Amazon Rekognition label detection run before thumbnail creation. A label at or above the configured confidence threshold rejects the photo. |
| Public gallery index | DynamoDB global secondary index containing only items with `status = APPROVED`. |
| Owner gallery index | DynamoDB global secondary index used to show a signed-in user their own photo statuses. |
| Storage reservation | Six MiB held against the app-wide storage ceiling for each pending or approved photo. Rejection, cleanup, or deletion releases it. |
| Upload quota | Monthly limit on upload URLs issued by the API. |
| Moderation quota | Separate monthly limit on Rekognition calls started by the Step Functions workflow. |
| TTL cleanup | DynamoDB's asynchronous deletion of expired upload records. A Streams event removes a staged source and releases its reservation. |
| CloudFront Free plan | A flat-rate CloudFront subscription with a zero monthly price and no usage overage charges for covered CloudFront services. |
