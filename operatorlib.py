import aiohttp
import argparse
import asyncio
import os
import random
import re
import string
import traceback
import yaml
from aiohttp import web
from base64 import b64encode, b64decode
from datetime import datetime
from collections import defaultdict
from math import ceil
from kubernetes_asyncio import client, config, watch
from kubernetes_asyncio.client.exceptions import ApiException
from passlib.context import CryptContext
from time import time

bcrypt_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

UPPER_FOLLOWED_BY_LOWER_RE = re.compile("(.)([A-Z][a-z]+)")
LOWER_OR_NUM_FOLLOWED_BY_UPPER_RE = re.compile("([a-z0-9])([A-Z])")
IMMUTABLE_FIELD = {"x-kubernetes-validations": [{
    "message": "Value is immutable",
    "rule": "self == oldSelf"}]}

STATUS_SUBRESOURCE = {
    "properties": {
        "phase": {
            "type": "string"
        },
        "conditions": {
            "items": {
                "properties": {
                    "lastTransitionTime": {
                        "format": "date-time",
                        "type": "string"
                    },
                    "message": {
                        "maxLength": 32768,
                        "type": "string"
                    },
                    "reason": {
                        "maxLength": 1024,
                        "minLength": 1,
                        "pattern": "^[A-Za-z]([A-Za-z0-9_,:]*[A-Za-z0-9_])?$",
                        "type": "string"
                    },
                    "status": {
                        "enum": [
                            "True",
                            "False",
                            "Unknown"
                        ],
                        "type": "string"
                    },
                    "type": {
                        "maxLength": 316,
                        "pattern": "^([a-z0-9]([-a-z0-9]*[a-z0-9])?(\\\\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*/)?(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])$",
                        "type": "string"
                    }
                },
                "required": [
                    "lastTransitionTime",
                    "status",
                    "type"
                ],
                "type": "object"
            },
            "type": "array"
        }
    },
    "type": "object"
}


def sentence_case(string):
    if string != "":
        result = re.sub("([A-Z])", r" \1", string)
        return result[:1].upper() + result[1:].lower()
    return


class NoAliasDumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, *args, **kwargs):
        return super().increase_indent(flow=flow, indentless=False)

    def ignore_aliases(self, data):
        return True


class Operator():
    """
    Base class for implementing Kubernetes operators in Python
    """

    tasks = {}

    @classmethod
    def generate_operator_cluster_role_rules(cls):
        if False:
            yield None

    @classmethod
    def get_base_printer_columns(cls):
        return []

    @classmethod
    def get_required_base_properties(cls):
        return []

    @classmethod
    def get_optional_base_properties(cls):
        return []

    @classmethod
    def generate_operator_crd_definition(cls):
        return []

    @classmethod
    def get_service_account_name(cls):
        return "%s-%s" % (cls.GROUP.replace(".", "-"), cls.OPERATOR)

    @classmethod
    def get_operator_namespace(cls):
        raise NotImplementedError("get_operator_namespace() method not overridden")

    @classmethod
    def generate_operator_tasks(cls, api_client, co, args):
        return []

    @classmethod
    def generate_random_string(self, size):
        return "".join([random.choice(
            string.ascii_letters + string.digits) for j in range(size)])

    @classmethod
    def generate_operator_deployment_definition(cls, image):
        sts_name = cls.OPERATOR
        sts = [{
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {
                "name": sts_name,
            },
            "spec": {
                "replicas": 1,
                "serviceName": sts_name,
                "selector": {
                    "matchLabels": {
                        "app": sts_name,
                    }
                },
                "template": {
                    "metadata": {
                        "labels": {
                            "app": sts_name,
                        }
                    },
                    "spec": {
                        "serviceAccountName": cls.get_service_account_name(),
                        "enableServiceLinks": False,
                        "imagePullSecrets": [{
                            "name": "regcred"
                        }],
                        "containers": [{
                            "name": cls.OPERATOR,
                            "image": image,
                            "ports": [{
                                "containerPort": 8000,
                                "name": "metrics",
                            }]
                        }]
                    }
                }
            }
        }]
        ns = cls.get_operator_namespace()
        if ns:
            sts[0]["metadata"]["namespace"] = ns
        return sts

    @classmethod
    def generate_operator_rbac_definition(cls):
        VALID_VERBS = set(["get", "create", "update", "patch", "delete", "list", "watch"])
        cluster_role_name = cls.get_service_account_name()
        merged = defaultdict(set)
        for group, resource, verbs in cls.generate_operator_cluster_role_rules():
            key = group.lower(), resource.lower()
            verbs = set(verbs)
            assert verbs.issubset(VALID_VERBS), verbs
            merged[key].update(verbs)

        regrouping = defaultdict(set)
        for (group, resource), verbs in merged.items():
            regrouping[(group, tuple(sorted(verbs)))].add(resource)

        unique_rules = sorted([(k[0], tuple(sorted(v)), k[1]) for (k, v) in regrouping.items()])

        yield {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {
                "name": cluster_role_name,
            },
            "rules": [{
                "apiGroups": [g],
                "resources": r,
                "verbs": v
            } for (g, r, v) in unique_rules]
        }

        yield {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {
                "name": cluster_role_name,
                "namespace": cls.get_operator_namespace(),
            }
        }

        yield {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {
                "name": cluster_role_name,
            },
            "subjects": [{
                "kind": "ServiceAccount",
                "name": cluster_role_name,
                "namespace": cls.get_operator_namespace(),
            }],
            "roleRef": {
                "kind": "ClusterRole",
                "name": cluster_role_name,
                "apiGroup": "rbac.authorization.k8s.io",
            }
        }

    def get_annotations(self):
        """
        Add `app.kubernetes.io/managed-by` annotation to generated resources
        """
        return [
            ("app.kubernetes.io/managed-by", "%s/%s" % (self.GROUP, self.OPERATOR))
        ]

    @classmethod
    def get_json_utcnow(cls):
        return "%sZ" % (datetime.utcnow().isoformat()[:19])

    def generate_manifests(self):
        """
        Generate array of desired Kubernetes resource manifests
        """
        return []

    @classmethod
    def build_argument_parser(cls):
        """
        Add `--dry-run` command line argument handling
        """
        parser = argparse.ArgumentParser(description="Run %s operator" % cls.__name__)

        subcommands = parser.add_subparsers(dest="subcommand")
        _ = subcommands.add_parser(
            "generate-rbac",
            description="Generate RBAC definitions")
        _ = subcommands.add_parser(
            "generate-crds",
            description="Generate CRD definitions")
        _ = subcommands.add_parser(
            "nuke",
            description="Generate command for completely removing the operator")
        generate_deployment_parser = subcommands.add_parser(
            "generate-deployment",
            description="Generate deployment definitions")

        generate_deployment_parser.add_argument("--image", help="Docker image")

        run_parser = subcommands.add_parser("run", description="Run the operator")
        run_parser.add_argument("--dry-run", action="store_true", help="Disable state mutation")
        return parser

    @classmethod
    async def generate_operator_metrics(cls):
        """
        Base Operator metrics
        """
        yield cls.METRIC_OPERATOR_INSTANCE_RECONCILE_LOOP_RESTART_COUNT, \
            cls._counter_instance_reconcile_loop_restart_count, \
            (cls.GROUP, cls.SINGULAR, cls.VERSION)
        yield cls.METRIC_OPERATOR_INSTANCE_RECONCILE_COUNT, \
            cls._counter_instance_reconcile_count, \
            (cls.GROUP, cls.SINGULAR, cls.VERSION)

    @classmethod
    async def _run(cls):
        args = vars(cls.build_argument_parser().parse_args())
        if args["subcommand"] == "run":
            print("Starting %s/%s" % (cls.GROUP, cls.OPERATOR))
            if os.getenv("KUBECONFIG"):
                await config.load_kube_config()
            else:
                config.load_incluster_config()
            api_client = client.ApiClient()
            co = client.CustomObjectsApi(api_client)

            args.pop("subcommand")
            tasks = cls.generate_operator_tasks(api_client, co, args)
            print("  Starting coroutines:")
            for task in tasks:
                print("    Starting", task.__name__)
            await asyncio.gather(*tasks)
        elif args["subcommand"] == "generate-crds":
            docs = sorted(
                cls.generate_operator_crd_definition(),
                key=lambda e: e["metadata"]["name"],
                reverse=True)
            for doc in docs:
                for version in doc["spec"]["versions"]:
                    if not version["additionalPrinterColumns"]:
                        version.pop("additionalPrinterColumns")
            print(cls.dump_yaml(docs))
        elif args["subcommand"] == "generate-rbac":
            print(cls.dump_yaml(cls.generate_operator_rbac_definition()))
        elif args["subcommand"] == "generate-deployment":
            print(cls.dump_yaml(cls.generate_operator_deployment_definition(args["image"])))
        elif args["subcommand"] == "nuke":
            for cmd in cls.generate_nuke_command():
                print(cmd)
        else:
            raise NotImplementedError("Not implemented subcommand: %s" % args["subcommand"])

    @classmethod
    def generate_nuke_command(cls):
        if False:
            yield None

    @classmethod
    def dump_yaml(cls, docs):
        buf = yaml.dump_all(docs, Dumper=NoAliasDumper, width=80)
        assert not buf.startswith("---"), repr(buf)
        assert buf.endswith("\n"), repr(buf)
        return "---\n" + buf[:-1]

    @classmethod
    def run(cls):
        """
        Run the asyncio event loop for this operator
        """
        asyncio.run(cls._run())


class InstanceMixin():
    cached_instances = {}

    INSTANCE_STATE_PENDING = "Pending"
    INSTANCE_STATE_ERROR = "Error"
    INSTANCE_STATE_BOUND = "Bound"
    INSTANCE_STATE_RELEASED = "Released"

    async def generate_instance_metrics(self):
        if False:
            yield

    @classmethod
    def generate_nuke_command(cls):
        yield from super().generate_nuke_command()
        yield "kubectl delete --all=true %s.%s" % (
            cls.SINGULAR,
            cls.GROUP)
        yield "kubectl delete customresourcedefinition %s.%s" % (
            cls.PLURAL.lower(),
            cls.GROUP)

    def create_instance_tasks(self):
        if False:
            yield None

    @classmethod
    def get_required_instance_properties(cls):
        return cls.get_required_base_properties()

    @classmethod
    def generate_operator_crd_definition(cls):
        return super().generate_operator_crd_definition() + [
            cls.generate_instance_definition()]

    @classmethod
    def generate_operator_cluster_role_rules(cls):
        yield from super().generate_operator_cluster_role_rules()
        yield cls.GROUP, cls.PLURAL, ("get", "list", "watch")
        yield cls.GROUP, cls.PLURAL + "/status", ("update", "patch")

    def get_instance_owner(self):
        """
        Return the instance as the owner for generated resources
        """
        return {
            "apiVersion": "%s/%s" % (self.GROUP, self.VERSION),
            "kind": self.SINGULAR,
            "name": self.name,
            "uid": self.uid,
        }

    CONDITION_INSTANCE_RESOURCES = "Resources"

    @classmethod
    def get_instance_condition_set(cls):
        return [
            cls.CONDITION_INSTANCE_RESOURCES,
        ]

    async def patch_instance_status(self, patches):
        return await self.co.patch_cluster_custom_object_status(
            self.GROUP, self.VERSION,
            self.PLURAL.lower(), self.name, patches)

    def get_instance_condition(self, condition):
        for j in self.status["conditions"]:
            if j["type"] == condition:
                if j["status"] == "True":
                    return True
        return False

    async def set_instance_condition(self, condition):
        for j in self.status["conditions"]:
            if j["type"] == condition:
                if j["status"] == "True":
                    return
        for index, j in enumerate(self.get_instance_condition_set()):
            if j == condition:
                path = "/status/conditions/%d" % index
                break
        else:
            raise

        patches = [{
            "op": "test",
            "path": "%s/type" % path,
            "value": condition,
        }, {
            "op": "replace",
            "path": "%s/status" % path,
            "value": "True",
        }, {
            "op": "replace",
            "path": "%s/lastTransitionTime" % path,
            "value": self.get_json_utcnow(),
        }]
        print("  Patching %s %s status:" % (self.SINGULAR, self.name))
        for patch in patches:
            print("    ", patch)

        try:
            await self.patch_instance_status(patches)
        except ApiException as e:
            if e.status == 409:
                print("Failed to update %s %s status: %s" % (
                    self.SINGULAR,
                    self.name,
                    e))
            else:
                raise

        # Update conditions array in the cached instance
        for j in self.status["conditions"]:
            if j["type"] == condition:
                j["status"] = "True"
                break

    @classmethod
    def generate_instance_conditions(cls):
        return [{
            "type": s,
            "status": "False",
            "lastTransitionTime": cls.get_json_utcnow(),
        } for s in cls.get_instance_condition_set()]

    async def reconcile_instance(self):
        """
        Reconcile resources for this custom resource
        """
        desired_state = self.generate_manifests()
        for manifest in desired_state:
            group, _, version = manifest["apiVersion"].partition("/")
            if version == "":
                version = group
                group = "core"
            # Take care for the case e.g. api_type is "apiextensions.k8s.io"
            # Only replace the last instance
            group = "".join(group.rsplit(".k8s.io", 1))
            # convert group name from DNS subdomain format to
            # python class name convention
            group = "".join(word.capitalize() for word in group.split("."))

            fcn_to_call = "{0}{1}Api".format(group, version.capitalize())
            k8s_api = getattr(client, fcn_to_call)(self.api_client)

            kind = manifest["kind"]
            kind = UPPER_FOLLOWED_BY_LOWER_RE.sub(r"\1_\2", kind)
            kind = LOWER_OR_NUM_FOLLOWED_BY_UPPER_RE.sub(r"\1_\2", kind).lower()

            try:
                func = getattr(k8s_api, "read_namespaced_{0}".format(kind))
            except AttributeError as e:
                print("No API for %s" % e)
                raise

            resp = await func(
                manifest["metadata"]["name"],
                manifest["metadata"]["namespace"],
                _preload_content=False)

            if not resp or resp.status == 404:
                print("  Creating %s %s/%s" % (manifest["kind"], manifest["metadata"]["namespace"], manifest["metadata"]["name"]))
                resp = await getattr(k8s_api, "create_namespaced_{0}".format(kind))(
                    manifest["metadata"]["namespace"],
                    manifest,
                    _preload_content=False)
            else:
                print("  Patching %s %s/%s" % (manifest["kind"], manifest["metadata"]["namespace"], manifest["metadata"]["name"]))
                resp = await getattr(k8s_api, "patch_namespaced_{0}".format(kind))(
                    manifest["metadata"]["name"],
                    manifest["metadata"]["namespace"],
                    manifest,
                    _preload_content=False)
                status = await resp.json()
                if status["kind"] == "Status" and status["status"] == "Failure":
                    print("  Failed to patch %s %s/%s",
                        manifest["kind"],
                        manifest["metadata"]["namespace"],
                        manifest["metadata"]["name"],
                        "because:", status["message"])
        await self.set_instance_condition(self.CONDITION_INSTANCE_RESOURCES)

    def get_label_selector(self, **extra):
        """
        Build labels and label selector for application/instance
        """
        labels = {
            "app.kubernetes.io/name": self.OPERATOR,
            "app.kubernetes.io/instance": self.get_target_name(),
            **extra
        }
        expressions = []
        for key, value in labels.items():
            expressions.append({
                "key": key,
                "operator": "In",
                "values": [value]
            })

        selector = {
            "matchExpressions": expressions
        }
        return labels, selector

    def get_target_name(self):
        """
        Generate target resource name
        """
        return self.name

    def get_target_namespace(self):
        """
        Generate target namespace
        """
        return self.namespace

    @classmethod
    def get_operator_namespace(cls):
        return cls.get_target_namespace()

    def __init__(self, body, dry_run=True):
        """
        Instantiate Python representation of the source custom resource
        """
        self.namespace = body["spec"]["claimRef"]["namespace"]
        self.name = body["metadata"]["name"]
        self.spec = body["spec"]
        self.uid = body["metadata"]["uid"]
        self.generation = body["metadata"]["generation"]
        self.status = body["status"]
        self.dry_run = dry_run

    def setup(self):
        """
        Set up additional attributes for the Python representation of the source custom resource
        """
        self.labels, self.label_selector = self.get_label_selector()
        self.annotations = dict(self.get_annotations())

    @classmethod
    async def _construct_resource(cls, args, co, body):
        inst = cls(body, *args)
        inst.setup()
        return inst

    _counter_instance_reconcile_loop_restart_count = 0
    _counter_instance_reconcile_count = 0

    METRIC_OPERATOR_INSTANCE_RECONCILE_LOOP_RESTART_COUNT = "codemowers_operator_instance_reconcile_loop_restart_count", \
        "Instance reconciler loop restart count", \
        ["group", "kind", "version"]
    METRIC_OPERATOR_INSTANCE_RECONCILE_COUNT = "codemowers_operator_instance_reconcile_count", \
        "Instance reconciler loop restart count", \
        ["group", "kind", "version"]

    @classmethod
    async def run_instance_reconciler_loop(cls, api_client, co, args):
        """
        Instance reconciler loop
        """
        w = watch.Watch()
        kwargs = {}
        cls.cached_instances.clear()
        while True:
            try:
                cls._counter_instance_reconcile_loop_restart_count += 1
                async for event in w.stream(co.list_cluster_custom_object, cls.GROUP, cls.VERSION, cls.PLURAL.lower(), **kwargs):

                    if isinstance(event, str):
                        print("Resource definition of %s not installed" % (
                            cls.SINGULAR))
                        await asyncio.sleep(60)
                        continue

                    body = event["object"]
                    kwargs["resource_version"] = body["metadata"]["resourceVersion"]
                    if event["type"] in ("ADDED", "MODIFIED"):
                        if "uid" not in body["spec"]["claimRef"]:
                            print("%s %s/%s gone, aborting %s %s reconcile" % (
                                cls.get_claim_singular(),
                                body["spec"]["claimRef"]["namespace"],
                                body["spec"]["claimRef"]["name"],
                                cls.SINGULAR,
                                body["metadata"]["name"]))
                            continue

                        try:
                            instance = await cls._construct_resource(args, co, body, api_client)
                            instance.setup()
                            prev_instance = cls.cached_instances.get(body["metadata"]["name"], None)
                            if prev_instance and prev_instance.generation == instance.generation:
                                print("No spec changes for %s %s" % (cls.SINGULAR, instance.name))
                                continue
                            print("Reconciling %s %s" % (cls.SINGULAR, instance.name))
                            cls._counter_instance_reconcile_count += 1

                            await instance.reconcile_instance()
                        except ReconcileDeferred as e:
                            print("Deferring %s %s reconcile due to: %s" % (
                                cls.SINGULAR,
                                body["metadata"]["name"],
                                e))
                            continue
                        except ReconcileError as e:
                            print("Instance reconciliation error:", e)
                            await cls.set_instance_phase(co, body["metadata"], cls.INSTANCE_STATE_ERROR)
                        except Exception as e:
                            print("Unhandled exception raised during instance reconcile:", e)
                            await cls.set_instance_phase(co, body["metadata"], cls.INSTANCE_STATE_ERROR)
                            raise
                        else:
                            cls.cached_instances[body["metadata"]["name"]] = instance
                            await cls.set_instance_phase(co, body["metadata"], cls.INSTANCE_STATE_BOUND)
                    elif event["type"] == "DELETED":
                        print("Deleting instance %s" % body["metadata"]["name"])
                        cls.cached_instances.pop(body["metadata"]["name"], None)
                        await instance.cleanup_instance()
                    else:
                        print("Don't know how to handle event type", event)
            except aiohttp.client_exceptions.ClientPayloadError as e:
                print("Unexpected aiohttp error in instance reconciler loop:", e)
                await asyncio.sleep(15)
            except ApiException as e:
                if e.status == 410:
                    print("Watch for %s expired, restarting" % cls.PLURAL)
                    await asyncio.sleep(3)
                else:
                    print("Kubernetes API error in instance reconciler loop:", e)
                    traceback.print_exc()
                    await asyncio.sleep(15)

    async def cleanup_instance(self):
        pass

    @classmethod
    def generate_operator_tasks(cls, api_client, co, args):
        return super().generate_operator_tasks(api_client, co, args) + [
            cls.run_instance_reconciler_loop(api_client, co, args)
        ]

    @classmethod
    def get_optional_instance_properties(cls):
        """
        Return optional instance properties
        """
        return cls.get_optional_base_properties()

    @classmethod
    def get_base_printer_columns(cls):
        """
        Return instance and claim base printer columns
        """
        return super().get_base_printer_columns() + [{
            "jsonPath": ".status.phase",
            "name": "Phase",
            "type": "string",
        }]

    @classmethod
    def get_instance_printer_columns(cls):
        """
        Return instance printer columns
        """
        return cls.get_base_printer_columns()

    @classmethod
    def generate_instance_definition(cls):
        """
        Generate CRD definitions for this operator
        """
        props = dict(cls.get_required_instance_properties())
        return {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {
                "name": "%s.%s" % (cls.PLURAL.lower(), cls.GROUP),
            },
            "spec": {
                "scope": "Cluster",
                "group": cls.GROUP,
                "names": {
                    "plural": cls.PLURAL.lower(),
                    "singular": cls.SINGULAR.lower(),
                    "kind": cls.SINGULAR,
                },
                "versions": [{
                    "subresources": {
                        "status": {}
                    },
                    "name": cls.VERSION,
                    "served": True,
                    "storage": True,
                    "additionalPrinterColumns": cls.get_instance_printer_columns(),
                    "schema": {
                        "openAPIV3Schema": {
                            "type": "object",
                            "required": ["spec"],
                            "properties": {
                                "status": STATUS_SUBRESOURCE,
                                "spec": {
                                    "type": "object",
                                    "required": list(sorted(props.keys())),
                                    "properties": dict(cls.get_optional_instance_properties()) | props,
                                }
                            }
                        }
                    }
                }],
            }
        }


class ReconcileError(Exception):
    pass


class ReconcileDeferred(ReconcileError):
    pass


class OperatorClassNotFound(ReconcileError):
    pass


class UpstreamSecretNotFound(ReconcileError):
    pass


class InstanceSecretMixin():
    CONDITION_INSTANCE_SECRET_CREATED = "SecretCreated"

    @classmethod
    def generate_operator_cluster_role_rules(cls):
        yield from super().generate_operator_cluster_role_rules()
        yield "", "secrets", ("get", "create")

    @classmethod
    def get_instance_condition_set(cls):
        return super().get_instance_condition_set() + [
            cls.CONDITION_INSTANCE_SECRET_CREATED,
        ]

    def get_instance_secret_name(self):
        return self.get_target_name()

    async def generate_instance_secret(self):
        raise NotImplementedError()

    async def reconcile_instance(self):

        secret_name = self.get_instance_secret_name()
        namespace = self.get_target_namespace()
        try:
            resp = await self.v1.read_namespaced_secret(secret_name, namespace)
        except ApiException as e:
            if e.status == 404:
                self.instance_secret = dict([j async for j in self.generate_instance_secret()])
                body = {
                    "metadata": {
                        "name": secret_name,
                        "namespace": namespace,
                        "ownerReferences": [self.get_instance_owner()]
                    },
                    "data": dict([(k, b64encode(v.encode("ascii")).decode("ascii"))
                        for k, v in self.instance_secret.items()]),
                }

                try:
                    resp = await self.v1.create_namespaced_secret(
                        namespace,
                        client.V1Secret(**body))
                except ApiException as e:
                    if e.status == 409:
                        pass
                    else:
                        raise
                else:
                    print("Instance secret %s/%s created" % (namespace, secret_name))
            else:
                raise
        else:
            self.instance_secret = dict([(k, b64decode(v.encode("ascii")).decode("ascii"))
                for k, v in resp.data.items()])
        await self.set_instance_condition(self.CONDITION_INSTANCE_SECRET_CREATED)

        await super().reconcile_instance()


class ClaimSecretMixin():

    CONDITION_CLAIM_SECRET = "ClaimSecret"

    @classmethod
    def generate_operator_cluster_role_rules(cls):
        yield from super().generate_operator_cluster_role_rules()
        yield "", "secrets", ("get", "create", "update")

    @classmethod
    def get_claim_condition_set(cls):
        return super().get_claim_condition_set() + [
            cls.CONDITION_CLAIM_SECRET,
        ]

    def get_claim_secret_name(self):
        return "%s-%s-owner-secrets" % (
            self.SINGULAR.lower(),
            self.spec["claimRef"]["name"])

    async def reconcile_instance(self):
        await super().reconcile_instance()
        secret_name = self.get_claim_secret_name()
        namespace = self.namespace

        try:
            resp = await self.v1.read_namespaced_secret(secret_name, namespace)
        except ApiException as e:
            if e.status == 404:
                self.claim_secret = dict([j async for j in self.generate_claim_secret()])
                body = {
                    "metadata": {
                        "name": secret_name,
                        "namespace": self.namespace,
                        "ownerReferences": [self.spec["claimRef"]]
                    },
                    "data": dict([(k, b64encode(v.encode("ascii")).decode("ascii"))
                        for k, v in self.claim_secret.items()]),
                }

                try:
                    resp = await self.v1.create_namespaced_secret(
                        self.namespace,
                        client.V1Secret(**body))
                except ApiException as e:
                    if e.status == 409:
                        print("Claim secret %s/%s already exists" % (
                            self.namespace, secret_name))
                    else:
                        raise
        else:
            self.claim_secret = dict([(k, b64decode(v.encode("ascii")).decode("ascii"))
                for k, v in resp.data.items()])


class ClaimMixin():
    CLAIM_STATE_PENDING = "Pending"
    CLAIM_STATE_ERROR = "Error"
    CLAIM_STATE_BOUND = "Bound"

    _counter_claim_reconcile_loop_restart_count = 0
    _counter_claim_reconcile_count = 0

    METRIC_OPERATOR_CLAIM_RECONCILE_LOOP_RESTART_COUNT = "codemowers_operator_claim_reconcile_loop_restart_count", \
        "Claim reconciler loop restart count", \
        ["group", "kind", "version"]
    METRIC_OPERATOR_CLAIM_RECONCILE_COUNT = "codemowers_operator_claim_reconcile_count", \
        "Claim reconciler loop restart count", \
        ["group", "kind", "version"]

    @classmethod
    def generate_nuke_command(cls):
        yield from super().generate_nuke_command()
        yield "kubectl delete --all=true --all-namespaces %s.%s" % (
            cls.get_claim_singular(),
            cls.GROUP)
        yield "kubectl delete customresourcedefinition %s.%s" % (
            cls.get_claim_plural().lower(),
            cls.GROUP)

    @classmethod
    def generate_operator_crd_definition(cls):
        return super().generate_operator_crd_definition() + [
            cls.generate_claim_definition()]

    @classmethod
    def get_claim_printer_columns(cls):
        """
        Return claim printer columns
        """
        return cls.get_base_printer_columns()

    @classmethod
    async def generate_operator_metrics(cls):
        """
        Implement ClaimMixin metrics
        """
        async for descriptor, value, labels in super().generate_operator_metrics():
            yield descriptor, value, labels
        yield cls.METRIC_OPERATOR_CLAIM_RECONCILE_LOOP_RESTART_COUNT,  \
            cls._counter_claim_reconcile_loop_restart_count, \
            (cls.GROUP, cls.SINGULAR, cls.VERSION)
        yield cls.METRIC_OPERATOR_CLAIM_RECONCILE_COUNT, \
            cls._counter_claim_reconcile_count, \
            (cls.GROUP, cls.SINGULAR, cls.VERSION)

    @classmethod
    def generate_operator_cluster_role_rules(cls):
        yield from super().generate_operator_cluster_role_rules()
        yield cls.GROUP, cls.get_claim_plural(), ("get", "list", "watch")
        yield cls.GROUP, cls.get_claim_plural() + "/status", ("update", "patch")

    @classmethod
    def get_claim_plural(cls):
        return "%ss" % cls.get_claim_singular()

    @classmethod
    def get_claim_singular(cls):
        return "%sClaim" % cls.SINGULAR

    @classmethod
    def get_claim_singular_lower(cls):
        return cls.get_claim_singular().lower()

    CONDITION_CLAIM_INSTANCE = "Instance"

    @classmethod
    def get_claim_condition_set(cls):
        return [
            cls.CONDITION_CLAIM_INSTANCE
        ]

    @classmethod
    def generate_claim_conditions(cls):
        return [{
            "type": s,
            "status": "False",
            "lastTransitionTime": cls.get_json_utcnow(),
        } for s in cls.get_claim_condition_set()]

    @classmethod
    def generate_instance_patches(cls, claim_body):
        return super().generate_instance_patches(claim_body) + [{
            "op": "replace",
            "path": "/spec/claimRef/resourceVersion",
            "value": claim_body["metadata"]["resourceVersion"]}]

    @classmethod
    def get_required_claim_properties(cls):
        return cls.get_required_base_properties()

    @classmethod
    def get_optional_claim_properties(cls):
        return cls.get_optional_base_properties()

    @classmethod
    def get_class_properties(self):
        """
        Add `reclaimPolicy` property for the class definition
        """
        return [
            ("reclaimPolicy", {"type": "string", "enum": ["Retain", "Delete"], **IMMUTABLE_FIELD})
        ]

    @classmethod
    def generate_instance_name(cls, body):
        return "%s-%s" % (cls.SINGULAR.lower(), body["metadata"]["uid"])

    @classmethod
    async def generate_instance_body(cls, claim_body, co):
        """
        Generate instance spec based on claim spec
        """
        try:
            class_body = await co.get_cluster_custom_object(
                cls.GROUP,
                cls.VERSION,
                cls.get_class_plural().lower(),
                claim_body["spec"]["class"])
        except ApiException as e:
            if e.status == 404:
                raise OperatorClassNotFound("Failed to read %s `%s`" % (cls.get_class_singular(), claim_body["spec"]["class"]))
            else:
                raise

        instance_spec = dict([(key, claim_body["spec"].get(key, None)) for key, _ in cls.get_optional_base_properties()]) | \
            dict([(key, claim_body["spec"].get(key)) for key, _ in cls.get_required_base_properties()])
        instance_spec["claimRef"] = {
            "kind": claim_body["kind"],
            "name": claim_body["metadata"]["name"],
            "namespace": claim_body["metadata"]["namespace"],
            "uid": claim_body["metadata"]["uid"],
            "resourceVersion": claim_body["metadata"]["resourceVersion"],
            "apiVersion": claim_body["apiVersion"],
        }
        body = {
            "apiVersion": "%s/%s" % (cls.GROUP, cls.VERSION),
            "kind": cls.SINGULAR,
            "metadata": {
                "name": cls.generate_instance_name(claim_body),
                "labels": claim_body["metadata"].get("labels", {}),
                "annotations": claim_body["metadata"].get("annotations", {}),
            },
            "spec": instance_spec
        }
        if class_body["spec"]["reclaimPolicy"] == "Delete":
            body["metadata"]["ownerReferences"] = [
                instance_spec["claimRef"]
            ]
        return body

    @classmethod
    async def set_claim_phase(cls, co, meta, phase):
        """
        Update claim status resource
        """
        try:
            await co.patch_namespaced_custom_object_status(
                cls.GROUP, cls.VERSION, meta["namespace"],
                cls.get_claim_plural().lower(), meta["name"], [{
                    "op": "replace",
                    "path": "/status/phase",
                    "value": phase}])
        except ApiException as e:
            if e.status == 409:
                print("Failed to update claim %s/%s status: %s" % (
                    meta["namespace"],
                    meta["name"],
                    e))
            elif e.status == 404:
                print("%s %s/%s already gone" % (cls.get_claim_singular(),
                    meta["namespace"], meta["name"]))
            else:
                raise

    @classmethod
    async def set_instance_phase(cls, co, meta, phase):
        """
        Update instance status resource
        """
        try:
            await co.patch_cluster_custom_object_status(
                cls.GROUP, cls.VERSION,
                cls.PLURAL.lower(), meta["name"], [{
                    "op": "replace",
                    "path": "/status/phase",
                    "value": phase}])
        except ApiException as e:
            if e.status == 409:
                print("Failed to update instance %s/%s status: %s" % (
                    meta["namespace"],
                    meta["name"],
                    e))
            else:
                raise

    @classmethod
    async def reconcile_claim(cls, api_client, co, body):
        # Initialize status subresource of claim
        if "status" not in body:
            await co.replace_namespaced_custom_object_status(
                cls.GROUP, cls.VERSION, body["metadata"]["namespace"],
                cls.get_claim_plural().lower(), body["metadata"]["name"], {
                    "apiVersion": "%s/%s" % (cls.GROUP, cls.VERSION),
                    "kind": cls.get_claim_singular(),
                    "status": {
                        "phase": cls.CLAIM_STATE_PENDING,
                        "conditions": cls.generate_claim_conditions(),
                    },
                    "metadata": {
                        "namespace": body["metadata"]["namespace"],
                        "name": body["metadata"]["name"],
                        "resourceVersion": body["metadata"]["resourceVersion"]}})
            raise ReconcileDeferred("Status initialized")

    @classmethod
    def generate_operator_tasks(cls, api_client, co, args):
        return super().generate_operator_tasks(api_client, co, args) + [
            cls.run_claim_reconciler_loop(api_client, co, args)
        ]

    @classmethod
    async def cleanup_claim(cls, api_client, co, body):
        # Remove claimRef from instance
        instance_name = cls.generate_instance_name(body)
        await co.patch_cluster_custom_object(
            cls.GROUP, cls.VERSION,
            cls.PLURAL.lower(), instance_name, [{
                "op": "remove",
                "path": "/spec/claimRef/uid"}])
        await co.patch_cluster_custom_object_status(
            cls.GROUP, cls.VERSION,
            cls.PLURAL.lower(), instance_name, [{
                "op": "replace",
                "path": "/status/phase",
                "value": cls.INSTANCE_STATE_RELEASED
            }])

        print("Deleted %s %s/%s" % (
            cls.get_claim_singular(),
            body["metadata"]["namespace"],
            body["metadata"]["name"]))

    @classmethod
    async def run_claim_reconciler_loop(cls, api_client, co, args):
        """
        Claim reconciler loop
        """
        print("Claim reconciler loop opening watch for %s" % cls.get_claim_singular())
        w = watch.Watch()

        # Claim handler follows
        kwargs = {}
        while True:
            try:
                cls._counter_claim_reconcile_loop_restart_count += 1
                async for event in w.stream(co.list_namespaced_custom_object, cls.GROUP, cls.VERSION, "", cls.get_claim_plural().lower(), **kwargs):
                    body = event["object"]
                    kwargs["resource_version"] = body["metadata"]["resourceVersion"]
                    if event["type"] in ("ADDED", "MODIFIED"):
                        print("Reconciling %s %s/%s" % (
                            cls.get_claim_singular(),
                            body["metadata"]["namespace"],
                            body["metadata"]["name"]))
                        cls._counter_claim_reconcile_count += 1
                        try:
                            await cls.reconcile_claim(api_client, co, body)
                        except ReconcileDeferred as e:
                            print("Deferring %s %s/%s reconcile due to: %s" % (
                                cls.get_claim_singular(),
                                body["metadata"]["namespace"],
                                body["metadata"]["name"],
                                e))
                            continue
                        except ReconcileError as e:
                            await cls.set_claim_phase(co, body["metadata"], cls.CLAIM_STATE_ERROR)
                            print("Claim reconciliation failed: %s" % e)
                        except Exception as e:
                            print("Unhandled exception raised during claim reconcile:", e)
                            raise
                    elif event["type"] == "DELETED":
                        await cls.cleanup_claim(api_client, co, body)
                    else:
                        print("Don't know how to handle event type", event)
            except aiohttp.client_exceptions.ClientPayloadError as e:
                print("Unexpected aiohttp error in claim reconciler loop:", e)
                await asyncio.sleep(15)
            except ApiException as e:
                if e.status == 410:
                    print("Watch for %s expired, restarting" % cls.get_claim_plural())
                    await asyncio.sleep(3)
                else:
                    print("Kubernetes API error in claim reconciler loop:", e)
                    traceback.print_exc()
                    await asyncio.sleep(15)

    @classmethod
    def generate_claim_definition(cls):
        """
        Generate claim custom resource definition
        """
        plural = cls.get_claim_plural()
        singular = cls.get_claim_singular()
        props = dict(cls.get_required_claim_properties())

        return {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {
                "name": "%s.%s" % (plural.lower(), cls.GROUP),
            },
            "spec": {
                "scope": "Namespaced",
                "group": cls.GROUP,
                "names": {
                    "plural": plural.lower(),
                    "singular": singular.lower(),
                    "kind": singular,
                },
                "versions": [{
                    "subresources": {
                        "status": {}
                    },
                    "name": cls.VERSION,
                    "schema": {
                        "openAPIV3Schema": {
                            "required": ["spec"],
                            "properties": {
                                "spec": {
                                    "properties": dict(cls.get_optional_claim_properties()) | props,
                                    "required": list(sorted(props.keys())),
                                    "type": "object",
                                },
                                "status": STATUS_SUBRESOURCE,
                            },
                            "type": "object",
                        },
                    },
                    "served": True,
                    "storage": True,
                    "additionalPrinterColumns": cls.get_claim_printer_columns(),
                }],
                "conversion": {
                    "strategy": "None",
                }
            }
        }


class CapacityMixin():
    """
    Integer capacity mixin represents usable capacity of a database, bucket, etc
    """

    def get_capacity(self):
        """
        Return parsed capacity as integer
        """
        return self._parse_capacity(self.spec["capacity"])

    @classmethod
    def _parse_capacity(cls, s):
        """
        Assumes the string is already validated by CRD
        """
        if s[-1] == "i":
            s = s[:-1]
            m = 1024
        else:
            m = 1000
        v, p = int(s[:-1]), s[-1]
        return v * m ** {"M": 2, "G": 3, "T": 4, "P": 5}[p]

    @classmethod
    def get_base_printer_columns(cls):
        """
        Add `Capacity` printer column for the Kubernetes CRD definition
        """
        return super().get_base_printer_columns() + [{
            "name": "Capacity",
            "jsonPath": ".spec.capacity",
            "type": "string",
        }]

    @classmethod
    def get_required_base_properties(cls):
        """
        Add `capacity` property for the Kubernetes CRD definition
        """
        return super().get_required_base_properties() + [
            ("capacity", {"type": "string", "pattern": "^[1-9][0-9]*[PTGMK]i?$"}),
        ]

    @classmethod
    def generate_instance_patches(cls, claim_body):
        """
        Allow claim to patch `capacity` attribute of the instance
        """
        return super().generate_instance_patches(claim_body) + [{
            "op": "replace",
            "path": "/spec/capacity",
            "value": claim_body["spec"]["capacity"]}]


class PersistentMixin():

    @classmethod
    def get_class_properties(self):
        """
        Add `storageClass` property for the Kubernetes CRD definition
        """
        return super().get_class_properties() + [
            ("storageClass", {"type": "string", **IMMUTABLE_FIELD})
        ]

    def get_persistent_volume_capacity(self):
        """
        Derive `PersistentVolume` capacity based on `capacity` attribute
        of CapacityMixin
        """
        raise NotImplementedError()

    def get_humanized_persistent_volume_capacity(self):
        """
        Format `PersistentVolume` capacity using binary prefixes Ki/Mi/Gi/etc
        """
        num = self.get_persistent_volume_capacity()
        for unit in ("", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"):
            if abs(num) < 1024:
                return "%d%s" % (ceil(num), unit)
            num /= 1024.0
        return "%dYi" % num

    @classmethod
    def get_class_printer_columns(cls):
        """
        Add storage class printer column
        """
        return super().get_class_printer_columns() + [{
            "name": "Storage class",
            "jsonPath": ".spec.storageClass",
            "type": "string",
        }]


class ConsoleMixin():
    """
    Mixin for handling console Service resource
    """

    def get_console_service_name(self):
        """
        Generate console service name
        """
        return "%s-console" % self.get_service_name()

    def generate_console_service(self):
        raise NotImplementedError()

    def generate_manifests(self):
        """
        Generate Service manifest for console service
        """
        return super().generate_manifests() + [{
            "kind": "Service",
            "apiVersion": "v1",
            "metadata": {
                "namespace": self.get_target_namespace(),
                "name": self.get_console_service_name(),
                "labels": self.labels,
                "annotations": dict(self.get_annotations() + [("codemowers.cloud/mixin", "ConsoleMixin")]),
                "ownerReferences": [self.get_instance_owner()],
            },
            "spec": self.generate_console_service()
        }]


class HeadlessMixin():
    """
    Mixin for handling headless Service resource
    """

    def get_headless_service_name(self):
        """
        Generate headless service name
        """
        return "%s-headless" % self.get_service_name()

    def generate_headless_service(self,):
        """
        Generate Kubernetes headless Service specification
        """
        return {
            "selector": self.labels,
            "clusterIP": "None",
            "publishNotReadyAddresses": True,
            "ports": self.generate_service_ports(),
        }

    def generate_manifests(self):
        """
        Generate Service manifest for headless service
        """
        return super().generate_manifests() + [{
            "kind": "Service",
            "apiVersion": "v1",
            "metadata": {
                "namespace": self.get_target_namespace(),
                "name": self.get_headless_service_name(),
                "labels": self.labels,
                "annotations": dict(self.get_annotations() + [("codemowers.cloud/mixin", "HeadlessMixin")]),
                "ownerReferences": [self.get_instance_owner()],
            },
            "spec": self.generate_headless_service()
        }]


class ServiceMixin():
    """
    Mixin for handling Service resource
    """
    @classmethod
    def generate_operator_cluster_role_rules(cls):
        yield from super().generate_operator_cluster_role_rules()
        yield "", "services", ("get", "create", "patch")

    def get_service_name(self):
        return self.get_target_name()

    def get_service_fqdn(self):
        return "%s.%s.svc.cluster.local" % (
            self.get_service_name(),
            self.get_target_namespace())

    def generate_service_ports(*args, **kwargs):
        raise NotImplementedError("generate_service_ports method required by StatefulSetMixin not implemented")

    def generate_service(self):
        """
        Generate Kubernetes Service specification
        """
        return {
            "selector": self.labels,
            "sessionAffinity": "ClientIP",
            "type": "ClusterIP",
            "ports": self.generate_service_ports()
        }

    def generate_manifests(self):
        return super().generate_manifests() + [{
            "kind": "Service",
            "apiVersion": "v1",
            "metadata": {
                "namespace": self.get_target_namespace(),
                "name": self.get_service_name(),
                "labels": self.labels,
                "annotations": dict(self.get_annotations() + [("codemowers.cloud/mixin", "ServiceMixin")]),
                "ownerReferences": [self.get_instance_owner()],
            },
            "spec": self.generate_service()
        }]


class PrimarySecondaryMixin():
    """
    Mixin for handling primary-secondary applications
    """

    def get_primary_role_name(self):
        return "primary"

    def get_secondary_role_name(self):
        return "secondary"

    def get_primary_service_name(self):
        return "%s-%s" % (self.get_target_name(), self.get_primary_role_name())

    def get_secondary_service_name(self):
        return "%s-%s" % (self.get_target_name(), self.get_secondary_role_name())

    def get_primary_service_fqdn(self):
        return "%s.%s.svc.cluster.local" % (
            self.get_primary_service_name(),
            self.get_target_namespace())

    def get_secondary_service_fqdn(self):
        return "%s.%s.svc.cluster.local" % (
            self.get_secondary_service_name(),
            self.get_target_namespace())

    def generate_primary_service(self):
        """
        Generate Kubernetes Service specification
        """
        extra = {
            "codemowers.cloud/cluster-role": self.get_primary_role_name()
        }
        labels, _ = self.get_label_selector(**extra)
        return {
            "selector": labels,
            "sessionAffinity": "ClientIP",
            "type": "ClusterIP",
            "ports": self.generate_service_ports()
        }

    def generate_secondary_service(self):
        """
        Generate Kubernetes Service specification
        """
        extra = {
            "codemowers.cloud/cluster-role": self.get_secondary_role_name()
        }
        labels, _ = self.get_label_selector(**extra)
        return {
            "selector": labels,
            "sessionAffinity": "ClientIP",
            "type": "ClusterIP",
            "ports": self.generate_service_ports()
        }

    def generate_manifests(self):
        """
        Generate Service manifests for primary-secondary application
        """
        return super().generate_manifests() + [{
            "kind": "Service",
            "apiVersion": "v1",
            "metadata": {
                "namespace": self.get_target_namespace(),
                "name": self.get_primary_service_name(),
                "labels": self.labels,
                "annotations": dict(self.get_annotations() + [(
                    "codemowers.cloud/mixin", "PrimarySecondaryMixin")]),
                "ownerReferences": [self.get_instance_owner()],
            },
            "spec": self.generate_primary_service()
        }, {
            "kind": "Service",
            "apiVersion": "v1",
            "metadata": {
                "namespace": self.get_target_namespace(),
                "name": self.get_secondary_service_name(),
                "labels": self.labels,
                "annotations": dict(self.get_annotations() + [(
                    "codemowers.cloud/mixin", "PrimarySecondaryMixin")]),
                "ownerReferences": [self.get_instance_owner()],
            },
            "spec": self.generate_secondary_service()
        }]


class UpstreamMixin():
    """
    Handle the case of attaching in cluster resources as downstream
    replicas for external resource
    """

    CONDITION_INSTANCE_UPSTREAM_SECRET_EXISTS = "UpstreamSecretExists"

    @classmethod
    def get_instance_condition_set(cls):
        return super().get_instance_condition_set() + [
            cls.CONDITION_INSTANCE_UPSTREAM_SECRET_EXISTS,
        ]

    @classmethod
    def get_optional_base_properties(cls):
        """
        Add `upstreamSecretRef` property for the Kubernetes resource definition
        """
        return super().get_optional_base_properties() + [
            ("upstreamSecretRef", {"type": "string"})
        ]

    @classmethod
    def get_base_printer_columns(cls):
        """
        Add `Upstream secret` printer column
        """
        return super().get_base_printer_columns() + [{
            "name": "Upstream secret",
            "jsonPath": ".spec.upstreamSecretRef",
            "type": "string",
        }]

    def get_upstream_secret_reference(self):
        source = self.spec.get("upstreamSecretRef", None)
        if source:
            if self.namespace != self.get_target_namespace():
                return "upstream-secret-%s" % self.uid
            else:
                return source
        return None

    async def reconcile_instance(self):
        source_secret_name = self.spec.get("upstreamSecretRef", None)
        if source_secret_name and self.namespace != self.get_target_namespace():
            copied_secret_name = "upstream-secret-%s" % self.uid
            print("Copying secret %s/%s to %s/%s" % (
                self.namespace,
                source_secret_name,
                self.get_target_namespace(),
                copied_secret_name))
            try:
                resp = await self.v1.read_namespaced_secret(
                    source_secret_name,
                    self.namespace)
            except ApiException as e:
                if e.status == 404:
                    raise UpstreamSecretNotFound("Upstream secret %s/%s not found" % (
                        self.namespace,
                        source_secret_name))
                else:
                    raise
            body = {
                "data": resp.data,
                "metadata": {
                    "name": copied_secret_name,
                    "namespace": self.get_target_namespace()
                }
            }
            try:
                resp = await self.v1.replace_namespaced_secret(
                    copied_secret_name,
                    self.get_target_namespace(),
                    client.V1Secret(**body))
            except ApiException as e:
                if e.status == 404:
                    resp = await self.v1.create_namespaced_secret(
                        self.get_target_namespace(),
                        client.V1Secret(**body))
                else:
                    raise
        await super().reconcile_instance()

    async def read_upstream_secret(self):
        secret_reference = self.get_upstream_secret_reference()
        if not secret_reference:
            return None
        try:
            resp = await self.v1.read_namespaced_secret(
                secret_reference,
                self.get_target_namespace())
        except ApiException as e:
            if e.status == 404:
                raise UpstreamSecretNotFound("Failed to read secret %s in namespace %s" % (
                    secret_reference, self.get_target_namespace()))
            else:
                raise
        return dict([(k, b64decode(v.encode("ascii")).decode("ascii"))
            for k, v in resp.data.items()])

    @classmethod
    def generate_instance_patches(cls, claim_body):
        return super().generate_instance_patches(claim_body) + [{
            "op": "replace",
            "path": "/spec/upstreamSecretRef",
            "value": claim_body["spec"].get("upstreamSecretRef", None)}]


class ClusterManagementMixin():
    """
    ClusterManagementMixin simplifies implementing master election for
    software that doesn't support built-in failover
    """

    # Pod is healthy and in sync with other pods
    CLUSTER_MEMBER_STATE_ONLINE = "ONLINE"

    # The pod is receiving state snapshot, replaying oplog, etc from a donor
    CLUSTER_MEMBER_STATE_RECOVERING = "RECOVERING"

    # The operator is able to connect to pod, but is unable to determine if pod is healthy
    CLUSTER_MEMBER_STATE_ERROR = "ERROR"

    # The operator is unable to connect to the pod
    CLUSTER_MEMBER_STATE_UNREACHABLE = "UNREACHABLE"

    # All pods are responding
    CLUSTER_STATE_ONLINE = "ONLINE"

    # Quorum of pods is responding, stateful set as a whole is usable
    CLUSTER_STATE_DEGRADED = "DEGRADED"

    # Less than quorum of pods is responding
    CLUSTER_STATE_STALE = "STALE"

    # No pods responding
    CLUSTER_STATE_UNKNOWN = "UNKNOWN"

    METRIC_CLUSTER_MEMBER_STATE = "codemowers_operator_cluster_member_state", \
        "Cluster member state", \
        ["instance", "pod", "state"]
    METRIC_CLUSTER_STATE = "codemowers_operator_cluster_state", \
        "Cluster state", \
        ["instance", "state"]

    CONDITION_INSTANCE_MASTER_ELECTED = "MasterElected"

    @classmethod
    def get_instance_condition_set(cls):
        return super().get_instance_condition_set() + [
            cls.CONDITION_INSTANCE_MASTER_ELECTED,
        ]

    @classmethod
    def get_cluster_member_state_set(cls):
        return set([getattr(cls, j) for j in dir(cls) if j.startswith("CLUSTER_MEMBER_STATE_")])

    @classmethod
    def get_cluster_state_set(cls):
        return set([getattr(cls, j) for j in dir(cls) if j.startswith("CLUSTER_STATE_")])

    def get_quorum_count(self):
        return self.class_spec["replicas"] // 2 + 1


class PersistentVolumeClaimMixin():
    @classmethod
    def generate_operator_cluster_role_rules(cls):
        yield from super().generate_operator_cluster_role_rules()
        yield "", "persistentvolumeclaims", ("create", "patch")

    def get_persistent_volume_claim_name(self):
        return "data"

    def generate_manifests(self):
        """
        Generate PersistentVolumeClaim manifests
        """
        manifests = super().generate_manifests()
        storage_class = self.class_spec.get("storageClass")
        if self.class_spec.get("podSpec"):
            if storage_class:
                for j in range(0, self.class_spec["replicas"]):
                    manifests.append({
                        "kind": "PersistentVolumeClaim",
                        "apiVersion": "v1",
                        "metadata": {
                            "namespace": self.get_target_namespace(),
                            "name": "%s-%s-%d" % (
                                self.get_persistent_volume_claim_name(),
                                self.get_target_name(),
                                j),
                            "labels": self.labels,
                            "annotations": dict(self.get_annotations() + [("codemowers.cloud/mixin", "PersistentVolumeClaimMixin")]),
                            "ownerReferences": [self.get_instance_owner()],
                        },
                        "spec": {
                            "accessModes": ["ReadWriteOnce"],
                            "storageClassName": storage_class,
                            "resources": {
                                "requests": {
                                    "storage": self.get_humanized_persistent_volume_capacity()
                                }
                            }
                        }
                    })
        return manifests


class PodSpecMixin():
    SECURITY_CONTEXT_UID = 999

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        pod_spec = self.class_spec["podSpec"]
        if "containers" not in pod_spec:
            pod_spec["containers"] = []
        for container_spec in pod_spec["containers"]:
            if "resources" not in container_spec:
                container_spec["resources"] = {}
            for j in ("requests", "limits"):
                if j not in container_spec["resources"]:
                    container_spec["resources"][j] = {}

    @classmethod
    def get_class_printer_columns(cls):
        return super().get_class_printer_columns() + [{
            "name": "Image",
            "jsonPath": ".spec.podSpec.containers[0].image",
            "type": "string",
        }]

    RE_POD_RESOURCE_LIMIT = r"^(\+|-)?(([0-9]+(\.[0-9]*)?)|(\.[0-9]+))(([KMGTPE]i)|[numkMGTPE]|([eE](\+|-)?(([0-9]+(\.[0-9]*)?)|(\.[0-9]+))))?$"

    POD_RESOURCES = {
        "type": "object",
        "properties": {
            "requests": {
                "type": "object",
                "properties": {
                    "cpu": {
                        "anyOf": [{
                            "type": "integer"
                        }, {
                            "type": "string"
                        }],
                        "x-kubernetes-int-or-string": True,
                        "pattern": RE_POD_RESOURCE_LIMIT
                    }
                }
            }, "limits": {
                "type": "object",
                "properties": {
                    "cpu": {
                        "anyOf": [{
                            "type": "integer"
                        }, {
                            "type": "string"
                        }],
                        "x-kubernetes-int-or-string": True,
                        "pattern": RE_POD_RESOURCE_LIMIT
                    }
                }
            }
        }
    }

    @classmethod
    def get_optional_base_properties(self):
        return super().get_optional_base_properties() + [
            ("podSpec", {
                "type": "object",
                "properties": {
                    "resources": self.POD_RESOURCES,
                }
            })
        ]

    def get_pod_topology_key(self):
        return "topology.kubernetes.io/zone"

    @classmethod
    def get_pod_properties(cls):
        return {
            "containers": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "image": {"type": "string"},
                        "imagePullPolicy": {"type": "string"},
                        "resources": cls.POD_RESOURCES,
                    }
                }
            },
            "nodeSelector": {
                "type": "object",
                "additionalProperties": {
                    "type": "string",
                }
            },
            "tolerations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "effect": {
                            "type": "string"
                        },
                        "key": {
                            "type": "string"
                        },
                        "operator": {
                            "type": "string"
                        },
                        "value": {
                            "type": "string"
                        }
                    }
                },
            }
        }

    @classmethod
    def get_class_properties(cls):
        return super().get_class_properties() + [(
            "podSpec", {
                "type": "object",
                "properties": cls.get_pod_properties()
            }
        )]

    def generate_pod_spec(self):
        raise NotImplementedError("generate_pod_spec method required by PodSpecMixin not implemented")

    def get_pod_security_context(self):
        """
        Return pod security context, defaulting dropped privileges
        """
        return {
            "fsGroupChangePolicy": "OnRootMismatch",
            "fsGroup": self.SECURITY_CONTEXT_UID,
            "runAsUser": self.SECURITY_CONTEXT_UID,
            "runAsNonRoot": True,
        }

    def get_container_security_context(self):
        """
        Return container security context, defaulting to immutable root filesystem
        """
        return {
            "readOnlyRootFilesystem": True,
        }


class ReplicasSpecMixin():
    @classmethod
    def get_class_printer_columns(cls):
        return super().get_class_printer_columns() + [{
            "name": "Replicas",
            "jsonPath": ".spec.replicas",
            "type": "integer",
        }]

    @classmethod
    def get_class_properties(self):
        """
        Add `replicas` property for the class definition
        """
        return super().get_class_properties() + [
            ("replicas", {"type": "integer", **IMMUTABLE_FIELD}),
        ]


class StatefulSetMixin():
    """
    Mixin for handling StatefulSet resource
    """

    @classmethod
    def generate_operator_cluster_role_rules(cls):
        yield from super().generate_operator_cluster_role_rules()
        yield "policy", "poddisruptionbudgets", ("create", "patch")
        yield "apps", "statefulsets", ("get", "create", "patch")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        pod_spec = self.class_spec["podSpec"]
        for container_spec in pod_spec["containers"]:
            if "workingDir" not in container_spec:
                container_spec["workingDir"] = "/data"

    def generate_pod_spec(self):
        raise NotImplementedError("generate_pod_spec method required by StatefulSetMixin not implemented")

    def generate_stateful_set_manifest(self):
        return {
            "selector": {
                "matchLabels": self.labels,
            },
            "serviceName": self.get_headless_service_name(),
            "replicas": self.class_spec["replicas"],
            "podManagementPolicy": "Parallel",
            "template": {
                "metadata": {
                    "labels": self.labels
                },
                "spec": {
                    "enableServiceLinks": False,
                } | self.generate_pod_spec()
            }
        }

    def get_pod_names(self):
        """
        Return list of pod names part of this StatefulSet
        """
        return [("%s-%d" % (self.get_target_name(), j)) for j in range(0, self.class_spec["replicas"])]

    def get_pod_fqdns(self):
        return ["%s.%s.%s.svc.cluster.local" % (
            j,
            self.get_headless_service_name(),
            self.get_target_namespace()) for j in self.get_pod_names()]

    async def generate_pod_metrics(self, pod_name):
        return

    async def generate_instance_metrics(self):
        async for descriptor, value, labels in super().generate_instance_metrics():
            yield descriptor, value, labels
        for pod_name in self.get_pod_names():
            try:
                async for descriptor, value, labels in self.generate_pod_metrics(pod_name):
                    yield descriptor, value, [pod_name] + labels
            except Exception as e:
                print("Uncaught exception while scraping pod %s metrics: %s" % (
                    pod_name, e))

    def generate_manifests(self):
        """
        Generate StatefulSet, PersistentVolumeClaim, PodDisruptionBudget manifests
        """
        manifests = super().generate_manifests()
        annotations = dict(self.get_annotations() + [("codemowers.cloud/mixin", "StatefulSetMixin")])

        if self.class_spec.get("podSpec"):
            stateful_set_spec = self.generate_stateful_set_manifest()
            storage_class = self.class_spec.get("storageClass")
            pod_spec = stateful_set_spec["template"]["spec"]

            # Set pod security context overrides
            if "securityContext" not in pod_spec:
                pod_spec["securityContext"] = {}
            pod_spec["securityContext"].update(self.get_pod_security_context())

            # Set container security context overrides
            for container_spec in pod_spec["containers"]:
                if "securityContext" not in container_spec:
                    container_spec["securityContext"] = {}
                container_spec["securityContext"].update(self.get_container_security_context())

            if storage_class:
                if "volumeMounts" not in pod_spec["containers"][0]:
                    pod_spec["containers"][0]["volumeMounts"] = []
                pod_spec["containers"][0]["volumeMounts"].append({
                    "name": "data",
                    "mountPath": "/data",
                })

                stateful_set_spec["volumeClaimTemplates"] = [{
                    "metadata": {
                        "name": "data"
                    }, "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        "storageClassName": storage_class,
                        "resources": {
                            "requests": {
                                "storage": "100Mi"
                            }
                        }
                    }

                }]

            if "revisionHistoryLimit" not in stateful_set_spec:
                stateful_set_spec["revisionHistoryLimit"] = 0

            # Specify pod anti affinity rules only if there are more than 1 replica
            if self.class_spec.get("replicas") > 1:
                if "affinity" not in stateful_set_spec["template"]["spec"]:
                    stateful_set_spec["template"]["spec"]["affinity"] = {}
                if "podAntiAffinity" not in stateful_set_spec["template"]["spec"]["affinity"]:
                    stateful_set_spec["template"]["spec"]["affinity"]["podAntiAffinity"] = {}
                if "requiredDuringSchedulingIgnoredDuringExecution" not in stateful_set_spec["template"]["spec"]["affinity"]["podAntiAffinity"]:
                    stateful_set_spec["template"]["spec"]["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"] = []
                stateful_set_spec["template"]["spec"]["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"].append({
                    "labelSelector": self.label_selector,
                    "topologyKey": self.class_spec.get("topologyKey", self.get_pod_topology_key())
                })

            # Create pod disruption budget only if more than 1 replica
            if self.class_spec.get("replicas") > 1:
                manifests.append({
                    "apiVersion": "policy/v1",
                    "kind": "PodDisruptionBudget",
                    "metadata": {
                        "namespace": self.get_target_namespace(),
                        "name": self.get_target_name(),
                        "labels": self.labels,
                        "annotations": annotations,
                        "ownerReferences": [self.get_instance_owner()],
                    },
                    "spec": {
                        "maxUnavailable": 1,
                        "selector": {
                            "matchLabels": self.labels
                        }
                    }
                })

            manifests.append({
                "apiVersion": "apps/v1",
                "kind": "StatefulSet",
                "metadata": {
                    "namespace": self.get_target_namespace(),
                    "name": self.get_target_name(),
                    "labels": self.labels,
                    "annotations": annotations,
                    "ownerReferences": [self.get_instance_owner()],
                },
                "spec": stateful_set_spec
            })
        return manifests


class SharedMixin():
    """
    Add many source resources to one target resource,
    this can be used to implement multiple logical databases in single database cluster
    """

    def get_instance_owner(self):
        """
        Return the instance as the owner for generated resources
        """
        if self.class_spec.get("shared", False):
            return {
                "apiVersion": "%s/%s" % (self.GROUP, self.VERSION),
                "kind": self.get_class_singular(),
                "name": self.class_name,
                "uid": self.class_uid,
            }
        else:
            return super().get_instance_owner()

    def get_target_name(self):
        """
        Derive target name from class name if resource is shared
        """
        if self.class_spec.get("shared"):
            return self.class_name
        else:
            return super().get_target_name()

    @classmethod
    def get_class_properties(self):
        """
        Add `shared` boolean property for the class definition
        """
        return super().get_class_properties() + [
            ("shared", {"type": "boolean", **IMMUTABLE_FIELD}),
        ]

    @classmethod
    def get_class_printer_columns(cls):
        """
        Add `Shared` column for the printer
        """
        return super().get_class_printer_columns() + [{
            "name": "Shared",
            "jsonPath": ".spec.shared",
            "type": "boolean",
        }]


class RoutedMixin:
    """
    Handle deployment of routers and connection poolers (eg. mysql-router, pgbouncer)
    """
    @classmethod
    def get_optional_base_properties(self):
        return super().get_optional_base_properties() + [
            ("routerPodSpec", {
                "type": "object",
                "properties": {
                    "resources": self.POD_RESOURCES,
                }
            })
        ]

    @classmethod
    def get_routed_mixin_properties(self):
        return [
            ("routers", {"type": "integer"}),
            ("routerPodSpec", {"type": "object", "properties": self.get_pod_properties()}),
        ]

    @classmethod
    def get_class_properties(self):
        return super().get_class_properties() + self.get_routed_mixin_properties()

    @classmethod
    def get_optional_instance_properties(self):
        return super().get_optional_instance_properties() + self.get_routed_mixin_properties()


class CustomResourceMixin:
    """
    Instantiate another custom resource in the target namespace
    """
    async def reconcile_instance(self):
        """
        Restart instance tasks if instance is reconciled
        """
        await super().reconcile_instance()
        manifests = self.generate_custom_resources()
        for manifest in manifests:
            group, version = manifest["apiVersion"].split("/")
            print("  Reconciling", group, manifest["kind"], manifest["metadata"]["name"])

            try:
                await self.co.create_namespaced_custom_object(
                    group,
                    version,
                    manifest["metadata"]["namespace"],
                    manifest["kind"].lower() + "s",
                    manifest)
            except ApiException as e:
                if e.status == 409:
                    pass
                else:
                    raise


class MonitoringMixin:
    @classmethod
    def generate_operator_tasks(cls, api_client, co, args):
        cls.metrics_buf = ""
        return super().generate_operator_tasks(api_client, co, args) + [
            cls.run_monitoring_loop(api_client, co, args),
            cls.run_monitoring_metrics_server_loop(),
        ]

    @classmethod
    async def run_monitoring_metrics_server_loop(cls):
        """
        Serve Prometheus metrics on port 8000
        """
        async def handler(request):
            return web.Response(text=cls.metrics_buf)
        app = web.Application()
        app.add_routes([web.get("/metrics", handler)])
        await web._run_app(app, host="0.0.0.0", port=8000)

    @classmethod
    async def scrape(cls):
        keys_seen = set()
        async for descriptor, value, label_values in cls.generate_operator_metrics():
            key, help, label_keys = descriptor
            if key not in keys_seen:
                yield "# HELP %s %s" % (key, help)
                yield "# TYPE %s gauge" % (key)
            if len(label_keys) != len(label_values):
                raise ValueError("Expected %d labels (%s), got %d (%s) for %s" % (
                    len(label_keys),
                    repr(label_keys),
                    len(label_values),
                    repr(label_values),
                    key))
            if label_keys:
                z = "%s{%s}" % (key, ",".join(["%s=%s" % (k, repr(v)) for k, v in zip(label_keys, label_values)]))
            else:
                z = key
            yield "%s %s" % (z, value)

    @classmethod
    async def run_monitoring_loop(cls, api_client, co, args):
        while True:
            buf = ""
            then = time()
            async for line in cls.scrape():
                buf += line + "\n"
            now = time()
            buf += "# TYPE codemowers_operator_scrape_duration_seconds gauge\n"
            buf += "codemowers_operator_scrape_duration_seconds %s\n" % (now - then)
            cls.metrics_buf = buf
            await asyncio.sleep(max(30 - (now - then), 0))


class PodMonitorMixin:
    @classmethod
    def generate_operator_cluster_role_rules(cls):
        yield from super().generate_operator_cluster_role_rules()
        yield "monitoring.coreos.com", "podmonitors", ("create", "patch")

    def generate_pod_monitor(self):
        return {
            "selector": {
                "matchLabels": self.labels
            }
        }

    def generate_manifests(self):
        return super().generate_manifests() + [{
            "apiVersion": "monitoring.coreos.com/v1",
            "kind": "PodMonitor",
            "metadata": {
                "namespace": self.get_target_namespace(),
                "name": self.get_target_name(),
                "labels": self.labels,
                "annotations": dict(self.get_annotations() + [("codemowers.cloud/mixin", "PodMonitorMixin")]),
                "ownerReferences": [self.get_instance_owner()],
            },
            "spec": self.generate_pod_monitor()
        }]


class InstanceTaskMixin:
    """
    Run task loop per instance with exception handling
    """
    INSTANCE_TASK_STARTUP_DELAY = 5
    INSTANCE_TASK_INTERVAL = 30
    INSTANCE_TASK_MIN_DELAY = 3
    INSTANCE_TASK_EXCEPTION_DELAY = 60

    class InstanceTaskDisabled(Exception):
        pass

    async def run_instance_task_loop(self):
        await asyncio.sleep(self.INSTANCE_TASK_STARTUP_DELAY)
        while True:
            try:
                then = time()
                await self.run_instance_task()
                now = time()
                duration = now - then
                await asyncio.sleep(max(
                    duration - self.INSTANCE_TASK_INTERVAL,
                    self.INSTANCE_TASK_MIN_DELAY))
            except self.InstanceTaskDisabled as e:
                print("  Instance task disabled for %s %s: %s" % (self.SINGULAR, self.name, e))
                break
            except Exception as e:
                print("Exception while running %s %s task: %s" % (
                    self.SINGULAR,
                    self.name,
                    e))
                await asyncio.sleep(self.INSTANCE_TASK_EXCEPTION_DELAY)
        # TODO: tasks.pop(key)

    async def cancel_instance_tasks(self):
        key = (self.SINGULAR, self.name)
        task = self.tasks.pop(key, None)
        if task:
            task.cancel()
        return key

    async def reconcile_instance(self):
        """
        Restart instance tasks if instance is reconciled
        """
        key = await self.cancel_instance_tasks()

        await super().reconcile_instance()

        self.tasks[key] = asyncio.create_task(self.run_instance_task_loop())

    async def cleanup_instance(self):
        await self.cancel_instance_tasks()
        await super().cleanup_instance()


class InstanceClaimMixin():
    async def reconcile_instance(self):
        try:
            await super().reconcile_instance()
        except Exception:
            raise
        else:
            try:
                await self.set_claim_phase(self.co, self.spec["claimRef"], self.CLAIM_STATE_BOUND)
            except ApiException as e:
                if e.status == 422:
                    raise ReconcileError("Failed to patch claim status for %s %s" % (
                        self.spec["claimRef"]["kind"],
                        self.spec["claimRef"]["name"]))
                else:
                    raise

    @classmethod
    async def reconcile_claim(cls, api_client, co, body):
        await super().reconcile_claim(api_client, co, body)
        instance_name = cls.generate_instance_name(body)
        try:
            current = await co.get_cluster_custom_object(cls.GROUP, cls.VERSION,
                cls.PLURAL.lower(),
                instance_name)
        except ApiException as e:
            if e.status != 404:
                raise
            current = None

        if not current:
            current = await co.create_cluster_custom_object(cls.GROUP, cls.VERSION,
                cls.PLURAL.lower(),
                await cls.generate_instance_body(body, co))

        if current["spec"]["claimRef"]["resourceVersion"] == body["metadata"]["resourceVersion"]:
            print("  Up to date %s %s/%s" % (cls.SINGULAR.lower(), body["metadata"]["namespace"], instance_name))
            return

        print("  Patching %s %s/%s" % (cls.SINGULAR.lower(), body["metadata"]["namespace"], instance_name))
        patches = cls.generate_instance_patches(body)
        for patch in patches:
            print("    Applying ", patch)
        await co.patch_cluster_custom_object(cls.GROUP, cls.VERSION,
            cls.PLURAL.lower(),
            instance_name,
            patches)
        return

    @classmethod
    def get_instance_printer_columns(cls):
        """
        Add claim and reclaim policy columns
        """
        return super().get_instance_printer_columns() + [{
            "name": "Claim namespace",
            "jsonPath": ".spec.claimRef.namespace",
            "type": "string",
        }, {
            "name": "Claim name",
            "jsonPath": ".spec.claimRef.name",
            "type": "string",
        }, {
            "name": "Reclaim policy",
            "jsonPath": ".metadata.ownerReferences[0].name",
            "type": "string",
        }]

    @classmethod
    def get_required_instance_properties(cls):
        """
        Add `claimRef` property for the instance resource definition
        """
        fields = (
            ("kind", {"type": "string", **IMMUTABLE_FIELD}),
            ("namespace", {"type": "string", **IMMUTABLE_FIELD}),
            ("name", {"type": "string", **IMMUTABLE_FIELD}),
            ("uid", {"type": "string", **IMMUTABLE_FIELD}),
            ("apiVersion", {"type": "string", **IMMUTABLE_FIELD}),
            ("resourceVersion", {"type": "string"}),
        )
        return super().get_required_instance_properties() + [
            ("claimRef", {
                # Missing uid means Claim is deleted
                "required": ["kind", "namespace", "name", "apiVersion", "resourceVersion"],
                "properties": dict(fields),
                "type": "object"
            })
        ]


class ClassedOperator(InstanceClaimMixin, InstanceMixin, ClaimMixin, Operator):
    """
    Operator subclass for building resource class based operators

    The idea here is to tuck away impementation details into class definition keeping
    the end user custom resource as simple as possible
    """

    @classmethod
    def generate_nuke_command(cls):
        yield from super().generate_nuke_command()
        yield "kubectl delete --all=true %s.%s" % (
            cls.get_class_singular(),
            cls.GROUP)
        yield "kubectl delete customresourcedefinition %s.%s" % (
            cls.get_class_plural().lower(),
            cls.GROUP)

    def __init__(self, body, class_body, **kwargs):
        super().__init__(body, **kwargs)
        self.class_name = class_body["metadata"]["name"]
        self.class_spec = class_body["spec"]
        self.class_uid = class_body["metadata"]["uid"]

    @classmethod
    def get_class_printer_columns(cls):
        return [{
            "name": "Reclaim policy",
            "jsonPath": ".spec.reclaimPolicy",
            "type": "string",
        }]

    @classmethod
    def generate_operator_cluster_role_rules(cls):
        yield from super().generate_operator_cluster_role_rules()
        yield cls.GROUP, cls.PLURAL, ("create", "patch")
        yield cls.GROUP, cls.get_class_plural(), ("get", "list", "watch")

    @classmethod
    def generate_operator_crd_definition(cls):
        return super().generate_operator_crd_definition() + [
            cls.generate_class_definition()]

    @classmethod
    async def generate_operator_metrics(cls):
        """
        Implement ClassedOperator metrics
        """
        async for descriptor, value, labels in super().generate_operator_metrics():
            yield descriptor, value, labels
        for instance in cls.cached_instances.values():
            async for descriptor, value, labels in instance.generate_instance_metrics():
                yield descriptor, value, [instance.name] + labels

    @classmethod
    def get_class_plural(cls):
        return "%sClasses" % cls.SINGULAR

    @classmethod
    def get_class_singular(cls):
        return "%sClass" % cls.SINGULAR

    @classmethod
    def get_class_singular_lower(cls):
        return cls.get_class_singular().lower()

    @classmethod
    def generate_instance_patches(cls, claim_body):
        return []

    def get_target_name(self):
        return "%s-%s" % (self.class_name, self.uid)

    def get_annotations(self):
        """
        Add `codemowers.cloud/class` annotation for target resources
        """
        return super().get_annotations() + [
            ("codemowers.cloud/class", self.class_name),
        ]

    @classmethod
    async def _construct_resource(cls, args, co, body, api_client):
        class_body = await co.get_cluster_custom_object(
            cls.GROUP,
            cls.VERSION,
            cls.get_class_plural().lower(),
            body["spec"]["class"])

        # Initialize status subresource of instance
        if "status" not in body or "conditions" not in body["status"]:
            initial_status = {
                "phase": cls.INSTANCE_STATE_PENDING,
                "conditions": cls.generate_instance_conditions(),
            }
            await co.replace_cluster_custom_object_status(
                cls.GROUP, cls.VERSION,
                cls.PLURAL.lower(), body["metadata"]["name"], {
                    "apiVersion": "%s/%s" % (cls.GROUP, cls.VERSION),
                    "kind": cls.SINGULAR,
                    "status": initial_status,
                    "metadata": {
                        "name": body["metadata"]["name"],
                        "resourceVersion": body["metadata"]["resourceVersion"]}})
            raise ReconcileDeferred("Status initialized")

        i = cls(body, class_body, **args)
        i.co = co
        i.api_client = api_client
        i.v1 = client.CoreV1Api(api_client)
        return i

    @classmethod
    def get_base_printer_columns(cls):
        return super().get_base_printer_columns() + [{
            "name": "Class",
            "jsonPath": ".spec.class",
            "type": "string",
        }]

    @classmethod
    def get_required_base_properties(cls):
        """
        Add `class` property for instance and claim both
        """
        return super().get_required_base_properties() + [
            ("class", {"type": "string", **IMMUTABLE_FIELD})
        ]

    @classmethod
    def generate_class_definition(cls):
        plural = cls.get_class_plural()
        singular = cls.get_class_singular()

        return {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {
                "name": "%s.%s" % (plural.lower(), cls.GROUP),
            },
            "spec": {
                "scope": "Cluster",
                "group": cls.GROUP,
                "names": {
                    "plural": plural.lower(),
                    "singular": singular.lower(),
                    "kind": singular,
                },
                "versions": [{
                    "name": cls.VERSION,
                    "schema": {
                        "openAPIV3Schema": {
                            "required": ["spec"],
                            "properties": {
                                "spec": {
                                    "properties": dict(cls.get_class_properties()),
                                    "type": "object",
                                },
                            },
                            "type": "object",
                        },
                    },
                    "served": True,
                    "storage": True,
                    "additionalPrinterColumns": cls.get_class_printer_columns(),
                }],
                "conversion": {
                    "strategy": "None",
                }
            }
        }


__all__ = \
    "ClassedOperator", \
    "Operator", \
    \
    "CapacityMixin", \
    "ClaimMixin", \
    "ClaimSecretMixin", \
    "ClusterManagementMixin", \
    "ConsoleMixin", \
    "CustomResourceMixin", \
    "HeadlessMixin", \
    "InstanceMixin", \
    "InstanceSecretMixin", \
    "PersistentMixin", \
    "PersistentVolumeClaimMixin", \
    "PodSpecMixin", \
    "PodMonitorMixin", \
    "PrimarySecondaryMixin", \
    "ReplicasSpecMixin", \
    "RoutedMixin", \
    "ServiceMixin", \
    "SharedMixin", \
    "StatefulSetMixin", \
    "UpstreamMixin"
