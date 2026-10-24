# Managed identity linkage

Identity linkage uses explicit verified email-to-canonical-UUID mapping rows. It never guesses identity from usernames or generates a replacement person ID. Existing usernames, membership identifiers and storage paths remain unchanged. Requiring verification for linkage does not make email verification mandatory for all account logins.

Managed mode accepts a closed, console-owned batch of 1–1000 mappings. The existing mapping validator still rejects unverified data, duplicate accounts, UUID collisions and canonical person reassignment. Existing email/UUID handovers retain their explicit administrator review requirement. A membership identity change still requires its affected workspaces to be stopped. The console endpoint requires current administrator authorization.

A private SQLite snapshot validates the proposed mutation before any remote fence. Normalized explicit mapping rows bind one authenticated `admin.identity.link` transition. The real Source write transaction then checks its retained authority identity/path and the full records/account-links preimage. Accounts, affected memberships, per-mutation audits and the result receipt commit together. Validation and commit use the same existing identity rules; no RPC is executed under the Source transaction.

A lost completion reply can initially return an error. Exact retry after reopening uses the retained result without repeating identity updates or success audits. The live account and membership effects must still match that result. Changed verification, email or membership data is rejected, never reconstructed from a receipt. Different input cannot replace a pending operation. Existing GPU reservations remain held.

Managed HTTP responses use status 202 with `state: pending`. This is a pending Source mutation, not proof that effective permissions or an active compute head have changed. Legacy responses remain unchanged. Runtime enablement remains unavailable until complete writer coverage, operator application/finalization and real component acknowledgments are integrated.

Cross-repository tests exercise the actual SQLite store and authenticated gateway RPC, lost replies before/after completion, reopen, unchanged local IDs, one mutation audit per effect and retained reservations. Source tests cover unverified mappings, collisions, reassignment, running workspaces, concurrent preimage edits, authority loss, mid-commit rollback and changed effects during retry. HTTP tests cover pending responses, changed input and revoked authorization. These are software tests with controlled transports, not live identity migration or production acceptance.

## Software qualification

Complete admin suite: 102 tests and 55 subtests passed. Native-pool suite: 448
tests passed. Complete serial compute suite: 5,967 tests and 68 subtests passed,
with six skips and 623 warnings (exit 0, 678.16 seconds). Candidate hashes
remained unchanged through the full run. See the machine-readable receipt in
`releases/managed-identity-linkage-20261025/software-qualification.json`.
