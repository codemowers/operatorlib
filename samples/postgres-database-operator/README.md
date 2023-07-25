# Postgres database operator

This operator manages logical databases in a Postgres database
and optionally instantiates a Postgres cluster using
[CloudNativePG](https://cloudnative-pg.io/)

To order a Postgres database use following,
note the `capacity` ends up as persistent volume size for dedicated clusters,
in case of shared clusters it has no effect although in future `pg_quota`
or similar might be enforced:

```
---
apiVersion: codemowers.cloud/v1beta1
kind: PostgresDatabaseClaim
metadata:
  name: example
spec:
  capacity: 1Gi
  class: shared
---
apiVersion: codemowers.cloud/v1beta1
kind: PostgresDatabaseClaim
metadata:
  name: example
spec:
  capacity: 1Gi
  class: dedicated
```

