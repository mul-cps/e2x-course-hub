"""Fresh neutral Hub accounts, with permanent Source ownership receipts.

A POST with an unknown outcome is deliberately not retried by adopting the user.
No failure path removes a Hub identity or its files.
"""

import json
import re
import sqlite3

from ..api.errors import GroupNotFoundError, UserNotFoundError


class WorkspaceAccountAdapter:
    def __init__(self, provider, hub_api, *, group, role="cps-workspace-kernel", compute=None):
        self.provider, self.hub = provider, hub_api
        self.group, self.role = group, role
        self.compute = compute

    async def plan_adoption(self, identifier, *, actor, reason, provenance):
        from .account_adoption import ReviewedAdoption

        return await ReviewedAdoption(self).plan(
            identifier, actor=actor, reason=reason, provenance=provenance
        )

    async def adopt_reviewed(self, review, *, actor):
        from .account_adoption import ReviewedAdoption

        return await ReviewedAdoption(self).execute(review, actor=actor)

    async def status(self, workspace):
        """Read-only account readiness; no provisioning or GPU availability claim."""
        try:
            bound = dict(workspace)
            course = next(
                (c for c in await self.provider.courses() if c["id"] == workspace.get("course_id")),
                {},
            )
            candidates = [
                b
                for b in course.get("workspace_bindings", [])
                if all(b.get(k) == workspace.get(k) for k in ("group_id", "hub_user", "hub_server"))
            ]
            if len(candidates) != 1:
                return "unavailable"
            for key in ("namespace", "pod"):
                if key in workspace and workspace[key] != candidates[0].get(key):
                    return "recovery_required"
                bound[key] = candidates[0].get(key)
            binding = self._binding(bound)
            row = self._row(binding)
            if row is None:
                try:
                    await self.hub.get_user(binding["hub_user"])
                except UserNotFoundError:
                    return "not_provisioned"
                return "review_required"
            if row["state"] != "ready":
                return "recovery_required"
            identity = self._receipt_identity(row)
            user = await self.hub.get_user(binding["hub_user"])
            self._model(user, binding, receipt=identity, require_stopped=False)
            group = await self.hub.get_group(self.group)
            if (
                self.group not in user["groups"]
                or group.get("name") != self.group
                or group.get("roles") != [self.role]
                or binding["hub_user"] not in group.get("users", [])
            ):
                return "recovery_required"
            return "ready"
        except (PermissionError, ValueError, UserNotFoundError, GroupNotFoundError):
            return "recovery_required"
        except Exception:
            return "unavailable"

    def _binding(self, workspace):
        fields = (
            "id",
            "course_id",
            "term_id",
            "group_id",
            "hub_user",
            "hub_server",
            "namespace",
            "pod",
        )
        result = {key: workspace.get(key) for key in fields}
        if any(
            not isinstance(result[key], str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", result[key])
            for key in fields
        ):
            raise ValueError(
                "Explicit safe Source workspace and Hub/Pod binding required "
                "for fresh account creation"
            )
        return {
            **result,
            "hub_api_url": self.hub.api_url.rstrip("/"),
            "kernel_group": self.group,
            "kernel_role": self.role,
        }

    def _row(self, binding):
        rows = self.provider.db.execute(
            "SELECT * FROM workspace_accounts WHERE console=? AND (hub_user=? OR workspace_id=?)",
            (self.provider.console, binding["hub_user"], binding["id"]),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1 or json.loads(rows[0]["binding"]) != binding:
            raise PermissionError(
                "Neutral account is permanently owned by a different workspace binding"
            )
        return dict(rows[0])

    def _model(self, user, binding, *, receipt=None, require_stopped=True):
        if (
            not isinstance(user, dict)
            or user.get("name") != binding["hub_user"]
            or user.get("admin") is not False
            or not isinstance(user.get("created"), str)
            or not user["created"]
            or not isinstance(user.get("roles"), list)
            or any(not isinstance(role, str) for role in user["roles"])
            or set(user["roles"]) - {"user", self.role}
            or not isinstance(user.get("groups"), list)
            or any(not isinstance(group, str) for group in user["groups"])
            or set(user["groups"]) - {self.group}
            or not isinstance(user.get("servers"), dict)
        ):
            raise PermissionError(
                "Hub neutral identity model is unavailable, privileged, or not "
                "dedicated to this workspace"
            )
        fingerprint = {"name": user["name"], "created": user["created"]}
        if receipt is not None and fingerprint != receipt:
            raise PermissionError(
                "Hub identity was recreated or differs from the persisted ownership receipt"
            )
        for name, server in user["servers"].items():
            # Hub can include its empty unnamed ORM spawner in stopped models.
            # It has no retained server/Pod identity and is not a writer.
            if (
                name == ""
                and isinstance(server, dict)
                and server.get("stopped") is True
                and server.get("pending") is None
                and server.get("ready", False) is False
                and server.get("state", {}) == {}
            ):
                continue
            if name != binding["hub_server"] or not isinstance(server, dict):
                raise PermissionError("Neutral identity has an unrelated Hub server")
            state = server.get("state", {})
            if (
                not isinstance(state, dict)
                or state.get("pod_name", binding["pod"]) != binding["pod"]
                or state.get("namespace", binding["namespace"]) != binding["namespace"]
            ):
                raise PermissionError("Hub server differs from the owned Source workspace binding")
            if require_stopped and (
                server.get("stopped") is not True or server.get("pending") is not None
            ):
                raise ValueError("Stop the owned workspace before account role reconciliation")
        return fingerprint

    async def ensure(self, workspace, *, actor):
        binding = self._binding(workspace)
        previous = None
        try:
            previous = self._row(binding)
            await self._ensure(binding, previous)
        except Exception as error:
            self.provider.denial(
                actor,
                "workspace_accounts",
                binding["hub_user"],
                binding,
                "denied" if isinstance(error, PermissionError) else "failure",
            )
            raise
        with self.provider.db:
            self.provider._audit(
                actor,
                "workspace_accounts",
                binding["hub_user"],
                json.dumps(previous, sort_keys=True) if previous else None,
                json.dumps(self._row(binding), sort_keys=True),
            )

    async def _preflight(self, binding):
        if (
            not isinstance(self.group, str)
            or not re.fullmatch(r"[a-zA-Z0-9_.-]+", self.group)
            or not isinstance(self.role, str)
            or not re.fullmatch(r"[a-zA-Z0-9_.-]+", self.role)
            or self.role in {"admin", "user", "server"}
        ):
            raise RuntimeError(
                "Configure the trusted neutral workspace group and minimum "
                "kernel role before provisioning"
            )
        if binding["group_id"] == self.group or any(
            group["id"] == self.group for group in await self.provider.list("groups")
        ):
            raise PermissionError(
                "The operator kernel role group cannot be a course membership group"
            )
        for table in ("email_links", "reviewed_account_aliases"):
            if self.provider.db.execute(
                "SELECT 1 FROM "
                + table
                + " WHERE console=? AND (username=? OR canonical_person_id=?)",
                (self.provider.console, binding["hub_user"], binding["hub_user"]),
            ).fetchone():
                raise PermissionError("Neutral owner cannot be an existing person identity")
        if any(
            binding["hub_user"] in (member.get("person_id"), member.get("canonical_person_id"))
            for member in await self.provider.list("memberships")
        ):
            raise PermissionError("Neutral owner cannot be an existing person identity")
        try:
            group = await self.hub.get_group(self.group)
        except GroupNotFoundError as error:
            raise RuntimeError(
                "Configured neutral kernel group is missing; an operator must provision its role"
            ) from error
        if (
            not isinstance(group, dict)
            or group.get("name") != self.group
            or group.get("roles") != [self.role]
        ):
            raise RuntimeError(
                "Configured neutral group must expose exactly the minimum kernel role"
            )

    def _receipt_identity(self, row):
        receipt = json.loads(row["receipt"]) if row["receipt"] else None
        # Existing v7 rows are actual successful-POST receipts, never inferred
        # from v6 owners. Preserve their provenance when upgrading this code.
        if isinstance(receipt, dict) and set(receipt) == {"name", "created"}:
            return receipt
        if not isinstance(receipt, dict) or receipt.get("version") != 1:
            raise PermissionError("Validated persistent ownership provenance required")
        if receipt.get("kind") == "source_creation" and set(receipt) == {
            "version",
            "kind",
            "identity",
        }:
            return receipt["identity"]
        if receipt.get("kind") == "reviewed_legacy_adoption":
            if row["state"] != "ready":
                raise PermissionError(
                    "Reviewed adoption must complete before automatic provisioning"
                )
            from .account_adoption import validate_receipt

            return validate_receipt(receipt, json.loads(row["binding"]), self.provider.console)
        raise PermissionError("Validated persistent ownership provenance required")

    async def _ensure(self, binding, row):
        await self._preflight(binding)
        if row is None:
            try:
                await self.hub.get_user(binding["hub_user"])
            except UserNotFoundError:
                pass
            else:
                raise PermissionError(
                    "Hub account already exists without a successful Source "
                    "creation receipt; choose a fresh owner"
                )
            try:
                with self.provider.db:
                    self.provider.db.execute(
                        "INSERT INTO workspace_accounts VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            self.provider.console,
                            binding["hub_user"],
                            binding["id"],
                            json.dumps(binding, sort_keys=True),
                            "intent",
                            None,
                        ),
                    )
            except sqlite3.IntegrityError as error:
                raise PermissionError(
                    "Concurrent Source account ownership claim; retry only a "
                    "completed creation receipt"
                ) from error
            created = await self.hub.create_user(binding["hub_user"])
            receipt = {
                "version": 1,
                "kind": "source_creation",
                "identity": self._model(created, binding),
            }
            # Persist evidence of a successful POST before any role grant. A
            # crash before this commit leaves only intent and requires review.
            with self.provider.db:
                self.provider.db.execute(
                    "UPDATE workspace_accounts SET state=?, receipt=? WHERE "
                    "console=? AND hub_user=?",
                    (
                        "created",
                        json.dumps(receipt, sort_keys=True),
                        self.provider.console,
                        binding["hub_user"],
                    ),
                )
            row = self._row(binding)
        if row["state"] not in ("created", "ready") or not row["receipt"]:
            raise PermissionError(
                "Fresh account creation outcome is ambiguous; preserve the "
                "account and request reviewed recovery"
            )
        try:
            user = await self.hub.get_user(binding["hub_user"])
        except UserNotFoundError as error:
            raise PermissionError(
                "Previously created neutral account is missing; automatic recreation is forbidden"
            ) from error
        identity = self._receipt_identity(row)
        self._model(user, binding, receipt=identity)
        if self.group not in user["groups"]:
            await self.hub.add_users_to_group(self.group, [binding["hub_user"]])
            user = await self.hub.get_user(binding["hub_user"])
            self._model(user, binding, receipt=identity)
        verified_group = await self.hub.get_group(self.group)
        if (
            self.group not in user["groups"]
            or verified_group.get("name") != self.group
            or verified_group.get("roles") != [self.role]
            or binding["hub_user"] not in verified_group.get("users", [])
        ):
            raise RuntimeError("Hub did not grant the configured minimum kernel role group")
        with self.provider.db:
            self.provider.db.execute(
                "UPDATE workspace_accounts SET state=? WHERE console=? AND hub_user=?",
                ("ready", self.provider.console, binding["hub_user"]),
            )
