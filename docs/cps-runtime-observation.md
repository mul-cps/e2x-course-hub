# Private Source runtime observation

Status: implemented; not deployed. This endpoint supports the planned remote
production coordinator. It does not install managed mode or authorize pool
activation.

`GET <service_prefix>/internal/v1/runtime-observation` requires the configured
console's private `compute_policy_token` as `Authorization: Bearer ...` and an
exact matching `X-CPS-Source: cps|cit`. This is server-to-server authentication;
Hub browser/admin OAuth tokens cannot substitute for it. Unconfigured service
authentication returns 503; invalid credentials or console return 403. Requests
must have no query or body. Responses are marked `Cache-Control: no-store`.

The version-1 `e2x-cps-source-runtime-observation/v1` response contains:

- `source`: console, coordinator database UUID from the actual local authority
  binding, hash of the resolved Source path, schema version and schema hash.
- `writer_guard`: locally validated installation/binding flags and the bound
  coordinator database UUID. It does not report remote coordinator readiness.
- `retained`: up to 32 Source receipt references and 32 Hub-journal operation
  references (IDs/input hashes/states), with an explicit truncation flag.

Reads use the installed `LocalCourseProvider` and `PoolMutationRPCGuard` in a
single local read transaction. An absent/changed guard, malformed schema or
history, or an existing transaction fails closed with 503. No schema migration,
network call, enrollment, grant change or journal mutation occurs. Raw paths,
SQL data, receipt results, operation intents and tokens are not returned.

The history window is bounded, not evidence of complete backup custody. It must
not be used as proof that a workspace stopped, a GPU allowance was released or
a production activation completed. The coordinator must independently validate
its installed process/storage bindings and complete retained history.
