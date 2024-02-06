#!/usr/bin/env python3
import httpx
import os
import asyncio
import jwt
from base64 import b64encode
from datetime import datetime, timedelta
from httpx_auth import AWS4Auth
from copy import deepcopy
from operatorlib import \
    ReconcileError, \
    SharedMixin, \
    PersistentVolumeClaimMixin, \
    PersistentMixin, \
    StatefulSetMixin, \
    PodMonitorMixin, \
    ServiceMonitorMixin, \
    PodSpecMixin, \
    ReplicasSpecMixin, \
    CustomResourceMixin, \
    CapacityMixin, \
    ConsoleMixin, \
    HeadlessMixin, \
    ServiceMixin, \
    IngressMixin, \
    InstanceSecretMixin, \
    ClaimSecretMixin, \
    ClassedOperator
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException


class MinioBucketOperator(SharedMixin,
                          PersistentVolumeClaimMixin,
                          PersistentMixin,
                          StatefulSetMixin,
                          ServiceMonitorMixin,
                          PodMonitorMixin,
                          CustomResourceMixin,
                          PodSpecMixin,
                          ReplicasSpecMixin,
                          CapacityMixin,
                          ConsoleMixin,
                          HeadlessMixin,
                          ServiceMixin,
                          IngressMixin,
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
    CONDITION_INSTANCE_PROMETHEUS_BEARER_TOKEN_GENERATED = "BucketMonitoringConfigured"

    def get_monitor_secret(self):
        return "%s-monitoring" % self.get_instance_secret_name()

    def generate_service_monitor_spec(self):
        spec = super().generate_service_monitor_spec()
        spec["endpoints"] = [{
            "targetPort": 9000,
            "path": "/minio/v2/metrics/cluster",
            "bearerTokenSecret": {
                "name": self.get_monitor_secret(),
                "key": "PROMETHEUS_BEARER_TOKEN"
            }
        }]
        return spec

    def generate_pod_monitor_spec(self):
        spec = super().generate_pod_monitor_spec()
        spec["podMetricsEndpoints"] = [{
            "targetPort": 9000,
            "path": "/minio/v2/metrics/node",
            "bearerTokenSecret": {
                "name": self.get_monitor_secret(),
                "key": "PROMETHEUS_BEARER_TOKEN"
            }
        }]
        return spec

    @classmethod
    def get_instance_condition_set(cls):
        return super().get_instance_condition_set() + [
            cls.CONDITION_INSTANCE_BUCKET_CREATED,
            cls.CONDITION_INSTANCE_BUCKET_QUOTA_SET,
            cls.CONDITION_INSTANCE_USER_CREATED,
            cls.CONDITION_INSTANCE_POLICY_CREATED,
            cls.CONDITION_INSTANCE_POLICY_ATTACHED,
            cls.CONDITION_INSTANCE_PROMETHEUS_BEARER_TOKEN_GENERATED
        ]

    def get_bucket_name(self):
        # Bucket max length is 63 and UUID is 36 characters
        # TODO: assert no double hyphens
        # https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucketnamingrules.html
        return "%s-%s" % (self.spec["claimRef"]["namespace"], self.uid)

    def get_s3_endpoint_url(self):
        return "https://%s" % self.get_ingress_host() if self.class_spec.get("ingressSpec") else self.instance_secret.get(
            "AWS_S3_ENDPOINT_URL", "http://%s" % self.get_service_fqdn())

    def get_minio_admin_uri(self):
        return self.instance_secret.get(
            "MINIO_ADMIN_URI", self.get_s3_endpoint_url().replace("://", "://%s:%s@" % (
                self.instance_secret["MINIO_ROOT_USER"],
                self.instance_secret["MINIO_ROOT_PASSWORD"],
            )))

    async def generate_instance_secret(self):
        root_user = "root"
        root_password = self.generate_random_string(128)
        yield "MINIO_ROOT_USER", root_user
        yield "MINIO_ROOT_PASSWORD", root_password

    async def generate_claim_secret(self):
        service_fqdn = self.get_service_fqdn()
        bucket_password = self.generate_random_string(128)
        access_key = bucket_name = self.get_bucket_name()

        # If specified in instance secret use it, otherwise derive from service FQDN
        endpoint_url = self.get_s3_endpoint_url()

        yield "AWS_ENDPOINTS", endpoint_url
        yield "AWS_S3_ENDPOINT_URL", endpoint_url
        yield "BASE_URI", "%s/%s/" % (endpoint_url, bucket_name)
        yield "BUCKET_NAME", bucket_name

        yield "AWS_DEFAULT_REGION", self.instance_secret.get("MINIO_REGION", "us-east-1")
        yield "AWS_ACCESS_KEY_ID", access_key
        yield "AWS_SECRET_ACCESS_KEY", bucket_password
        yield "MINIO_URI", "http://%s:%s@%s" % (access_key, bucket_password, service_fqdn)

    async def reconcile_instance(self):
        await super().reconcile_instance()

        region = self.instance_secret.get("MINIO_REGION", "us-east-1")
        aws = AWS4Auth(
            access_id=self.instance_secret["MINIO_ROOT_USER"],
            secret_key=self.instance_secret["MINIO_ROOT_PASSWORD"],
            region=region,
            service="s3")
        bucket_name = self.get_bucket_name()
        async with httpx.AsyncClient() as requests:
            try:
                base_url = self.get_s3_endpoint_url()
                url = "%s/%s/" % (base_url, bucket_name)

                # Create bucket
                print("  Creating bucket %s" % url)
                r = await requests.put(url, auth=aws)
                if r.status_code not in (200, 409):
                    raise ReconcileError("Creating bucket %s returned status code %d" % (
                        bucket_name, r.status_code))
                await self.set_instance_condition(self.CONDITION_INSTANCE_BUCKET_CREATED)

                # Set quota
                url = "%s/minio/admin/v3/set-bucket-quota?bucket=%s" % (base_url, bucket_name)
                r = await requests.put(url, auth=aws, json={
                    "quota": self.get_capacity(),
                    "quotatype": "hard",
                })
                if r.status_code not in (200,):
                    raise ReconcileError("Setting quota for bucket %s returned status code %d" % (
                        bucket_name, r.status_code))
                await self.set_instance_condition(self.CONDITION_INSTANCE_BUCKET_QUOTA_SET)
            except httpx.ConnectError:
                raise ReconcileError("Failed to connect to Minio at %s, use AWS_S3_ENDPOINT_URL key in the generated secret to override" % base_url)
            except httpx.ConnectTimeout:
                raise ReconcileError("Timed out connecting to Minio at %s" % base_url)

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

        # Generate Prometheus bearer token
        token = jwt.encode(
            payload={
                "exp": (datetime.now() + timedelta(days=3650)).timestamp(),
                "sub": self.instance_secret["MINIO_ROOT_USER"],
                "iss": "prometheus",
            },
            key=self.instance_secret["MINIO_ROOT_PASSWORD"],
            algorithm="HS512"
        )

        body = {
            "metadata": {
                "name": self.get_monitor_secret(),
                "namespace": self.get_target_namespace(),
                "ownerReferences": [self.get_instance_owner()]
            },
            "data": {
                "PROMETHEUS_BEARER_TOKEN": b64encode(token.encode("ascii")).decode("ascii")
            }
        }

        try:
            await self.v1.create_namespaced_secret(
                self.get_target_namespace(),
                client.V1Secret(**body))
        except ApiException as e:
            if e.status == 409:
                pass
            else:
                raise
        else:
            print("  Prometheus secret %s/%s created" % (
                self.get_target_namespace(),
                self.get_monitor_secret()))
        await self.set_instance_condition(self.CONDITION_INSTANCE_PROMETHEUS_BEARER_TOKEN_GENERATED)

    async def invoke_minio_admin(self, *args):
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/mc", "--json", "--insecure", "admin", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ | {
                "MC_HOST_s3": self.get_minio_admin_uri()
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
