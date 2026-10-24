# Managed Source deletion qualification

Managed mode now supports deleting unreferenced local courses, terms and groups. Other record deletions remain denied until their controlled lifecycle is covered. Legacy mode remains available with its existing response status.

The Source prepares an authenticated admission fence before its SQL transaction, compares the original record and references again inside that transaction, and atomically retains the deletion result with the audit. Exact retries use the retained original record for current authorization. A lost completion response can initially return an error; retry completes the same transition without another deletion. This does not provide automatic background reconciliation.

Every managed operation rechecks the Source authority row, database path, console and service origin. Missing or changed custody fails closed without reconstruction.

Verification on 2026-10-24: complete admin suite 97 passed and 38 subtests passed; companion compute native-pool suite 437 passed. Cross-repository tests cover normal completion and lost replies before and after completion for both put and delete, with database reopen, one mutation audit and retained global reservation. Concurrent preimage/reference changes and authority loss prevent deletion; an unauthorized HTTP retry remains forbidden.

This is source qualification only. Active-head finalization remains unavailable; runtime activation, complete writer coverage and production qualification are still pending.
