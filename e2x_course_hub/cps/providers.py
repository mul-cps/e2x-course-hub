"""Source-neutral local course records. No Moodle client is imported or initialized."""
from contextlib import nullcontext
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
        try:return await method(self,*args,**kwargs)
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

    def __init__(self, path, console):
        if console not in ('cps', 'cit'):
            raise ValueError('console must be cps or cit')
        self.console = console
        self._importing = False
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        if self.db.execute('PRAGMA user_version').fetchone()[0]>3:
            self.db.close()
            raise ValueError('Console database schema is newer than this application; restore matching versions')
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
                PRAGMA user_version=3;
            ''')
            if 'canonical_person_id' not in {row[1] for row in self.db.execute('PRAGMA table_info(email_links)')}:
                self.db.execute('ALTER TABLE email_links ADD COLUMN canonical_person_id TEXT')
            self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS email_links_canonical ON email_links(console,canonical_person_id)')

    async def list(self, kind, course_id=None):
        self._kind(kind)
        rows = self.db.execute('SELECT payload FROM records WHERE console=? AND kind=? ORDER BY id', (self.console, kind))
        records = [json.loads(row['payload']) for row in rows]
        return [r for r in records if course_id is None or r.get('course_id') == course_id]

    def _kind(self, kind):
        if kind not in KINDS: raise ValueError('unknown record kind')

    @audited_mutation
    async def put(self, kind, record, *, actor):
        self._kind(kind)
        record = dict(record)
        identifier = record['id']
        if not isinstance(identifier, str) or not identifier or not actor:
            raise ValueError('id and actor are required')
        previous = self.db.execute('SELECT payload FROM records WHERE console=? AND kind=? AND id=?', (self.console, kind, identifier)).fetchone()
        old = json.loads(previous['payload']) if previous else None
        # CRUD updates preserve unspecified metadata, provenance, policy and references.
        if old:record={**old,**record}
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
        payload = json.dumps(record, sort_keys=True)
        with (nullcontext() if self._importing else self.db):
            self.db.execute('INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?)', (self.console, kind, identifier, payload))
            self._audit(actor, kind, identifier, previous['payload'] if previous else None, payload)
        return record

    @audited_mutation
    async def delete(self, kind, identifier, *, actor):
        self._kind(kind)
        previous = self.db.execute('SELECT payload FROM records WHERE console=? AND kind=? AND id=?', (self.console, kind, identifier)).fetchone()
        if not previous: raise KeyError(identifier)
        if json.loads(previous['payload']).get('source') != 'local': raise PermissionError('externally managed record')
        if kind == 'groups':
            children = await self.list('workspaces') + await self.list('memberships') + await self.list('groupings')
            if any(w.get('group_id') == identifier or identifier in w.get('group_ids',[]) for w in children):
                raise ValueError('group still has workspace/membership references; controlled lifecycle required')
        if kind == 'terms':
            term = json.loads(previous['payload'])
            term_id = term.get('term_id',identifier)
            for child in ('memberships','groups','groupings','assignments','workspaces'):
                if any(r.get('term_id') == term_id for r in await self.list(child,term['course_id'])):
                    raise ValueError('term still has references; controlled lifecycle required')
        if kind == 'courses':
            for child in KINDS[1:]:
                if await self.list(child, identifier): raise ValueError('course still has referenced records; archive workspaces first')
        with self.db:
            self.db.execute('DELETE FROM records WHERE console=? AND kind=? AND id=?', (self.console, kind, identifier))
            self._audit(actor, kind, identifier, previous['payload'], None)

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

    @audited_mutation
    async def migrate_local(self, records, *, actor):
        """Explicit import preserves IDs and all workspace references; never match names."""
        with self.db:
            self._importing = True
            try:
                for kind in KINDS:
                    for record in records.get(kind, []):
                        await self.put(kind, record, actor=actor)
            finally:
                self._importing = False

    @audited_mutation
    async def link_identities(self, rows, *, actor):
        from .identity import reviewed_email_mapping
        mapping=reviewed_email_mapping(rows)
        proofs={(row['hub'],row['username']):row for row in rows}
        with self.db:
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
                self.db.execute('INSERT INTO email_links(console,username,email,canonical_person_id) VALUES (?,?,?,?) ON CONFLICT(console,username) DO UPDATE SET email=excluded.email,canonical_person_id=excluded.canonical_person_id',(hub,username,email,person))
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
