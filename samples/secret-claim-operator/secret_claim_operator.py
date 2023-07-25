#!/usr/bin/env python3
from base64 import b64encode
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException
from operatorlib import ClaimSecretMixin, ClaimMixin, Operator


class SecretClaimOperator(ClaimSecretMixin, ClaimMixin, Operator):
    OPERATOR = "secret-claim-operator"
    GROUP = "codemowers.cloud"
    VERSION = "v1beta1"

    @classmethod
    def get_operator_namespace(cls):
        return "secret-claim-operator"

    @classmethod
    def get_claim_singular(cls):
        return "SecretClaim"

    @classmethod
    def get_required_claim_properties(cls):
        return super(SecretClaimOperator, cls).get_required_claim_properties() + [
            ("size", {
                "default": 32,
                "type": "integer",
                "description": "Generated secret length",
            }),
            ("mapping", {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "Secret key"
                        },
                        "value": {
                            "type": "string",
                            "description": "Secret value with suitable placeholders"
                        },
                    }
                },
            })
        ]

    @classmethod
    async def reconcile_claim(cls, api_client, co, body):
        v1 = client.CoreV1Api(api_client)
        ctx = {
            "plaintext": cls.generate_random_string(body["spec"]["size"])
        }
        d = {}
        for o in body["spec"]["mapping"]:
            d[o["key"]] = b64encode((o["value"] % ctx).encode("ascii")).decode("ascii")

        body = {
            "metadata": {
                "name": body["metadata"]["name"],
                "namespace": body["metadata"]["namespace"],
                "ownerReferences": [{
                    "apiVersion": "%s/%s" % (cls.GROUP, cls.VERSION),
                    "kind": cls.get_claim_singular(),
                    "name": body["metadata"]["name"],
                    "uid": body["metadata"]["uid"],
                }]
            },
            "data": d
        }

        try:
            await v1.create_namespaced_secret(
                body["metadata"]["namespace"],
                client.V1Secret(**body))
        except ApiException as e:
            if e.status == 409:
                print("Secret %s/%s already exists" % (
                    body["metadata"]["namespace"],
                    body["metadata"]["name"]))
            else:
                raise


if __name__ == "__main__":
    SecretClaimOperator.run()
