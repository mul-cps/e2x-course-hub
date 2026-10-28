"""Private, read-only observation of an installed managed Source process."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
import uuid

from tornado import web


PROTOCOL = 'e2x-cps-source-runtime-observation/v1'
MAX_HISTORY = 32
PAGE_PROTOCOL = 'e2x-cps-source-runtime-history-page/v1'
_PROCESS = (os.getpid(), str(uuid.uuid4()))


def _process_id():
    # Continuity only, not a Kubernetes process attestation or activation grant.
    global _PROCESS
    if _PROCESS[0] != os.getpid():
        _PROCESS = (os.getpid(), str(uuid.uuid4()))
    return _PROCESS[1]


class HistoryChanged(ValueError):
    pass

_DIGEST = re.compile(r'^sha256:[a-f0-9]{64}$')
_JOURNAL_STATES = frozenset({'prepared', 'armed', 'completed', 'stopped',
    'hub-completed', 'control-completed', 'source-completed', 'effective'})
_SOURCE_HISTORY_DOMAIN = b'e2x-cps-source-receipt-set/v1\0'
_HUB_HISTORY_DOMAIN = b'e2x-cps-hub-operation-set/v1\0'


def _history_ref_hash(domain, rows, *, journal=False):
    digest = hashlib.sha256(domain)
    count = 0
    import json
    for row in rows:
        row = tuple(row)
        if len(row) != (3 if journal else 2):
            raise ValueError('Managed Source history malformed')
        if not _uuid(row[0]) or not _digest(row[1]):
            raise ValueError('Managed Source history malformed')
        if len(row) == 3 and row[2] not in _JOURNAL_STATES:
            raise ValueError('Managed Source journal history malformed')
        encoded = json.dumps(list(row), ensure_ascii=False,
            separators=(',', ':')).encode('utf-8')
        digest.update(len(encoded).to_bytes(8, 'big') + encoded)
        count += 1
    return count, 'sha256:' + digest.hexdigest()


def _uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _digest(value):
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _local_observation(provider, console, *, page=None):
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
        if 'pool_source_receipt' not in tables:
            raise ValueError('Managed Source receipt history missing')
        receipt_count, receipt_hash = _history_ref_hash(
            _SOURCE_HISTORY_DOMAIN,
            db.execute('SELECT transition_id,input_sha256 FROM pool_source_receipt '
                'ORDER BY transition_id'))
        receipts = [dict(transition_id=row[0], input_sha256=row[1])
            for row in reversed(db.execute('SELECT transition_id,input_sha256 '
                'FROM pool_source_receipt ORDER BY rowid DESC LIMIT ?',
                (MAX_HISTORY,)).fetchall())]

        operations = []
        if 'hub_journal_operation' in tables:
            from .hub_journal import SourceHubJournal
            journal = getattr(provider, 'hub_journal', None)
            if (type(journal) is not SourceHubJournal or journal.provider is not provider
                    or journal._db is not db):
                raise ValueError('Managed Source journal owner unavailable')
            # Reuse the installed journal's complete original codec/schema check
            # inside this read transaction; do not call an API that opens another.
            journal._validate()
            operation_count, operation_hash = _history_ref_hash(
                _HUB_HISTORY_DOMAIN,
                db.execute('SELECT operation_id,input_sha256,state FROM '
                    'hub_journal_operation ORDER BY operation_id'), journal=True)
            operations = [dict(operation_id=row[0], input_sha256=row[1], state=row[2])
                for row in reversed(db.execute('SELECT operation_id,input_sha256,state '
                    'FROM hub_journal_operation ORDER BY rowid DESC LIMIT ?',
                    (MAX_HISTORY,)).fetchall())]
        else:
            if getattr(provider, 'hub_journal', None) is not None:
                raise ValueError('Managed Source journal schema missing')
            operation_count, operation_hash = _history_ref_hash(
                _HUB_HISTORY_DOMAIN, (), journal=True)
        truncated = receipt_count > MAX_HISTORY or operation_count > MAX_HISTORY
        result = {
            'source': {'console': console, 'database_id': identity[0][2],
                'source_path_sha256': 'sha256:' + hashlib.sha256(source_path.encode()).hexdigest(),
                'user_version': user_version, 'schema_sha256': schema_hash},
            'retained': {'source_receipts': receipts,
                'hub_operations': operations, 'truncated': truncated,
                'source_receipt_count': receipt_count,
                'source_receipt_set_sha256': receipt_hash,
                'hub_operation_count': operation_count,
                'hub_operation_set_sha256': operation_hash},
            'guard': guard,
        }
        if page is not None:
            kind, offset, expected_count, expected_digest = page
            if kind == 'source_receipts':
                count, digest = receipt_count, receipt_hash
                fields, table, identity = ('transition_id', 'input_sha256'), 'pool_source_receipt', 'transition_id'
            else:
                count, digest = operation_count, operation_hash
                fields, table, identity = ('operation_id', 'input_sha256', 'state'), 'hub_journal_operation', 'operation_id'
            if (count, digest) != (expected_count, expected_digest):
                raise HistoryChanged('Retained Source history changed')
            # Both the full-set commitment and this canonical page come from
            # the same read transaction. SQL identifiers are fixed above.
            records = [] if count == 0 else [dict(zip(fields, row)) for row in db.execute(
                'SELECT ' + ','.join(fields) + ' FROM ' + table + ' ORDER BY ' + identity + ' LIMIT ? OFFSET ?',
                (MAX_HISTORY, offset))]
            result.pop('retained')
            result.update(process_id=_process_id(), page=dict(kind=kind, offset=offset,
                count=count, set_sha256=digest, records=records,
                next_offset=offset + len(records) if offset + len(records) < count else None))
        return result
    finally:
        db.rollback()


def build_runtime_observation(provider, console, *, page=None):
    """Read actual local Source custody and retained history in one SQL snapshot."""
    local = _local_observation(provider, console, page=page)
    guard = local.pop('guard')
    return {'protocol': PAGE_PROTOCOL if page is not None else PROTOCOL, 'version': 1, **local,
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
        if self.request.body:
            raise web.HTTPError(400, 'Empty observation request body required')
        authorization = self.request.headers.get('Authorization', '')
        source = self.request.headers.get('X-CPS-Source', '')
        expected = 'Bearer ' + token
        if (source != console or not hmac.compare_digest(
                authorization.encode('utf-8'), expected.encode('ascii'))):
            raise web.HTTPError(403, 'Source service authentication required')
        page = None
        if self.request.query:
            args = self.request.query_arguments
            fields = {'kind', 'offset', 'expected_count', 'expected_set_sha256'}
            if set(args) != fields or any(len(values) != 1 for values in args.values()):
                raise web.HTTPError(400, 'Closed retained history page query required')
            kind, offset, count, digest = (self.get_query_argument(key) for key in
                ('kind', 'offset', 'expected_count', 'expected_set_sha256'))
            if (kind not in ('source_receipts', 'hub_operations') or not _digest(digest)
                    or any(re.fullmatch(r'0|[1-9][0-9]{0,18}', value) is None for value in (offset, count))):
                raise web.HTTPError(400, 'Canonical retained history page query required')
            offset, count = int(offset), int(count)
            if offset % MAX_HISTORY or not (offset < count or offset == count == 0):
                raise web.HTTPError(400, 'Canonical retained history page offset required')
            page = kind, offset, count, digest
        try:
            result = build_runtime_observation(self.settings.get('course_provider'), console, page=page)
        except HistoryChanged:
            raise web.HTTPError(409, 'Retained Source history changed') from None
        except (ValueError, TypeError, KeyError, sqlite3.Error, OSError):
            raise web.HTTPError(503, 'Source runtime observation unavailable') from None
        self.write(result)
