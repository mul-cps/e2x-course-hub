"""Source-neutral local course records. No Moodle client is imported or initialized."""
from contextlib import nullcontext
import json
import sqlite3
from datetime import datetime, timezone
from typing import Protocol

KINDS = ('courses', 'terms', 'memberships', 'groups', 'groupings')

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

class LocalCourseProvider:
    capabilities = frozenset({'create', 'update', 'delete'})

    def __init__(self, path, console):
        if console not in ('cps', 'cit'):
            raise ValueError('console must be cps or cit')
        self.console = console
        self._importing = False
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS records (
                    console TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(console, kind, id));
                CREATE TABLE IF NOT EXISTS audit (
                    actor TEXT NOT NULL, console TEXT NOT NULL, kind TEXT NOT NULL,
                    target TEXT NOT NULL, previous TEXT, current TEXT,
                    time TEXT NOT NULL, outcome TEXT NOT NULL);
                PRAGMA user_version=1;
            ''')

    async def list(self, kind, course_id=None):
        self._kind(kind)
        rows = self.db.execute('SELECT payload FROM records WHERE console=? AND kind=? ORDER BY id', (self.console, kind))
        records = [json.loads(row['payload']) for row in rows]
        return [r for r in records if course_id is None or r.get('course_id') == course_id]

    def _kind(self, kind):
        if kind not in KINDS: raise ValueError('unknown record kind')

    async def put(self, kind, record, *, actor):
        self._kind(kind)
        record = dict(record)
        identifier = record['id']
        if not isinstance(identifier, str) or not identifier or not actor:
            raise ValueError('id and actor are required')
        previous = self.db.execute('SELECT payload FROM records WHERE console=? AND kind=? AND id=?', (self.console, kind, identifier)).fetchone()
        old = json.loads(previous['payload']) if previous else None
        # Backend protection is authoritative even when clients hide editing controls.
        if record.get('source', 'local') != 'local' or (old and old.get('source') != 'local'):
            raise PermissionError('externally managed records require a reviewed source migration')
        record.setdefault('source', 'local')
        record.setdefault('external_id', None)
        if record['external_id'] is not None:
            raise PermissionError('local records cannot claim external identity')
        if kind != 'courses':
            course_id = record.get('course_id')
            exists = self.db.execute("SELECT 1 FROM records WHERE console=? AND kind='courses' AND id=?", (self.console, course_id)).fetchone()
            if not exists: raise ValueError('course reference does not exist in this console')
        payload = json.dumps(record, sort_keys=True)
        with (nullcontext() if self._importing else self.db):
            self.db.execute('INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?)', (self.console, kind, identifier, payload))
            self._audit(actor, kind, identifier, previous['payload'] if previous else None, payload)
        return record

    async def delete(self, kind, identifier, *, actor):
        self._kind(kind)
        previous = self.db.execute('SELECT payload FROM records WHERE console=? AND kind=? AND id=?', (self.console, kind, identifier)).fetchone()
        if not previous: raise KeyError(identifier)
        if json.loads(previous['payload']).get('source') != 'local': raise PermissionError('externally managed record')
        if kind == 'courses':
            for child in KINDS[1:]:
                if await self.list(child, identifier): raise ValueError('course still has referenced records; archive workspaces first')
        with self.db:
            self.db.execute('DELETE FROM records WHERE console=? AND kind=? AND id=?', (self.console, kind, identifier))
            self._audit(actor, kind, identifier, previous['payload'], None)

    def _audit(self, actor, kind, identifier, old, new):
        self.db.execute('INSERT INTO audit VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (actor, self.console, kind, identifier, old, new, datetime.now(timezone.utc).isoformat(), 'success'))

    def audit(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM audit WHERE console=? ORDER BY rowid', (self.console,))]

    async def courses(self): return await self.list('courses')
    async def terms(self, course_id): return await self.list('terms', course_id)
    async def members(self, course_id): return await self.list('memberships', course_id)
    async def groups(self, course_id): return await self.list('groups', course_id)
    async def groupings(self, course_id): return await self.list('groupings', course_id)

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
