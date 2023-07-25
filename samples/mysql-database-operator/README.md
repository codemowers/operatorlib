# MySQL database operator

This operator manages logical databases in a MySQL or MariaDB database
and optionally instantiates a MariaDB cluster using
[mariadb-operator](https://github.com/mariadb-operator/mariadb-operator)

To order a database use following,
note the `capacity` ends up as persistent volume size for dedicated clusters.
In case of shared clusters it has no effect:

```
---
apiVersion: codemowers.cloud/v1beta1
kind: MysqlDatabaseClaim
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
  class: shared
```

