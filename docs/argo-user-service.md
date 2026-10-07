# Dedicated native Argo user service (source qualification)

`e2x_course_hub.cps.argo_service.create_argo_service` builds an independent,
read-only Tornado service for the native Argo **3.7.18** UI (the existing
operational chart is **0.45.26**). It does not initialize or modify the course
admin application, Hub configuration, roles, groups, manifests, or existing
operational `/argo/` ingress. Source tests are not live Hub/browser evidence.

The browser entry is `/argo/workflows/cps-workflows?limit=50`. Native breadcrumb
navigation to `/argo/workflows` also serves the same fixed SPA index. API routes
remain restricted to `cps-workflows`. `/argo/login` starts the existing Hub OAuth
flow; the native UI's supplied `redirect` argument is ignored and its callback
returns to the fixed landing page. No Keycloak, Dex, or Moodle setup is needed.

## Fixed trust boundary

The service creates a fresh `ArgoHubOAuth` instance using the existing
`CPSHubOAuth` S256 PKCE, one-use state, verifier-cookie and local-redirect
hardening. It does not reuse the admin OAuth singleton, ID, token or secret.
Only authenticated **Hub users** carrying
`access:services!service=cps-argo-ui` (or `cit-argo-ui` on CIT) are accepted;
teaching or administrative permissions are not required. Hub services cannot
log in as human visitors. OAuth, state and verifier cookies are Secure,
HttpOnly, SameSite=Lax and restricted to `/argo/`. The visitor-bound `_xsrf`
cookie is Secure, SameSite=Lax and restricted to `/argo/`; it is readable for
normal XSRF clients.

Browser Authorization headers and URL tokens are ignored. The signed OAuth
cookie supplies the actual visitor token. Only these server-created headers go
to the fixed compute URL:

```text
Authorization: Bearer <actual visitor Hub OAuth token>
X-CPS-Hub: cps  # fixed deployment source, or cit
```

The compute owner adapter must install its `/argo` contract with the existing
visitor identity dependency and a fresh identity renewal callback. It alone
authorizes canonical owners, queries, workflow objects, Pod logs, and fixed
artifact pairs. Neither the namespace nor upstream authority can be selected
by a browser. No Kubernetes, Argo, S3 or admin token is sent to the browser or
the native asset server. Response cookies, redirect locations, and arbitrary
upstream headers are not forwarded.

Allowed compute routes are GET `api/v1/{info,version,userinfo}`, GET
`api/v1/workflows/cps-workflows` and `/{name}` and `/{name}/log`, GET
`api/v1/workflow-events/cps-workflows`, and GET/HEAD
`artifact-files/cps-workflows/workflows/{name}/{node}/inputs/snapshot` or
`.../outputs/executed-notebook`. All other APIs, actions, HTTP mutations,
namespaces, encoded paths and arbitrary artifact keys are denied. Native UI
buttons for unavailable actions remain unsupported.

Read-only API/artifact requests carrying `Sec-Fetch-Site: same-origin` and an
absent Origin or the exact configured public origin can authenticate by cookie
without an XSRF header. This exception is restricted to the exact route
allowlist: native EventSource and the native Argo fetch client cannot add Hub's
XSRF header. Normal navigation and static resources keep Hub 5.5.2 checks.
Cross-site requests and mismatched Origin are rejected; CORS is not enabled.
HTTP writes retain Hub XSRF validation and are denied even after it succeeds.

SSE chunks are flushed as they arrive, with `no-store` and
`X-Accel-Buffering: no`. Each stream lasts at most 30 seconds and carries at most
16 MiB. The downstream adapter also bounds events and renews identity during
events; EventSource reconnects after the finite response closes. Browser
disconnect cancels the local await; Tornado may retain its upstream request
until the independent 30-second HTTP timeout. Fixed artifacts stream up to
100 MiB as `application/octet-stream`, with a 120-second end-to-end timeout
(including the adapter's bounded, up-to-60-second spool). Metadata and assets
have a 16 MiB limit. Downloads expose the adapter's fixed attachment names.
Artifact responses retain the adapter's validated Content-Length (at most
100 MiB); missing/invalid length is denied, and interrupted or truncated
transfers abort the browser connection so they cannot appear as completed
downloads.
The service shares four active upstream/transfer slots across compute APIs,
SSE, artifacts and native assets; excess requests fail closed with HTTP 503.
Each response exposes `pending_flush_bytes` and checks a 1 MiB pending-write
cap **before** writing another chunk. Successful or failed flush completion
decrements the counter; a cap breach or flush error aborts the browser
connection, and later callbacks discard their chunks. Buffered metadata/assets
use 64 KiB chunks and await flush completion; the upstream streaming callback
stays synchronous, so it uses bounded abort instead of asynchronous backpressure.
Slots release only after the actual upstream request and pending browser writes
finish, including after errors/disconnects. A disconnected browser cannot free
a slot while Tornado still retains its remote request. The independent HTTP
deadline bounds that retained request; the same deadline closes pending browser
writes. This bounds source-level capacity and buffering, but real slow-browser
and concurrency load qualification remains required before production activation.

Only fixed `.js`, `.css`, image and font asset paths and the reviewed workflow
SPA routes are requested from the pinned native Argo backend at `/argo/`.
Asset requests carry no credential or browser header. `connect-src 'self'`
prevents native UI connections to another authority. TLS certificate and
hostname validation are always enabled for the Hub, compute and native asset
backend; all three require explicit CA bundle paths.

## Configuration and independent qualification deployment

Every `ArgoServiceConfig` field can be supplied through an environment variable:

| Variable | Required value |
| --- | --- |
| `ARGO_USER_SOURCE` | `cps` or `cit` |
| `ARGO_USER_OAUTH_CLIENT_ID` | `service-cps-argo-ui` or `service-cit-argo-ui` |
| `ARGO_USER_OAUTH_CLIENT_SECRET` | Independently generated dedicated Hub service token/client secret |
| `ARGO_USER_COOKIE_SECRET` | Independent stable secret of at least 32 characters |
| `ARGO_USER_PUBLIC_ORIGIN` | HTTPS origin of this service, with no path |
| `ARGO_USER_HUB_API_URL` | Fixed existing Hub HTTPS API URL, ending in `/api` |
| `ARGO_USER_HUB_AUTHORIZATION_URL` | Existing Hub HTTPS `/api/oauth2/authorize` URL |
| `ARGO_USER_COMPUTE_URL` | Fixed HTTPS compute gateway origin, with no path |
| `ARGO_USER_NATIVE_ARGO_URL` | Operator-pinned Argo 3.7.18 HTTPS backend URL ending in `/argo` |
| `ARGO_USER_HUB_CA_FILE` | Installed trusted CA bundle for the Hub API |
| `ARGO_USER_COMPUTE_CA_FILE` | Installed trusted CA bundle for compute |
| `ARGO_USER_NATIVE_ARGO_CA_FILE` | Installed trusted CA bundle for native Argo |

There is no fallback to administrative `JUPYTERHUB_CLIENT_ID` or
`JUPYTERHUB_API_TOKEN`. Missing or invalid URLs, secrets, source, client ID,
native base path or CA files fail during configuration. Files must contain
valid trusted CA certificates; existence validation does not qualify the
certificate chain. The native backend must serve its reviewed assets with
`/argo/` as the base href. It supplies **only UI assets**; none of its raw API
routes should be exposed through this service.

Run the factory as its own process/backend. For example, a launcher can create
`app = create_argo_service(ArgoServiceConfig.from_env())`, bind `app.listen` to
an operator-selected private port, and run Tornado's IOLoop. Terminate TLS at
a reviewed ingress and sanitize forwarding headers there. Use an independent
qualification HTTPS origin/backend with the same `/argo/` path while the
operational `/argo/` stays unchanged. Callback origin must exactly match that
qualification origin. OAuth state is process-local: qualify a single replica
or sticky routing before attempting multiple replicas.

## Exact Hub registration and scope grant required before live login

The root operator must review and register a new service on each selected Hub.
For CPS, the registration must have service name `cps-argo-ui`, resulting client
ID `service-cps-argo-ui`, a **new** independently generated service API token,
the dedicated private backend URL, and exact OAuth redirect URI
`https://<reviewed-service-origin>/argo/oauth_callback`. CIT uses `cit-argo-ui`
and a separate token/secret and callback origin. Register no additional OAuth
client scopes; Hub adds its normal identity and service-access scopes. Do not
copy the course admin service credentials or require its role.

An illustrative **review-only** JupyterHub configuration shape is:

```python
c.JupyterHub.services.append({
    "name": "cps-argo-ui",
    "url": "http://<dedicated-private-backend>:<port>",
    "api_token": "<new-dedicated-secret-from-secret-store>",
    "oauth_redirect_uri": "https://<reviewed-service-origin>/argo/oauth_callback",
    "oauth_client_allowed_scopes": [],
})
c.JupyterHub.load_roles.append({
    "name": "cps-argo-ui-user",
    "scopes": ["access:services!service=cps-argo-ui"],
    "groups": ["<reviewed-normal-user-group>"],
})
```

The reviewed group must already exist or be created through the operator's
existing identity policy; this module creates no role or membership. Granting
the scope to an existing non-admin group permits normal users without granting
admin-console access. Registering the service alone does not grant that scope
to users. Confirm the issued **visitor** OAuth token retains the dedicated
service access scope and the compute visitor resolver can introspect its
identity. Verify login/callback and certificate chains on the actual Hub
5.5.2; fixture identities are not evidence of live scope registration.

Before replacing any operational routing, root review must qualify two real
normal users in separate browsers, expired/revoked identities, callback/state
replay rejection, native 3.7.18 asset/base-href/navigation behavior, workflow
and event ownership, finite log streams, exact downloads and denial of every
unknown API/action. Review the shared compute adapter source/tests first.
Source factory completion does not satisfy those deployment gates.

## Source verification

`python -m unittest discover -s tests -p test_argo_service.py -v` exercises the
installed JupyterHub 5.5.2 handlers, PKCE token exchange against a local fixture,
state matching/replay/expiry, cookie flags, separate visitor identities,
service scope denial, server-created headers, TLS request configuration,
allowlisted paths, native login/navigation, actual Tornado SSE chunk delivery,
fixed artifact GET/HEAD, an 8 MiB real-transport download, shared capacity 503s,
controlled slow flushes, successful/failed flush accounting, and release after
disconnects/remote errors. Remote Hub identity responses and network endpoints
are fixtures; these tests do not establish deployed credentials, browser UX,
cross-user workflow isolation, live CA trust or operational ingress behavior.
