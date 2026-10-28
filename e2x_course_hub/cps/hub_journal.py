"""Internal durable Source evidence for managed ordinary Hub group operations.

This is a transaction primitive, never authorization, a dispatcher or a migration.
Task 4 must establish genuine first-installation custody before explicitly creating
SOURCE_JOURNAL_SCHEMA and setting Source user_version to 6. Existing providers
still accept only version 5. Construction never creates, adopts or repairs custody.
Task 3B must package the native codec and supply authenticated controller observations.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
import uuid

SOURCE_JOURNAL_VERSION = 1
SOURCE_DATABASE_VERSION = 6  # Independent marker survives loss of ALL journal tables.
SOURCE_JOURNAL_SCHEMA = {
    'hub_journal_authority': 'CREATE TABLE hub_journal_authority(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL CHECK(version=1),binding TEXT NOT NULL,manifest TEXT NOT NULL)',
    'hub_journal_operation': 'CREATE TABLE hub_journal_operation(operation_id TEXT PRIMARY KEY,input_sha256 TEXT NOT NULL,state TEXT NOT NULL,record TEXT NOT NULL)',
}
_GROUP_ACTIONS = frozenset(('user-create', 'group-create', 'group-users-add', 'group-users-remove', 'group-delete'))
_EFFECT_BOUND = 4 * 1024 * 1024
_RECEIPT_BOUND = 8 * 1024 * 1024
_SOURCE_COLUMNS = {
    'records': ['console', 'kind', 'id', 'payload'],
    'email_links': ['console', 'username', 'email', 'canonical_person_id', 'verified'],
}
_AUDIT_COLUMNS = ['actor', 'console', 'kind', 'target', 'previous', 'current', 'time', 'outcome']
_SENSITIVE = frozenset(('password', 'passwd', 'secret', 'token', 'api_token', 'api_key',
                        'authorization', 'credentials', 'credential', 'cookie', 'access_token',
                        'refresh_token', 'client_secret'))
_TICKET_FIELDS = {'transition_id', 'predecessor', 'operation', 'input_sha256', 'actor',
                  'instance', 'target', 'mutation_result', 'receipt', 'effect_sha256', 'database_id'}
_RECORD_FIELDS = {'operation_id', 'input_sha256', 'intent', 'source_preimage', 'hub_preimage',
                  'actor', 'ticket', 'binding', 'effect', 'effect_sha256', 'receipt',
                  'audit_rowid', 'audit_time'}


def _require(condition):
    if not condition:
        raise ValueError('Source Hub journal context or custody differs')


def _json(value, bound=None):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    _require(bound is None or len(raw.encode('utf-8')) <= bound)
    return raw


def _hash(value):
    return 'sha256:' + hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _uuid(value):
    _require(type(value) is str and str(uuid.UUID(value)) == value)


def _digest(value):
    _require(type(value) is str and re.fullmatch(r'sha256:[0-9a-f]{64}', value) is not None)


def _no_credentials(value):
    if type(value) is dict:
        for key, item in value.items():
            _require(type(key) is str and key.lower().replace('-', '_') not in _SENSITIVE)
            _no_credentials(item)
    elif type(value) is list:
        for item in value:
            _no_credentials(item)


def _coordinator(value):
    _require(type(value) is str and value and not any(
        c.isspace() or ord(c) < 32 or ord(c) == 127 or c in '\\%' for c in value))
    url = urlsplit(value)
    _require(url.scheme == 'https' and url.hostname and url.username is None
             and url.password is None and not url.query and not url.fragment
             and url.geturl() == value and url.netloc == url.netloc.lower()
             and not url.path.endswith('/') and '//' not in url.path
             and all(part not in ('.', '..') for part in url.path.split('/'))
             and (url.port is None or 0 < url.port < 65536))


def _intent(value):
    # Lazy: the existing compute SDK is an integration dependency of Task 3B.
    from cps_compute.native_pool_hub_effects import validate_hub_intent
    result = validate_hub_intent(value)
    _require(result['request']['action'] in _GROUP_ACTIONS | {'workspace-account-create','shared-student-remove','shared-student-add'})
    return result


class SourceHubJournal:
    """Synchronous evidence over an existing LocalCourseProvider connection.

    capture_source() returns canonical column names and raw ordered SQL row values
    for records and email_links, including exact stored payload strings. The
    digest of that observation is the existing intent.source_revision. Audit and
    receipts are excluded from the preimage because completion itself adds them.
    Controller-supplied snapshots are compared to actual SQL under each new write
    lock; they prove concurrency consistency, never visitor authorization.

    All calls require a clean connection. Each call owns its own transaction;
    no callback, network I/O, roster write or coordinator completion is performed.
    Returned dictionaries are detached original evidence, including on retry.
    """

    def __init__(self, provider, *, console, source_path, hub_api, coordinator_url,
                 coordinator_database_id, installation_id, writer_actor, writer_instance):
        from .providers import LocalCourseProvider
        try:
            _require(type(provider) is LocalCourseProvider and console in ('cps', 'cit'))
            _uuid(coordinator_database_id)
            _uuid(installation_id)
            _uuid(writer_instance)
            _coordinator(coordinator_url)
            _require(type(writer_actor) is str and 0 < len(writer_actor) <= 256
                     and not any(ord(c) < 32 or ord(c) == 127 for c in writer_actor))
            _require(type(source_path) is str and source_path == str(Path(source_path).resolve(strict=True)))
            # Reuse the exact existing codec for Hub API validation as well.
            _intent(dict(console=console, actor='configuration-check', hub_api=hub_api,
                         request=dict(action='group-create', name='configuration-check'),
                         before=None, source_revision='sha256:' + '0' * 64))
            self.provider = provider
            self._binding_json = _json(dict(console=console, source_path=source_path, hub_api=hub_api,
                coordinator_url=coordinator_url, coordinator_database_id=coordinator_database_id,
                installation_id=installation_id, writer_actor=writer_actor, writer_instance=writer_instance))
            with self._transaction():
                self._validate()
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, RecursionError):
            raise ValueError('Source Hub journal requires matching retained custody') from None

    @property
    def _db(self):
        return self.provider.db

    @property
    def _binding(self):
        return json.loads(self._binding_json)

    @contextmanager
    def _transaction(self, *, write=False):
        db = self._db
        # Outside try: never commit OR roll back a caller's pending transaction.
        if db.in_transaction:
            raise ValueError('Source Hub journal requires a clean connection')
        installation=getattr(self.provider,'managed_installation',None)
        if installation is not None:installation.check_source(self.provider)
        from cps_compute.native_pool_activation import _snapshot
        before=_snapshot(db) if installation is not None and write else None
        started = False
        try:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            started = True
            yield
            after=_snapshot(db) if before is not None else None
            successor=(installation.stage_source(self,before,after)
                if before is not None and before!=after else None)
            db.commit()
            if successor is not None:installation.finish(successor)
        except Exception:
            if started:
                db.rollback()
            raise ValueError('Source Hub journal operation rejected; retained custody required') from None

    def _schema(self):
        from .pool_heads import SOURCE_SCHEMA
        db, binding = self._db, self._binding
        _require(db.execute('PRAGMA user_version').fetchone()[0] in (SOURCE_DATABASE_VERSION,7))
        _require(dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB 'hub_journal_*'")) == SOURCE_JOURNAL_SCHEMA)
        _require(dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB 'pool_source_*'")) == SOURCE_SCHEMA)
        actual = next(row[2] for row in db.execute('PRAGMA database_list') if row[1] == 'main')
        _require(actual and str(Path(actual).resolve(strict=True)) == binding['source_path'])
        _require(self.provider.console == binding['console'])
        _require([tuple(row) for row in db.execute('SELECT * FROM console_owner')] == [(binding['console'],)])
        _require([tuple(row) for row in db.execute('SELECT * FROM pool_source_authority')] == [(
            binding['console'], binding['coordinator_url'], binding['coordinator_database_id'], binding['source_path'])])
        for table, columns in dict(_SOURCE_COLUMNS, audit=_AUDIT_COLUMNS).items():
            _require([r[1] for r in db.execute('PRAGMA table_info('+table+')')] == columns)
            _require(not db.execute('SELECT 1 FROM '+table+' WHERE console != ? LIMIT 1', (binding['console'],)).fetchone())
        rows = list(db.execute('SELECT id,version,binding,manifest FROM hub_journal_authority'))
        _require(len(rows) == 1 and tuple(rows[0])[:3] == (1, SOURCE_JOURNAL_VERSION, self._binding_json))
        _digest(rows[0][3])
        return rows[0][3]

    def _snapshot(self):
        result = {}
        for table, columns in _SOURCE_COLUMNS.items():
            order = 'console,kind,id' if table == 'records' else 'console,username'
            rows = [list(row) for row in self._db.execute('SELECT '+','.join(columns)+' FROM '+table+' ORDER BY '+order)]
            result[table] = dict(columns=list(columns), rows=rows)
            if table == 'records':
                for row in rows:
                    _no_credentials(json.loads(row[-1]))
        _json(result, _EFFECT_BOUND)
        return result

    def _prepared(self, *, operation_id, input_sha256, intent, source_preimage,
                  hub_preimage, actor, ticket):
        _uuid(operation_id)
        _digest(input_sha256)
        intent = _intent(intent)
        _json(source_preimage, _EFFECT_BOUND)
        _no_credentials(source_preimage)
        binding = self._binding
        _require(intent['console'] == binding['console'] and intent['hub_api'] == binding['hub_api']
                 and actor == intent['actor'] and hub_preimage == intent['before']
                 and intent['source_revision'] == _hash(source_preimage)
                 and input_sha256 == _hash(dict(operation='admin.hub', payload=intent)))
        _require(type(ticket) is dict and set(ticket) == _TICKET_FIELDS)
        _json(ticket, _EFFECT_BOUND)
        _require(ticket == dict(transition_id=operation_id, predecessor=ticket['predecessor'],
            operation='admin.hub', input_sha256=input_sha256, actor=binding['writer_actor'],
            instance=binding['writer_instance'], target=None, mutation_result=None,
            receipt=None, effect_sha256=None, database_id=binding['coordinator_database_id']))
        if ticket['predecessor'] is not None:
            _digest(ticket['predecessor'])
        record=dict(operation_id=operation_id, input_sha256=input_sha256, intent=intent,
            source_preimage=source_preimage, hub_preimage=hub_preimage, actor=actor,
            ticket=ticket, binding=binding, effect=None, effect_sha256=None, receipt=None,
            audit_rowid=None, audit_time=None)
        if intent['request']['action'] in ('shared-student-remove','shared-student-add'):
            from cps_compute.native_pool_shared_effects import source_postimage
            _require(self._db.execute('PRAGMA user_version').fetchone()[0]==7)
            source_postimage(source_preimage,intent['request'])
            record['lifecycle']=dict(version=3,stopped=None,control=None,finalized=None,stops=[])
        return record

    def _effect(self, record, effect, effect_sha256):
        from cps_compute.native_pool_hub_effects import validate_effect_record
        _digest(effect_sha256)
        _json(effect, _EFFECT_BOUND)
        binding = self._binding
        validated = validate_effect_record(effect, transition_id=record['operation_id'],
            actor=binding['writer_actor'], instance=binding['writer_instance'],
            input_sha256=record['input_sha256'])
        _require(validated['intent'] == record['intent'] and _hash(validated) == effect_sha256)
        return validated

    def _receipt(self, record, value):
        _json(value, _RECEIPT_BOUND)
        intent = record['intent']
        context = dict(protocol='cps-hub-permission-receipt/v1', version=1,
            transition_id=record['operation_id'], input_sha256=record['input_sha256'],
            effect_sha256=record['effect_sha256'], console=self._binding['console'],
            service=self._binding['console']+'-admin', actor=record['actor'])
        _require(type(value) is dict and set(value) == set(context) | {'result'}
                 and type(value['version']) is int
                 and all(value[key] == expected for key, expected in context.items()))
        request, before = intent['request'], intent['before']
        action = request['action']
        if action in ('shared-student-remove','shared-student-add'):
            from cps_compute.native_pool_shared_effects import shared_result
            _require(value['result']==shared_result(intent,record['lifecycle']['stopped']))
            return value
        if action == 'workspace-account-create':
            from cps_compute.native_pool_neutral_effects import validate_neutral_after
            after=validate_neutral_after(intent['console'],request['descriptor'],value.get('result',{}).get('after'))
            _require(after['authority']==before['authority'] and
                (before['course_group'] is None or after['course_group']==before['course_group']))
        elif action == 'user-create':
            after=value.get('result',{}).get('after')
            _require(type(after) is dict and set(after)=={'id','name','created','admin','roles','groups'}
                and type(after['id']) is int and after['id']>0 and after['name']==request['name']
                and type(after['created']) is str and after['created'] and after['admin'] is False
                and after['roles']==['user'] and after['groups']==[])
        elif action == 'group-create':
            after = dict(name=request['name'], users=[], roles=[], properties_sha256=_hash({}))
        elif action == 'group-delete':
            after = None
        else:
            users = set(before['users'])
            if action == 'group-users-add':
                users.update(request['users'])
            else:
                users.difference_update(request['users'])
            after = dict(before, users=sorted(users))
        _require(value['result'] == dict(action=action, before=before, after=after))
        return value

    def _audit(self, record):
        return (record['actor'], self._binding['console'], 'admin.hub',
                (record['intent']['request']['binding']['owner'] if record['intent']['request']['action']=='workspace-account-create'
                 else record['intent']['request']['workspace'] if record['intent']['request']['action'] in ('shared-student-remove','shared-student-add')
                 else record['intent']['request']['name']), _json(record['hub_preimage']),
                _json(record['receipt']), record['audit_time'], 'success')

    def _validate(self):
        expected_manifest = self._schema()
        digest = hashlib.sha256()
        # Per-record bounds only: total history is streamed without a size cap.
        for operation_id, input_sha256, state, raw in self._db.execute(
                'SELECT operation_id,input_sha256,state,record FROM hub_journal_operation ORDER BY operation_id'):
            _require(type(raw) is str and len(raw.encode('utf-8')) <= 5*_EFFECT_BOUND+_RECEIPT_BOUND)
            record = json.loads(raw)
            shared=type(record) is dict and record.get('intent',{}).get('request',{}).get('action') in ('shared-student-remove','shared-student-add')
            _require(type(record) is dict and set(record) == _RECORD_FIELDS|({'lifecycle'} if shared else set()) and raw == _json(record))
            prepared = self._prepared(**{key:record[key] for key in (
                'operation_id', 'input_sha256', 'intent', 'source_preimage', 'hub_preimage', 'actor', 'ticket')})
            _require((operation_id, input_sha256) == (record['operation_id'], record['input_sha256'])
                     and record['binding'] == self._binding and state in (('prepared','armed','stopped','hub-completed','control-completed','source-completed','effective') if shared else ('prepared','armed','completed')))
            if shared:
                from .shared_journal import validate_record
                validate_record(self,record,state,prepared)
            elif state == 'prepared':
                _require(record == prepared)
            else:
                self._effect(record, record['effect'], record['effect_sha256'])
                if state == 'armed':
                    _require(all(record[key] is None for key in ('receipt', 'audit_rowid', 'audit_time')))
                else:
                    self._receipt(record, record['receipt'])
                    _require(type(record['audit_rowid']) is int and record['audit_rowid'] > 0
                             and type(record['audit_time']) is str and record['audit_time'])
                    receipt = self._db.execute('SELECT input_sha256,result FROM pool_source_receipt WHERE transition_id=?',
                                              (operation_id,)).fetchone()
                    audit = self._db.execute('SELECT * FROM audit WHERE rowid=?', (record['audit_rowid'],)).fetchone()
                    _require(receipt is not None and tuple(receipt) == (input_sha256, _json(record['receipt']))
                             and audit is not None and tuple(audit) == self._audit(record))
            self._manifest_row(digest, (operation_id, input_sha256, state, raw))
        _require(expected_manifest == 'sha256:' + digest.hexdigest())

    @staticmethod
    def _manifest_row(digest, row):
        raw = _json(list(row)).encode('utf-8')
        digest.update(str(len(raw)).encode('ascii') + b':' + raw)

    def _save(self, record, state, *, new=False):
        db = self._db
        values = (record['operation_id'], record['input_sha256'], state, _json(record))
        if new:
            db.execute('INSERT INTO hub_journal_operation VALUES(?,?,?,?)', values)
        else:
            db.execute('UPDATE hub_journal_operation SET state=?,record=? WHERE operation_id=?',
                       (state, values[3], values[0]))
        digest = hashlib.sha256()
        for row in db.execute('SELECT operation_id,input_sha256,state,record FROM hub_journal_operation ORDER BY operation_id'):
            self._manifest_row(digest, row)
        db.execute('UPDATE hub_journal_authority SET manifest=? WHERE id=1', ('sha256:'+digest.hexdigest(),))
        self._validate()
        return json.loads(_json(dict(record, state=state)))

    def _find(self, operation_id, input_sha256):
        _uuid(operation_id)
        _digest(input_sha256)
        row = self._db.execute('SELECT input_sha256,state,record FROM hub_journal_operation WHERE operation_id=?',
                               (operation_id,)).fetchone()
        if row is None:
            return None
        _require(row[0] == input_sha256)
        return dict(json.loads(row[2]), state=row[1])

    def capture_source(self):
        """Observe exact local SQL; this does not grant permission for any change."""
        with self._transaction():
            self._validate()
            return self._snapshot()

    def read(self, operation_id, input_sha256):
        """Return detached historical evidence only after whole-history validation."""
        with self._transaction():
            self._validate()
            return self._find(operation_id, input_sha256)

    def read_all(self):
        """Return detached retained records after one complete history validation."""
        with self._transaction():
            self._validate()
            return [dict(json.loads(raw), state=state) for _, _, state, raw in self._db.execute(
                'SELECT operation_id,input_sha256,state,record FROM hub_journal_operation ORDER BY operation_id')]

    def prepare(self, *, operation_id, input_sha256, intent, source_preimage,
                hub_preimage, actor, ticket):
        """Persist exact local intent BEFORE retaining the global Hub effect."""
        with self._transaction(write=True):
            self._validate()
            record = self._prepared(operation_id=operation_id, input_sha256=input_sha256,
                intent=intent, source_preimage=source_preimage, hub_preimage=hub_preimage, actor=actor, ticket=ticket)
            existing = self._find(operation_id, input_sha256)
            if existing:
                _require(all(existing[key] == value for key, value in record.items()
                             if key not in ('effect', 'effect_sha256', 'receipt', 'audit_rowid', 'audit_time')))
                if existing['state'] == 'completed':
                    return existing
            _require(source_preimage == self._snapshot())
            if existing:
                return existing
            return self._save(record, 'prepared', new=True)

    def arm(self, operation_id, input_sha256, *, effect, effect_sha256):
        """Persist exact retained global effect BEFORE any receiving-handler request."""
        with self._transaction(write=True):
            self._validate()
            record = self._find(operation_id, input_sha256)
            _require(record is not None and record['state'] in ('prepared', 'armed'))
            _require(record['source_preimage'] == self._snapshot())
            validated = self._effect(record, effect, effect_sha256)
            if record.pop('state') == 'armed':
                _require(record['effect'] == validated and record['effect_sha256'] == effect_sha256)
                return dict(record, state='armed')
            record.update(effect=validated, effect_sha256=effect_sha256)
            return self._save(record, 'armed')

    def complete(self, operation_id, input_sha256, *, receipt):
        """Atomically retain Hub evidence, local Source receipt and visitor audit.

        This records local completion only. The caller still owns coordinator
        completion, visitor/course authorization and shared workspace lifecycle.
        No Hub mutation is executed here, even when recovering a lost response.
        """
        with self._transaction(write=True):
            self._validate()
            record = self._find(operation_id, input_sha256)
            _require(record is not None and record['state'] in ('armed', 'completed'))
            receipt = self._receipt(record, receipt)
            if record.pop('state') == 'completed':
                _require(record['receipt'] == receipt)
                return dict(record, state='completed')
            _require(record['source_preimage'] == self._snapshot())
            record.update(receipt=receipt, audit_time=datetime.now(timezone.utc).isoformat())
            self._db.execute('INSERT INTO pool_source_receipt VALUES(?,?,?)',
                             (operation_id, input_sha256, _json(receipt)))
            cursor = self._db.execute('INSERT INTO audit VALUES(?,?,?,?,?,?,?,?)', self._audit(record))
            record['audit_rowid'] = cursor.lastrowid
            return self._save(record, 'completed')
