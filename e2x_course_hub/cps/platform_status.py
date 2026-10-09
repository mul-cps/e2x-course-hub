"""Read-only, operator-published GPU sharing status; no qualification inference.

Mount a ConfigMap JSON file and set CPS_PLATFORM_STATUS_PATH. A record expires
15 minutes after updatedAt; update the timestamp while qualification continues.
No browser parameter can choose the path or change a status/check.
"""
from datetime import datetime, timedelta, timezone
import json
import os
import re
import stat

from tornado import web
from .handlers import RecordsHandler

MAX_BYTES = 16384
MAX_AGE = timedelta(minutes=15)
STATES = {'qualification', 'limited-pilot', 'enabled', 'unavailable'}
CHECK_STATES = {'pending', 'passed', 'blocked'}
UTC_TIMESTAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z')


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
    unavailable = {'version': 1, 'state': 'unavailable', 'title': 'GPU group sharing: status unavailable',
        'detail': 'A current operator status is unavailable. Availability has not been confirmed.',
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


class PlatformStatusHandler(RecordsHandler):
    SUPPORTED_METHODS = ('GET', 'HEAD', 'OPTIONS')
    @web.authenticated
    async def get(self):
        await self.authorize()
        self.set_header('Cache-Control', 'no-store')
        self.write(read_status())


default_handlers = [(r'/api/platform-status', PlatformStatusHandler)]
