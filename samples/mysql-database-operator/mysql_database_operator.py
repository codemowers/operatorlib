#!/usr/bin/env python3
import aiomysql
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException
from operatorlib import \
    ReconcileError, \
    CustomResourceMixin, \
    SharedMixin, \
    PersistentVolumeClaimMixin, \
    PersistentMixin, \
    RoutedMixin, \
    PodSpecMixin, \
    ReplicasSpecMixin, \
    CapacityMixin, \
    InstanceSecretMixin, \
    ClaimSecretMixin, \
    ClassedOperator


class MysqlDatabaseOperator(SharedMixin,
                            PersistentVolumeClaimMixin,
                            PersistentMixin,
                            RoutedMixin,
                            PodSpecMixin,
                            ReplicasSpecMixin,
                            CapacityMixin,
                            InstanceSecretMixin,
                            ClaimSecretMixin,
                            CustomResourceMixin,
                            ClassedOperator):
    """
    MySQL operator implementation
    """
    OPERATOR = "mysql-database-operator"
    GROUP = "codemowers.cloud"
    VERSION = "v1beta1"
    SINGULAR = "MysqlDatabase"
    PLURAL = "MysqlDatabases"

    CONDITION_INSTANCE_DATABASE_CREATED = "DatabaseCreated"
    CONDITION_INSTANCE_USER_CREATED = "DatabaseUserCreated"
    CONDITION_INSTANCE_PRIVILEGES_GRANTED = "DatabasePrivilegesGranted"

    SAFE_PRIVILEGES = (
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "CREATE",
        "DROP",
        "INDEX",
        "ALTER",
        "CREATE TEMPORARY TABLES"
    )

    _phpmyadmin_connections = set()

    @classmethod
    def get_instance_condition_set(cls):
        return super().get_instance_condition_set() + [
            cls.CONDITION_INSTANCE_DATABASE_CREATED,
            cls.CONDITION_INSTANCE_USER_CREATED,
            cls.CONDITION_INSTANCE_PRIVILEGES_GRANTED,
        ]

    def get_persistent_volume_capacity(self):
        return self.get_capacity()

    def get_persistent_volume_claim_name(self):
        return "storage"

    @classmethod
    def get_target_namespace(cls):
        return "mysql-clusters"

    def get_instance_name(self):
        """
        Generate target resource name
        """
        return "%s_%s" % (self.spec["claimRef"]["namespace"], self.uid)

    async def generate_instance_secret(self):
        pwd = self.generate_random_string(32)
        yield "MYSQL_HOST", "primary-%s.%s.svc" % (
            self.get_target_name(), self.get_target_namespace())
        yield "MYSQL_TCP_PORT", "3306"
        yield "MYSQL_PWD", pwd

    async def generate_claim_secret(self):
        username = database = self.get_instance_name()
        password = self.generate_random_string(128)
        yield "MYSQL_HOST", self.instance_secret["MYSQL_HOST"]
        yield "MYSQL_TCP_PORT", self.instance_secret["MYSQL_TCP_PORT"]
        yield "MYSQL_USER", username
        yield "MYSQL_PWD", password
        yield "MYSQL_DATABASE", database
        yield "DATABASE_URL", "mysql://%s:%s@%s:%d/%s" % (
            username,
            password,
            self.instance_secret["MYSQL_HOST"],
            int(self.instance_secret["MYSQL_TCP_PORT"]),
            database)

    async def reconcile_instance(self):
        await super().reconcile_instance()

        try:
            self.get_instance_condition(self.CONDITION_INSTANCE_PRIVILEGES_GRANTED)
        except self.InstanceConditionNotSet:
            pass
        else:
            return

        try:
            conn = await aiomysql.connect(
                host=self.instance_secret["MYSQL_HOST"],
                user="root",
                password=self.instance_secret["MYSQL_PWD"],
                port=int(self.instance_secret["MYSQL_TCP_PORT"]))
            cur = await conn.cursor()

            # Create database
            await cur.execute("CREATE DATABASE IF NOT EXISTS `%s`" % self.claim_secret["MYSQL_DATABASE"])
            await self.set_instance_condition(self.CONDITION_INSTANCE_DATABASE_CREATED)

            # Create user
            await cur.execute("CREATE USER IF NOT EXISTS '%(MYSQL_USER)s'@'%%' IDENTIFIED BY '%(MYSQL_PWD)s'" %
                self.claim_secret)
            await self.set_instance_condition(self.CONDITION_INSTANCE_USER_CREATED)

            # Set grants
            await cur.execute("GRANT %s ON `%s`.* TO '%s'@'%%'" % (
                ", ".join(self.SAFE_PRIVILEGES),
                self.claim_secret["MYSQL_DATABASE"],
                self.claim_secret["MYSQL_USER"]))
            await cur.execute("FLUSH PRIVILEGES")
            await self.set_instance_condition(self.CONDITION_INSTANCE_PRIVILEGES_GRANTED)
        except aiomysql.OperationalError as e:
            raise ReconcileError("Failed to connect to MySQL server at %s: %s" %
                (self.instance_secret["MYSQL_HOST"], e))

        self._phpmyadmin_connections.add(self.instance_secret["MYSQL_HOST"])
        connections = [(h, 3306) for h in sorted(self._phpmyadmin_connections)]

        body = {
            "metadata": {
                "name": "phpmyadmin-connections",
                "namespace": self.get_target_namespace(),
            },
            "data": {
                "PMA_HOSTS": ",".join([h for h, p in connections]),
                "PMA_PORTS": ",".join([str(p) for h, p in connections])
            }
        }
        try:
            await self.v1.replace_namespaced_config_map(
                body["metadata"]["name"],
                body["metadata"]["namespace"],
                client.V1ConfigMap(**body))
        except ApiException as e:
            if e.status == 404:
                await self.v1.create_namespaced_config_map(
                    body["metadata"]["namespace"],
                    client.V1ConfigMap(**body))
            else:
                raise

    @classmethod
    def generate_operator_cluster_role_rules(cls):
        yield from super().generate_operator_cluster_role_rules()
        yield "mariadb.mmontes.io", "mariadbs", ("create", "delete", "patch")
        yield "", "configmaps", ("update", "create")

    def generate_mariadb_spec(self):
        cnt = self.class_spec["podSpec"]["containers"][0]
        i, t = cnt["image"].split(":", 1)
        d = {
            "image": {
                "repository": i,
                "tag": t,
                "pullPolicy": cnt["imagePullPolicy"],
            },
            "rootPasswordSecretKeyRef": {
                "name": self.get_instance_secret_name(),
                "key": "MYSQL_PWD",
            },
            "replicas": self.class_spec["replicas"],

            "volumeClaimTemplate": {
                "storageClassName": self.class_spec.get("storageClass"),
                "accessModes": ["ReadWriteOnce"],
                "resources": {
                    "requests": {
                        "storage": self.get_capacity(),
                    }
                }
            },
            "affinity": {
                "podAntiAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": [{
                        "labelSelector": self.label_selector,
                        "topologyKey": self.get_pod_topology_key(),
                    }]
                }
            }
        }

        if self.class_spec["replicas"] > 1:
            d["replication"] = { "enabled": True }
            d["podDisruptionBudget"] = { "maxUnavailable": 1 }
        return d

    def generate_custom_resources(self):
        if "podSpec" not in self.class_spec:
            return []
        if "replicas" not in self.class_spec:
            return []
        if "storageClass" not in self.class_spec:
            return []

        if "mariadb" in self.class_spec["podSpec"]["containers"][0]["image"]:
            return [{
                "apiVersion": "mariadb.mmontes.io/v1alpha1",
                "kind": "MariaDB",
                "metadata": {
                    "namespace": self.get_target_namespace(),
                    "name": self.get_target_name(),
                    "ownerReferences": [self.get_instance_owner()],
                },
                "spec": self.generate_mariadb_spec()
            }]
        else:
            raise NotImplementedError("Don't know how to handle image:", i)


assert "routers" in [j[0] for j in MysqlDatabaseOperator.get_class_properties()]
assert "podSpec" in [j[0] for j in MysqlDatabaseOperator.get_class_properties()]
assert "storageClass" in [j[0] for j in MysqlDatabaseOperator.get_class_properties()]

if __name__ == "__main__":
    MysqlDatabaseOperator.run()
