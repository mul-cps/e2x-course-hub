# Legacy workspace runtime custody acceptance

This documents the Source acceptance protocol; it does not establish live
deployment or qualification.

## Preconditions

Acceptance requires a ready, persisted `reviewed_legacy_adoption` receipt and an
exact permanent binding for the bound workspace row. The existing Hub identity
must retain the receipt's unchanged `name` and `created` values. Adoption is a
separately controlled operation, not a consequence of a read or ensure request.

## Fresh owning-Service proof

Source queries every ready, validated `reviewed_legacy_adoption` legacy row,
including rows whose runtime role has been removed. For each acceptance, it
makes a fresh authenticated GET as its owning Source Service to `hub.api_url + '/cps/legacy-workspace-runtime-custody?'`.
The query contains exactly these fields, derived from the bound row:
`owner`, `workspace`, `course`, `term`, `group`, `server`, `namespace`, `pod`.
The descriptor has the same fields except `owner`: `workspace`, `course`, `term`,
`group`, `server`, `namespace`, `pod`.
Do not send `source` or another asserted-authority query parameter. Send
`Cache-Control: no-store`, refuse redirects, and never cache or reuse the proof.

HTTP 200 requires the current user to list the exact runtime role and the
following validated closed JSON shape. Reject missing or additional keys at
every object level:

- Top level: `version`, `kind`, `source`, `descriptor`, `identity`,
  `runtime_role`, `checked_at`.
- `version`: integer `1`; `kind`: `hub_legacy_runtime_custody`;
  `source`: exactly the owning Source, `cps` or `cit`.
- `descriptor`: exactly the seven descriptor fields listed above, matching the
  bound row without drift.
- `identity`: exactly `id` (positive integer), `name`, `created` (UTC ISO timestamp).
- `runtime_role`: exactly `id` (positive integer), `name`, `incarnation`, `scopes`.
  Its name is `cps-workspace-runtime-<owner>`; incarnation is a canonical UUID.
  Scopes equal exactly, in this order:
  `['access:servers!server=<owner>/rtc', 'users:activity!user=<owner>']`.
- `checked_at`: UTC ISO timestamp no more than 60 seconds old and no more than
  5 seconds in the future at validation time.

The receipt's identity `name` and `created` must still match. Hub enrollment
checks the immutable identity UID; Source additionally compares `id` when its
normal Hub identity model includes it. Names alone never establish custody.

## Hub enrollment authority

The permanent enrollment endpoint authenticates the actual mapped Service.
Hub validates the current immutable identity UID, runtime role ID and name,
incarnation, exact scopes, membership, and descriptor against the permanent
binding. Existing enrollment with missing, revoked, or recreated identity/role
custody returns HTTP 403. No enrollment row returns HTTP 404 after validation of
the trusted Source and descriptor.
Every drift is denied; a matching name cannot substitute for the bound object.

## Source behavior boundaries

Custody validation itself performs no grants, role creation, or Hub account
creation; existing separately controlled lifecycle behavior remains in place.
Source rechecks the permanent row after the awaited fetch; any binding
change denies acceptance. There is no proof cache.

HTTP 404 indicates missing enrollment, or an old Hub lacking this endpoint. It
allows only the unchanged old model when the user has no legacy runtime role;
any present runtime role is denied. HTTP 403 indicates revoked or invalid
enrolled state and always denies, even if the current user no longer lists the
runtime role. Other failures and malformed, stale, redirected, or unavailable
proofs have no fallback acceptance path. The computed registered-CPU hash path
is unchanged.
