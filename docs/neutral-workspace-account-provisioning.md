# Permanent neutral workspace accounts

New Source accounts use the installed `cps-compute-hub` permanent registry through
`POST /hub/api/cps/workspace-accounts`. The request uses the owning Source's actual
configured Hub Service API token with `custom:cps:provision:workspace-account`.
Hub derives `cps` from its trusted `cps-admin` Service mapping, or `cit` from
`cit-admin`; callers cannot supply the Source, owner, role, permissions or members.
No ordinary users POST or `admin:users` grant is required.

The closed body has exactly `workspace`, `course`, `term`, `group`, `server`,
`namespace`, and `pod`, copied from the full administrator-controlled Source
binding. Before creating a new account, Source requires the bound owner to equal:

```python
source + '-ws-' + sha256((source + '\0' + workspace).encode('utf-8')).hexdigest()[:32]
```

The descriptor hash is SHA256 of UTF-8 JSON for `dict(source=source, **body)`, with
`sort_keys=True`, `separators=(',', ':')` and `ensure_ascii=True`. Source also
preserves its exact Hub API URL, fixed kernel group and role in its permanent
local binding. Changing any owner/workspace/course/term/group/server/namespace/
Pod or endpoint binding requires reconciliation, never automatic reassignment.
Canonical human/alias collisions and use of the kernel group as a course
membership group remain prohibited.

Hub atomically creates a non-admin user with only its ordinary user role and
membership in `cps-workspace-kernel-accounts`, whose only role is the fixed
zero-scope `cps-workspace-kernel`. It commits the permanent registry receipt in
the same transaction. Source validates and stores the complete authoritative
receipt: version integer `1`, kind `hub_neutral_creation`, source, workspace,
owner, descriptor hash, identity `{id, name, created}`, fixed group and role.
The identity ID must be a positive integer and the creation timestamp a Hub UTC
timestamp. A receipt cannot be replaced, shortened or inferred from a user GET.

Source's durable stages are `hub_intent -> created -> ready`. After a POST timeout,
crash, or local receipt persistence failure, retry the same binding through the
same POST endpoint. Hub returns the original receipt only while its identity and
minimum permissions still match. Every subsequent `ensure()` also rechecks that
permanent receipt through Hub. Source performs no group grant for these receipts;
removed membership, role changes, privilege changes, deletion, recreation, or a
different identity ID fail closed. Keep the accounts, receipts and files during
recovery. Source audit uses the POST result without issuing a speculative GET to
the provisioning endpoint.

Existing adopted owners, `source_creation` receipts and successful original v7
`{name, created}` receipts retain their IDs, provenance and lifecycle behavior.
Unknown legacy ordinary POST `intent` rows stay fenced; they cannot use the new
idempotent retry path. [Reviewed legacy adoption](reviewed-workspace-account-adoption.md)
remains an explicit administrator workflow with retained storage and person bindings.
The current qualified legacy owners `cpsnativegroupa`, `cpsnativegroupb`, and
`citnativegroupa` are unchanged.

Provisioning creates no server, storage, Shares, compute-profile grant or GPU
authorization. New accounts remain stopped after provisioning/preparation.
An ordinary CPU Start still requires the existing profile, membership, storage
and filtered Hub scope gates. Native GPU execution needs separate qualification;
this change adds no owners to any GPU allowlist and does not change MIG policy.
Hub registry migration, backups, actual Service scope validation and live runtime
qualification are separate deployment gates.

The compatibility gate imports the real reviewed `cps_compute_hub.neutral_accounts`
implementation, compares both Sources' owner/hash algorithms and uses the actual
Hub ORM transaction/receipt/revocation behavior with isolated fictional databases:

```sh
/path/to/installed-source-and-hub-env/bin/python \
  -m unittest discover -s tests -p hub_contract_workspace_accounts.py -q
```

Run this against reviewed noneditable Source and Hub wheels as well as the full
Source suite. It qualifies the packaged contract locally, not a live deployment.
