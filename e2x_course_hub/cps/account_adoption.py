"""Explicit, authenticated legacy adoption; no user creation or inferred receipt."""

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return "sha256:" + hashlib.sha256(encode(value).encode()).hexdigest()


REVIEW_FIELDS = {
    "version",
    "source",
    "actor",
    "reason",
    "provenance",
    "binding",
    "records",
    "identity",
    "registration",
    "writers",
    "authority",
    "review_id",
}


def validate_review(review, *, source, actor):
    if (
        not isinstance(review, dict)
        or set(review) != REVIEW_FIELDS
        or type(review["version"]) is not int
        or review["version"] != 1
        or review["source"] != source
        or review["actor"] != actor
        or any(
            not isinstance(review[key], str) or not 1 <= len(review[key].strip()) <= 2048
            for key in ("actor", "reason", "provenance")
        )
        or review["review_id"] != digest({k: v for k, v in review.items() if k != "review_id"})
    ):
        raise PermissionError("Exact owning admin review packet and provenance required")


def validate_receipt(receipt, binding, source):
    if (
        set(receipt) != {"version", "kind", "identity", "review", "reserved_at"}
        or receipt["version"] != 1
        or receipt["kind"] != "reviewed_legacy_adoption"
        or not isinstance(receipt["reserved_at"], str)
        or not receipt["reserved_at"]
    ):
        raise PermissionError("Validated persistent reviewed adoption provenance required")
    review = receipt["review"]
    validate_review(
        review, source=source, actor=review.get("actor") if isinstance(review, dict) else None
    )
    if review["binding"] != binding or review["identity"] != receipt["identity"]:
        raise PermissionError("Reviewed ownership receipt differs from the permanent binding")
    return receipt["identity"]


class ReviewedAdoption:
    def __init__(self, accounts):
        self.accounts = accounts
        self.provider, self.hub, self.compute = accounts.provider, accounts.hub, accounts.compute

    @contextmanager
    def _exclusive(self):
        # An OS lock fences simultaneous instances across awaited Hub calls and
        # releases on process exit. It never expires while a writer is alive.
        path = self.provider.db.execute("PRAGMA database_list").fetchone()[2]
        if not path:
            raise RuntimeError("Persistent Source database required for reviewed adoption")
        fd = os.open(
            path + ".workspace-adoption.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise PermissionError("Concurrent reviewed adoption is in progress") from error
            yield
        finally:
            os.close(fd)

    async def _actor(self, actor):
        if not isinstance(actor, str) or not actor:
            raise PermissionError("Authenticated owning administrator required")
        current = await self.hub.get_user(actor)
        if current.get("name") != actor or current.get("admin") is not True:
            raise PermissionError("Authenticated owning administrator required")
        if self.compute is None:
            raise RuntimeError("Owning gateway required to verify shutdown and cleanup")

    async def _context(self, identifier):
        workspace = next(
            (w for w in await self.provider.list("workspaces") if w["id"] == identifier), None
        )
        if not workspace:
            raise ValueError("Existing owning Source workspace required")
        course = next(
            (r for r in await self.provider.courses() if r["id"] == workspace["course_id"]), None
        )
        term = next(
            (
                r
                for r in await self.provider.terms(workspace["course_id"])
                if r["id"] == workspace.get("term_id")
            ),
            None,
        )
        group = next(
            (
                r
                for r in await self.provider.groups(workspace["course_id"])
                if r["id"] == workspace.get("group_id")
            ),
            None,
        )
        records = {"workspace": workspace, "course": course, "term": term, "group": group}
        if any(
            not r or r.get("source", "local") != "local" or r.get("external_id") is not None
            for r in records.values()
        ):
            raise PermissionError(
                "Owned local workspace, course, term and group required for reviewed adoption"
            )
        if (
            workspace.get("state") != "stopped"
            or workspace.get("archive_pending")
            or workspace.get("archived")
            or course.get("reconciliation_pending")
            or course.get("membership_removal_pending")
        ):
            raise ValueError("All Source writers and reconciliation operations must be stopped")
        candidates = [
            b
            for b in course.get("workspace_bindings", [])
            if all(b.get(k) == workspace.get(k) for k in ("group_id", "hub_user", "hub_server"))
        ]
        if len(candidates) != 1:
            raise PermissionError("Exact unambiguous owning course workspace binding required")
        configured = candidates[0]
        if any(k in workspace and workspace[k] != configured.get(k) for k in ("namespace", "pod")):
            raise PermissionError("Source namespace or Pod differs from the owning course binding")
        bound = {**workspace, **{k: configured.get(k) for k in ("namespace", "pod")}}
        binding = self.accounts._binding(bound)
        if group.get("term_id") not in (None, workspace["term_id"]):
            raise PermissionError("Owned group belongs to a different term")
        return records, binding

    async def _central(self, binding, *, cleanup, actor):
        registration = await self.compute.workspace_registration(binding["id"])
        expected = {
            "principal": "workspace:" + self.provider.console + ":" + binding["id"],
            "workspace": binding["id"],
            "owner": binding["hub_user"],
            "server": binding["hub_server"],
            "namespace": binding["namespace"],
            "pod": binding["pod"],
        }
        if (
            not isinstance(registration, dict)
            or any(registration.get(k) != v for k, v in expected.items())
            or ("source" in registration and registration["source"] != self.provider.console)
            or any(
                k in registration and registration[k] != binding[k]
                for k in ("course_id", "term_id", "group_id")
            )
        ):
            raise PermissionError(
                "Gateway registration differs from the exact owning Source binding"
            )
        reservation = await self.compute.reservation_state(binding["id"])
        if (
            not isinstance(reservation, dict)
            or reservation.get("active") is not False
            or reservation.get("attempt") is not None
        ):
            raise ValueError(
                "Confirmed inactive gateway reservation with no pending attempt required"
            )
        if cleanup:
            await self.compute.release_after_shutdown(
                binding["id"], confirmed_by_hub=True, actor=actor, attempt=None
            )
            reservation = await self.compute.reservation_state(binding["id"])
            if (
                not isinstance(reservation, dict)
                or reservation.get("active") is not False
                or reservation.get("attempt") is not None
            ):
                raise ValueError("Gateway shutdown and native cleanup remain unconfirmed")
            if await self.compute.workspace_registration(binding["id"]) != registration:
                raise PermissionError("Gateway registration or storage changed during cleanup")
        return registration

    async def _observe(self, identifier, *, actor, reason, provenance, cleanup=False):
        await self._actor(actor)
        records, binding = await self._context(identifier)
        await self.accounts._preflight(binding)
        authority = digest(self.provider.lifecycle_snapshot(binding["course_id"]))
        row = self.accounts._row(binding)
        if row is not None and row["state"] not in (
            "adoption_reserved",
            "adoption_granting",
            "ready",
        ):
            raise PermissionError("An ambiguous Source creation claim cannot be adopted")
        if row is not None and (
            not row["receipt"]
            or json.loads(row["receipt"]).get("kind") != "reviewed_legacy_adoption"
        ):
            raise PermissionError(
                "Source creation provenance cannot be replaced by legacy adoption"
            )
        writers = []
        for writer in await self.provider.list("workspaces"):
            if writer["id"] != identifier and writer.get("hub_user") == binding["hub_user"]:
                raise PermissionError("Hub identity is referenced by another Source workspace")
            if writer.get("group_id") != binding["group_id"]:
                continue
            writer_records, writer_binding = await self._context(writer["id"])
            user = await self.hub.get_user(writer_binding["hub_user"])
            identity = self.accounts._model(user, writer_binding)
            if (
                writer["id"] == identifier
                and row is None
                and self.accounts.role not in user["roles"]
            ):
                raise PermissionError("Existing direct neutral marker required for legacy adoption")
            registration = await self._central(writer_binding, cleanup=cleanup, actor=actor)
            writers.append(
                {
                    "records": writer_records,
                    "binding": writer_binding,
                    "identity": identity,
                    "registration": registration,
                }
            )
        # Capture again after all awaits; never bless changed Source authority.
        if digest(self.provider.lifecycle_snapshot(binding["course_id"])) != authority:
            raise PermissionError("Source authority changed during reviewed adoption preflight")
        target = next(w for w in writers if w["binding"]["id"] == identifier)
        packet = {
            "version": 1,
            "source": self.provider.console,
            "actor": actor,
            "reason": reason,
            "provenance": provenance,
            "binding": binding,
            "records": records,
            "identity": target["identity"],
            "registration": target["registration"],
            "writers": sorted(writers, key=lambda w: w["binding"]["id"]),
            "authority": authority,
        }
        packet["review_id"] = digest(packet)
        validate_review(packet, source=self.provider.console, actor=actor)
        return packet

    async def plan(self, identifier, *, actor, reason, provenance):
        return await self._observe(identifier, actor=actor, reason=reason, provenance=provenance)

    def _cas(self, binding, previous, state, receipt):
        with self.provider.db:
            self.provider.db.execute("BEGIN IMMEDIATE")
            if self.accounts._row(binding) != previous:
                raise PermissionError("Ownership stage changed concurrently")
            if (
                digest(self.provider.lifecycle_snapshot(binding["course_id"]))
                != receipt["review"]["authority"]
            ):
                raise PermissionError("Source authority changed before ownership stage commit")
            if previous is None:
                self.provider.db.execute(
                    "INSERT INTO workspace_accounts VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        self.provider.console,
                        binding["hub_user"],
                        binding["id"],
                        encode(binding),
                        state,
                        encode(receipt),
                    ),
                )
            else:
                changed = self.provider.db.execute(
                    "UPDATE workspace_accounts SET state=?,receipt=? WHERE "
                    "console=? AND hub_user=? AND binding=? AND state=? AND receipt IS ?",
                    (
                        state,
                        encode(receipt),
                        self.provider.console,
                        binding["hub_user"],
                        previous["binding"],
                        previous["state"],
                        previous["receipt"],
                    ),
                ).rowcount
                if changed != 1:
                    raise PermissionError("Ownership compare-and-swap failed")
            self.provider._audit(
                receipt["review"]["actor"],
                "workspace-account-adoption",
                binding["hub_user"],
                encode(previous) if previous else None,
                encode(self.accounts._row(binding)),
            )
        return self.accounts._row(binding)

    async def execute(self, review, *, actor):
        validate_review(review, source=self.provider.console, actor=actor)
        binding = review["binding"]
        try:
            with self._exclusive():
                observed = await self._observe(
                    binding["id"],
                    actor=actor,
                    reason=review["reason"],
                    provenance=review["provenance"],
                    cleanup=True,
                )
                if observed != review:
                    raise PermissionError(
                        "Source, Hub identity, storage or writers changed since review"
                    )
                row = self.accounts._row(binding)
                if row is None:
                    receipt = {
                        "version": 1,
                        "kind": "reviewed_legacy_adoption",
                        "identity": review["identity"],
                        "review": review,
                        "reserved_at": datetime.now(timezone.utc).isoformat(),
                    }
                    row = self._cas(binding, None, "adoption_reserved", receipt)
                else:
                    receipt = json.loads(row["receipt"])
                    validate_receipt(receipt, binding, self.provider.console)
                    if receipt["review"] != review:
                        raise PermissionError("Retry must use the exact persisted adoption review")
                if row["state"] != "ready":
                    row = self._cas(binding, row, "adoption_granting", receipt)
                # No mutation before permanent binding, review and recoverable
                # group stage are durable. Ambiguous grants retry by readback.
                await self._actor(actor)
                user = await self.hub.get_user(binding["hub_user"])
                self.accounts._model(user, binding, receipt=receipt["identity"])
                if self.accounts.group not in user["groups"]:
                    if row["state"] == "ready":
                        raise PermissionError(
                            "Completed adoption membership was revoked; retry cannot restore it"
                        )
                    await self.hub.add_users_to_group(self.accounts.group, [binding["hub_user"]])
                user = await self.hub.get_user(binding["hub_user"])
                self.accounts._model(user, binding, receipt=receipt["identity"])
                group = await self.hub.get_group(self.accounts.group)
                if (
                    self.accounts.group not in user["groups"]
                    or group.get("roles") != [self.accounts.role]
                    or group.get("name") != self.accounts.group
                    or binding["hub_user"] not in group.get("users", [])
                ):
                    raise RuntimeError("Reviewed group mutation outcome remains unconfirmed")
                if row["state"] != "ready":
                    self._cas(binding, row, "ready", receipt)
                return {"state": "ready", "provenance": "reviewed_legacy_adoption"}
        except Exception as error:
            self.provider.denial(
                actor,
                "workspace-account-adoption",
                binding.get("hub_user", ""),
                {"review_id": review["review_id"]},
                "denied" if isinstance(error, PermissionError) else "failure",
            )
            raise
