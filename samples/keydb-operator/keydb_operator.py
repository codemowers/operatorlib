#!/usr/bin/env python3
from operatorlib import InstanceTaskMixin
from redis_operator import RedisBase


class KeyDB(RedisBase):
    """
    Redis operator implementation
    """
    OPERATOR = "keydb-operator"
    GROUP = "codemowers.cloud"
    VERSION = "v1beta1"
    SINGULAR = "Keydb"
    PLURAL = "Keydbs"

    SECURITY_CONTEXT_UID = 999

    @classmethod
    def get_target_namespace(cls):
        return "keydb-clusters"

    async def run_instance_task(self):
        raise InstanceTaskMixin.InstanceTaskDisabled("No need task for KeyDB")

    def generate_pod_spec(self):
        """
        Generate pod specification
        """
        storage_class = self.class_spec.get("storageClass")
        replicas = self.class_spec["replicas"]
        pod_spec = self.class_spec["podSpec"]

        pod_spec["volumes"] = [{
            "name": "config",
            "secret": {
                "secretName": self.get_instance_secret_name(),
                "items": [{
                    "key": "redis.conf",
                    "path": "redis.conf",
                }]
            }
        }, {
            "name": "scripts",
            "configMap": {
                "name": "scripts",
                "defaultMode": 0o777,
                "items": [{
                    "key": "entrypoint.sh",
                    "path": "entrypoint.sh",
                }, {
                    "key": "liveness.sh",
                    "path": "liveness.sh",
                }, {
                    "key": "readiness.sh",
                    "path": "readiness.sh",
                }]
            }
        }]

        # Assume it's the first container in the pod
        container_spec = pod_spec["containers"][0]
        container_spec["workingDir"] = "/data"

        args = [
            "/etc/redis/redis.conf",
            "--maxmemory",
            "%d" % self.get_capacity(),
        ]

        container_spec["resources"]["requests"]["memory"] = "%dMi" % (self.get_capacity() // 1048576)
        container_spec["resources"]["limits"]["memory"] = container_spec["resources"]["requests"]["memory"]

        if replicas > 1:
            args += [
                "--active-replica",
                "yes",
                "--multi-master",
                "yes",
                "--min-slaves-to-write",
                str(self.get_quorum_count() - 1),
                "--min-slaves-max-lag",
                "30",
            ]

        if storage_class:
            container_spec["resources"]["limits"]["memory"] = "%dMi" % (self.get_capacity() // 524288)
        else:
            args += [
                "--save",
                ""
            ]

        # Create stateful set
        container_spec["args"] = container_spec.get("args", []) + args
        container_spec["env"] = [{
            "name": "SERVICE_NAME",
            "value": self.get_headless_service_name(),
        }, {
            "name": "REPLICAS",
            "value": " ".join(self.get_pod_names())
        }]
        container_spec["volumeMounts"] = [{
            "name": "config",
            "mountPath": "/etc/redis",
            "readOnly": True
        }]

        for script in ("entrypoint",):
            container_spec["volumeMounts"].append({
                "name": "scripts",
                "mountPath": "/%s.sh" % script,
                "subPath": "%s.sh" % script,
                "readOnly": True,
            })

        return pod_spec


KeyDB.generate_primary_service = KeyDB.generate_service
KeyDB.generate_secondary_service = KeyDB.generate_service

assert "podSpec" in [j[0] for j in KeyDB.get_class_properties()]
assert "storageClass" in [j[0] for j in KeyDB.get_class_properties()]

if __name__ == "__main__":
    KeyDB.run()
