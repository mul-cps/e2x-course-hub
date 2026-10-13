"""Reviewed adoption is distinct from successful Source POST provenance."""

import asyncio
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from test_workspace_accounts import HubFixture

from e2x_course_hub.cps.account_adoption import digest
from e2x_course_hub.cps.accounts import WorkspaceAccountAdapter
from e2x_course_hub.cps.providers import LocalCourseProvider


class AdoptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "source.sqlite"
        self.provider = LocalCourseProvider(self.path, "cps")
        self.hub = HubFixture()
        self.hub.users["operator"] = {"name": "operator", "admin": True}
        self.hub.users["legacy-owner"] = {
            "name": "legacy-owner",
            "admin": False,
            "created": "2026-10-01T00:00:00Z",
            "roles": ["user", "cps-workspace-kernel"],
            "groups": [],
            "servers": {},
        }
        self.workspace = {
            "id": "w",
            "course_id": "c",
            "term_id": "t",
            "group_id": "g",
            "hub_user": "legacy-owner",
            "hub_server": "rtc",
            "profile": "interactive-cpu",
            "state": "stopped",
            "source": "local",
            "external_id": None,
        }
        self.course = {
            "id": "c",
            "workspace_bindings": [
                {
                    "group_id": "g",
                    "hub_user": "legacy-owner",
                    "hub_server": "rtc",
                    "namespace": "jupyterhub",
                    "pod": "jupyter-legacy-owner--rtc",
                }
            ],
        }
        for kind, record in [
            ("courses", self.course),
            ("terms", {"id": "t", "course_id": "c"}),
            ("groups", {"id": "g", "course_id": "c", "term_id": "t"}),
            ("workspaces", self.workspace),
        ]:
            await self.provider.put(kind, record, actor="operator")
        self.calls = []
        self.gate = None
        self.cleanup_change = False
        self.reservation = {"active": False, "attempt": None}
        self.registration = {
            "source": "cps",
            "workspace": "w",
            "owner": "legacy-owner",
            "server": "rtc",
            "namespace": "jupyterhub",
            "pod": "jupyter-legacy-owner--rtc",
            "course_id": "c",
            "term_id": "t",
            "group_id": "g",
            "storage": {"pvc": "retained-pvc", "version": 1},
        }
        outer = self

        class Compute:
            async def workspace_registration(self, identifier):
                return copy.deepcopy(outer.registration)

            async def reservation_state(self, identifier):
                if outer.gate:
                    await outer.gate.wait()
                return copy.deepcopy(outer.reservation)

            async def release_after_shutdown(self, identifier, **kwargs):
                outer.calls.append(("cleanup", identifier, kwargs))
                if outer.cleanup_change:
                    outer.registration["storage"]["pvc"] = "changed-during-cleanup"
                return {"released": True}

        self.compute = Compute()
        self.adapter = WorkspaceAccountAdapter(
            self.provider, self.hub, group="neutral-kernels", compute=self.compute
        )

    async def asyncTearDown(self):
        self.provider.db.close()
        self.tmp.cleanup()

    async def plan(self):
        return await self.adapter.plan_adoption(
            "w",
            actor="operator",
            reason="Reviewed legacy ownership",
            provenance="review:fixture-20261013",
        )

    async def test_plan_is_read_only_and_execute_preserves_rows_and_identity(self):
        before = [
            tuple(r) for r in self.provider.db.execute("SELECT * FROM records ORDER BY kind,id")
        ]
        packet = await self.plan()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.hub.added, [])
        self.assertEqual(
            self.provider.db.execute("SELECT COUNT(*) FROM workspace_accounts").fetchone()[0], 0
        )
        result = await self.adapter.adopt_reviewed(packet, actor="operator")
        self.assertEqual(result, {"state": "ready", "provenance": "reviewed_legacy_adoption"})
        self.assertEqual(
            before,
            [tuple(r) for r in self.provider.db.execute("SELECT * FROM records ORDER BY kind,id")],
        )
        self.assertEqual(self.hub.created, [])
        self.assertEqual(self.hub.users["legacy-owner"]["created"], "2026-10-01T00:00:00Z")
        self.assertEqual(self.hub.added, ["legacy-owner"])
        receipt = json.loads(
            self.provider.db.execute("SELECT receipt FROM workspace_accounts").fetchone()[0]
        )
        self.assertEqual(receipt["kind"], "reviewed_legacy_adoption")
        self.assertEqual(receipt["review"], packet)
        self.assertEqual(receipt["identity"], packet["identity"])
        self.assertTrue(
            any(r["kind"] == "workspace-account-adoption" for r in self.provider.audit())
        )

    async def test_fresh_ensure_refuses_unreviewed_owner_then_accepts_adoption(self):
        bound = {**self.workspace, **self.course["workspace_bindings"][0]}
        with self.assertRaises(PermissionError):
            await self.adapter.ensure(bound, actor="operator")
        await self.adapter.adopt_reviewed(await self.plan(), actor="operator")
        await self.adapter.ensure(bound, actor="operator")
        self.assertEqual(self.hub.created, [])

    async def test_exact_review_and_actor_source_binding_and_identity_are_required(self):
        packet = await self.plan()
        for field, value in [
            ("actor", "other"),
            ("source", "cit"),
            ("reason", ""),
            ("identity", {"name": "legacy-owner", "created": "changed"}),
            ("binding", {**packet["binding"], "pod": "different"}),
            ("registration", {**packet["registration"], "storage": {"pvc": "other"}}),
        ]:
            changed = {**copy.deepcopy(packet), field: value}
            changed["review_id"] = digest({k: v for k, v in changed.items() if k != "review_id"})
            with self.subTest(field=field), self.assertRaises((PermissionError, ValueError)):
                await self.adapter.adopt_reviewed(changed, actor="operator")
        self.assertEqual(self.hub.added, [])

    async def test_identity_and_authority_changes_after_review_are_refused(self):
        packet = await self.plan()
        original = copy.deepcopy(self.hub.users["legacy-owner"])
        for change in [
            {"admin": True},
            {"created": "new"},
            {"roles": ["user", "admin"]},
            {"groups": ["unrelated"]},
            {"servers": {"foreign": {"stopped": True}}},
            {"servers": {"rtc": {"stopped": True, "pending": "spawn"}}},
            {"servers": {"rtc": {"stopped": False, "pending": None}}},
        ]:
            self.hub.users["legacy-owner"] = {**copy.deepcopy(original), **change}
            with self.subTest(change=change), self.assertRaises((PermissionError, ValueError)):
                await self.adapter.adopt_reviewed(packet, actor="operator")
        self.hub.users["legacy-owner"] = original
        await self.provider.put("courses", {"id": "c", "name": "changed"}, actor="operator")
        with self.assertRaises(PermissionError):
            await self.adapter.adopt_reviewed(packet, actor="operator")
        self.assertEqual(self.hub.added, [])

    async def test_admin_canonical_group_and_writer_preflight_fail_closed(self):
        for mutation in ("actor", "canonical", "role-group", "reservation", "local"):
            with self.subTest(mutation=mutation):
                if mutation == "actor":
                    self.hub.users["operator"]["admin"] = False
                if mutation == "canonical":
                    self.provider.db.execute(
                        "INSERT INTO email_links VALUES "
                        "('cps','legacy-owner','example.invalid','person',1)"
                    )
                    self.provider.db.commit()
                if mutation == "role-group":
                    self.hub.group["roles"] = ["admin"]
                if mutation == "reservation":
                    self.reservation["attempt"] = "pending"
                if mutation == "local":
                    self.provider.db.execute(
                        "UPDATE records SET "
                        "payload=json_set(payload,'$.source','moodle') WHERE kind='groups'"
                    )
                    self.provider.db.commit()
                with self.assertRaises((PermissionError, ValueError, RuntimeError)):
                    await self.plan()
                self.hub.users["operator"]["admin"] = True
                self.provider.db.execute("DELETE FROM email_links")
                self.provider.db.commit()
                self.hub.group["roles"] = ["cps-workspace-kernel"]
                self.reservation["attempt"] = None
        self.assertEqual(self.hub.added, [])

    async def test_group_failure_retries_same_review_after_restart_without_creation(self):
        packet = await self.plan()
        self.hub.grant_failure = True
        with self.assertRaises(RuntimeError):
            await self.adapter.adopt_reviewed(packet, actor="operator")
        self.provider.db.close()
        self.provider = LocalCourseProvider(self.path, "cps")
        self.adapter = WorkspaceAccountAdapter(
            self.provider, self.hub, group="neutral-kernels", compute=self.compute
        )
        self.hub.grant_failure = False
        await self.adapter.adopt_reviewed(packet, actor="operator")
        await self.adapter.adopt_reviewed(packet, actor="operator")
        self.assertEqual(self.hub.added, ["legacy-owner"])
        self.assertEqual(self.hub.created, [])

    async def test_receipt_failure_keeps_reserved_binding_and_same_review_can_recover(self):
        packet = await self.plan()
        self.provider.db.execute(
            "CREATE TRIGGER fail_stage BEFORE UPDATE ON workspace_accounts "
            "BEGIN SELECT RAISE(ABORT,'stage unavailable'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            await self.adapter.adopt_reviewed(packet, actor="operator")
        self.assertEqual(self.hub.added, [])
        self.provider.db.execute("DROP TRIGGER fail_stage")
        self.provider.db.commit()
        await self.adapter.adopt_reviewed(packet, actor="operator")
        self.assertEqual(self.hub.added, ["legacy-owner"])

    async def test_competing_adoption_cannot_grant_or_reassign_permanent_binding(self):
        packet = await self.plan()
        self.gate = asyncio.Event()
        task = asyncio.create_task(self.adapter.adopt_reviewed(packet, actor="operator"))
        second = LocalCourseProvider(self.path, "cps")
        other = WorkspaceAccountAdapter(
            second, self.hub, group="neutral-kernels", compute=self.compute
        )
        try:
            await asyncio.sleep(0)
            with self.assertRaises(PermissionError):
                await other.adopt_reviewed(packet, actor="operator")
        finally:
            self.gate.set()
            await task
            second.db.close()
        bound = {**packet["binding"], "id": "different"}
        with self.assertRaises(PermissionError):
            await self.adapter.ensure(bound, actor="operator")

    async def test_schema7_backup_and_fingerprint_retain_review_provenance(self):
        from e2x_course_hub.cps.recovery import _sqlite

        await self.adapter.adopt_reviewed(await self.plan(), actor="operator")
        backup = Path(self.tmp.name) / "backup.sqlite"
        self.provider.backup(backup)
        before = _sqlite(backup, "cps", "c")
        with sqlite3.connect(backup) as db:
            receipt = json.loads(db.execute("SELECT receipt FROM workspace_accounts").fetchone()[0])
            self.assertEqual(receipt["kind"], "reviewed_legacy_adoption")
            receipt["review"]["reason"] = "tampered"
            db.execute("UPDATE workspace_accounts SET receipt=?", (json.dumps(receipt),))
        after = _sqlite(backup, "cps", "c")
        self.assertNotEqual(
            before["tables"]["workspace_accounts"], after["tables"]["workspace_accounts"]
        )

    async def test_readiness_uses_persistent_provenance_and_current_hub_readback(self):
        self.assertEqual(await self.adapter.status(self.workspace), "review_required")
        packet = await self.plan()
        await self.adapter.adopt_reviewed(packet, actor="operator")
        self.assertEqual(await self.adapter.status(self.workspace), "ready")
        self.hub.users["legacy-owner"]["groups"] = []
        self.hub.group["users"] = []
        self.assertEqual(await self.adapter.status(self.workspace), "recovery_required")
        self.hub.users["legacy-owner"]["created"] = "recreated"
        self.assertEqual(await self.adapter.status(self.workspace), "recovery_required")

    async def test_final_receipt_failure_recovers_ambiguous_grant_by_readback(self):
        packet = await self.plan()
        self.provider.db.execute(
            "CREATE TRIGGER fail_ready BEFORE UPDATE ON workspace_accounts "
            "WHEN NEW.state='ready' BEGIN SELECT RAISE(ABORT,'ready unavailable'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            await self.adapter.adopt_reviewed(packet, actor="operator")
        self.assertEqual(self.hub.added, ["legacy-owner"])
        with self.assertRaises(PermissionError):
            await self.adapter.ensure(packet["binding"], actor="operator")
        self.provider.db.execute("DROP TRIGGER fail_ready")
        self.provider.db.commit()
        await self.adapter.adopt_reviewed(packet, actor="operator")
        self.assertEqual(self.hub.added, ["legacy-owner"])

    async def test_real_v6_fixture_upgrade_retains_records_and_refuses_implicit_adoption(self):
        fixture = Path(__file__).parent / "fixtures" / "source-v6-901a885.sql"
        legacy = Path(self.tmp.name) / "legacy.sqlite"
        with sqlite3.connect(legacy) as db:
            db.executescript(fixture.read_text())
            before = db.execute("SELECT * FROM records ORDER BY kind,id").fetchall()
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
        upgraded = LocalCourseProvider(legacy, "cps")
        try:
            self.assertEqual(
                before,
                [tuple(r) for r in upgraded.db.execute("SELECT * FROM records ORDER BY kind,id")],
            )
            self.assertEqual(
                upgraded.db.execute("SELECT COUNT(*) FROM workspace_accounts").fetchone()[0], 0
            )
            adapter = WorkspaceAccountAdapter(
                upgraded, self.hub, group="neutral-kernels", compute=self.compute
            )
            with self.assertRaises(PermissionError):
                await adapter.ensure(
                    {**self.workspace, **self.course["workspace_bindings"][0]}, actor="operator"
                )
            packet = await adapter.plan_adoption(
                "w", actor="operator", reason="v6 fixture review", provenance="901a885"
            )
            await adapter.adopt_reviewed(packet, actor="operator")
            self.assertEqual(
                before,
                [tuple(r) for r in upgraded.db.execute("SELECT * FROM records ORDER BY kind,id")],
            )
        finally:
            upgraded.db.close()

    async def test_missing_duplicate_binding_and_gateway_pvc_change_refuse_adoption(self):
        packet = await self.plan()
        self.registration["storage"]["pvc"] = "foreign-pvc"
        with self.assertRaises(PermissionError):
            await self.adapter.adopt_reviewed(packet, actor="operator")
        self.registration["storage"]["pvc"] = "retained-pvc"
        for bindings in ([], self.course["workspace_bindings"] * 2):
            await self.provider.put(
                "courses", {"id": "c", "workspace_bindings": bindings}, actor="operator"
            )
            with self.assertRaises(PermissionError):
                await self.plan()
        self.assertEqual(self.hub.added, [])

    async def test_another_source_workspace_writer_and_existing_claim_are_refused(self):
        packet = await self.plan()
        await self.provider.put(
            "workspaces", {**self.workspace, "id": "other", "state": "starting"}, actor="operator"
        )
        with self.assertRaises(PermissionError):
            await self.adapter.adopt_reviewed(packet, actor="operator")
        await self.provider.delete("workspaces", "other", actor="operator")
        with self.provider.db:
            self.provider.db.execute(
                "INSERT INTO workspace_accounts VALUES (?,?,?,?,?,?)",
                (
                    "cps",
                    "legacy-owner",
                    "other",
                    json.dumps({**packet["binding"], "id": "other"}),
                    "intent",
                    None,
                ),
            )
        with self.assertRaises(PermissionError):
            await self.adapter.adopt_reviewed(packet, actor="operator")
        self.assertEqual(self.hub.added, [])

    async def test_canonical_collision_in_an_unattached_reviewed_alias_is_denied(self):
        with self.provider.db:
            self.provider.db.execute(
                "INSERT INTO reviewed_account_aliases VALUES (?,?,?,?,?,?,?)",
                (
                    "cps",
                    "human-login",
                    "legacy-owner",
                    None,
                    "operator",
                    "review",
                    "2026-10-13",
                ),
            )
        with self.assertRaises(PermissionError):
            await self.plan()

    async def test_production_open_requires_explicit_v6_upgrade_and_initialization(self):
        from e2x_course_hub.course_service.app import CourseServiceApp

        self.assertFalse(
            CourseServiceApp.class_traits()["allow_console_schema_upgrade"].default_value
        )
        missing = Path(self.tmp.name) / "missing.sqlite"
        with self.assertRaises(ValueError):
            LocalCourseProvider(missing, "cps", allow_schema_upgrade=False)
        self.assertFalse(missing.exists())
        legacy = Path(self.tmp.name) / "old.sqlite"
        fixture = Path(__file__).parent / "fixtures" / "source-v6-901a885.sql"
        with sqlite3.connect(legacy) as db:
            db.executescript(fixture.read_text())
        original = legacy.read_bytes()
        with self.assertRaises(ValueError):
            LocalCourseProvider(legacy, "cps", allow_schema_upgrade=False)
        self.assertEqual(legacy.read_bytes(), original)
        explicit = LocalCourseProvider(legacy, "cps", allow_schema_upgrade=True)
        explicit.db.close()
        reopened = LocalCourseProvider(legacy, "cps", allow_schema_upgrade=False)
        self.assertEqual(reopened.db.execute("PRAGMA user_version").fetchone()[0], 7)
        reopened.db.close()
        with sqlite3.connect(legacy) as db:
            db.execute("ALTER TABLE email_links DROP COLUMN verified")
        original = legacy.read_bytes()
        with self.assertRaises(ValueError):
            LocalCourseProvider(legacy, "cps", allow_schema_upgrade=False)
        self.assertEqual(legacy.read_bytes(), original)

    async def test_empty_stopped_default_spawner_is_not_a_foreign_writer(self):
        self.hub.users["legacy-owner"]["servers"] = {
            "": {"stopped": True, "pending": None, "ready": False, "state": {}}
        }
        packet = await self.plan()
        await self.adapter.adopt_reviewed(packet, actor="operator")
        self.assertEqual(self.hub.created, [])

    async def test_completed_review_retry_does_not_restore_revoked_membership(self):
        packet = await self.plan()
        await self.adapter.adopt_reviewed(packet, actor="operator")
        self.hub.users["legacy-owner"]["groups"] = []
        self.hub.group["users"] = []
        with self.assertRaises(PermissionError):
            await self.adapter.adopt_reviewed(packet, actor="operator")
        self.assertEqual(self.hub.added, ["legacy-owner"])

    async def test_storage_changed_during_cleanup_is_refused_before_reservation_or_grant(self):
        packet = await self.plan()
        self.cleanup_change = True
        with self.assertRaises(PermissionError):
            await self.adapter.adopt_reviewed(packet, actor="operator")
        self.assertEqual(self.hub.added, [])
        self.assertEqual(
            self.provider.db.execute("SELECT COUNT(*) FROM workspace_accounts").fetchone()[0], 0
        )
