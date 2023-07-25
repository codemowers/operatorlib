#!/usr/bin/env python
import redis.asyncio as redis
import itertools
import json
from base64 import b64encode
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException
from operatorlib import UpstreamMixin, InstanceSecretMixin, \
    PersistentVolumeClaimMixin, PersistentMixin, CapacityMixin, \
    ClusterManagementMixin, PrimarySecondaryMixin, StatefulSetMixin, \
    PodSpecMixin, ReplicasSpecMixin, HeadlessMixin, ServiceMixin, \
    ClassedOperator, MonitoringMixin, ClaimSecretMixin, InstanceTaskMixin


class RedisBase(InstanceTaskMixin,
                ClaimSecretMixin,
                UpstreamMixin,
                InstanceSecretMixin,
                PersistentVolumeClaimMixin,
                PersistentMixin,
                CapacityMixin,
                ClusterManagementMixin,
                PrimarySecondaryMixin,
                MonitoringMixin,
                StatefulSetMixin,
                PodSpecMixin,
                ReplicasSpecMixin,
                HeadlessMixin,
                ServiceMixin,
                ClassedOperator):

    _redis_connection_pools = {}

    @classmethod
    def generate_operator_cluster_role_rules(cls):
        yield from super().generate_operator_cluster_role_rules()
        yield "", "pods", ("patch",)

    async def generate_instance_secret(self):
        pwd = self.generate_random_string(128)
        yield "REDIS_PASSWORD", pwd
        yield "redis.conf", "masterauth \"%s\"\nrequirepass \"%s\"\n" % (pwd, pwd)

    async def generate_claim_secret(self):
        pwd = self.instance_secret["REDIS_PASSWORD"]
        master = self.get_primary_service_fqdn()
        slave = self.get_secondary_service_fqdn()
        yield "REDIS_MASTER", master
        yield "REDIS_SLAVE", slave
        yield "REDIS_PORT", "6379"
        yield "REDIS_PASSWORD", pwd
        yield "REDIS_MASTER_URI", "redis://%s@%s" % (pwd, master)
        yield "REDIS_SLAVE_URI", "redis://%s@%s" % (pwd, slave)
        for j in range(0, 16):
            yield "REDIS_MASTER_%d_URI" % j, "redis://%s@%s/%d" % (pwd, master, j)
            yield "REDIS_SLAVE_%d_URI" % j, "redis://%s@%s/%d" % (pwd, slave, j)

    async def get_redis_connection_for_pod(self, pod_name):
        hostname = "%s.%s" % (pod_name, self.get_headless_service_name())
        if hostname not in self._redis_connection_pools:
            self._redis_connection_pools[hostname] = redis.ConnectionPool(
                host=hostname,
                password=self.instance_secret["REDIS_PASSWORD"])
        return redis.Redis(connection_pool=self._redis_connection_pools[hostname])

    async def generate_pod_metrics(self, pod_name):
        conn = await self.get_redis_connection_for_pod(pod_name)
        try:
            redis_info = await conn.info()
        except redis.ConnectionError as e:
            print("Failed to connect to pod %s: %s" % (
                pod_name, e))
            yield self.METRIC_CLUSTER_MEMBER_STATE, 1, [self.CLUSTER_MEMBER_STATE_UNREACHABLE]
        except redis.RedisError as e:
            print("Error while talking to pod %s: %s" % (
                pod_name, e))
            yield self.METRIC_CLUSTER_MEMBER_STATE, 1, [self.CLUSTER_MEMBER_STATE_ERROR]
        else:
            if redis_info.get("master_sync_in_progress", 0):
                yield self.METRIC_CLUSTER_MEMBER_STATE, 1, \
                    [self.CLUSTER_MEMBER_STATE_RECOVERING]
            else:
                yield self.METRIC_CLUSTER_MEMBER_STATE, 1, \
                    [self.CLUSTER_MEMBER_STATE_ONLINE]

            # info metrics
            yield self.METRIC_REDIS_INSTANCE_INFO, 1, [
                redis_info["role"],
                redis_info["maxmemory_policy"],
                redis_info["redis_build_id"],
                redis_info["redis_mode"],
                redis_info["redis_version"]]
            yield self.METRIC_REDIS_MASTER_REPLID, 1, [
                redis_info["role"],
                redis_info["master_replid"]]

            # gauge metrics
            yield self.METRIC_REDIS_CONNECTED_CLIENTS, \
                redis_info["connected_clients"], []
            yield self.METRIC_REDIS_MEMORY_USAGE, \
                redis_info["used_memory"], []
            yield self.METRIC_REDIS_REPL_BACKLOG_HISTLEN, \
                redis_info["repl_backlog_histlen"], \
                [redis_info["role"]]
            yield self.METRIC_REDIS_MASTER_REPL_OFFSET, \
                redis_info["master_repl_offset"], \
                [redis_info["role"]]

    METRIC_REDIS_MASTER_REPL_OFFSET = \
        "redis_replication_offset", \
        "The server's current replication offset (master_repl_offset)", \
        ["instance", "pod", "role"]
    METRIC_REDIS_MASTER_REPLID = \
        "redis_replication_info", \
        "The replication ID of the Redis server (master_replid)", \
        ["instance", "pod", "role", "id"]
    METRIC_REDIS_REPL_BACKLOG_HISTLEN = \
        "redis_replication_backlog_length_bytes", \
        "Size in bytes of the data in the replication backlog buffer", \
        ["instance", "pod", "role"]
    METRIC_REDIS_CONNECTED_CLIENTS = \
        "redis_connected_client_count", \
        "Redis client count", \
        ["instance", "pod"]
    METRIC_REDIS_MEMORY_USAGE = \
        "redis_memory_used_bytes", \
        "Redis memory usage", \
        ["instance", "pod"]
    METRIC_REDIS_INSTANCE_INFO = \
        "redis_info", \
        "Redis instance info", \
        ["instance", "pod", "role", "maxmemory_policy", "redis_build_id", "redis_mode", "redis_version"]

    def get_primary_role_name(self):
        return "master"

    def get_secondary_role_name(self):
        return "slave"

    def get_persistent_volume_capacity(self):
        return 2 * self.get_capacity()

    def generate_service_ports(self):
        return [{"port": 6379, "name": "redis"}]


class Redis(RedisBase):
    OPERATOR = "redis-operator"
    GROUP = "codemowers.cloud"
    VERSION = "v1beta1"
    SINGULAR = "Redis"
    PLURAL = "Redises"

    _redis_commander_connections = {}

    async def reconcile_redis_commander_config(self):
        conns = []
        fqdns = [self.get_primary_service_fqdn(), self.get_secondary_service_fqdn()]
        for fqdn in fqdns:
            for db in range(0, 16):
                conns.append({
                    "dbIndex": db,
                    "port": 6379,
                    "label": "%s.%s/%d" % (
                        self.spec["claimRef"]["namespace"],
                        self.spec["claimRef"]["name"],
                        db),
                    "host": fqdn,
                    "password": self.instance_secret["REDIS_PASSWORD"],
                })
        self._redis_commander_connections[self.uid] = conns

        default_config = {
            "redis": {
                "readOnly": False,
                "flushOnImport": False,
                "useScan": True,
                "scanCount": 100,
                "rootPattern": "*",
                "connectionName": "redis-commander",
                "defaultLabel": "local",
                "defaultSentinelGroup": "mymaster"
            },
            "noSave": False,
            "noLogData": False,
            "ui": {
                "sidebarWidth": 250,
                "locked": False,
                "cliHeight": 320,
                "cliOpen": False,
                "foldingChar": ":",
                "jsonViewAsDefault": "none",
                "binaryAsHex": True,
                "maxHashFieldSize": 0
            },
            "server": {
                "address": "0.0.0.0",
                "port": 8081,
                "urlPrefix": "",
                "trustProxy": False,
                "clientMaxBodySize": "100kb",
                "httpAuth": {
                    "username": "",
                    "password": "",
                    "passwordHash": "",
                    "comment": "to enable http auth set username and either password or passwordHash",
                    "jwtSecret": ""
                }
            },
            "sso": {
                "enabled": False,
                "jwtSharedSecret": "",
                "jwtAlgorithms": ["HS256", "HS384", "HS512"],
                "allowedIssuer": "",
                "audience": "",
                "subject": ""
            },

        }
        conns = list(itertools.chain.from_iterable([v for k, v
            in self._redis_commander_connections.items()]))
        body = {
            "metadata": {
                "name": "redis-commander",
                "namespace": self.get_target_namespace(),
            },
            "data": {
                "local-production.json": b64encode(json.dumps(default_config | {
                    "connections": conns}).encode("ascii")).decode("ascii")
            }
        }
        try:
            await self.v1.replace_namespaced_secret(
                body["metadata"]["name"],
                body["metadata"]["namespace"],
                client.V1Secret(**body))
        except ApiException as e:
            if e.status == 404:
                await self.v1.create_namespaced_secret(
                    body["metadata"]["namespace"],
                    client.V1Secret(**body))
            else:
                raise

    async def reconcile_instance(self):
        await super().reconcile_instance()
        await self.reconcile_redis_commander_config()

    async def run_instance_task(self):
        upstream_secret_reference = self.get_upstream_secret_reference()
        if upstream_secret_reference:
            raise InstanceTaskMixin.InstanceTaskDisabled(
                "Not electing master because instance uses upstream secret")
        pod_names = self.get_pod_names()
        headless_service_name = self.get_headless_service_name()

        current_master = None

        print("Polling pods of %s %s/%s" % (self.SINGULAR, self.namespace, self.name))
        healthy_pods = list()
        for pod_name in pod_names:
            conn = await self.get_redis_connection_for_pod(pod_name)
            try:
                await conn.info("replication")
            except redis.ConnectionError as e:
                print("Failed to connect to pod %s: %s" % (
                    pod_name, e))
                member_state = self.CLUSTER_MEMBER_STATE_UNREACHABLE
            except redis.RedisError as e:
                print("Error while talking to pod %s: %s" % (
                    pod_name, e))
                member_state = self.CLUSTER_MEMBER_STATE_ERROR
            else:
                member_state = self.CLUSTER_MEMBER_STATE_ONLINE

            if member_state != self.CLUSTER_MEMBER_STATE_ONLINE:
                continue
            healthy_pods.append(pod_name)

        cluster_state = self.CLUSTER_STATE_UNKNOWN

        if upstream_secret_reference:
            # The upstream secret should contain key redis.conf with
            # masterauth, requirepass, replicaof
            pass
        else:
            if healthy_pods:
                if len(healthy_pods) == len(pod_names):
                    cluster_state = self.CLUSTER_STATE_ONLINE
                elif len(healthy_pods) >= self.get_quorum_count():
                    cluster_state = self.CLUSTER_STATE_DEGRADED
                else:
                    cluster_state = self.CLUSTER_STATE_STALE

                # Pods don't have upstream master and we should elect master
                if current_master not in healthy_pods:
                    current_master = healthy_pods[0]
                    print("  Electing %s as new master for %s %s/%s" % (
                        current_master,
                        self.SINGULAR, self.namespace, self.name))
                    try:
                        # Try to set all healthy replicaofpods as non-writable masters
                        for pod_name in healthy_pods:
                            conn = await self.get_redis_connection_for_pod(pod_name)
                            print("    Issuing `replicaof no one` on %s" % pod_name)
                            await conn.replicaof("NO", "ONE")
                    except redis.BusyLoadingError:
                        print("    Pod", pod_name, "busy loading")
                    else:
                        # If unsetting master on pods succeeds,
                        # Reconfigure slaves to follow new master
                        for pod_name in healthy_pods:
                            if pod_name == current_master:
                                continue
                            conn = await self.get_redis_connection_for_pod(pod_name)
                            print("    Issuing `replicaof %s.%s` on %s" % (current_master, headless_service_name, pod_name))
                            await conn.replicaof(
                                "%s.%s" % (current_master, headless_service_name),
                                "6379")
                        await self.set_instance_condition(self.CONDITION_INSTANCE_MASTER_ELECTED)

        for pod_name in pod_names:
            if pod_name == current_master:
                role = self.get_primary_role_name()
            elif pod_name in healthy_pods:
                role = self.get_secondary_role_name()
            else:
                role = "unknown"
            await self.v1.patch_namespaced_pod(pod_name,
                self.get_target_namespace(), [{
                    "op": "replace",
                    "path": "/metadata/labels/codemowers.cloud~1cluster-master",
                    "value": current_master,
                }, {
                    "op": "replace",
                    "path": "/metadata/labels/codemowers.cloud~1cluster-role",
                    "value": role
                }])

        if not (self.class_spec["replicas"] >= 2):
            raise InstanceTaskMixin.InstanceTaskDisabled(
                "Not enough replicas (2+) to repeatedly perform master election")

    @classmethod
    def get_target_namespace(cls):
        return "redis-clusters"

    def generate_pod_spec(self):
        """
        Generate Kubernetes StatefulSet specification
        """
        storage_class = self.class_spec.get("storageClass")
        replicas = self.class_spec["replicas"]
        pod_spec = self.class_spec["podSpec"]
        upstream_secret_reference = self.get_upstream_secret_reference()

        pod_spec["volumes"] = [{
            "name": "config",
            "secret": {
                "secretName": upstream_secret_reference if upstream_secret_reference else self.get_instance_secret_name(),
                "items": [{
                    "key": "redis.conf",
                    "path": "redis.conf",
                }]
            }
        }]

        # Assume it's the first container in the pod
        container_spec = pod_spec["containers"][0]

        args = [
            "/etc/redis/redis.conf",
            "--maxmemory", "%d" % self.get_capacity(),
        ]

        if not self.get_upstream_secret_reference():
            if replicas > 1:
                args += [
                    "--min-replicas-to-write",
                    str(self.get_quorum_count() - 1),
                    "--min-replicas-max-lag",
                    "30",
                ]

        container_spec["resources"]["requests"]["memory"] = "%dMi" % (self.get_capacity() // 1048576)
        container_spec["resources"]["limits"]["memory"] = container_spec["resources"]["requests"]["memory"]

        if storage_class:
            container_spec["resources"]["limits"]["memory"] = "%dMi" % (self.get_capacity() // 524288)
        else:
            # Disable BGSAVE if no PV-s are attached
            args += [
                "--save",
                ""
            ]

        # Create stateful set
        container_spec["args"] = container_spec.get("args", []) + args
        container_spec["volumeMounts"] = [{
            "name": "config",
            "mountPath": "/etc/redis",
            "readOnly": True
        }]
        container_spec["env"] = [{
            "name": "CLUSTER_MASTER",
            "valueFrom": {
                "fieldRef": {
                    "fieldPath": "metadata.labels['codemowers.cloud/cluster-master']"
                }
            }
        }, {
            "name": "CLUSTER_ROLE",
            "valueFrom": {
                "fieldRef": {
                    "fieldPath": "metadata.labels['codemowers.cloud/cluster-role']"
                }
            }
        }]
        return pod_spec


assert "class" in [j[0] for j in Redis.get_required_instance_properties()]
assert "reclaimPolicy" in [j[0] for j in Redis.get_class_properties()]
assert "podSpec" in [j[0] for j in Redis.get_optional_instance_properties()]
assert "podSpec" in [j[0] for j in Redis.get_optional_claim_properties()]
assert "podSpec" in [j[0] for j in Redis.get_class_properties()]
assert "replicas" in [j[0] for j in Redis.get_class_properties()]
assert "storageClass" in [j[0] for j in Redis.get_class_properties()]

if __name__ == "__main__":
    Redis.run()
