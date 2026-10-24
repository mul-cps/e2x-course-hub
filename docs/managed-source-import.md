# Managed Source import

Managed mode accepts one closed import of 1–1000 local records. The caller retains existing IDs; no name matching, external provenance takeover or identity mapping is inferred. Unknown record kinds, duplicate IDs within a kind and invalid relationships are rejected before admission fencing.

Normalization and relational validation use an isolated in-memory SQLite snapshot with the existing record validator. This validation view never becomes a writer or installs custody. A single authenticated `admin.record.import` transition binds the normalized ordered batch. After preparation, the actual Source transaction rechecks its authority identity/path and the complete record/account-link preimage. All record updates, per-record success audits and the import result receipt commit atomically. No network call runs inside that transaction. Concurrent changes, lost authority or an exception during commit leave the batch unapplied and admission fenced.

Completion is a separate RPC. A lost reply can initially return an error; the exact batch can resume from the local receipt after process/database reopen. It does not replay the mutations or success audits. Different batches cannot replace the pending transition. No background worker or operator activation is implied.

The assignment endpoint uses the retained batch for an authorized exact retry, preserving the original grouping. It rejects changed assignment settings and rechecks current visitor permissions. Managed responses use HTTP 202 and `state: pending`; legacy responses remain unchanged. Pending Source records do not mean the new effective permissions or workspaces are active.

The gateway accepts only authenticated console-owned bounded normalized import intents. Cross-repository tests exercise the actual HTTP/RPC and SQLite stores, normal completion, lost replies before/after completion, reopen, one mutation audit and retained global reservations. Admin tests exercise a referenced multi-record batch, invalid references, duplicates, concurrent preimage edits, lost authority and mid-commit rollback. HTTP tests exercise the assignment retry, changed settings and revoked authorization.

Runtime enablement remains unavailable until operator application, all authoritative writers and activation acknowledgments are integrated. Identity migration, legacy Hub permission mutations and lifecycle-sensitive deletion remain separately guarded.

## Software qualification

On 2026-10-25 the complete admin suite passed 99 tests and 44 subtests. The
compute native-pool suite passed 445 tests. The complete compute suite passed
5,964 tests and 68 subtests, with six skips and 623 warnings (exit 0, 677.59s).
Source hashes remained unchanged through the complete run. The machine-readable
receipt is in `releases/managed-source-import-20261025/software-qualification.json`.
No live runtime configuration, identities, workspaces or reservations were changed.
