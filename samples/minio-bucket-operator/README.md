# Minio bucket operator

The Minio bucket operator manages buckets in a Minio cluster and
optionally manages a Minio cluster itself.

To order S3 bucket, note the `capacity` ends up as quota for the bucket:

```
---
apiVersion: codemowers.cloud/v1beta1
kind: MinioBucketClaim
metadata:
  name: example
spec:
  capacity: 1Gi
  class: shared
```

In this case `miniobucket-example-owner-secrets` is returned into the originating namespace
