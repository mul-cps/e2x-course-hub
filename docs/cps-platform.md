# CPS/CIT console extension

Status: planned / not deployed (Moodle integration).

Upstream foundation: `db04b6f02a54392f78479303bbed326627fa13c5`.
RBAC foundation: `4683d1a09051f8aa0d38a2e6ab181b4979b246b2`, pinned in pyproject.toml.

The CPS modules are independent of upstream infrastructure/profile providers.
`CourseServiceApp.console_owner` is `cps` or `cit`. Deploy separate OAuth clients,
service tokens, SQLite PVCs and console instances. Run one replica per database.
`database_path` must point to persistent storage; back up using SQLite's backup API
(`LocalCourseProvider.backup`) rather than copying a live database. Restore with
the matching application revision and Hub database; test restoration before promotion.
The initial SQLite schema is version 1 (`PRAGMA user_version`).

Local authenticated administrator CRUD is at `/api/local/{courses,terms,memberships,groups,groupings}`;
`/api/local/audit` reads successful mutations. Upstream course-scoped instructor APIs remain
available. Browser writes require a CSRF token. No browser receives service tokens.
Source changes and externally managed record edits are rejected by the backend.
Import existing records explicitly with `migrate_local`, using existing IDs and references;
never reconcile by name. Export existing Hub memberships before changing authenticator group ownership.
The import boundary is implemented, but production export/import and identity mapping need operator review.

```python
c.CourseServiceApp.console_owner = 'cps'
c.CourseServiceApp.database_path = '/data/courses.sqlite'
c.CourseServiceApp.course_providers = {
    'local': {'enabled': True},
    'moodle': {'enabled': False, 'baseUrl': None, 'authSecretRef': None,
               'syncInterval': '10m', 'readOnly': True},
}
```

Disabled Moodle performs no initialization, secret lookup, requests or scheduling.
Enabling it fails before database/application initialization. No Moodle dependencies,
credentials, jobs, LTI authentication or setup wizard are installed.
The planned provider reads Moodle Web Services into normalized course, term,
membership, group and grouping records. `reconcile` forwards any provider snapshot
to a source-neutral async sink; production Hub Shares/workspace provisioning still
requires its implementation and acceptance testing.

Official enrolment remains MUonline/CAMPUSonline's authority. Moodle will own teaching
rosters, roles and assignment groupings. Compute policy owns resource entitlements;
consoles own research projects and workspace lifecycle. The university enrolment
bridge is an ICT dependency. Authentik remains login authority through Dex.
Read-only Web Services are planned first, optional LTI 1.3 workspace links later;
grade return and direct CAMPUSonline integration are later work.
A source handover requires explicit person/course mapping and a reviewed migration.
Changing configuration or matching names cannot overwrite local memberships.

Official guides: [External Services](https://moodledev.io/docs/5.1/apis/subsystems/external)
and [LTI](https://docs.moodle.org/501/en/mod/lti).

OAuth uses JupyterHub 5.5.2's HubOAuthCallbackHandler and HubOAuthenticated rather than
an independent callback implementation. State, redirect and PKCE compatibility are
owned by this pinned dependency. Proxy/TLS callback and login qualification remain
release gates. Cookies are secure, HttpOnly and SameSite=Lax; CSRF protection is enabled.

Remaining release gates: course UI CRUD for new normalized records; Projects, Compute,
Shared Workspaces and Audit UI; central policy grant/reservation integration; real Hub
Shares/RTC reconciliation; existing identity/storage migration; failed-mutation auditing;
SQLite restore exercise and end-to-end OAuth tests against both deployed Hubs.
Do not release this branch as a production console until those gates pass.

The upstream source audit found that HubOAuth 5.5.2 exchanges authorization codes
with a confidential `client_secret` and does not pass `code_verifier`. This fork does
not claim PKCE support. The existing Hub service OAuth path must remain a confidential
client; a PKCE-required provider configuration is an unqualified gate and must not
be enabled. The callback validates matching signed state using upstream code and the
CPS wrapper additionally rejects external/scheme-relative/backslash redirects.

For existing records, CourseMetadata and TermConfig now deserialize absent provenance
as `source=local`, retaining original course IDs and dictionary term keys. The explicit
`cps.migration.import_upstream` exports exact existing Hub group names and usernames
into SQLite without changing any Hub group, PVC or workspace path. Import is an
operator action after backup, not an automatic login or startup mutation. Upstream
membership mutation methods enforce local-source editability before writes.

`cps.compute.ComputePolicyClient` provides the server-only shared policy boundary:
HTTPS service-token calls for console-owned grants and global workspace reservation
acquire/release. Bind service tokens to the console owner in the gateway. Release
requires a trusted Hub shutdown poll; browser input is never shutdown evidence.
This client is not yet wired to console routes or a lifecycle controller.
