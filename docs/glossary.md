# Glossary

| Term | Meaning in Sunroom |
| --- | --- |
| Incoming image | Private source file uploaded to the `incoming/` prefix while it waits for processing. |
| Original | Private copy of the uploaded image retained after processing. |
| Thumbnail | Generated JPEG variant in the `small`, `medium`, or `large` size. The API gives the browser a time-limited signed URL for each published thumbnail. |
| Public gallery index | DynamoDB global secondary index containing items with `status = READY`. |
| Owner gallery index | DynamoDB global secondary index used to show a signed-in user their own photo statuses. |
| Storage reservation | Six MiB held against the app-wide storage ceiling for each pending or ready photo. Processing failures and deletion release it. |
| Upload quota | Monthly limit on upload tickets issued by the API. |
| TTL cleanup | DynamoDB's asynchronous deletion of expired upload records. A Streams event removes a staged source and releases its reservation. |
