"""Live GPU access is a sanitized observation, never production qualification."""

import asyncio
import copy
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from tornado import web
from tornado.httpclient import AsyncHTTPClient
from tornado.testing import AsyncHTTPSTestCase, AsyncHTTPTestCase

from e2x_course_hub.cps import platform_status
from e2x_course_hub.cps.compute import ComputePolicyClient
from e2x_course_hub.cps.providers import LocalCourseProvider
from e2x_course_hub.schema.user import User

NOW = datetime(2026, 10, 13, 21, 0, tzinfo=timezone.utc)
REVISION = "sha256:" + "a" * 64
POLICY = "b" * 64


def registry():
    return {
        "revision": REVISION,
        "policy_hash": POLICY,
        "ready": True,
        "qualified_profiles": ["interactive-shared-5", "interactive-shared-10"],
        "approvals": {
            "team": {
                "owner": "neutral",
                "server": "rtc",
                "members": ["protected-person-id"],
                "principal": "workspace:cps:team",
                "profiles": ["interactive-shared-5"],
                "ceiling": {"cpu": "4", "memory": "16Gi", "gpuMemoryGiB": 5},
                "namespace": "jupyterhub",
                "pod": "jupyter-neutral--rtc",
                "group_id": "group",
                "course_id": "course",
                "term_id": "term",
                "storage": {
                    "namespace": "jupyterhub",
                    "pvc": "pvc",
                    "pv": "pv",
                    "path": "/mnt/retained",
                    "group_hash": "c" * 64,
                    "storage_version": 2,
                    "readonly_mounts": [],
                },
            }
        },
    }


class Client:
    console = "cps"

    def __init__(self, value=None, error=None):
        self.value = registry() if value is None else value
        self.error = error
        self.calls = 0

    async def native_group_approvals(self):
        self.calls += 1
        if self.error:
            raise self.error
        return copy.deepcopy(self.value)


class AvailabilityTest(IsolatedAsyncioTestCase):
    async def test_ready_is_own_source_aggregate_without_identity_or_storage_payloads(self):
        result = await platform_status.read_availability(Client(), now=NOW)
        self.assertEqual(
            result,
            {
                "state": "ready",
                "revision": REVISION,
                "policyHash": POLICY,
                "approvedProfiles": ["interactive-shared-5"],
                "approvedWorkspaceCount": 1,
                "checkedAt": "2026-10-13T21:00:00Z",
            },
        )
        encoded = json.dumps(result)
        for secret in ("protected-person-id", "neutral", "/mnt/retained", "team", "group"):
            self.assertNotIn(secret, encoded)

    async def test_pending_registry_does_not_claim_ready(self):
        value = registry()
        value["ready"] = False
        result = await platform_status.read_availability(Client(value), now=NOW)
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["approvedWorkspaceCount"], 1)

    async def test_empty_own_registry_has_no_approved_profiles_or_workspace_claim(self):
        value = registry()
        value["approvals"] = {}
        result = await platform_status.read_availability(Client(value), now=NOW)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["approvedWorkspaceCount"], 0)
        self.assertEqual(result["approvedProfiles"], [])

    async def test_malformed_and_foreign_payloads_fail_closed(self):
        cases = []
        for key, value in [
            ("revision", "a" * 64),
            ("policy_hash", "sha256:" + POLICY),
            ("ready", 1),
            ("qualified_profiles", ["interactive-cpu"]),
            ("qualified_profiles", ["interactive-shared-5"] * 2),
            ("qualified_profiles", ["interactive-shared-5"] * 33),
            ("approvals", []),
            ("unexpected", True),
        ]:
            changed = registry()
            changed[key] = value
            cases.append(changed)
        for key, value in [
            ("principal", "workspace:cit:team"),
            ("namespace", "cit-jhub"),
            ("profiles", ["interactive-shared-20"]),
            ("profiles", ["interactive-shared-5"] * 2),
            ("members", ["protected-person-id"] * 2),
            ("members", ["x" * 257]),
            ("storage", {"path": "/mnt/retained"}),
            ("owner", "x" * 129),
        ]:
            changed = registry()
            changed["approvals"]["team"][key] = value
            cases.append(changed)
        changed = registry()
        changed["approvals"]["cit:team"] = changed["approvals"].pop("team")
        cases.append(changed)
        for value in cases:
            with self.subTest(value=value):
                result = await platform_status.read_availability(Client(value), now=NOW)
                self.assertEqual(result["state"], "unavailable")
                self.assertIsNone(result["revision"])
                self.assertEqual(result["approvedProfiles"], [])
                self.assertEqual(result["approvedWorkspaceCount"], 0)

    async def test_unconfigured_error_and_timeout_are_unavailable_without_error_details(self):
        for client in [None, Client(error=RuntimeError("private-credential"))]:
            result = await platform_status.read_availability(client, now=NOW)
            self.assertEqual(result["state"], "unavailable")
            self.assertNotIn("private-credential", json.dumps(result))

        class SlowClient(Client):
            async def native_group_approvals(self):
                await asyncio.sleep(10)

        with patch.object(platform_status, "AVAILABILITY_TIMEOUT", 0.001):
            result = await platform_status.read_availability(SlowClient(), now=NOW)
        self.assertEqual(result["state"], "unavailable")


class AvailabilityHTTPTest(AsyncHTTPTestCase):
    def get_app(self):
        self.admin = True
        self.client = Client()
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = LocalCourseProvider(Path(self.tmp.name) / "db", "cps")
        outer = self

        class Handler(platform_status.PlatformStatusHandler):
            def get_current_user(self):
                return (
                    {"name": "visitor"}
                    if self.get_secure_cookie("signed-user") == b"visitor"
                    else None
                )

            async def get_user(self):
                return User(username="visitor", admin=outer.admin, groups=[])

        class Hub:
            async def get_user(self, name):
                return {"name": name, "admin": outer.admin, "groups": []}

        return web.Application(
            [(r"/api/platform-status", Handler)],
            login_url="/login",
            last_config_check=time.time(),
            api=NS(course_api=NS(hub_api=Hub())),
            course_provider=self.provider,
            compute_policy=self.client,
            cookie_secret="test",
        )

    def tearDown(self):
        self.provider.db.close()
        self.tmp.cleanup()
        super().tearDown()

    def headers(self):
        cookie = web.create_signed_value("test", "signed-user", "visitor").decode()
        return {"Cookie": "signed-user=" + cookie}

    def test_real_authenticated_cookie_get_composes_ready_with_missing_operator_report(self):
        with patch.dict("os.environ", {}, clear=True):
            response = self.fetch("/api/platform-status", headers=self.headers())
        self.assertEqual(response.code, 200)
        result = json.loads(response.body)
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["title"], "Production qualification report: unavailable")
        self.assertEqual(result["availability"]["state"], "ready")
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_anonymous_bad_cookie_and_current_nonadmin_do_not_fetch_registry(self):
        for headers in [{}, {"Cookie": "signed-user=visitor"}]:
            self.assertEqual(
                self.fetch("/api/platform-status", headers=headers, follow_redirects=False).code,
                302,
            )
        self.admin = False
        self.assertEqual(self.fetch("/api/platform-status", headers=self.headers()).code, 403)
        self.assertEqual(self.client.calls, 0)

    def test_fresh_operator_checks_are_preserved_not_upgraded_by_ready_registry(self):
        report = {
            "version": 1,
            "state": "qualification",
            "title": "Packing qualification",
            "detail": "Full pool remains unqualified.",
            "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
            "checks": [{"id": "packing", "label": "Full pool packing", "state": "pending"}],
        }
        with patch.object(platform_status, "read_status", return_value=report):
            response = self.fetch("/api/platform-status", headers=self.headers())
        result = json.loads(response.body)
        self.assertEqual({k: result[k] for k in report}, report)
        self.assertEqual(result["availability"]["state"], "ready")
        self.assertEqual(report.get("availability"), None)

    def test_enabled_operator_report_does_not_override_pending_or_failed_live_registry(self):
        report = {
            "version": 1,
            "state": "enabled",
            "title": "Qualified",
            "detail": "Operator report.",
            "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
            "checks": [{"id": "qualification", "label": "Qualification", "state": "passed"}],
        }
        self.client.value["ready"] = False
        with patch.object(platform_status, "read_status", return_value=report):
            result = json.loads(self.fetch("/api/platform-status", headers=self.headers()).body)
        self.assertEqual(result["state"], "enabled")
        self.assertEqual(result["availability"]["state"], "pending")
        self.client.error = RuntimeError("private")
        with patch.object(platform_status, "read_status", return_value=report):
            result = json.loads(self.fetch("/api/platform-status", headers=self.headers()).body)
        self.assertEqual(result["state"], "enabled")
        self.assertEqual(result["availability"]["state"], "unavailable")


class ConfiguredPolicyHTTPSTest(AsyncHTTPSTestCase):
    """Use the released policy client through a real private HTTPS endpoint."""

    def get_app(self):
        self.policy_requests = 0
        outer = self

        class Handler(platform_status.PlatformStatusHandler):
            def get_current_user(self):
                return (
                    {"name": "visitor"}
                    if self.get_secure_cookie("signed-user") == b"visitor"
                    else None
                )

            async def get_user(self):
                return User(username="visitor", admin=True, groups=[])

        class Hub:
            async def get_user(self, name):
                return {"name": name, "admin": True, "groups": []}

        class Policy(web.RequestHandler):
            async def get(self):
                outer.policy_requests += 1
                if self.request.headers.get("Authorization") != "Bearer policy-private-token":
                    raise web.HTTPError(401)
                self.write(registry())

        return web.Application(
            [(r"/api/platform-status", Handler), (r"/internal/v1/native-group-approvals", Policy)],
            login_url="/login",
            last_config_check=time.time(),
            api=NS(course_api=NS(hub_api=Hub())),
            cookie_secret="test",
        )

    def setUp(self):
        super().setUp()
        self.policy_client = ComputePolicyClient(self.get_url(""), "policy-private-token", "cps")
        self._app.settings["compute_policy"] = self.policy_client
        # Only the local test server's generated self-signed certificate is accepted here.
        # Production still uses the configured client with certificate verification enabled.
        self.transport = AsyncHTTPClient(force_instance=True, defaults={"validate_cert": False})
        self.factory = patch(
            "e2x_course_hub.cps.compute.AsyncHTTPClient", return_value=self.transport
        )
        self.factory.start()

    def tearDown(self):
        self.factory.stop()
        self.transport.close()
        super().tearDown()

    def test_normal_configured_client_fetches_real_source_owned_registry(self):
        cookie = web.create_signed_value("test", "signed-user", "visitor").decode()
        response = self.fetch("/api/platform-status", headers={"Cookie": "signed-user=" + cookie})
        self.assertEqual(response.code, 200)
        self.assertEqual(json.loads(response.body)["availability"]["state"], "ready")
        self.assertEqual(self.policy_requests, 1)

    def test_actual_policy_authentication_failure_cannot_claim_gpu_ready(self):
        self.policy_client.token = "wrong-policy-token"
        cookie = web.create_signed_value("test", "signed-user", "visitor").decode()
        response = self.fetch("/api/platform-status", headers={"Cookie": "signed-user=" + cookie})
        self.assertEqual(response.code, 200)
        self.assertEqual(json.loads(response.body)["availability"]["state"], "unavailable")
        self.assertEqual(self.policy_requests, 1)
