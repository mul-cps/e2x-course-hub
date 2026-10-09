The administration console reads an operator status from a mounted JSON file.
Set `CPS_PLATFORM_STATUS_PATH` to its absolute path, for example
`/etc/cps-platform-status/status.json`. Mount the ConfigMap as a directory rather
than a `subPath` file so operator updates reach the running service.

The authenticated `GET /api/platform-status` route uses the same current Hub
administrator authorization as the compute administration route. It returns
`Cache-Control: no-store`. It exposes no write operation and accepts no browser
source or availability override. The service prefix still applies when mounted
under JupyterHub.

Use exactly these fields and refresh `updatedAt` while qualification continues:

```json
{
  "version": 1,
  "state": "qualification",
  "title": "GPU group sharing: qualification in progress",
  "detail": "Workspace startup is blocked. Standard GPU profiles remain disabled.",
  "updatedAt": "2026-10-09T12:00:00Z",
  "checks": [
    {"id": "applications", "label": "Applications", "state": "passed"},
    {"id": "startup", "label": "Workspace startup", "state": "blocked"},
    {"id": "cleanup", "label": "Cleanup", "state": "pending"}
  ]
}
```

The accepted states are `qualification`, `limited-pilot`, `enabled`, and
`unavailable`. Check states are `pending`, `passed`, or `blocked`. A record expires
15 minutes after its UTC timestamp; timestamps more than 30 seconds in the future
are rejected. Missing, malformed, duplicate, oversized, nonregular, or stale
records return explicit `unavailable` status. An `enabled` record requires at
least one check and every check must have passed. This display records the
operator's assessment; it does not qualify or enable a compute profile.

Compute access and Shared workspaces display the same compact status panel,
including blocked, remaining, and passed checks. Each panel refreshes after
20 seconds, aborts a request after 8 seconds, and cancels polling on navigation.
A failed refresh replaces the previous display with `unavailable`.
