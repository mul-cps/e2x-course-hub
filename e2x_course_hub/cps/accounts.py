"""Fresh neutral Hub accounts, with permanent Source ownership receipts.

New accounts use the permanent Hub receipt endpoint. Exact retries never infer
ownership from a user GET or restore revoked permissions. Legacy receipts and
identifiers retain their provenance. No failure path removes identities/files.
"""

import hashlib
import json
import re
import sqlite3
from datetime import datetime

from ..api.errors import GroupNotFoundError, UserNotFoundError


def _creation_descriptor(source, binding):
    """Wire contract qualified against cps_compute_hub.neutral_accounts."""
    body = {key: binding[field] for key, field in (
        ("workspace", "id"), ("course", "course_id"), ("term", "term_id"),
        ("group", "group_id"), ("server", "hub_server"),
        ("namespace", "namespace"), ("pod", "pod"))}
    if source not in {"cps", "cit"}:
        raise PermissionError("Configured owning Source required for Hub account creation")
    for key, value in body.items():
        pattern = r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}"
        label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        if key in {"namespace", "server"}:
            pattern = label
        elif key == "pod":
            pattern = label + r"(?:\." + label + r")*"
        if (not isinstance(value, str) or re.fullmatch(pattern, value) is None
                or (key == "pod" and len(value) > 253)):
            raise ValueError("Invalid permanent Hub workspace descriptor")
    serialized = json.dumps(dict(source=source, **body), sort_keys=True,
                            separators=(",", ":"), ensure_ascii=True)
    owner = source + "-ws-" + hashlib.sha256((source + "\0" + body["workspace"]).encode()).hexdigest()[:32]
    return body, owner, hashlib.sha256(serialized.encode()).hexdigest()


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
            await self._validated_model(user, binding, receipt=identity, require_stopped=False)
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

    async def _validated_model(self, user, binding, *, receipt=None, require_stopped=True):
        """Recheck adopted legacy custody, including removal of an enrolled role."""
        _, registered_owner, _ = _creation_descriptor(self.provider.console, binding)
        runtime_role = "cps-workspace-runtime-" + binding["hub_user"]
        custody = None
        if binding["hub_user"] != registered_owner:
            row = self._row(binding)
            persisted = json.loads(row["receipt"]) if row and row["receipt"] else None
            reviewed = (row is not None and row["state"] == "ready" and isinstance(persisted, dict)
                        and persisted.get("kind") == "reviewed_legacy_adoption")
            if reviewed:
                from .legacy_runtime import LegacyEnrollmentMissing, fetch_custody, validate_custody

                identity = self._receipt_identity(row)
                if not isinstance(user, dict) or identity != {key: user.get(key) for key in ("name", "created")}:
                    raise PermissionError("Hub legacy identity differs from reviewed Source ownership")
                try:
                    custody = await fetch_custody(self.hub, binding)
                except LegacyEnrollmentMissing:
                    # The missing endpoint/row permits only the original model,
                    # which still rejects every unproved legacy runtime role.
                    pass
                else:
                    validate_custody(custody, source=self.provider.console, binding=binding,
                                     user=user, receipt=persisted)
                if self._row(binding) != row:
                    raise PermissionError("Reviewed legacy Source ownership changed during Hub readback")
            elif (isinstance(user, dict) and isinstance(user.get("roles"), list)
                    and runtime_role in user["roles"]):
                raise PermissionError("Completed persistent reviewed legacy adoption required")
        return self._model(user, binding, receipt=receipt, require_stopped=require_stopped,
                           legacy_runtime_custody=custody)

    def _model(self, user, binding, *, receipt=None, require_stopped=True, legacy_runtime_custody=None):
        allowed_roles = {"user", self.role}
        # The permanent Hub provisioning POST validates the actual runtime role
        # scopes, RoleID, Source-service custody and immutable identity before
        # this model check. Accept its computed name only for the exact bound
        # registered owner; role names do not replace that Hub-side authority.
        _, registered_owner, _ = _creation_descriptor(self.provider.console, binding)
        if binding["hub_user"] == registered_owner:
            allowed_roles.add("cps-workspace-runtime-" + registered_owner)
        elif legacy_runtime_custody is not None:
            from .legacy_runtime import validate_custody

            row = self._row(binding)
            if row is None or row["state"] != "ready":
                raise PermissionError("Completed persistent reviewed legacy adoption required")
            allowed_roles.add(validate_custody(legacy_runtime_custody, source=self.provider.console,
                binding=binding, user=user, receipt=json.loads(row["receipt"] or "null")))
        if (
            not isinstance(user, dict)
            or user.get("name") != binding["hub_user"]
            or user.get("admin") is not False
            or not isinstance(user.get("created"), str)
            or not user["created"]
            or not isinstance(user.get("roles"), list)
            or any(not isinstance(role, str) for role in user["roles"])
            or set(user["roles"]) - allowed_roles
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
        if receipt.get("kind") == "hub_neutral_creation":
            identity = self._creation_receipt(receipt, json.loads(row["binding"]))
            return {key: identity[key] for key in ("name", "created")}
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

    def _creation_receipt(self, receipt, binding):
        _, owner, digest = _creation_descriptor(self.provider.console, binding)
        identity = receipt.get("identity") if isinstance(receipt, dict) else None
        expected = dict(version=1, kind="hub_neutral_creation", source=self.provider.console,
                        workspace=binding["id"], owner=owner, descriptor_hash=digest,
                        group="cps-workspace-kernel-accounts", role="cps-workspace-kernel")
        if (binding["hub_user"] != owner or self.group != expected["group"]
                or self.role != expected["role"]
                or not isinstance(receipt, dict) or set(receipt) != set(expected) | {"identity"}
                or type(receipt.get("version")) is not int
                or any(receipt.get(key) != value for key, value in expected.items())
                or not isinstance(identity, dict) or set(identity) != {"id", "name", "created"}
                or type(identity.get("id")) is not int or identity["id"] <= 0
                or identity.get("name") != owner
                or not isinstance(identity.get("created"), str)
                or not identity["created"].endswith("Z")):
            raise PermissionError("Exact typed permanent Hub creation receipt required")
        try:
            timestamp = datetime.fromisoformat(identity["created"][:-1] + "+00:00")
            if "T" not in identity["created"] or timestamp.tzinfo is None:
                raise ValueError("Hub creation timestamp must include UTC time")
        except ValueError as error:
            raise PermissionError("Actual Hub creation timestamp required") from error
        return identity

    async def _ensure_hub_creation(self, binding, row):
        body, owner, _ = _creation_descriptor(self.provider.console, binding)
        if (binding["hub_user"] != owner or self.group != "cps-workspace-kernel-accounts"
                or self.role != "cps-workspace-kernel"):
            raise PermissionError("Computed neutral owner and fixed Hub kernel group required")
        if row is None:
            try:
                with self.provider.db:
                    self.provider.db.execute("INSERT INTO workspace_accounts VALUES (?, ?, ?, ?, ?, ?)",
                        (self.provider.console, owner, binding["id"],
                         json.dumps(binding, sort_keys=True), "hub_intent", None))
            except sqlite3.IntegrityError as error:
                raise PermissionError("Concurrent permanent Source account ownership claim") from error
            row = self._row(binding)
        if row["state"] not in ("hub_intent", "created", "ready"):
            raise PermissionError("Permanent Hub creation outcome requires reconciliation")
        previous = json.loads(row["receipt"]) if row["receipt"] else None
        if previous is not None:
            self._creation_receipt(previous, binding)
        # This POST is the authority for identity ID, custody and revocation,
        # including lost responses and Source receipt-persistence failures.
        receipt = await self.hub.create_workspace_account(body)
        identity = self._creation_receipt(receipt, binding)
        if previous is not None and receipt != previous:
            raise PermissionError("Permanent Hub creation receipt cannot be replaced")
        with self.provider.db:
            self.provider.db.execute("UPDATE workspace_accounts SET state=?, receipt=? WHERE "
                "console=? AND hub_user=? AND binding=? AND state=? AND receipt IS ?",
                ("created", json.dumps(receipt, sort_keys=True), self.provider.console, owner,
                 row["binding"], row["state"], row["receipt"]))
        current = self._row(binding)
        if json.loads(current["receipt"] or "null") != receipt:
            raise PermissionError("Concurrent Source creation receipt differs")
        user = await self.hub.get_user(owner)
        self._model(user, binding, receipt={key: identity[key] for key in ("name", "created")})
        group = await self.hub.get_group(self.group)
        if (self.group not in user["groups"] or group.get("name") != self.group
                or group.get("roles") != [self.role] or owner not in group.get("users", [])):
            raise PermissionError("Permanent Hub kernel membership was revoked or is unconfirmed")
        with self.provider.db:
            self.provider.db.execute("UPDATE workspace_accounts SET state=? WHERE console=? AND hub_user=?",
                                     ("ready", self.provider.console, owner))

    async def _ensure(self, binding, row):
        await self._preflight(binding)
        receipt = json.loads(row["receipt"]) if row and row["receipt"] else None
        if (row is None or row["state"] == "hub_intent"
                or isinstance(receipt, dict) and receipt.get("kind") == "hub_neutral_creation"):
            return await self._ensure_hub_creation(binding, row)
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
        await self._validated_model(user, binding, receipt=identity)
        if self.group not in user["groups"]:
            await self.hub.add_users_to_group(self.group, [binding["hub_user"]])
            user = await self.hub.get_user(binding["hub_user"])
            await self._validated_model(user, binding, receipt=identity)
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
