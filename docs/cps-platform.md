# CPS/CIT console extension

Moodle integration: **Status: planned / not deployed**.

Upstream foundation: `db04b6f02a54392f78479303bbed326627fa13c5`.
RBAC foundation: `4683d1a09051f8aa0d38a2e6ab181b4979b246b2`, pinned in pyproject.toml.

## Deployment and storage

The CPS modules are separate from upstream infrastructure/profile providers.
`CourseServiceApp.console_owner` is `cps` or `cit`. Deploy separate OAuth clients,
service tokens, SQLite PVCs and console instances. Run one replica per database.
`database_path` must point to persistent storage. Back up using SQLite's backup API
(`LocalCourseProvider.backup`) rather than copying a live database. Restore with
the matching application revision and Hub database; test restoration before promotion.
The initial SQLite schema is version 3 (`PRAGMA user_version`).

```python
c.CourseServiceApp.console_owner = 'cps'
c.CourseServiceApp.database_path = '/data/courses.sqlite'
c.CourseServiceApp.course_providers = {
    'local': {'enabled': True},
    'moodle': {'enabled': False, 'baseUrl': None, 'authSecretRef': None,
               'syncInterval': '10m', 'readOnly': True},
}
c.CourseServiceApp.compute_policy_url = 'https://compute.internal.example'
c.CourseServiceApp.compute_policy_token = '<private console-specific service token>'
c.CourseServiceApp.compute_gateway_url = 'https://compute.internal.example'
# Enable only after compatibility qualification against the deployed Hub OAuth endpoint.
c.CourseServiceApp.oauth_pkce = True
```

These tokens must come from Kubernetes Secrets, never browser configuration.
All new administration APIs check current Hub administrator status on every request;
instructors retain upstream course-scoped member operations. Course metadata and term
provenance defaults to local when loading old YAML, retaining original course IDs,
term dictionary keys, profile settings, mounts, and storage paths. Backend membership
mutations reject externally managed courses/terms. Local memberships used by running
shared workspaces must go through stop/revoke operations rather than generic deletion.

## Local records and explicit migration

Authenticated admin CRUD: `/api/local/{courses,terms,memberships,groups,groupings,projects}`.
Read-only lists also include workspaces/assignments and `/api/local/audit`.
The UI provides Local Courses, Projects, Compute, Shared Workspaces, Assignments,
Audit and Integrations. Administrator requests are subject to CSRF protection.
Source changes and externally managed record edits are rejected by the backend.
Source-neutral reconciliation forwards a provider snapshot into an asynchronous sink.

`cps.migration.import_upstream` explicitly exports exact existing Hub group names and
usernames into SQLite without changing Hub groups, PVCs or workspace paths. Imports
are atomic on invalid child references. Import after backup, before changing
authenticator group ownership, never automatically on login. Canonical person aliases
need an explicit reviewed mapping; assignment memberships require `canonical_person_id`
before any global GPU reservation. Matching display names never merges identities.
The production export/import, grant seeding and storage qualification are operator gates.

Assignments support CSV with `person_id,group_id` headers, manual group/member lists,
and deterministic random allocation with explicit seed. IDs and provenance flow into
local groups/groupings/memberships. Existing assignment replacement fails closed;
reallocation requires stop and reviewed mapping. Assignment closure stops associated
shared writers and prevents restart. Files remain intact and `archive_pending` remains
true until an operator verifies all other writers are stopped and applies a read-only
filesystem archive. The application does not pretend it has performed that operation.

## Shared policy and workspace lifecycle

`cps.compute.ComputePolicyClient` uses server-only HTTPS service tokens for console-owned
grants and global workspace reservations. Central policy supplies enabled profiles and
validates selected fixed workspace profiles against global effective member entitlements,
pooled allowance and the course ceiling. Browser resource overrides are rejected.
Expired/time-bounded grants are evaluated by the shared compute service; the console
uses its authoritative grants API and records the real administrator around mutations.

Create via `/api/workspaces/create` with explicit existing neutral Hub account/named-server
identifiers, owned course/group IDs, selected profile and course ceiling. The console
never derives a user's home from a display name. Neutral account provisioning and NFS
mount policy remain trusted Hub adapter/deployment configuration.

Workspace startup validates policy, acquires all-member reservations, synchronizes
only the controlled group, starts the fixed profile and grants native group Shares.
A startup error triggers a shutdown poll before reservation release. On member removal,
the adapter stops and confirms the server is absent from running Hub servers, removes
group membership, revokes/replaces Shares, releases reservations, then updates local
membership state. The stopped workspace reports kernel interruption and preserved files.
Restart revalidates current pooled policy. Shutdown timeout retains membership/reservations.
`/api/workspaces/{id}/{start,stop,remove-member}` provides these controlled operations.
The gateway independently requires a trusted shutdown observer, never browser evidence.

## Actual visitor API bridge

`/api/compute/v1/...` allows selected public compute routes and forwards only the visitor's
Hub OAuth token, `X-CPS-Hub`, content type and idempotency key. It never forwards service
credentials or permits internal administrative routes. Visitor identities remain separate
when two browsers collaborate in one RTC kernel. Shared-kernel SDK identity remains the
workspace principal. No browser data is used to identify who executed a notebook cell.

JupyterLab PageConfig `cpsComputeGatewayUrl` must be the same-origin path
`/services/<console>/api/compute/v1/`. Bootstrap CSRF through the sibling authenticated
`/api/compute/xsrf` endpoint (`Cache-Control: no-store`) and send its `xsrf_token` as
`X-XSRFToken` on writes. Notebook JavaScript cannot read the console-path cookie directly.

The bridge also permits `POST /api/compute/v1/workflows/<key>/artifacts/retain`
with the gateway's fixed artifact body (`snapshot` or `executed-notebook`). It
preserves visitor authentication and CSRF checks; the gateway checks ownership
and coordinates retention against deletion. A deletion claim returns 409, and a
disabled lifecycle returns 503. Delete and unretain routes remain unavailable.
This additive route requires the compatible compute lifecycle release; it does
not enable cleanup or grant console users administrative credentials.

## OAuth

State validation is retained from pinned JupyterHub 5.5.2. The wrapper rejects external,
scheme-relative and backslash redirects. Auth/session cookies are Secure, HttpOnly and
SameSite=Lax. Optional S256 PKCE uses a separate signed, Secure, HttpOnly same-site
10-minute verifier cookie tied to the OAuth state. The token exchange adds `code_verifier`
and preserves confidential client authentication. Consumed/expired state is rejected;
verifier context is isolated per concurrent request. The opt-in avoids silently changing
existing confidential-client deployments. An HTTP fake authorization/token endpoint test
covers completed PKCE flow and replay rejection; deployed Hub compatibility remains a gate.

## Planned Moodle boundary

Disabled Moodle causes no initialization, secret lookup, network requests or scheduling.
Enabling it fails startup. No Moodle credentials, dependencies, jobs, CRDs, LTI login or
setup wizard are installed. Local course management stays active.

`tests/test_provider_startup.py` exercises complete `CourseServiceApp.initialize`
for both console owners using isolated databases and synthetic Hub configuration.
It guards socket connections, Tornado periodic-work startup and the configured
Moodle secret-file path; disabled-provider construction is forbidden. Enabling
Moodle must raise the planned/not-deployed configuration error before creating
persistent console files. Provider/migration/reconciliation tests separately cover
local CRUD, reference preservation and fake-source snapshots. These backend tests
do not qualify production OAuth, rendered pages or a deployed release image.

```sh
python -m unittest discover -s tests
```

Official enrolment remains MUonline/CAMPUSonline authority. Moodle will own teaching
rosters, roles and assignment groupings; compute policy owns resource entitlements;
consoles own research projects and workspace lifecycle. The university enrolment bridge
is an ICT dependency. Authentik remains login authority through Dex.
Read-only Web Services are planned first, optional LTI 1.3 workspace links later;
grade return and direct CAMPUSonline integration are later work.
Source handover requires explicit person/course mapping and a reviewed migration.

Official guides: [External Services](https://moodledev.io/docs/5.1/apis/subsystems/external)
and [LTI](https://docs.moodle.org/501/en/mod/lti).

## Release gates

No production rollout is claimed. Remaining gates: real two-Hub OAuth/PKCE and browser
RTC tests; neutral-account/NFS provisioning; canonical identity/grant import and login
persistence; global trusted shutdown-observer bindings; complete mutation audit coverage
including upstream Hub writes; source-neutral reconciliation against real Hub Shares;
read-only archive operation and restore exercise; SQLite migration qualification.

## V1 implementation additions

Course and term operations now use `e2x_hub_rbac`'s pinned `Scope`, `RoleAssignment`
and `PermissionChecker`. Course owners and term instructors can manage their scoped
records, allocate assignments and operate existing shared workspaces without a global
administrator role. List APIs filter records by fresh scoped role assignments. Compute
grants, canonical identity links, audit overview, course resource ceilings and approved
neutral-account bindings remain global administrator operations. TA access is read-only.
Old group names are retained; `legacy_rbac_roles` explicitly interprets reviewed aliases
(default: `instructor`), without renaming groups or silently broadening privileges.

Workspace creation requires a course record's administrator-managed `resource_ceiling`
and exact `workspace_bindings` entry (`group_id`, `hub_user`, `hub_server`). Resource
ceilings are refreshed from the owned course before every startup and revalidated by
shared policy. An instructor cannot select an unrelated Hub user, arbitrary administrative
Hub group or resource override through the workspace API.

Normalized record validation rejects unknown fields and wrong types, checks owned course,
term, group and grouping references, and prevents silent course/term reassociation of
existing IDs. Group deletion preserves membership/workspace/grouping references.

Identity authority is explicitly verified or administrator-reviewed **normalized email**, linked to an explicitly supplied stable canonical **person UUID**.
`reviewed_email_mapping` takes explicit existing `(hub, username, email, person_id)` rows and proof
flags, rejects missing/unreviewed/duplicate mappings, and never infers email from a username.
Different CPS/CIT usernames may link to the same reviewed email and UUID. `POST /api/identities`
is administrator-only and persists owned links. Membership canonical UUID values must
match those stored links; normalized email remains metadata and the reviewed linkage authority. Existing username/path identifiers remain unchanged.
`import_upstream(..., reviewed_identity_rows=...)` fails on missing user mappings; an
import without this argument preserves records but intentionally supplies no canonical
identity and cannot reserve GPU workspaces until mappings are explicitly reviewed.
No production identity mapping has been generated or installed.

Schema version 3 adds explicit canonical UUID links while preserving earlier record keys.
Earlier email-only links receive no inferred UUID: explicit administrator-reviewed mappings
are required to migrate affected membership canonical IDs, and active workspaces must
stop before that migration. Existing usernames, membership IDs and references remain intact.
Request-level audit covers every console/upstream API mutation outcome, including invalid
JSON, denied authorization, DELETE, assignment and lifecycle failures. Provider mutations
are audited on success/failure; Hub API writes capture actual request actor and observed
before/after values. Missing Hub observations are explicit `unavailable`, never fabricated.
No API token is stored in audit entries. Restore and verify the matching application and
schema version; production restore remains an acceptance gate.

On an empty persistent volume, startup exclusively creates an empty local foundation:
no roles, profiles, courses or mounts. Existing files are never overwritten. The tested
command `python -m e2x_course_hub.course_service.app --config=/config/config.py` loads the
Python configuration through the explicit `config` alias. Seed reviewed profiles/course
configuration separately; empty defaults intentionally grant no compute permission.

The console service needs scoped Hub permissions for its API reads, controlled group
management, neutral-account server lifecycle and native Shares. Start with per-neutral-user
and per-controlled-group filters for `read:users`, `read:groups`, `groups`, `servers`,
`read:shares` and `shares`; add `admin:users` only for explicitly approved account
provisioning (disabled by default). Service OAuth access scopes and visitors are separate
from this server-side token. Qualify the exact Hub scope names/filters against the pinned
Hub before deployment; never give the browser these service credentials.

Time-bounded local teaching memberships validate timezone-aware `starts`/`expires`.
An explicitly matched expired/future membership makes its Hub role group ineffective in
console and existing upstream course permission checks, even when login retained that
group. Unimported groups retain existing behavior until the reviewed grant migration.

### Distributed workload visitor bridge

The authenticated compute proxy explicitly permits JobSet listing, inspection
and logs by GET, plus fixed-descriptor submission and termination by POST.
Named routes require the central `cps-js-` hexadecimal identifier; arbitrary Pod,
artifact, internal, traversal and encoded routes remain excluded. Each request
uses the actual visitor token and the console's trusted Hub selector. Browser
Authorization headers cannot replace that token. The gateway independently
checks canonical workload ownership and central submission policy. Pair this
admin release with the compute gateway/addon release providing JobSet list/log
endpoints; disabled distributed runtime remains disabled. Two-visitor proxy
tests cover token separation and reject unsupported routes. Production OAuth
and rendered addon qualification remain separate rollout requirements.
