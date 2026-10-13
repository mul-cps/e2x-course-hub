# Reviewed legacy neutral account adoption

Fresh `ensure()` uses the permanent packaged Hub endpoint described in
[neutral-workspace-account-provisioning.md](neutral-workspace-account-provisioning.md).
It refuses an existing Hub user without an authoritative permanent Hub receipt.
Existing successful Source POST receipts retain their legacy provenance and
identifiers. A v6 database upgrade adds an empty `workspace_accounts`
table. It does not infer ownership from usernames, marker roles or old workspace
records. Do not insert synthetic creation receipts into that table.

Reviewed adoption is a separate administrator operation. The Source backend
checks the current authenticated actor through its owning Hub, reads the exact
local workspace/course/term/group, and resolves any absent namespace/Pod fields
only from one exact existing `course.workspace_bindings` entry. It checks the
actual Hub `{name, created}` identity, allowed direct user/marker roles, dedicated
groups/server identities, canonical human collisions and every Source writer in
the same course group. Empty stopped unnamed Hub ORM spawners are accepted only
when they have no pending, ready or retained Pod state.

The owning gateway must have the exact source/workspace/owner/server/namespace/
Pod registration. Any supplied course/term/group fields must match Source.
The full registration, including its existing storage descriptor, PVC and
version when present, is captured and compared. No missing course binding or
storage descriptor is manufactured. An inactive reservation with no attempt is
required. Execute additionally uses the existing trusted shutdown/native-cleanup
probe, then rechecks both reservation and registration before reservation of
ownership or group mutation.

## Deployment prerequisites

Use one source-specific console database, Hub URL, service credential and
gateway credential per deployment. Stage these exact settings before planning:

```python
c.CourseServiceApp.console_owner = 'cps'  # 'cit' for the independent CIT source
c.CourseServiceApp.neutral_workspace_group = 'cps-workspace-kernel-accounts'
c.CourseServiceApp.neutral_workspace_role = 'cps-workspace-kernel'
c.CourseServiceApp.allow_console_schema_upgrade = False
```

The final application defaults to refusing database initialization and upgrades.
An existing ownership table must have its six ordered text columns and expected
nullability, owner primary key `(console, hub_user)` and nonpartial workspace
uniqueness `(console, workspace_id)`. A complete-looking schema7 table without
those exclusions is refused before writing, including when schema upgrade is
explicitly authorized; the upgrade switch does not repair malformed ownership.
After taking the reviewed online v6 backup, explicitly set
`allow_console_schema_upgrade=True` for the controlled first boot (or use the
provider's explicit upgrade interface on an authorized offline copy), verify
schema 7 and its unchanged old records, then disable it. A missing, foreign,
partial or older database must not silently become a new production console.
The CLI never opens SQLite, initializes a schema or inserts SQL receipts.

The operator must install and verify the fixed Hub group with exactly the
`cps-workspace-kernel` role. The role must have zero scopes; standard Hub 5 user
and group API models expose role names, so this scope definition is an operator
Hub configuration gate. Keep Authenticator/OAuthenticator group and role
management disabled and preserve inherited human/admin refresh behavior.
Source must have narrowly scoped permission to inspect the reviewed users and
fixed group, and mutate that group's users for explicit reviewed adoption.
New account creation uses only `custom:cps:provision:workspace-account`; it does
not require `admin:users` or an ordinary users POST.
Gateway `neutralWorkspaceGroup` must be `cps-workspace-kernel-accounts`, and the
interactive neutral login guard must recognize current group proof. Do not put
member arrays back into Hub `load_groups`, and do not enable extra native
workspace/profile allowlists as a consequence of adopting their owners.

Root owns that coordinated Hub/gateway/cluster configuration, source image and
publisher pins, stopped-writer custody, actual service-token validation,
pre/post online backups, PV/PVC UID and NFS-path/data-hash preservation, and the
controlled actual migrations. This Source implementation neither changes nor
qualifies those live resources.

## Plan, review and execute

Use the actual owning authenticated administrator's Hub token, authorized to
access this console service, in an owned private regular file. A console service
token is not an administrator impersonation mechanism. Bearer authentication
uses the existing Hub-authenticated service flow; the backend rechecks current
administrator status and binds the review to that actor.
Before each JSON POST the CLI makes an authenticated HTTPS GET to the same
Source prefix's `/app`, retains applicable cookies only in memory, and supplies
the returned `_xsrf` cookie and `X-XSRFToken` header. Missing or inapplicable XSRF
cookies and all bootstrap/POST redirects fail closed. It does not launch browser
OAuth or bypass the server's XSRF checks.

```sh
umask 077
workspace-account-migration \
  --source-url https://hub.example.invalid/services/cps-admin \
  --token-file /private/owning-admin-token \
  --workspace reviewed-workspace-id \
  --reason 'Reviewed existing neutral ownership and retained storage' \
  --provenance 'change-ticket:reviewed-evidence-reference' \
  --packet /private/workspace-review.json
```

Default plan performs only readbacks and saves a 0600 packet. It changes no
account, group, workspace record or storage. The protected packet contains the
exact actor/source, reason/provenance, permanent binding, full owning Source
records, actual Hub identity, full gateway registration/storage, related writers,
Source authority hash and deterministic review ID. Review it in private against
the protected backup/Hub/storage evidence. Do not edit the packet to make a
preflight pass. A changed Source record, canonical identity, Hub creation time,
registration/PVC, binding or writer requires reconciliation and a new review.

```sh
workspace-account-migration \
  --source-url https://hub.example.invalid/services/cps-admin \
  --token-file /private/owning-admin-token \
  --packet /private/workspace-review.json \
  --execute
```

Execute accepts no workspace, actor, reason, provenance or fingerprint overrides.
It forwards the exact protected packet to
`POST /api/workspace-account-adoptions/<id>/execute`; plan uses the same owning
Source route ending in `/plan`, with only `reason` and `provenance`. Source
compares the supplied packet to fresh trusted Hub/gateway/Source observations.
Gateway registration is read through the configured source-scoped authenticated
internal GET route. Its principal must exactly equal
`workspace:<owning-console>:<workspace-id>`, together with the exact workspace,
owner, server, namespace and Pod binding. Retained legacy registrations may omit
`source`; when that field exists it must also equal the owning console. The full
raw registration and storage remain in the review and must survive cleanup
unchanged. No `source` field is invented and no registration PUT is performed.
The dedicated Hub owner is checked separately and cannot replace the workspace
ID in the principal. The shared fictional registration fixture is also checked
against the actual pinned gateway SDK's `validate_workspace_registration`, not
an equivalent Source implementation. Run that compatibility check explicitly
with the gateway's dependencies and source checkout:

```sh
PYTHONPATH=/path/to/pinned/cps-compute/src /path/to/gateway-env/bin/python \
  -m unittest discover -s tests -p gateway_contract_workspace_adoption.py -q
```
The CLI requires HTTPS and protected owned files, refuses symlinks and redirects,
and prints no tokens, identities, user/email lists or server response bodies.

## Durable outcomes and recovery

An OS lock serializes asynchronous adoption across console instances and is
released by process exit. The console database and its adoption lock sidecar
must reside on one supported local SQLite filesystem shared by any instances
accessing that same DB; independent copies are not a distributed lock.
`BEGIN IMMEDIATE`, unique owner/workspace constraints and explicit row/authority
compare-and-swap reserve the permanent binding and provenance before any group
mutation. Audited durable stages are `adoption_reserved -> adoption_granting ->
ready`. The receipt kind is `reviewed_legacy_adoption`, with the review and actual
identity. Previously successful ordinary Source POST receipts retain kind
`source_creation` (or their original v7 `{name, created}` shape). New permanent
Hub endpoint receipts have kind `hub_neutral_creation` and retain the full receipt.

Do not delete an owner, recreate it, release an active reservation, change a
Pod/PVC name or replace retained files to clear a failure. A reserved/granting
stage survives receipt failure, group failure, an ambiguous grant response and
restart. Keep the exact packet and retry after reconciling the cause. Readback
avoids a duplicate grant after an ambiguous success. `ensure()` accepts adoption
only after its validated receipt reaches `ready`. An unknown legacy ordinary
POST `intent` remains fenced and cannot be converted to reviewed legacy provenance.
New `hub_intent` rows retry only the exact permanent Hub endpoint and descriptor.

A completed unchanged review is idempotent. A completed review retry refuses to
restore removed group membership. Current group proof remains authoritative;
the console's read-only `account_state` uses current identity/group readback and
shows review, recovery or unavailable status when readiness is unconfirmed. This
status is account readiness, not GPU/profile availability or server readiness.

Schema 7 online backups and coordinated recovery fingerprints include complete
`workspace_accounts` rows, typed provenance and adoption audit rows. Rollback to
901a885/schema 6 requires the matching pre-cutover database, Hub/group/config and
storage custody state, together with controlled reconciliation of any writes
after cutover. An image-only rollback is insufficient.

The regression fixture `tests/fixtures/source-v6-901a885.sql` was generated by
the actual `901a885e921e22cc037d4b64715245cb39a29d72` LocalCourseProvider using only
fictional records. Tests upgrade that v6 fixture without changing its identifiers
or records, refuse implicit adoption, then perform explicit adoption. Fixture
and HTTP tests do not establish a real production migration or restoration.
