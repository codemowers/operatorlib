#!/usr/bin/env python3
from operatorlib import ClaimMixin, Operator, ReconcileError
import aiohttp
import os


class WildduckOperator(ClaimMixin, Operator):
    OPERATOR = "wildduck-operator"
    GROUP = "codemowers.cloud"
    VERSION = "v1beta1"

    @classmethod
    def get_operator_namespace(cls):
        return "wildduck"

    @classmethod
    def get_claim_singular(cls):
        return "OIDCUser"

    @classmethod
    def get_claim_plural(cls):
        return "OIDCUsers"

    @classmethod
    async def reconcile_claim(cls, api_client, co, body):
        username = body["metadata"]["name"]

        # TODO: Cleaner env var handling
        WILDDUCK_API_TOKEN = os.environ["WILDDUCK_API_TOKEN"]
        WILDDUCK_API_URL = os.environ["WILDDUCK_API_URL"]
        MANAGED_DOMAIN = os.environ["MANAGED_DOMAIN"]
        ALLOWED_GROUPS = os.environ["ALLOWED_GROUPS"].split(",")
        ACCOUNT_TYPES = os.getenv("ACCOUNT_TYPES", "person,service").split(",")
        QUOTA = int(os.environ.get("QUOTA", 15 * 2**30))
        SPAM_LEVEL = int(os.environ.get("SPAM_LEVEL", "25"))
        HEADERS = {"X-Access-Token": WILDDUCK_API_TOKEN}

        if body["spec"]["type"] not in ACCOUNT_TYPES:
            print("  Skipping user %s of unexpected type %s", username, body["spec"]["type"])
            return

        forwarding = {}
        display_name = {}

        if "profile" in body["status"]:
            if "name" in body["status"]["profile"]:
                display_name["name"] = body["status"]["profile"]["name"]  # TODO: sanitize?

        # Determine forwarding address outside organization
        # TODO: Have this in status subresource
        if "primaryEmail" in body["status"]:
            forwarding = {"targets": [body["spec"]["email"]]}
        elif "emails" in body["github"]:
            for j in body["emails"]["github"]:
                forwarding = {"targets": [j["email"]]}
                break
            for j in body["emails"]["github"]:
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
                        resp.status, code, username, await resp.json()))
            elif resp.status != 200:
                raise ReconcileError("Wildduck API returned %d while creating user %s: %s" % (
                    resp.status, username, await resp.json()))

            resp = await session.get(
                "%s/users/resolve/%s" % (WILDDUCK_API_URL, username),
                headers=HEADERS)

            if resp.status != 200:
                raise ReconcileError("Wildduck API returned %d while looking up user %s: %s" % (
                    resp.status, username, await resp.json()))

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
