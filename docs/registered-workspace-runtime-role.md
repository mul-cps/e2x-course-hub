# Registered workspace runtime role

This narrow change preserves Source `55c9a8865c462e4baeb306965fb1a6ed5da07dbd` course-start exclusion and accepts the computed `cps-workspace-runtime-<owner>` model role only when `<owner>` equals the existing validated seven-field descriptor's derived owner. Legacy owners cannot claim this role. Control roles, other owners' runtime roles and unrelated roles remain rejected.

The permanent Hub POST remains the authority for the immutable creation receipt, actual UserID/creation time, actual runtime/control role IDs and scopes, mapped owning Service and bound role-free course group. Source does not grant roles or infer privilege from a name. The companion Hub extension supplies exactly the registered owner's RTC access and activity scopes while retaining the fixed zero-scope kernel role, and assigns lifecycle/share/group management solely to the owning Source Service. The v1 identity receipt and Source database schema remain unchanged.

Tests cover normal permanent account preparation with the matching runtime role, unchanged authoritative receipt, denial of control/foreign/unregistered role names, and the existing account and course-start concurrency contracts. Actual live deployment and notebook acceptance are separate operator gates.
