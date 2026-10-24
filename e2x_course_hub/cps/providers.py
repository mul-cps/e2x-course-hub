"""Source-neutral local course records. No Moodle client is imported or initialized."""
from contextlib import nullcontext
from pathlib import Path
import json
import sqlite3
from datetime import datetime, timezone
from typing import Protocol
from functools import wraps

KINDS = ('courses', 'terms', 'assignments', 'groups', 'memberships', 'groupings', 'projects', 'workspaces')

class CourseProvider(Protocol):
    async def courses(self): ...
    async def members(self, course_id): ...
    async def groups(self, course_id): ...
    async def groupings(self, course_id): ...

class MoodleCourseProvider:
    def __init__(self, *args, **kwargs):
        raise ValueError('Moodle status: planned / not deployed; enabling this unfinished provider is unsupported')

    async def courses(self): raise NotImplementedError
    async def members(self, course_id): raise NotImplementedError
    async def groups(self, course_id): raise NotImplementedError
    async def groupings(self, course_id): raise NotImplementedError

def audited_mutation(method):
    @wraps(method)
    async def wrapped(self,*args,**kwargs):
        try:
            self._pool_mode_guard()
            return await method(self,*args,**kwargs)
        except Exception as error:
            if not self._importing:
                kind=args[0] if args and method.__name__ in ('put','delete') else method.__name__
                proposed=args[1] if len(args)>1 else args[0] if args else {}
                identifier=proposed.get('id','') if isinstance(proposed,dict) else str(proposed) if method.__name__=='delete' else ''
                self.denial(kwargs.get('actor','unknown'),kind,identifier,proposed,
                    'denied' if isinstance(error,PermissionError) else 'failure')
            raise
    return wrapped

class LocalCourseProvider:
    capabilities = frozenset({'create', 'update', 'delete'})

    def __init__(self, path, console, *, v2_managed=False, pool_guard=None):
        if console not in ('cps', 'cit'):
            raise ValueError('console must be cps or cit')
        if type(v2_managed) is not bool:raise ValueError('Explicit v2 management flag required')
        if v2_managed:
            from .pool_heads import PoolMutationRPCGuard
            if type(pool_guard) is not PoolMutationRPCGuard or pool_guard.client.console!=console:
                raise ValueError('Managed Source requires exact server RPC guard')
        self.pool_guard=pool_guard if v2_managed else None
        self.console = console
        self._importing = False
        self.db = sqlite3.connect(Path(path).absolute().as_uri()+'?mode=rw',uri=True) if v2_managed else sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        from .pool_heads import SOURCE_SCHEMA
        installed=dict(self.db.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB 'pool_source_*'"))
        if v2_managed:
            if installed!=SOURCE_SCHEMA or [tuple(r) for r in self.db.execute('SELECT console,origin,database_id,source_path FROM pool_source_authority')]!=[(*pool_guard.binding,str(Path(path).resolve()))]:
                self.db.close();raise ValueError('Explicit Source custody migration required')
            if self.db.execute('PRAGMA user_version').fetchone()[0]!=5:
                self.db.close();raise ValueError('Matching Source database version required')
            return
        if installed:
            self.db.close();raise ValueError('Persisted managed Source requires its RPC coordinator')
        if self.db.execute('PRAGMA user_version').fetchone()[0]>5:
            self.db.close()
            raise ValueError('Console database schema is newer than this application; restore matching versions')
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        owners = set()
        if 'records' in tables:
            owners.update(row[0] for row in self.db.execute('SELECT DISTINCT console FROM records'))
        for table in ('email_links','audit'):
            if table in tables:
                owners.update(row[0] for row in self.db.execute('SELECT DISTINCT console FROM '+table))
        if 'console_owner' in tables:
            owners.update(row[0] for row in self.db.execute('SELECT owner FROM console_owner'))
        if owners - {console}:
            self.db.close()
            raise ValueError('CPS and CIT require separate console SQLite databases')
        with self.db:
            self.db.executescript('''
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS records (
                    console TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(console, kind, id));
                CREATE TABLE IF NOT EXISTS email_links (
                    console TEXT NOT NULL, username TEXT NOT NULL, email TEXT NOT NULL, canonical_person_id TEXT,
                    PRIMARY KEY(console,username), UNIQUE(console,email));
                CREATE TABLE IF NOT EXISTS audit (
                    actor TEXT NOT NULL, console TEXT NOT NULL, kind TEXT NOT NULL,
                    target TEXT NOT NULL, previous TEXT, current TEXT,
                    time TEXT NOT NULL, outcome TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS console_owner (owner TEXT PRIMARY KEY);
                PRAGMA user_version=5;
            ''')
            owners = {row[0] for row in self.db.execute('SELECT DISTINCT console FROM records')}
            owners.update(row[0] for row in self.db.execute('SELECT owner FROM console_owner'))
            if owners - {console}:
                raise ValueError('CPS and CIT require separate console SQLite databases')
            self.db.execute('INSERT OR IGNORE INTO console_owner VALUES (?)', (console,))
            if 'canonical_person_id' not in {row[1] for row in self.db.execute('PRAGMA table_info(email_links)')}:
                self.db.execute('ALTER TABLE email_links ADD COLUMN canonical_person_id TEXT')
            if 'verified' not in {row[1] for row in self.db.execute('PRAGMA table_info(email_links)')}:
                self.db.execute('ALTER TABLE email_links ADD COLUMN verified INTEGER NOT NULL DEFAULT 0')
            self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS email_links_canonical ON email_links(console,canonical_person_id)')

    def _pool_mode_guard(self):
        from .pool_heads import SOURCE_SCHEMA
        installed=dict(self.db.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB 'pool_source_*'"))
        if self.pool_guard is None and installed:
            raise ValueError('Managed Source cannot use an already-open legacy writer')
        if self.pool_guard is not None and installed!=SOURCE_SCHEMA:
            raise ValueError('Managed Source custody missing or corrupt')
        if self.pool_guard is not None:
            guard=self.pool_guard
            if guard.binding!=(self.console,guard.client.base_url,guard.database_id) or guard.client.console!=self.console:
                raise ValueError('Managed Source console/origin binding changed')
            actual=str(Path(next(row[2] for row in self.db.execute('PRAGMA database_list') if row[1]=='main')).resolve())
            rows=[tuple(row) for row in self.db.execute('SELECT console,origin,database_id,source_path FROM pool_source_authority')]
            if rows!=[(*guard.binding,actual)]:
                raise ValueError('Managed Source authority identity/path missing or changed')

    async def list(self, kind, course_id=None):
        self._kind(kind)
        rows = self.db.execute('SELECT payload FROM records WHERE console=? AND kind=? ORDER BY id', (self.console, kind))
        records = [json.loads(row['payload']) for row in rows]
        return [r for r in records if course_id is None or r.get('course_id') == course_id]

    def _kind(self, kind):
        if kind not in KINDS: raise ValueError('unknown record kind')

    async def _normalize_record(self, kind, record, *, actor):
        self._kind(kind)
        record = dict(record)
        identifier = record['id']
        if not isinstance(identifier, str) or not identifier or not actor:
            raise ValueError('id and actor are required')
        previous = self.db.execute('SELECT payload FROM records WHERE console=? AND kind=? AND id=?', (self.console, kind, identifier)).fetchone()
        old = json.loads(previous['payload']) if previous else None
        # CRUD updates preserve unspecified metadata, provenance, policy and references.
        if old:record={**old,**record}
        record=json.loads(json.dumps(record,sort_keys=True,separators=(',',':')))
        if old and kind not in ('courses','projects'):
            if any(old.get(key) != record.get(key) for key in ('course_id','term_id')):
                raise ValueError('record course/term association is immutable; reviewed migration required')
        # Backend protection is authoritative even when clients hide editing controls.
        if record.get('source', 'local') != 'local' or (old and old.get('source') != 'local'):
            raise PermissionError('externally managed records require a reviewed source migration')
        record.setdefault('source', 'local')
        record.setdefault('external_id', None)
        if record['external_id'] is not None:
            raise PermissionError('local records cannot claim external identity')
        if kind in ('terms', 'memberships', 'groups', 'groupings', 'assignments', 'workspaces'):
            course_id = record.get('course_id')
            exists = self.db.execute("SELECT 1 FROM records WHERE console=? AND kind='courses' AND id=?", (self.console, course_id)).fetchone()
            if not exists: raise ValueError('course reference does not exist in this console')
        from .validation import validate
        record = await validate(self,kind,record)
        return record, previous['payload'] if previous else None

    @audited_mutation
    async def put(self, kind, record, *, actor):
        record, previous_payload = await self._normalize_record(kind,record,actor=actor)
        identifier=record['id']
        previous={'payload':previous_payload} if previous_payload is not None else None
        payload = json.dumps(record, sort_keys=True)
        ticket=None
        if self.pool_guard is not None:
            if self.db.in_transaction:raise ValueError('Remote fence cannot run inside a Source transaction')
            if previous and json.loads(previous['payload'])==record:
                current=await self.pool_guard.client._request('native-pool-head/current')
                if current.get('database_id')!=self.pool_guard.database_id:raise ValueError('Source custody changed')
                receipt=self.db.execute('SELECT input_sha256,result FROM pool_source_receipt WHERE transition_id=?',(current.get('transition_id'),)).fetchone()
                if receipt is not None:
                    from .pool_heads import digest
                    expected=digest({'operation':'admin.record.put','payload':{'console':self.console,'kind':kind,'record':record}})
                    if receipt['input_sha256']!=expected or json.loads(receipt['result'])!=record:
                        raise ValueError('Retained Source operation receipt differs')
                    await self.pool_guard.complete({'transition_id':current['transition_id']},json.loads(receipt['result']))
                return record
            ticket=await self.pool_guard.prepare('admin.record.put',{'console':self.console,'kind':kind,'record':record})
        with (nullcontext() if self._importing else self.db):
            if ticket is not None:
                self.db.execute('BEGIN IMMEDIATE')
                self._pool_mode_guard()
                current=self.db.execute('SELECT payload FROM records WHERE console=? AND kind=? AND id=?',(self.console,kind,identifier)).fetchone()
                if (current['payload'] if current else None)!=(previous['payload'] if previous else None):
                    raise ValueError('Source preimage changed while admission was fenced')
            self.db.execute('INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?)', (self.console, kind, identifier, payload))
            self._audit(actor, kind, identifier, previous['payload'] if previous else None, payload)
            if ticket is not None:
                self.db.execute('INSERT INTO pool_source_receipt VALUES(?,?,?)',
                                (ticket['transition_id'],ticket['input_sha256'],payload))
        if ticket is not None:await self.pool_guard.complete(ticket,record)
        return record

    async def _deletion_receipt(self, kind, identifier):
        if self.pool_guard is None:return None
        self._pool_mode_guard()
        if self.db.in_transaction:raise ValueError('Remote fence cannot run inside a Source transaction')
        current=await self.pool_guard.client._request('native-pool-head/current')
        if current.get('database_id')!=self.pool_guard.database_id:raise ValueError('Source custody changed')
        row=self.db.execute('SELECT input_sha256,result FROM pool_source_receipt WHERE transition_id=?',
                            (current.get('transition_id'),)).fetchone()
        if row is None:return None
        result=json.loads(row['result'])
        if (not isinstance(result,dict) or set(result)!={'deleted','kind','record'}
            or result['deleted'] is not True or result['kind']!=kind
            or not isinstance(result['record'],dict) or result['record'].get('id')!=identifier):return None
        from .pool_heads import digest
        payload={'console':self.console,'kind':kind,'record':result['record']}
        if result['record'].get('source')!='local' or row['input_sha256']!=digest({'operation':'admin.record.delete','payload':payload}):
            raise ValueError('Retained Source deletion receipt differs')
        return current,result

    async def deleted_record(self, kind, identifier):
        """Original record for authorizing an exact pending-delete retry, never discovery."""
        self._kind(kind)
        receipt=await self._deletion_receipt(kind,identifier)
        return receipt[1]['record'] if receipt else None

    def _check_delete_references(self, kind, identifier, record):
        # Local SQL only: the same checks run again under the write transaction.
        def records(child, course_id=None):
            rows=self.db.execute('SELECT payload FROM records WHERE console=? AND kind=?',(self.console,child))
            values=[json.loads(row['payload']) for row in rows]
            return [r for r in values if course_id is None or r.get('course_id')==course_id]
        if kind=='groups':
            children=records('workspaces')+records('memberships')+records('groupings')
            if any(r.get('group_id')==identifier or identifier in r.get('group_ids',[]) for r in children):
                raise ValueError('group still has workspace/membership references; controlled lifecycle required')
        if kind=='terms':
            term_id=record.get('term_id',identifier)
            for child in ('memberships','groups','groupings','assignments','workspaces'):
                if any(r.get('term_id')==term_id for r in records(child,record['course_id'])):
                    raise ValueError('term still has references; controlled lifecycle required')
        if kind=='courses':
            if any(records(child,identifier) for child in KINDS[1:]):
                raise ValueError('course still has referenced records; archive workspaces first')

    @audited_mutation
    async def delete(self, kind, identifier, *, actor):
        self._kind(kind)
        if not isinstance(identifier,str) or not identifier or not actor:raise ValueError('id and actor are required')
        if self.pool_guard is not None:
            if kind not in ('courses','terms','groups'):self.pool_guard.deny_uncovered('lifecycle-sensitive deletion')
            if self.db.in_transaction:raise ValueError('Remote fence cannot run inside a Source transaction')
        previous=self.db.execute('SELECT payload FROM records WHERE console=? AND kind=? AND id=?',
                                  (self.console,kind,identifier)).fetchone()
        if previous is None:
            receipt=await self._deletion_receipt(kind,identifier)
            if receipt is None:raise KeyError(identifier)
            current,result=receipt
            if current.get('receipt') is None:await self.pool_guard.complete(current,result)
            return
        record=json.loads(previous['payload'])
        if record.get('source')!='local':raise PermissionError('externally managed record')
        self._check_delete_references(kind,identifier,record)
        ticket=None
        if self.pool_guard is not None:
            ticket=await self.pool_guard.prepare('admin.record.delete',
                {'console':self.console,'kind':kind,'record':record})
        result={'deleted':True,'kind':kind,'record':record}
        with self.db:
            if ticket is not None:
                self.db.execute('BEGIN IMMEDIATE');self._pool_mode_guard()
                current=self.db.execute('SELECT payload FROM records WHERE console=? AND kind=? AND id=?',
                                        (self.console,kind,identifier)).fetchone()
                if current is None or current['payload']!=previous['payload']:
                    raise ValueError('Source preimage changed while admission was fenced')
                self._check_delete_references(kind,identifier,record)
            self.db.execute('DELETE FROM records WHERE console=? AND kind=? AND id=?',(self.console,kind,identifier))
            self._audit(actor,kind,identifier,previous['payload'],None)
            if ticket is not None:
                self.db.execute('INSERT INTO pool_source_receipt VALUES(?,?,?)',
                    (ticket['transition_id'],ticket['input_sha256'],json.dumps(result,sort_keys=True)))
        if ticket is not None:await self.pool_guard.complete(ticket,result)

    def _audit(self, actor, kind, identifier, old, new):
        self.db.execute('INSERT INTO audit VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (actor, self.console, kind, identifier, old, new, datetime.now(timezone.utc).isoformat(), 'success'))

    def denial(self, actor, kind, identifier, requested, outcome):
        previous = self.db.execute('SELECT payload FROM records WHERE console=? AND kind=? AND id=?', (self.console,kind,identifier)).fetchone()
        with self.db:
            self.db.execute('INSERT INTO audit VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (actor,self.console,kind,identifier,previous['payload'] if previous else None,
                 json.dumps(requested,sort_keys=True),datetime.now(timezone.utc).isoformat(),outcome))

    def audit(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM audit WHERE console=? ORDER BY rowid', (self.console,))]

    async def courses(self): return await self.list('courses')
    async def terms(self, course_id): return await self.list('terms', course_id)
    async def members(self, course_id): return await self.list('memberships', course_id)
    async def groups(self, course_id): return await self.list('groups', course_id)
    async def groupings(self, course_id): return await self.list('groupings', course_id)

    async def retained_import(self, kind, identifier):
        """Retained batch for an already-authorized exact HTTP retry, never discovery."""
        self._kind(kind);self._pool_mode_guard()
        if self.pool_guard is None:return None
        if self.db.in_transaction:raise ValueError('Remote fence cannot run inside a Source transaction')
        current=await self.pool_guard.client._request('native-pool-head/current')
        if current.get('database_id')!=self.pool_guard.database_id:raise ValueError('Source custody changed')
        row=self.db.execute('SELECT input_sha256,result FROM pool_source_receipt WHERE transition_id=?',
            (current.get('transition_id'),)).fetchone()
        if row is None:return None
        result=json.loads(row['result'])
        if type(result) is not dict or set(result)!={'imported'}:return None
        batch=result['imported']
        if type(batch) is not list or not 1<=len(batch)<=1000 or any(type(item) is not dict
            or set(item)!={'kind','record'} or item['kind'] not in KINDS or type(item['record']) is not dict
            or item['record'].get('source')!='local' or type(item['record'].get('id')) is not str for item in batch):
            raise ValueError('Retained Source import is corrupt')
        from .pool_heads import digest
        if row['input_sha256']!=digest({'operation':'admin.record.import','payload':{'console':self.console,'records':batch}}):
            raise ValueError('Retained Source import receipt differs')
        return batch if any(item['kind']==kind and item['record']['id']==identifier for item in batch) else None

    def _import_preimage(self):
        return {table:[tuple(row) for row in self.db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                for table in ('records','email_links')}

    async def _managed_import(self, records, *, actor):
        if self.db.in_transaction:raise ValueError('Remote fence cannot run inside a Source transaction')
        if type(records) is not dict or set(records)-set(KINDS) or not actor:
            raise ValueError('Closed owned import and actor required')
        if any(type(rows) is not list for rows in records.values()):raise ValueError('Import records must be lists')
        count=sum(len(rows) for rows in records.values())
        if not 1<=count<=1000:raise ValueError('Import requires 1 to 1000 records')
        # A private in-memory validation view never becomes a Source writer or
        # adopts copied custody. No RPC, audit, or mutation touches the live DB.
        view=object.__new__(LocalCourseProvider)
        view.console=self.console;view.db=sqlite3.connect(':memory:');view.db.row_factory=sqlite3.Row
        planned=[];seen=set()
        try:
            self.db.backup(view.db)
            before=view._import_preimage()
            for kind in KINDS:
                for supplied in records.get(kind,[]):
                    record,_=await view._normalize_record(kind,supplied,actor=actor)
                    key=(kind,record['id'])
                    if key in seen:raise ValueError('Duplicate record in import')
                    seen.add(key);planned.append({'kind':kind,'record':record})
                    view.db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?,?)',
                        (self.console,kind,record['id'],json.dumps(record,sort_keys=True)))
        finally:view.db.close()
        from .pool_heads import digest
        operation='admin.record.import';payload={'console':self.console,'records':planned}
        expected=digest({'operation':operation,'payload':payload})
        current=await self.pool_guard.client._request('native-pool-head/current')
        if current.get('database_id')!=self.pool_guard.database_id:raise ValueError('Source custody changed')
        receipt=self.db.execute('SELECT input_sha256,result FROM pool_source_receipt WHERE transition_id=?',
            (current.get('transition_id'),)).fetchone()
        result={'imported':planned}
        if receipt is not None:
            if receipt['input_sha256']!=expected or json.loads(receipt['result'])!=result:
                raise ValueError('Retained Source import receipt differs')
            for item in planned:
                row=self.db.execute('SELECT payload FROM records WHERE console=? AND kind=? AND id=?',
                    (self.console,item['kind'],item['record']['id'])).fetchone()
                if row is None or json.loads(row['payload'])!=item['record']:
                    raise ValueError('Retained imported records changed')
            await self.pool_guard.complete(current,result)
            return
        ticket=await self.pool_guard.prepare(operation,payload)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE');self._pool_mode_guard()
            if self._import_preimage()!=before:raise ValueError('Source preimage changed while import was fenced')
            for item in planned:
                kind,record=item['kind'],item['record']
                previous=self.db.execute('SELECT payload FROM records WHERE console=? AND kind=? AND id=?',
                    (self.console,kind,record['id'])).fetchone()
                raw=json.dumps(record,sort_keys=True)
                self.db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?,?)',(self.console,kind,record['id'],raw))
                self._audit(actor,kind,record['id'],previous['payload'] if previous else None,raw)
            self.db.execute('INSERT INTO pool_source_receipt VALUES(?,?,?)',
                (ticket['transition_id'],ticket['input_sha256'],json.dumps(result,sort_keys=True)))
        await self.pool_guard.complete(ticket,result)

    @audited_mutation
    async def migrate_local(self, records, *, actor):
        self._pool_mode_guard()
        """Explicit import preserves IDs and all workspace references; never match names."""
        if self.pool_guard is not None:return await self._managed_import(records,actor=actor)
        with self.db:
            self._importing = True
            try:
                for kind in KINDS:
                    for record in records.get(kind, []):
                        await self.put(kind, record, actor=actor)
            finally:
                self._importing = False

    def _identity_projection(self, usernames):
        return {'accounts':[dict(row) for row in self.db.execute('SELECT * FROM email_links WHERE console=? ORDER BY username',
                    (self.console,)) if row['username'] in usernames],
            'memberships':[record for row in self.db.execute("SELECT payload FROM records WHERE console=? AND kind='memberships' ORDER BY id",
                    (self.console,)) if (record:=json.loads(row['payload']))['person_id'] in usernames]}

    async def _managed_identity_link(self, rows, *, actor):
        if self.db.in_transaction:raise ValueError('Remote fence cannot run inside a Source transaction')
        if type(rows) is not list or not 1<=len(rows)<=1000 or not actor:
            raise ValueError('Bounded explicit identity mappings and actor required')
        allowed={'hub','username','email','person_id','verified','administrator_reviewed'}
        if any(type(row) is not dict or set(row)-allowed or row.get('hub')!=self.console
            or type(row.get('administrator_reviewed',False)) is not bool for row in rows):
            raise ValueError('Closed console-owned reviewed identity mappings required')
        from .identity import reviewed_email_mapping
        mapping=reviewed_email_mapping(rows)
        proofs={row['username']:row.get('administrator_reviewed',False) for row in rows}
        normalized=[{'hub':self.console,'username':username,**mapping[(self.console,username)],
            'verified':True,'administrator_reviewed':proofs[username]} for username in sorted(proofs)]
        view=object.__new__(LocalCourseProvider)
        view.console=self.console;view.db=sqlite3.connect(':memory:');view.db.row_factory=sqlite3.Row
        try:
            self.db.backup(view.db);before=view._import_preimage()
            linked=await view._link_identities(normalized,actor=actor)
            result={'linked':linked,**view._identity_projection(set(linked))}
        finally:view.db.close()
        from .pool_heads import digest
        operation='admin.identity.link';payload={'console':self.console,'mappings':normalized}
        expected=digest({'operation':operation,'payload':payload})
        current=await self.pool_guard.client._request('native-pool-head/current')
        if current.get('database_id')!=self.pool_guard.database_id:raise ValueError('Source custody changed')
        receipt=self.db.execute('SELECT input_sha256,result FROM pool_source_receipt WHERE transition_id=?',
            (current.get('transition_id'),)).fetchone()
        if receipt is not None:
            if receipt['input_sha256']!=expected or json.loads(receipt['result'])!=result:
                raise ValueError('Retained Source identity receipt differs')
            if self._identity_projection(set(linked))!={key:result[key] for key in ('accounts','memberships')}:
                raise ValueError('Retained Source identity effects changed')
            await self.pool_guard.complete(current,result)
            return linked
        ticket=await self.pool_guard.prepare(operation,payload)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE');self._pool_mode_guard()
            if self._import_preimage()!=before:raise ValueError('Source preimage changed while identity migration was fenced')
            actual=await self._link_identities(normalized,actor=actor)
            if actual!=linked or self._identity_projection(set(linked))!={key:result[key] for key in ('accounts','memberships')}:
                raise ValueError('Source identity effects differ from validated plan')
            self.db.execute('INSERT INTO pool_source_receipt VALUES(?,?,?)',
                (ticket['transition_id'],ticket['input_sha256'],json.dumps(result,sort_keys=True)))
        await self.pool_guard.complete(ticket,result)
        return linked

    @audited_mutation
    async def link_identities(self, rows, *, actor):
        if self.pool_guard is not None:return await self._managed_identity_link(rows,actor=actor)
        with self.db:return await self._link_identities(rows,actor=actor)

    async def _link_identities(self, rows, *, actor):
        from .identity import reviewed_email_mapping
        mapping=reviewed_email_mapping(rows)
        proofs={(row['hub'],row['username']):row for row in rows}
        for (hub,username),identity in mapping.items():
            if hub!=self.console:continue
            email,person=identity['email'],identity['person_id']
            collision=self.db.execute('SELECT username FROM email_links WHERE console=? AND canonical_person_id=? AND username<>?',(hub,person,username)).fetchone()
            if collision:raise ValueError('duplicate canonical person within Hub requires explicit alias migration')
            previous=self.db.execute('SELECT email,canonical_person_id FROM email_links WHERE console=? AND username=?',(hub,username)).fetchone()
            if previous:
                if previous['canonical_person_id'] and previous['canonical_person_id']!=person:
                    raise ValueError('existing canonical person reassignment requires a separate reviewed migration')
                if (previous['email']!=email or previous['canonical_person_id'] is None) and proofs[(hub,username)].get('administrator_reviewed') is not True:
                    raise ValueError('email/UUID handover requires explicit administrator-reviewed mapping')
            self.db.execute('INSERT INTO email_links(console,username,email,canonical_person_id,verified) VALUES (?,?,?,?,1) ON CONFLICT(console,username) DO UPDATE SET email=excluded.email,canonical_person_id=excluded.canonical_person_id,verified=1',(hub,username,email,person))
            for row in self.db.execute("SELECT id,payload FROM records WHERE console=? AND kind='memberships'",(hub,)).fetchall():
                membership=json.loads(row['payload'])
                if membership['person_id']!=username:continue
                old_canonical=membership.get('canonical_person_id')
                if old_canonical not in (None,person,previous['email'] if previous else None):
                    raise ValueError('membership canonical identity conflicts with reviewed mapping')
                if old_canonical!=person:
                    if any(w.get('group_id')==membership.get('group_id') and w.get('state')!='stopped' for w in await self.list('workspaces')):
                        raise ValueError('stop affected workspace before canonical identity migration')
                    updated={**membership,'canonical_person_id':person}
                    payload=json.dumps(updated,sort_keys=True)
                    self.db.execute("UPDATE records SET payload=? WHERE console=? AND kind='memberships' AND id=?",(payload,hub,row['id']))
                    self._audit(actor,'identity-membership',row['id'],row['payload'],payload)
            self._audit(actor,'identities',username,json.dumps(dict(previous)) if previous else None,json.dumps(identity))
        return {username:identity for (hub,username),identity in mapping.items() if hub==self.console}

    def backup(self, path):
        with sqlite3.connect(str(path)) as target:
            self.db.backup(target)


def configure_provider(config, path, console):
    settings=config.get('nativePoolHeads',{'enabled':False})
    if type(settings) is not dict or set(settings)!={'enabled'} or type(settings['enabled']) is not bool:
        raise ValueError('Exact default-off nativePoolHeads configuration required')
    if settings['enabled']:
        raise ValueError('V2 operator applicator and complete authoritative writer coverage unavailable')
    if config.get('moodle', {}).get('enabled', False):
        return MoodleCourseProvider()
    if not config.get('local', {}).get('enabled', True):
        raise ValueError('v1 requires the local course provider')
    return LocalCourseProvider(path, console)

async def reconcile(provider: CourseProvider, apply):
    """Send normalized snapshots to the same workspace/Share reconciliation sink."""
    for course in await provider.courses():
        identifier = course['id']
        await apply({'course': course, 'members': await provider.members(identifier),
                     'groups': await provider.groups(identifier), 'groupings': await provider.groupings(identifier)})
