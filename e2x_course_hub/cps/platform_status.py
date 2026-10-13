"""Independent operator qualification and source-owned live GPU access observations.

Mount a ConfigMap JSON file and set CPS_PLATFORM_STATUS_PATH. A record expires
15 minutes after updatedAt; update the timestamp while qualification continues.
No browser parameter can choose the path or change a status/check.
"""
import asyncio
import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone

from tornado import web

from .handlers import RecordsHandler

MAX_BYTES = 16384
MAX_AGE = timedelta(minutes=15)
STATES = {'qualification', 'limited-pilot', 'enabled', 'unavailable'}
CHECK_STATES = {'pending', 'passed', 'blocked'}
UTC_TIMESTAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z')
AVAILABILITY_TIMEOUT = 5
_REVISION = re.compile(r'sha256:[a-f0-9]{64}')
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}')
_PROFILE = re.compile(r'interactive-shared-[1-9][0-9]{0,2}')
_APPROVAL_FIELDS = {'owner', 'server', 'members', 'principal', 'profiles', 'ceiling',
    'namespace', 'pod', 'group_id', 'course_id', 'term_id', 'storage'}
_STORAGE_FIELDS = {'namespace', 'pvc', 'pv', 'path', 'group_hash',
    'storage_version', 'readonly_mounts'}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('Duplicate status field')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('Non-JSON status constant')


def _text(value, limit):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit and all(
        ord(char) >= 32 or char in '\n\t' for char in value)


def validate_status(document, *, now):
    if (not isinstance(document, dict) or set(document) != {'version', 'state', 'title', 'detail', 'updatedAt', 'checks'}
            or type(document['version']) is not int or document['version'] != 1
            or not isinstance(document['state'], str) or document['state'] not in STATES
            or not _text(document['title'], 200) or not _text(document['detail'], 2000)
            or not isinstance(document['updatedAt'], str) or not UTC_TIMESTAMP.fullmatch(document['updatedAt'])):
        raise ValueError('Invalid operator status')
    updated = datetime.fromisoformat(document['updatedAt'].replace('Z', '+00:00'))
    if now - updated > MAX_AGE or updated - now > timedelta(seconds=30):
        raise ValueError('Status timestamp is stale or future')
    checks = document['checks']
    if not isinstance(checks, list) or len(checks) > 32: raise ValueError('Bounded status checks required')
    identifiers = set()
    for check in checks:
        if (not isinstance(check, dict) or set(check) != {'id', 'label', 'state'}
                or not isinstance(check['id'], str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', check['id'])
                or check['id'] in identifiers or not _text(check['label'], 160)
                or not isinstance(check['state'], str) or check['state'] not in CHECK_STATES):
            raise ValueError('Invalid operator check')
        identifiers.add(check['id'])
    if document['state'] == 'enabled' and (not checks or any(check['state'] != 'passed' for check in checks)):
        raise ValueError('Enabled status requires passed checks')
    return document


def read_status(path=None, *, now=None):
    now = now or datetime.now(timezone.utc)
    unavailable = {'version': 1, 'state': 'unavailable',
        'title': 'Production qualification report: unavailable',
        'detail': ('A current operator qualification report is unavailable. '
                   'Broader production qualification remains unconfirmed.'),
        'updatedAt': now.isoformat(timespec='seconds').replace('+00:00', 'Z'), 'checks': []}
    path = path if path is not None else os.environ.get('CPS_PLATFORM_STATUS_PATH')
    if not path: return unavailable
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): return unavailable
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES: return unavailable
        document = json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
        return validate_status(document, now=now)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return unavailable


def _validate_availability(registry, source):
    """Check the known server contract before retaining aggregate non-personal fields."""
    if (
        source not in ('cps', 'cit')
        or not isinstance(registry, dict)
        or set(registry) != {'revision', 'policy_hash', 'ready', 'qualified_profiles', 'approvals'}
        or not isinstance(registry['revision'], str)
        or not _REVISION.fullmatch(registry['revision'])
        or not isinstance(registry['policy_hash'], str)
        or not _REVISION.fullmatch(registry['policy_hash'])
        or type(registry['ready']) is not bool
    ):
        raise ValueError('Invalid live approval registry')
    qualified = registry['qualified_profiles']
    if (
        not isinstance(qualified, list)
        or len(qualified) > 32
        or any(not isinstance(p, str) or not _PROFILE.fullmatch(p) for p in qualified)
        or len(set(qualified)) != len(qualified)
    ):
        raise ValueError('Invalid qualified profiles')
    approvals = registry['approvals']
    if not isinstance(approvals, dict) or len(approvals) > 32:
        raise ValueError('Bounded source approvals required')
    if len(json.dumps(registry, allow_nan=False).encode()) > 1024 * 1024:
        raise ValueError('Oversized live registry')
    profiles = set()
    namespace = {'cps': 'jupyterhub', 'cit': 'cit-jhub'}[source]
    for workspace, approval in approvals.items():
        if (
            not isinstance(workspace, str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', workspace)
            or not isinstance(approval, dict)
            or set(approval) != _APPROVAL_FIELDS
            or approval['principal'] != 'workspace:' + source + ':' + workspace
            or approval['namespace'] != namespace
            or any(
                not isinstance(approval[k], str) or not _NAME.fullmatch(approval[k])
                for k in ('owner', 'server', 'group_id', 'course_id', 'term_id', 'pod')
            )
            or not isinstance(approval['profiles'], list)
            or len(approval['profiles']) != 1
            or not isinstance(approval['profiles'][0], str)
            or approval['profiles'][0] not in qualified
        ):
            raise ValueError('Invalid source-owned approval')
        members = approval['members']
        if (
            not isinstance(members, list)
            or not 1 <= len(members) <= 64
            or any(not isinstance(p, str) or not re.fullmatch(r'[^\s]{1,256}', p) for p in members)
            or len(set(members)) != len(members)
            or approval['owner'] in members
        ):
            raise ValueError('Invalid bounded membership')
        ceiling = approval['ceiling']
        if (
            not isinstance(ceiling, dict)
            or set(ceiling) != {'cpu', 'memory', 'gpuMemoryGiB'}
            or not _text(ceiling['cpu'], 32)
            or not _text(ceiling['memory'], 32)
            or type(ceiling['gpuMemoryGiB']) is not int
            or not 1 <= ceiling['gpuMemoryGiB'] <= 1024
        ):
            raise ValueError('Invalid resource ceiling')
        storage = approval['storage']
        if (
            not isinstance(storage, dict)
            or set(storage) != _STORAGE_FIELDS
            or storage['namespace'] != namespace
            or any(not _text(storage[k], 4096) for k in ('pvc', 'pv', 'path', 'group_hash'))
            or type(storage['storage_version']) is not int
            or storage['storage_version'] not in (1, 2)
            or not isinstance(storage['readonly_mounts'], list)
            or len(storage['readonly_mounts']) > 32
        ):
            raise ValueError('Invalid bounded storage')
        for mount in storage['readonly_mounts']:
            if (
                not isinstance(mount, dict)
                or set(mount) != {'server', 'path', 'mount_path', 'read_only'}
                or mount['read_only'] is not True
                or any(not _text(mount[k], 4096) for k in ('server', 'path', 'mount_path'))
            ):
                raise ValueError('Invalid readonly mount')
        profiles.update(approval['profiles'])
    return {
        'state': 'ready' if registry['ready'] else 'pending',
        'revision': registry['revision'],
        'policyHash': registry['policy_hash'],
        'approvedProfiles': sorted(profiles),
        'approvedWorkspaceCount': len(approvals),
    }


async def read_availability(client, *, now=None):
    # The gateway computes ready from current authenticated process readbacks.
    # Its registry has no wall-clock field. checkedAt timestamps this fresh GET,
    # not the durable approval revision and not the operator qualification file.
    result = {
        'state': 'unavailable',
        'revision': None,
        'policyHash': None,
        'approvedProfiles': [],
        'approvedWorkspaceCount': 0,
    }
    if client is not None:
        try:
            registry = await asyncio.wait_for(client.native_group_approvals(), AVAILABILITY_TIMEOUT)
            result = _validate_availability(registry, client.console)
        except Exception:
            # This read-only status is best effort. Never return private error details.
            pass
    result['checkedAt'] = (
        (now or datetime.now(timezone.utc)).isoformat(timespec='seconds').replace('+00:00', 'Z')
    )
    return result


class PlatformStatusHandler(RecordsHandler):
    SUPPORTED_METHODS = ('GET', 'HEAD', 'OPTIONS')
    @web.authenticated
    async def get(self):
        await self.authorize()
        self.set_header('Cache-Control', 'no-store')
        report = read_status()
        availability = await read_availability(self.settings.get('compute_policy'))
        self.write({**report, 'availability': availability})


default_handlers = [(r'/api/platform-status', PlatformStatusHandler)]
