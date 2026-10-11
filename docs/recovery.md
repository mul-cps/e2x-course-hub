# Connected recovery acceptance

`python -m e2x_course_hub.cps.recovery` composes the existing Hub and console
backups, then checks a restored CPS **and** CIT application through authenticated
GETs. It never invokes Kubernetes, restores a database, starts a notebook,
creates a credential, or changes an application. Importing it performs no work;
each command requires `--execute`.

Use the existing `backup-hubs.py`, `backup-console.py` and
`restore-hub-backups.py` from the cluster repository. Do not copy live SQLite
files: use the existing online SQLite backup, which includes committed WAL
pages. The composed directory retains the original Hub filenames and checksum
manifest format, so the existing isolated PostgreSQL restore accepts it.

All inputs and results must be operator-owned, outside Git, with directory
mode 0700 and regular file mode 0600. Symlinks are rejected. The program prints
only acceptance flags; configurations, database rows, role password hashes and
browser cookies stay in private files. Use a new result path each time.

## Capture actual PostgreSQL comparisons

Prepare one query per source. Run it with the existing trusted database access,
`psql -X -qAt -v ON_ERROR_STOP=1`, redirecting stdout to a private JSON file.
The query runs in a repeatable-read **read-only** transaction. It returns only
hashes of all application table/sequence rows, structural schema, role
properties, exact role memberships, and resolved object ownership/ACLs.
It requires the database administrator because it hashes `pg_authid` in place;
role passwords are never emitted.

```sh
python -m e2x_course_hub.cps.recovery postgres-sql --execute \
  --source cps --output /PRIVATE/cps-source-fingerprint.sql
python -m e2x_course_hub.cps.recovery postgres-sql --execute \
  --source cit --output /PRIVATE/cit-source-fingerprint.sql
```

For the restored database, prepare the same source query with
`--bootstrap-role restore_<16 hex digits>` **only** when using the existing
rootless PostgreSQL restore tool's exact random bootstrap role. It excludes
that single role and its memberships, rather than all similarly named roles.
No other exclusions are accepted. The source query excludes nothing. Run the
restored query before that tool removes its isolated container, or against the
database restored for the connected exercise **before** any Hub/console
startup, browser login or Start writes. Retain this baseline: normal login and
Start legitimately change Hub tokens, activity and server rows afterward.
SQL execution is a separate
operator action; the CPU tests cover query construction, not PostgreSQL
execution.

`LogicalRestoreProbe` supplies a callback for the existing restore loop. The
cluster tool owner can add an optional `fingerprint_probe=None` keyword to
`restore` and call it after selecting each restored database, before its
existing `finally` removes the unique container:

```python
if fingerprint_probe is not None:
    fingerprint_probe(ns, user, lambda query: sql(
        'psql', '-X', '-qAt', '-d', databases[0], '-v', 'ON_ERROR_STOP=1', data=query))
```

This retains the existing exact images, network-none rootless containers,
restore commands, private error handling and cleanup. It executes only the
callback's fixed read-only SQL; no second PostgreSQL restore implementation is
introduced. With that reviewed optional hook, the bounded operator invocation
is:

```python
from e2x_course_hub.cps.recovery import LogicalRestoreProbe
probe = LogicalRestoreProbe('/PRIVATE/logical-fingerprints')
existing_restore_tool.restore('/PRIVATE/hub-bundle', '/PRIVATE/logical-restore',
                              fingerprint_probe=probe)
probe.finish()
```

Omitting source fingerprints records actual dumped-DB observations with
`source_comparison_passed: false`. To compare a new matching capture, construct
the probe with a second argument `{'cps': '/PRIVATE/cps-source.json',
'cit': '/PRIVATE/cit-source.json'}`. Both hashes must match before a receipt is
written; partial or foreign-scope runs cannot pass. The callback alone never
changes `connected_restore_passed`, coordination or production qualification.

## Compose the matching private inputs

The private capture input is a closed version-1 JSON object:

```json
{
  "version": 1,
  "capture_id": "11111111-1111-4111-8111-111111111111",
  "hub_bundle": "/PRIVATE/existing-hub-backup",
  "sources": {
    "cps": {
      "console_bundle": "/PRIVATE/existing-cps-console-backup",
      "config": "/PRIVATE/cps-config.py",
      "server_config": "/PRIVATE/cps-config.yml",
      "hub_fingerprint": "/PRIVATE/cps-source-fingerprint.json",
      "actor": "cps-admin-actor",
      "owner": "cps-restore-fixture-owner",
      "server": "rtc",
      "course_id": "existing-local-course",
      "notebook_image": "registry/notebook@sha256:<64 hex digits>",
      "storage": {
        "pvc_uid": "original-cps-pvc-uid",
        "pv_uid": "original-cps-pv-uid",
        "nfs_server": "source-nas",
        "nfs_path": "/source/cps-store",
        "mount_path": "/home/jovyan/project",
        "sentinel_path": "project/existing-saved-file",
        "sentinel_sha256": "<64 hex digits>"
      }
    },
    "cit": "the same closed object using distinct actual CIT inputs"
  }
}
```

The CIT value must be a real object; the text above abbreviates it. Every actor,
course, owner, storage identity and image must come from the respective actual
source. The console manifest binds its application image and central policy;
the Hub image is extracted from its original running-image capture. The saved
sentinel must predate the backup and reside beneath the captured store mount.

Run with the independently reviewed original Hub validator and its raw SHA256:

```sh
python -m e2x_course_hub.cps.recovery capture --execute \
  --input /PRIVATE/capture-inputs.json --output /PRIVATE/composed-capture \
  --hub-validator /PATH/restore-hub-backups.py \
  --hub-validator-sha256 f302e6a28d81d52f0ec2fa51ee227509bb153b756b02be31feee4ac9bfcadf18
```

The 2026-10-11 completed Hub backup can supply `hub_bundle`; it remains a
sequential capture. It cannot be retroactively declared coordinated.

An optional `writer_window` object has exactly `before` and `after` private
JSON paths. Each snapshot has `captured_at` (UTC) and `writers`, with four keys:
`cps_hub`, `cit_hub`, `cps_console`, `cit_console`. Each contains the full raw
`deployment` and namespace-wide `pods` List from the operator's read-only
capture. Expected Deployments are `hub` in `jupyterhub`/`cit-jhub` and
`cps-admin`/`cit-admin` in `cps-compute`. Both observations must show replicas
zero, the current observed generation, no selector-matching Pod, and unchanged
UID/generation/full spec. The bounded window is at most ten minutes and must
bracket each backup/fingerprint timestamp. Operators must retain exclusive
writer custody and begin the existing backup commands after the first fence;
two observations do not establish custody by themselves. This records
application writer exclusion and **never** global database/NFS atomicity.
Without these actual observations `coordinated_capture` stays false.

## Check an isolated connected restore

Restore matching images, Hub PostgreSQL/config/roles/ACLs, and separate console
SQLite/configs into `restore-cps-<first 8 capture UUID characters>` and
`restore-cit-<first 8 characters>`. Use independent clones of the source NFS
stores. Production namespaces, original PVC/PV UIDs, NFS aliases, ancestor or
descendant source directories, extra source PVCs/direct NFS mounts and GPU
resources are rejected. Configure isolated routing and egress so restored
OAuth, Hub API, database and storage traffic cannot reach production; retain
the original mounted config bytes with isolated DNS/routing where needed.
The verifier cannot establish network isolation from an origin string alone.

In each isolated application, perform a real browser login and a normal CPU
named-server start. Existing authenticated browser cookies must cover the Hub,
admin service and notebook URLs. Do not mint a bearer token for this check.
Export each browser's existing Netscape cookie jar privately. Bind each
loopback port-forward to that isolated Hub's captured Pod; use distinct literal
`127.0.0.1` or `[::1]` origins with explicit ports. No redirects, proxies,
production DNS names or Authorization header fallback are used.

The private targets JSON has exactly `cps` and `cit`. Each value has:

| Field | Actual restored input |
| --- | --- |
| `namespace` | Exact isolated namespace above |
| `origin` | Distinct `http://127.0.0.1:PORT` or verified HTTPS loopback origin |
| `cookie_jar` | Private existing authenticated browser cookies |
| `console_database` | Fresh online SQLite snapshot of the restored console |
| `config`, `server_config` | Mounted restored config snapshots |
| `hub_fingerprint` | Fixed query output from that restored Hub database |
| `hub_pod`, `console_pod` | Full actual Running/Ready Pod JSON, pinned image IDs |
| `pod`, `pvc`, `pv` | Full actual named CPU notebook Pod and cloned store objects |

```sh
python -m e2x_course_hub.cps.recovery verify --execute \
  --bundle /PRIVATE/composed-capture --input /PRIVATE/restore-targets.json \
  --output /PRIVATE/connected-restore-receipt.json
```

Local comparisons run before any HTTP request. Both sources must then pass
`/hub/api/user`, their normal admin courses API, the owning user's ready named
server API, and a notebook Contents API read of the preexisting sentinel.
The restored row/schema/role/ACL fingerprints and mounted configs must match;
the sentinel checksum and exact new PVC/PV claim chain must agree. This checks
an observed running server, rather than issuing or claiming to observe the
Start action itself. Failure exits 1 and emits no credential-bearing body.
Success sets `connected_restore_passed: true`, retains
`productionQualified: false`, and preserves the capture's coordination flag.
Off-host recovery, complete NFS data recovery and production promotion remain
separate acceptance gates.
