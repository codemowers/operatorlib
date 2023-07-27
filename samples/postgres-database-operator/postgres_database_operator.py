#!/usr/bin/env python3
import aiopg
import psycopg2
from base64 import b64decode
from operatorlib import \
    ReconcileError, \
    SharedMixin, \
    PersistentMixin, \
    CustomResourceMixin, \
    RoutedMixin, \
    PodSpecMixin, \
    ReplicasSpecMixin, \
    CapacityMixin, \
    ClassedOperator, \
    ClaimSecretMixin


class PostgresDatabaseOperator(ClaimSecretMixin,
                               SharedMixin,
                               PersistentMixin,
                               CustomResourceMixin,
                               RoutedMixin,
                               PodSpecMixin,
                               ReplicasSpecMixin,
                               CapacityMixin,
                               ClassedOperator):

    """
    Postgres logical database operator implementation using CloudNativePG
    """
    OPERATOR = "postgres-database-operator"
    GROUP = "codemowers.cloud"
    VERSION = "v1beta1"
    SINGULAR = "PostgresDatabase"
    PLURAL = "PostgresDatabases"

    CONDITION_INSTANCE_DATABASE_CREATED = "DatabaseCreated"
    CONDITION_INSTANCE_USER_CREATED = "DatabaseUserCreated"
    CONDITION_INSTANCE_PRIVILEGES_GRANTED = "DatabasePrivilegesGranted"

    @classmethod
    def get_instance_condition_set(cls):
        return super().get_instance_condition_set() + [
            cls.CONDITION_INSTANCE_DATABASE_CREATED,
            cls.CONDITION_INSTANCE_USER_CREATED,
            cls.CONDITION_INSTANCE_PRIVILEGES_GRANTED,
        ]

    @classmethod
    def generate_operator_cluster_role_rules(cls):
        yield from super().generate_operator_cluster_role_rules()
        yield "", "secrets", ("get", "create")
        yield "postgresql.cnpg.io", "clusters", ("create", "delete", "patch")

    @classmethod
    def get_target_namespace(cls):
        return "postgres-clusters"

    async def generate_claim_secret(self):
        username = database = self.get_instance_name()
        password = self.generate_random_string(63)
        hostname, port, _, _ = await self.read_target_secrets()

        yield "PGHOST", hostname,
        yield "PGUSER", username,
        yield "PGPORT", str(port)
        yield "PGPASSWORD", password
        yield "PGDATABASE", database,
        yield "PGPASS", "%s:%d:%s:%s:%s\n" % (hostname, port, database,
            username, password)
        yield "DATABASE_URL", "postgres://%s:%s@%s:%d/%s" % (
            username, password, hostname, port, database)

    def get_instance_name(self):
        """
        Generate target resource name
        """
        return "%s_%s" % (self.spec["claimRef"]["namespace"], self.uid)

    def generate_custom_resources(self):
        if not self.class_spec["podSpec"]:
            return []
        if not self.class_spec["storageClass"]:
            return []
        if not self.class_spec["replicas"]:
            return []
        return [{
            "apiVersion": "postgresql.cnpg.io/v1",
            "kind": "Cluster",
            "metadata": {
                "namespace": self.get_target_namespace(),
                "name": self.get_target_name(),
                "ownerReferences": [self.get_instance_owner()],
            },
            "spec": {
                "postgresGID": 999,
                "postgresUID": 999,
                "imageName": self.class_spec["podSpec"]["containers"][0]["image"],
                "instances": self.class_spec["replicas"],
                "storage": {
                    "size": self.spec["capacity"],
                    "storageClass": self.class_spec.get("storageClass"),
                },
                "monitoring": {
                    "enablePodMonitor": True
                },
                "affinity": {
                    "enablePodAntiAffinity": True,
                    "topologyKey": self.get_pod_topology_key(),
                }
            }
        }]

    async def read_target_secrets(self):
        secret_name = "%s-superuser" % self.get_target_name()
        resp = await self.v1.read_namespaced_secret(secret_name, self.get_target_namespace())
        cluster_secrets = dict([(
            key, b64decode(value.encode("ascii")).decode("ascii")) for
            key, value in resp.data.items()])
        hostname, port, _, superuser, password = cluster_secrets["pgpass"][:-1].split(":")
        if "." not in hostname:
            hostname = "%s.%s.svc" % (hostname, self.get_target_namespace())
        return hostname, int(port), superuser, password

    async def reconcile_instance(self):
        await super().reconcile_instance()

        if self.get_instance_condition(self.CONDITION_INSTANCE_PRIVILEGES_GRANTED):
            return

        hostname, port, superuser, password = await self.read_target_secrets()

        try:
            async with aiopg.connect(database=superuser,
                                     user=superuser,
                                     password=password,
                                     port=port,
                                     host=hostname) as conn:

                cursor = await conn.cursor()

                try:
                    await cursor.execute("CREATE DATABASE \"%s\"" % self.claim_secret["PGDATABASE"])
                except psycopg2.errors.DuplicateDatabase:
                    pass
                await self.set_instance_condition(self.CONDITION_INSTANCE_DATABASE_CREATED)

                try:
                    await cursor.execute("CREATE USER \"%s\" WITH ENCRYPTED PASSWORD %s;" % (
                        self.claim_secret["PGUSER"], repr(self.claim_secret["PGPASSWORD"])))
                except psycopg2.errors.DuplicateObject:
                    pass
                await self.set_instance_condition(self.CONDITION_INSTANCE_USER_CREATED)

                await cursor.execute("GRANT ALL PRIVILEGES ON DATABASE \"%s\" TO \"%s\"" % (
                    self.claim_secret["PGDATABASE"], self.claim_secret["PGUSER"]))
                await self.set_instance_condition(self.CONDITION_INSTANCE_PRIVILEGES_GRANTED)
        except psycopg2.errors.OperationalError:
            raise ReconcileError("Failed to connect to Postgres server at %s" % (
                hostname))


if __name__ == "__main__":
    PostgresDatabaseOperator.run()
