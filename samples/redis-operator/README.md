# Redis

The Redis operator instantiates a Redis cluster and manages master election for it.
Note that you might want to distinguish `persistent` and `ephemeral` classes for Redis:

```
---
apiVersion: codemowers.cloud/v1beta1
kind: RedisClaim
metadata:
  name: example
spec:
  capacity: 512Mi
  class: ephemeral
---
apiVersion: codemowers.cloud/v1beta1
kind: RedisClaim
metadata:
  name: example
spec:
  capacity: 512Mi
  class: persistent
```

In this case `redis-example-owner-secrets` is returned into the originating namespace.

Note that `capacity` is used to derive `maxmemory` configuration for the Redis,
for persistent scenarios underlying persistent volume is allocated twice the amount
to accommodate BGSAVE behaviour.

In the persistent mode BGSAVE behaviour dumps the memory contents to disk
at least once per hour up to once per minute if there are more than 10000 write operations.
Note that AOF is not used in persistent mode so there is a chance to lose
writes which happened between BGSAVE operations.

You really should not use Redis as durable data storage.
