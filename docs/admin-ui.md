# Admin service UI

The overview links to one Courses area (local courses, terms, memberships,
groups and groupings). The Hub course servers tab preserves the upstream
course server/profile flow. Projects, compute grants, workspace lifecycle,
assignments and audit use their existing authenticated APIs.

Forms preserve identifiers and let the backend preserve unspecified provenance
and policy metadata. Reference selectors show authorized records. When reference
data is unavailable, fields accept explicit identifiers and backend validation
remains authoritative. Canonical compute identity is not inferred from email or
username. Workspace creation derives account bindings and ceilings from approved
course data; missing bindings fail before submission. Removing members and
closing assignments interrupt writers and preserve files.

Run `npm ci`, `npx playwright install chromium`, `npm run test:ui` and
`npm run build` in `course-service-ui`. Browser tests use synthetic identities
and intercepted API fixtures, never a production login or actual mutations.
They qualify UI serialization, validation, errors, confirmations, accessibility
focus and responsive rendering; live permission/lifecycle qualification remains
separate. `index.html` supplies a development entry, while production uses the
existing server-injected config and template.


## UI release on the deployed backend

The October 2026 UI revision is packaged as a full `e2x-course-hub` wheel so
Python packaging owns the updated static assets. It preserves all Python
application sources, schema and dependency declarations from deployed revision
`e36805560e5f4d07626fd616e9a3b73d64901627`. There is no database schema migration.
The deployed workspace grant discovery and stopped-workspace profile editor
remain available; unsupported newer approval mutation APIs are not introduced.

Build the UI and both distributions in the verified checkout:

```sh
cd course-service-ui
npm ci
npm run build
cd ..
HATCH_BUILD_NO_HOOKS=true python -m hatchling build -t wheel -t sdist --directory /tmp/cps-admin-ui-dist-20261013
```

The hook is disabled because the exact frontend has already been built. Before
image construction, validate each wheel `RECORD` hash and size, confirm that the
wheel owns every fresh UI asset under
`e2x_course_hub-0.1.1.data/data/share/e2x_course_hub/static/course-service-ui/`,
and compare its complete Python source inventory and bytes against the deployed
Git revision. Verify the sdist includes the same frontend source and generated
static assets. Distribution version remains `0.1.1`; record the release source
revision and artifact SHA256 alongside it.

Prepare a build context containing exactly the verified wheel in `dist/`:

```sh
mkdir -p dist
cp /tmp/cps-admin-ui-dist-20261013/e2x_course_hub-0.1.1-py3-none-any.whl dist/
```

`Dockerfile.ui` requires `ADMIN_BASE_IMAGE` to be an immutable digest reference.
For this release, the required base and rollback reference is:

```text
ghcr.io/mul-cps/e2x-course-hub@sha256:12af62e86125a59ef4292e834c8bd1b4d25b686d631c6a37dca206d20b233a03
```

The recipe installs the complete wheel using `--no-index --no-deps
--force-reinstall`. It updates application-owned UI files while retaining
existing backend code, dependency versions, compute SDK, startup configuration,
and identity/authentication implementation. It restores `USER 10001:10001`.
Image publication and rollout are separate authorized operations. Record the
new image digest, confirm application-owned assets and unchanged backend after
installation, and verify both consoles. Rollback restores the immutable image
above; it requires no reverse schema migration.
