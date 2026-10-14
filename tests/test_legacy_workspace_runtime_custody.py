"""Legacy runtime roles need reviewed Source ownership and current Hub custody."""

import copy
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from tornado.httpclient import HTTPClientError, HTTPResponse

import test_reviewed_workspace_adoption as adoption_fixture


class LegacyRuntimeCustodyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await adoption_fixture.AdoptionTests.asyncSetUp(self)
        review = await adoption_fixture.AdoptionTests.plan(self)
        await self.adapter.adopt_reviewed(review, actor="operator")
        self.bound = {**self.workspace, **self.course["workspace_bindings"][0]}
        self.binding = self.adapter._binding(self.bound)
        self.original_row = self.adapter._row(self.binding)
        self.hub.users["legacy-owner"]["roles"] = [
            "cps-workspace-kernel", "cps-workspace-runtime-legacy-owner"]
        self.hub.users["legacy-owner"]["id"] = 14
        self.hub.auth_header = {"Authorization": "token fixture-source-service"}
        self.requests = []
        self.response_status = 200
        self.response_headers = {}
        self.change_source_row = False
        self.proof = {
            "version": 1,
            "kind": "hub_legacy_runtime_custody",
            "source": "cps",
            "descriptor": {
                "workspace": "w", "course": "c", "term": "t", "group": "g",
                "server": "rtc", "namespace": "jupyterhub",
                "pod": "jupyter-legacy-owner--rtc",
            },
            "identity": {"id": 14, "name": "legacy-owner", "created": "2026-10-01T00:00:00Z"},
            "runtime_role": {
                "id": 18, "name": "cps-workspace-runtime-legacy-owner",
                "incarnation": "831c44a8-0757-4fbd-9b06-a0196c50bd45",
                "scopes": ["access:servers!server=legacy-owner/rtc", "users:activity!user=legacy-owner"],
            },
            "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        outer = self

        class Client:
            async def fetch(self, request):
                outer.requests.append(request)
                if outer.change_source_row:
                    outer.provider.db.execute("UPDATE workspace_accounts SET state='adoption_granting'")
                    outer.provider.db.commit()
                if outer.response_status in (403, 404):
                    raise HTTPClientError(outer.response_status)
                return HTTPResponse(request, outer.response_status, headers=outer.response_headers,
                    buffer=io.BytesIO(json.dumps(outer.proof).encode()))

        self.hub.client = Client()

    async def asyncTearDown(self):
        await adoption_fixture.AdoptionTests.asyncTearDown(self)

    async def test_reviewed_legacy_account_accepts_fresh_authenticated_custody_without_receipt_change(self):
        await self.adapter.ensure(self.bound, actor="operator")
        self.assertEqual(await self.adapter.status(self.workspace), "ready")
        self.assertEqual(self.adapter._row(self.binding), self.original_row)
        self.assertEqual(self.hub.created, [])
        self.assertEqual(len(self.requests), 2)
        for request in self.requests:
            self.assertEqual(request.method, "GET")
            self.assertEqual(urlsplit(request.url).path, "/hub/api/cps/legacy-workspace-runtime-custody")
            self.assertEqual(parse_qs(urlsplit(request.url).query), {
                "owner": ["legacy-owner"], "workspace": ["w"], "course": ["c"], "term": ["t"],
                "group": ["g"], "server": ["rtc"], "namespace": ["jupyterhub"],
                "pod": ["jupyter-legacy-owner--rtc"],
            })
            self.assertEqual(request.headers["Authorization"], "token fixture-source-service")
            self.assertEqual(request.headers["Cache-Control"], "no-store")
            self.assertFalse(request.follow_redirects)

    async def test_runtime_name_alone_and_revoked_enrollment_are_denied_on_every_readback(self):
        self.response_status = 403
        with self.assertRaises(PermissionError):
            await self.adapter.ensure(self.bound, actor="operator")
        self.assertEqual(await self.adapter.status(self.workspace), "recovery_required")
        self.response_status = 200
        await self.adapter.ensure(self.bound, actor="operator")
        self.response_status = 403
        with self.assertRaises(PermissionError):
            await self.adapter.ensure(self.bound, actor="operator")
        self.assertEqual(self.adapter._row(self.binding), self.original_row)

    async def test_non_reviewed_or_incomplete_source_receipt_cannot_fetch_custody(self):
        old = self.original_row
        identity = {"name": "legacy-owner", "created": "2026-10-01T00:00:00Z"}
        variants = [("ready", identity), ("ready", {"version": 1, "kind": "source_creation", "identity": identity}),
                    ("adoption_granting", json.loads(old["receipt"]))]
        for state, receipt in variants:
            with self.subTest(state=state, receipt=receipt):
                self.provider.db.execute("UPDATE workspace_accounts SET state=?, receipt=?", (state, json.dumps(receipt)))
                self.provider.db.commit()
                with self.assertRaises(PermissionError):
                    await self.adapter.ensure(self.bound, actor="operator")
        self.assertEqual(self.requests, [])

    async def test_closed_custody_schema_identity_descriptor_role_and_scopes_fail_closed(self):
        original = copy.deepcopy(self.proof)
        variants = [dict(original, version=True), dict(original, extra=True), dict(original, source="cit"),
                    dict(original, kind="hub_neutral_creation")]
        for key in original:
            changed = copy.deepcopy(original)
            del changed[key]
            variants.append(changed)
        for key in ("descriptor", "identity", "runtime_role"):
            variants.append(dict(original, **{key: None}))
        for key in original["descriptor"]:
            changed = copy.deepcopy(original)
            changed["descriptor"][key] = "different"
            variants.append(changed)
        for section, key, values in (
            ("identity", "id", (0, True, "14", 15)),
            ("identity", "name", ("other",)),
            ("identity", "created", ("2026-10-02T00:00:00Z", "2026-10-01Z")),
            ("runtime_role", "id", (0, True, "18")),
            ("runtime_role", "name", ("admin", "cps-workspace-runtime-other")),
            ("runtime_role", "incarnation", ("not-a-uuid", "831C44A8-0757-4FBD-9B06-A0196C50BD45")),
            ("runtime_role", "scopes", (["self"], ["users:activity!user=legacy-owner", "access:servers!server=legacy-owner/rtc"],
                ["access:servers!server=legacy-owner/rtc", "users:activity!user=legacy-owner", "admin:users"])),
        ):
            for value in values:
                changed = copy.deepcopy(original)
                changed[section][key] = value
                variants.append(changed)
        for section in ("descriptor", "identity", "runtime_role"):
            changed = copy.deepcopy(original)
            changed[section]["extra"] = True
            variants.append(changed)
        for changed in variants:
            self.proof = changed
            with self.subTest(proof=changed), self.assertRaises(PermissionError):
                await self.adapter.ensure(self.bound, actor="operator")
        self.assertEqual(self.adapter._row(self.binding), self.original_row)

    async def test_expired_future_and_non_utc_custody_are_denied(self):
        now = datetime.now(timezone.utc)
        for checked_at in ((now - timedelta(seconds=61)).isoformat(),
                           (now + timedelta(seconds=6)).isoformat(), "2026-10-14", "not-a-date",
                           now.replace(tzinfo=None).isoformat(), now.isoformat().replace("+00:00", "+01:00")):
            self.proof["checked_at"] = checked_at
            with self.subTest(checked_at=checked_at), self.assertRaises(PermissionError):
                await self.adapter.ensure(self.bound, actor="operator")

    async def test_success_body_on_non_200_or_redirect_cannot_authorize_runtime_role(self):
        for status in (201, 204, 301, 302, 307, 308):
            self.response_status = status
            with self.subTest(status=status), self.assertRaises(PermissionError):
                await self.adapter.ensure(self.bound, actor="operator")

    async def test_legacy_without_runtime_role_retains_existing_behavior_without_custody(self):
        self.hub.users["legacy-owner"]["roles"] = ["user", "cps-workspace-kernel"]
        self.response_status = 404
        await self.adapter.ensure(self.bound, actor="operator")
        self.assertEqual(await self.adapter.status(self.workspace), "ready")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.adapter._row(self.binding), self.original_row)

    async def test_enrolled_custody_rejects_restored_default_roles_or_missing_kernel_role(self):
        for roles in (
            ["cps-workspace-kernel", "cps-workspace-runtime-legacy-owner", "user"],
            ["cps-workspace-kernel", "cps-workspace-runtime-legacy-owner", "self"],
            ["cps-workspace-runtime-legacy-owner"],
        ):
            self.hub.users["legacy-owner"]["roles"] = roles
            with self.subTest(roles=roles), self.assertRaises(PermissionError):
                await self.adapter.ensure(self.bound, actor="operator")
        self.assertEqual(self.adapter._row(self.binding), self.original_row)

    async def test_removed_runtime_role_with_revoked_hub_custody_is_not_unenrolled(self):
        self.hub.users["legacy-owner"]["roles"].remove("cps-workspace-runtime-legacy-owner")
        self.response_status = 403
        with self.assertRaises(PermissionError):
            await self.adapter.ensure(self.bound, actor="operator")
        self.assertEqual(await self.adapter.status(self.workspace), "recovery_required")
        self.assertEqual(self.adapter._row(self.binding), self.original_row)

    async def test_missing_enrollment_cannot_authorize_a_present_runtime_role(self):
        self.response_status = 404
        with self.assertRaises(PermissionError):
            await self.adapter.ensure(self.bound, actor="operator")

    async def test_successful_enrollment_requires_runtime_role_in_actual_current_model(self):
        self.hub.users["legacy-owner"]["roles"].remove("cps-workspace-runtime-legacy-owner")
        with self.assertRaises(PermissionError):
            await self.adapter.ensure(self.bound, actor="operator")

    async def test_source_row_change_during_hub_fetch_cannot_be_blessed_or_rewritten(self):
        self.change_source_row = True
        with self.assertRaises(PermissionError):
            await self.adapter.ensure(self.bound, actor="operator")
        row = self.adapter._row(self.binding)
        self.assertEqual(row["state"], "adoption_granting")
        self.assertEqual(row["receipt"], self.original_row["receipt"])

    async def test_current_model_without_database_id_uses_hub_custody_and_preserves_legacy_receipt(self):
        del self.hub.users["legacy-owner"]["id"]
        await self.adapter.ensure(self.bound, actor="operator")
        self.assertEqual(self.adapter._row(self.binding), self.original_row)

    async def test_valid_custody_cannot_bypass_neutral_privilege_group_or_server_checks(self):
        original = copy.deepcopy(self.hub.users["legacy-owner"])
        for change in (
            {"admin": True},
            {"roles": original["roles"] + ["admin"]},
            {"groups": original["groups"] + ["unrelated"]},
            {"servers": {"foreign": {"stopped": True}}},
            {"servers": {"rtc": {"stopped": True, "state": {"pod_name": "other"}}}},
            {"servers": {"rtc": {"stopped": True, "state": {"namespace": "other"}}}},
        ):
            self.hub.users["legacy-owner"] = {**copy.deepcopy(original), **change}
            with self.subTest(change=change), self.assertRaises(PermissionError):
                await self.adapter.ensure(self.bound, actor="operator")
        self.assertEqual(self.adapter._row(self.binding), self.original_row)
