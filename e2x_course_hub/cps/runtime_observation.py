"""Private, read-only observation of an installed managed Source process."""
from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
import uuid

from tornado import web


PROTOCOL = 'e2x-cps-source-runtime-observation/v1'
MAX_HISTORY = 32
_DIGEST = re.compile(r'^sha256:[a-f0-9]{64}$')
_JOURNAL_STATES = frozenset({'prepared', 'armed', 'completed', 'stopped',
    'hub-completed', 'control-completed', 'source-completed', 'effective'})


def _uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _digest(value):
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _local_observation(provider, console):
    from .providers import LocalCourseProvider
    from .pool_heads import PoolMutationRPCGuard, SOURCE_SCHEMA

    if (type(provider) is not LocalCourseProvider or provider.console != console
            or type(provider.pool_guard) is not PoolMutationRPCGuard
            or provider.pool_guard.client.console != console):
        raise ValueError('Managed Source runtime unavailable')
    db = provider.db
    if db.in_transaction:
        raise ValueError('Source runtime observation requires clean connection')
    db.execute('BEGIN')
    try:
        provider._pool_mode_guard()
        installed = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB 'pool_source_*'"))
        if installed != SOURCE_SCHEMA:
            raise ValueError('Managed Source writer guard missing')
        path_rows = [row for row in db.execute('PRAGMA database_list') if row[1] == 'main']
        if len(path_rows) != 1 or not path_rows[0][2]:
            raise ValueError('Managed Source database path unavailable')
        from pathlib import Path
        source_path = str(Path(path_rows[0][2]).resolve(strict=True))
        guard = provider.pool_guard
        identity = [tuple(row) for row in db.execute(
            'SELECT console,origin,database_id,source_path FROM pool_source_authority')]
        expected = [(*guard.binding, source_path)]
        if len(identity) != 1 or identity != expected or not _uuid(identity[0][2]):
            raise ValueError('Managed Source authority changed')
        user_version = db.execute('PRAGMA user_version').fetchone()[0]
        if type(user_version) is not int or user_version < 1:
            raise ValueError('Managed Source schema version unavailable')
        schema_rows = [tuple(row) for row in db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "AND type IN ('table','index') AND sql IS NOT NULL ORDER BY type,name")]
        import json
        schema_raw = json.dumps(schema_rows, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        schema_hash = 'sha256:' + hashlib.sha256(schema_raw).hexdigest()

        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        receipts = []
        if 'pool_source_receipt' not in tables:
            raise ValueError('Managed Source receipt history missing')
        rows = db.execute('SELECT transition_id,input_sha256 FROM pool_source_receipt '
            'ORDER BY rowid DESC LIMIT ?', (MAX_HISTORY + 1,)).fetchall()
        truncated = len(rows) > MAX_HISTORY
        for transition_id, input_hash in reversed(rows[:MAX_HISTORY]):
            if not _uuid(transition_id) or not _digest(input_hash):
                raise ValueError('Managed Source receipt history malformed')
            receipts.append({'transition_id': transition_id, 'input_sha256': input_hash})

        operations = []
        if 'hub_journal_operation' in tables:
            rows = db.execute('SELECT operation_id,input_sha256,state FROM hub_journal_operation '
                'ORDER BY rowid DESC LIMIT ?', (MAX_HISTORY + 1,)).fetchall()
            truncated = truncated or len(rows) > MAX_HISTORY
            for operation_id, input_hash, state in reversed(rows[:MAX_HISTORY]):
                if (not _uuid(operation_id) or not _digest(input_hash)
                        or state not in _JOURNAL_STATES):
                    raise ValueError('Managed Source journal history malformed')
                operations.append({'operation_id': operation_id,
                    'input_sha256': input_hash, 'state': state})
        return {
            'source': {'console': console, 'database_id': identity[0][2],
                'source_path_sha256': 'sha256:' + hashlib.sha256(source_path.encode()).hexdigest(),
                'user_version': user_version, 'schema_sha256': schema_hash},
            'retained': {'source_receipts': receipts,
                'hub_operations': operations, 'truncated': truncated},
            'guard': guard,
        }
    finally:
        db.rollback()


def build_runtime_observation(provider, console):
    """Read actual local Source custody and retained history in one SQL snapshot."""
    local = _local_observation(provider, console)
    guard = local.pop('guard')
    return {'protocol': PROTOCOL, 'version': 1, **local,
        'writer_guard': {'installed': True, 'binding_valid': True,
            'coordinator': {'database_id': guard.database_id}}}


class RuntimeObservationHandler(web.RequestHandler):
    """Service-token-only route; browser OAuth identities are never accepted."""

    async def get(self):
        self.set_header('Cache-Control', 'no-store')
        token = self.settings.get('runtime_observation_token')
        console = self.settings.get('console_owner')
        if (type(token) is not str or not token or any(ord(c) < 33 or ord(c) > 126 for c in token)
                or console not in ('cps', 'cit')):
            raise web.HTTPError(503, 'Source runtime observation is not configured')
        if self.request.query or self.request.body:
            raise web.HTTPError(400, 'Empty bounded observation request required')
        authorization = self.request.headers.get('Authorization', '')
        source = self.request.headers.get('X-CPS-Source', '')
        expected = 'Bearer ' + token
        if (source != console or not hmac.compare_digest(
                authorization.encode('utf-8'), expected.encode('ascii'))):
            raise web.HTTPError(403, 'Source service authentication required')
        try:
            result = build_runtime_observation(self.settings.get('course_provider'), console)
        except (ValueError, TypeError, KeyError, sqlite3.Error, OSError):
            raise web.HTTPError(503, 'Source runtime observation unavailable') from None
        self.write(result)
