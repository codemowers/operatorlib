#!/usr/bin/env python3
import httpx
import os
import asyncio
from httpx_auth import AWS4Auth
from copy import deepcopy
from operatorlib import \
    ReconcileError, \
    SharedMixin, \
    PersistentVolumeClaimMixin, \
    PersistentMixin, \
    StatefulSetMixin, \
    PodSpecMixin, \
    ReplicasSpecMixin, \
    CapacityMixin, \
    ConsoleMixin, \
    HeadlessMixin, \
    ServiceMixin, \
    InstanceSecretMixin, \
    ClaimSecretMixin, \
    ClassedOperator


class MinioBucketOperator(SharedMixin,
                          PersistentVolumeClaimMixin,
                          PersistentMixin,
                          StatefulSetMixin,
                          PodSpecMixin,
                          ReplicasSpecMixin,
                          CapacityMixin,
                          ConsoleMixin,
                          HeadlessMixin,
                          ServiceMixin,
                          InstanceSecretMixin,
                          ClaimSecretMixin,
                          ClassedOperator):

    """
    Minio bucket operator implementation
    """
    OPERATOR = "minio-bucket-operator"
    GROUP = "codemowers.cloud"
    VERSION = "v1beta1"
    SINGULAR = "MinioBucket"
    PLURAL = "MinioBuckets"

    SECURITY_CONTEXT_UID = 1000

    CONDITION_INSTANCE_BUCKET_CREATED = "BucketCreated"
    CONDITION_INSTANCE_BUCKET_QUOTA_SET = "BucketQuotaSet"
    CONDITION_INSTANCE_USER_CREATED = "BucketUserCreated"
    CONDITION_INSTANCE_POLICY_CREATED = "BucketPolicyCreated"
    CONDITION_INSTANCE_POLICY_ATTACHED = "BucketPolicyAttached"

    @classmethod
    def get_instance_condition_set(cls):
        return super().get_instance_condition_set() + [
            cls.CONDITION_INSTANCE_BUCKET_CREATED,
            cls.CONDITION_INSTANCE_BUCKET_QUOTA_SET,
            cls.CONDITION_INSTANCE_USER_CREATED,
            cls.CONDITION_INSTANCE_POLICY_CREATED,
            cls.CONDITION_INSTANCE_POLICY_ATTACHED,
        ]

    def get_bucket_name(self):
        # Bucket max length is 63 and UUID is 36 characters
        # TODO: assert no double hyphens
        # https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucketnamingrules.html
        return "bucket-%s" % self.uid

    async def generate_instance_secret(self):
        root_user = "root"
        root_password = self.generate_random_string(128)
        yield "MINIO_ROOT_USER", root_user
        yield "MINIO_ROOT_PASSWORD", root_password
        yield "MINIO_ADMIN_URI", "http://%s:%s@%s" % (
            root_user, root_password, self.get_service_fqdn())

    async def generate_claim_secret(self):
        service_fqdn = self.get_service_fqdn()
        bucket_password = self.generate_random_string(128)
        access_key = bucket_name = self.get_bucket_name()
        yield "BASE_URI", "http://%s/%s/" % (service_fqdn, bucket_name)
        yield "BUCKET_NAME", bucket_name
        yield "AWS_S3_ENDPOINT_URL", "http://%s" % self.get_service_fqdn()
        yield "AWS_DEFAULT_REGION", self.instance_secret.get("MINIO_REGION", "us-east-1")
        yield "AWS_ACCESS_KEY_ID", access_key
        yield "AWS_SECRET_ACCESS_KEY", bucket_password
        yield "MINIO_URI", "http://%s:%s@%s" % (access_key, bucket_password, service_fqdn)

    async def reconcile_instance(self):
        await super().reconcile_instance()

        if self.get_instance_condition(self.CONDITION_INSTANCE_POLICY_ATTACHED):
            return

        region = self.instance_secret.get("MINIO_REGION", "us-east-1")
        aws = AWS4Auth(
            access_id=self.instance_secret["MINIO_ROOT_USER"],
            secret_key=self.instance_secret["MINIO_ROOT_PASSWORD"],
            region=region,
            service="s3")
        bucket_name = self.get_bucket_name()
        async with httpx.AsyncClient() as requests:
            try:
                base_url = "http://%s" % self.get_service_fqdn()
                url = "%s/%s/" % (base_url, bucket_name)

                # Create bucket
                print("Creating bucket %s" % url)
                r = await requests.put(url, auth=aws)
                if r.status_code not in (200, 409):
                    raise ReconcileError("Creating bucket returned status code %d" % r.status_code)
                await self.set_instance_condition(self.CONDITION_INSTANCE_BUCKET_CREATED)

                # Set quota
                url = "%s/minio/admin/v3/set-bucket-quota?bucket=%s" % (base_url, bucket_name)
                r = await requests.put(url, auth=aws, json={
                    "quota": self.get_capacity(),
                    "quotatype": "hard",
                })
                if r.status_code not in (200,):
                    raise ReconcileError("Setting quota for bucket returned status code %d" % r.status_code)
                await self.set_instance_condition(self.CONDITION_INSTANCE_BUCKET_QUOTA_SET)
            except httpx.ConnectError:
                raise ReconcileError("Failed to connect to Minio at %s" % base_url)

        # Create Minio user
        exitcode, stdout, stderr = await self.invoke_minio_admin("user", "add", "s3",
            self.claim_secret["AWS_ACCESS_KEY_ID"],
            self.claim_secret["AWS_SECRET_ACCESS_KEY"])
        if exitcode != 0:
            raise ReconcileError("Failed to create user: %s" % (stdout or stderr))
        await self.set_instance_condition(self.CONDITION_INSTANCE_USER_CREATED)

        # Create Minio owner policy TODO: Create once with Job?
        exitcode, stdout, stderr = await self.invoke_minio_admin(
            "policy", "create", "s3", "owner", "/app/minio-owner.json")
        if exitcode != 0:
            raise ReconcileError("Failed to create owner policy: %s" % (stdout or stderr))
        await self.set_instance_condition(self.CONDITION_INSTANCE_POLICY_CREATED)

        # Set Minio owner policy
        exitcode, stdout, stderr = await self.invoke_minio_admin(
            "policy", "attach", "s3", "owner",
            "-u", self.claim_secret["AWS_ACCESS_KEY_ID"])
        if exitcode != 0 and b"XMinioAdminPolicyChangeAlreadyApplied" not in stdout:
            raise ReconcileError("Failed to attach owner policy: %s" % (stdout or stderr))
        await self.set_instance_condition(self.CONDITION_INSTANCE_POLICY_ATTACHED)

    async def invoke_minio_admin(self, *args):
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/mc", "--json", "--insecure", "admin", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ | {
                "MC_HOST_s3": self.instance_secret["MINIO_ADMIN_URI"]
            })
        await proc.wait()
        return proc.returncode, await proc.stdout.read(), await proc.stderr.read()

    def get_parity_count(self):
        # Minio defaults to
        #  2 parity blocks with 4-5 nodes
        #  3 parity blocks with 6-7 nodes
        #  4 parity blocks with 8+ nodes
        if self.class_spec["replicas"] <= 5:
            return 2
        elif self.class_spec["replicas"] <= 7:
            return 3
        else:
            return 4

    def get_persistent_volume_capacity(self):
        # Minio uses EC:4 by default
        overhead_factor = 1.05
        total_count = self.class_spec["replicas"]
        if total_count > 1:
            data_count = total_count - self.get_parity_count()
            return overhead_factor * self.get_capacity() / data_count
        else:
            return overhead_factor * self.get_capacity()

    @classmethod
    def get_target_namespace(cls):
        return "minio-clusters"

    def generate_headless_service(self):
        """
        Generate Kubernetes headless Service specification
        """
        return {
            "selector": self.labels,
            "clusterIP": "None",
            "publishNotReadyAddresses": True,
            "ports": [{
                "name": "http",
                "port": 9000
            }]
        }

    def generate_console_service(self):
        """
        Generate Kubernetes console Service specification
        """
        return {
            "sessionAffinity": "ClientIP",
            "selector": self.labels,
            "clusterIP": "None",
            "ports": [{
                "name": "http",
                "targetPort": 9001,
                "port": 80,
            }]
        }

    def generate_service(self):
        """
        Generate Kubernetes Service specification
        """
        return {
            "selector": self.labels,
            "sessionAffinity": "ClientIP",
            "type": "ClusterIP",
            "ports": [{
                "port": 80,
                "targetPort": 9000,
                "name": "http",
            }]
        }

    def generate_pod_spec(self):
        """
        Generate Kubernetes StatefulSet specification
        """

        pod_spec = deepcopy(self.class_spec["podSpec"])

        if "args" not in pod_spec["containers"][0]:
            pod_spec["containers"][0]["args"] = ["server"]
        pod_spec["containers"][0]["args"] += [
            "--console-address",
            "0.0.0.0:9001",
        ]
        pod_spec["containers"][0]["ports"] = [{
            "name": "http",
            "containerPort": 9000,
        }, {
            "name": "console",
            "containerPort": 9001
        }]
        if self.class_spec["replicas"] > 1:
            disk_config = "http://%s-{0...%d}.%s.%s.svc.cluster.local/data" % (
                self.get_service_name(),
                self.class_spec["replicas"] - 1,
                self.get_headless_service_name(),
                self.get_target_namespace())
        else:
            disk_config = "/data"
        pod_spec["containers"][0]["args"].append(disk_config)
        pod_spec["containers"][0]["envFrom"] = [{
            "secretRef": {
                "name": self.get_instance_secret_name()
            }
        }]

        return pod_spec


if __name__ == "__main__":
    MinioBucketOperator.run()
