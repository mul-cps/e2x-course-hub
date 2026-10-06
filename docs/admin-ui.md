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

`Dockerfile.ui` builds a frontend-only qualification image on the immutable
currently deployed backend. It changes no database schema, backend packages,
policy, credentials, roles or identity settings. Record the frontend source
revision and resulting digest when deploying. Rollback uses the previous image
digest. A full application release still requires its independent gates.
