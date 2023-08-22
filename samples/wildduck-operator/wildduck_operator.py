#!/usr/bin/env python3
from operatorlib import ClaimMixin, Operator, ReconcileError
import aiohttp
import os


class WildduckOperator(ClaimMixin, Operator):
    OPERATOR = "wildduck-operator"
    GROUP = "codemowers.io"
    VERSION = "v1alpha1"

    @classmethod
    def get_operator_namespace(cls):
        return "wildduck"

    @classmethod
    def get_claim_singular(cls):
        return "OIDCGatewayUser"

    @classmethod
    def get_claim_plural(cls):
        return "OIDCGatewayUsers"

    @classmethod
    async def reconcile_claim(cls, api_client, co, body):
        username = body["metadata"]["name"]
        if body["spec"]["type"] != "person":
            print("  Skipping non-person user:", username)
            return

        # TODO: Cleaner env var handling
        WILDDUCK_API_TOKEN = os.environ["WILDDUCK_API_TOKEN"]
        WILDDUCK_API_URL = os.environ["WILDDUCK_API_URL"]
        MANAGED_DOMAIN = os.environ["MANAGED_DOMAIN"]
        ALLOWED_GROUPS = os.environ["ALLOWED_GROUPS"].split(",")
        QUOTA = int(os.environ.get("QUOTA", 15 * 2**30))
        SPAM_LEVEL = int(os.environ.get("SPAM_LEVEL", "25"))
        HEADERS = {"X-Access-Token": WILDDUCK_API_TOKEN}

        forwarding = {}
        display_name = {}

        if "profile" in body["status"]:
            if "name" in body["status"]["profile"]:
                display_name["name"] = body["status"]["profile"]["name"]  # TODO: sanitize?

        # Determine forwarding address outside organization
        # TODO: Have this in status subresource
        if "email" in body["spec"]:
            forwarding = {"targets": [body["spec"]["email"]]}
        elif "githubEmails" in body["spec"]:
            for j in body["spec"]["githubEmails"]:
                forwarding = {"targets": [j["email"]]}
                break
            for j in body["spec"]["githubEmails"]:
                if j["primary"]:
                    forwarding = {"targets": [j["email"]]}
                    break
        else:
            print("Warning: couldn't determine forwarding address for user %s" % username)


        # User account is considered enabled in Wildduck
        # if user is member of one of the allowed groups
        enabled = False
        for g in body["status"]["groups"]:
            group = "%(prefix)s:%(name)s" % g
            if group in ALLOWED_GROUPS:
                enabled = True

        async with aiohttp.ClientSession() as session:
            managed_address = "%s@%s" % (username, MANAGED_DOMAIN)
            u = {
                "username": username,
                "address": managed_address,
                "password": False,
                "fromWhitelist": [managed_address],
            } | display_name | forwarding

            resp = await session.post(
                "%s/users" % WILDDUCK_API_URL,
                headers=HEADERS,
                json=u)
            # TODO: Move to 409 Conflict
            if resp.status == 500:
                code = (await resp.json())["code"]
                if code == "UserExistsError":
                    pass
                else:
                    raise ReconcileError("Wildduck API returned %d (%s) while creating user %s: %s" % (
                        resp.status, code, username, u))
            elif resp.status != 200:
                raise ReconcileError("Wildduck API returned %d while creating user %s: %s" % (
                    resp.status, username, u))

            resp = await session.get(
                "%s/users/resolve/%s" % (WILDDUCK_API_URL, username),
                headers=HEADERS)

            if resp.status != 200:
                raise ReconcileError("Wildduck API returned %d while looking up user %s" % (
                    resp.status, username))

            identifier = (await resp.json())["id"]

            resp = await session.get(
                "%s/users/%s" % (WILDDUCK_API_URL, identifier),
                headers=HEADERS)

            d = {
                "disabledScopes": ["pop3"],
                "suspended": not enabled,
                "spamLevel": 25,
                "quota": QUOTA,
                "internalData": {"codemowers.cloud/managed-by": "wildduck-operator"}
            } | display_name
            if not enabled:
                # Reset forwarding when account is suspended
                d |= forwarding

            resp = await session.put(
                "%s/users/%s" % (WILDDUCK_API_URL, identifier),
                headers=HEADERS,
                json=d)
            if resp.status != 200:
                code = (await resp.json())["code"]
                raise ReconcileError("Wildduck API returned %d (%s) while updating user %s" % (
                    resp.status, code, username))

            # TODO: Add cleanup code
            #resp = await session.delete(
            #    "%s/users/%s" % (WILDDUCK_API_URL, identifier),
            #    headers=HEADERS)
            #if resp.status != 200:
            #    raise ReconcileError("Wildduck API returned %d (%s) while deleting user %s" % (
            #        resp.status, code, username))

if __name__ == "__main__":
    WildduckOperator.run()
