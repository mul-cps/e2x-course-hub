"""Fresh owning-Hub authority for an explicitly adopted legacy runtime role."""

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlencode
from uuid import UUID

from tornado.httpclient import HTTPClientError, HTTPRequest

from .account_adoption import validate_receipt


class LegacyEnrollmentMissing(PermissionError):
    """A Hub 404 is compatible only with an owner lacking a runtime role."""


def descriptor(binding):
    return {key: binding[field] for key, field in (
        ("workspace", "id"), ("course", "course_id"), ("term", "term_id"),
        ("group", "group_id"), ("server", "hub_server"),
        ("namespace", "namespace"), ("pod", "pod"))}


def _utc(value):
    if (not isinstance(value, str) or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", value) is None):
        raise PermissionError("Actual UTC Hub custody timestamp required")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PermissionError("Actual UTC Hub custody timestamp required") from error


def validate_custody(custody, *, source, binding, user, receipt, now=None):
    """Validate a closed response; the caller must fetch it from the owning Hub."""
    if not isinstance(receipt, dict) or receipt.get("kind") != "reviewed_legacy_adoption":
        raise PermissionError("Persistent reviewed legacy Source ownership required")
    identity = validate_receipt(receipt, binding, source)
    owner = binding["hub_user"]
    role_name = "cps-workspace-runtime-" + owner
    if (source not in {"cps", "cit"} or binding["hub_server"] != "rtc"
            or not isinstance(custody, dict)
            or set(custody) != {"version", "kind", "source", "descriptor", "identity", "runtime_role", "checked_at"}
            or type(custody.get("version")) is not int or custody["version"] != 1
            or custody["kind"] != "hub_legacy_runtime_custody" or custody["source"] != source
            or custody["descriptor"] != descriptor(binding)
            or not isinstance(identity, dict) or set(identity) != {"name", "created"}
            or not isinstance(user, dict)
            or {key: user.get(key) for key in ("name", "created")} != identity
            or not isinstance(user.get("roles"), list) or role_name not in user["roles"]):
        raise PermissionError("Exact reviewed legacy Source identity and Hub custody required")
    current = custody["identity"]
    runtime = custody["runtime_role"]
    if (not isinstance(current, dict) or set(current) != {"id", "name", "created"}
            or type(current.get("id")) is not int or current["id"] <= 0
            or {key: current.get(key) for key in ("name", "created")} != identity
            or current["name"] != owner
            or ("id" in user and (type(user["id"]) is not int or user["id"] != current["id"]))
            or not isinstance(runtime, dict) or set(runtime) != {"id", "name", "incarnation", "scopes"}
            or type(runtime.get("id")) is not int or runtime["id"] <= 0
            or runtime["name"] != role_name
            or runtime["scopes"] != ["access:servers!server=" + owner + "/rtc", "users:activity!user=" + owner]
            or not isinstance(runtime["incarnation"], str)):
        raise PermissionError("Current immutable legacy identity and exact runtime role required")
    try:
        if str(UUID(runtime["incarnation"])) != runtime["incarnation"]:
            raise ValueError("Noncanonical incarnation")
    except ValueError as error:
        raise PermissionError("Current canonical Hub runtime role incarnation required") from error
    _utc(current["created"])
    checked = _utc(custody["checked_at"])
    age = ((now if now is not None else datetime.now(timezone.utc)) - checked).total_seconds()
    if not -5 <= age <= 60:
        raise PermissionError("Fresh owning Hub runtime custody required")
    return role_name


async def fetch_custody(hub, binding):
    """Never reuse, redirect, or assert Source identity in the Hub query."""
    url = hub.api_url.rstrip("/") + "/cps/legacy-workspace-runtime-custody?" + urlencode(
        {"owner": binding["hub_user"], **descriptor(binding)})
    try:
        response = await hub.client.fetch(HTTPRequest(url, method="GET",
            headers={**hub.auth_header, "Cache-Control": "no-store"}, follow_redirects=False))
    except HTTPClientError as error:
        if error.code == 404:
            raise LegacyEnrollmentMissing("Hub has no legacy runtime enrollment") from error
        if error.code in (401, 403):
            raise PermissionError("Current owning Hub legacy runtime custody unavailable or revoked") from error
        raise
    if response.code != 200:
        raise PermissionError("Exact successful owning Hub custody response required")
    try:
        return json.loads(response.body)
    except (ValueError, UnicodeDecodeError) as error:
        raise PermissionError("Closed owning Hub custody JSON required") from error
