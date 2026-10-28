"""Pure authorization boundary checks for managed local shared-roster additions.

End-to-end permission/registration reconciliation is covered by the managed
PostgreSQL operation test after the shared database runtime is released.
"""
import asyncio
import sqlite3
import uuid

import pytest

from e2x_course_hub.cps.shared_lifecycle import SharedLifecycleController

PERSON_1 = '00000000-0000-4000-8000-000000000011'
PERSON_2 = '00000000-0000-4000-8000-000000000012'


class Installation:
    def check_source(self, provider):
        assert provider.console == 'cps'


class Provider:
    console = 'cps'
    managed_installation = Installation()

    def __init__(self, *, verified=True):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE email_links(console TEXT, username TEXT, canonical_person_id TEXT, verified INTEGER)')
        self.db.executemany('INSERT INTO email_links VALUES(?,?,?,?)', [
            ('cps', 'student-1', PERSON_1, 1),
            ('cps', 'student-2', PERSON_2, int(verified)),
        ])


class Journal:
    def capture_source(self):
        return {'records': {'rows': []}}


class Controller:
    binding = type('Binding', (), {'console': 'cps', 'hub_api': 'https://hub.example/hub/api'})()
    legacy_roles = None

    def __init__(self, *, verified=True, workspace_state='stopped'):
        self.provider = Provider(verified=verified)
        self.journal = Journal()
        self.records = {
            'courses': [dict(id='course', source='local', workspace_bindings=[
                dict(group_id='group', hub_user='workspace-owner', hub_server='group')])],
            'terms': [dict(id='term', course_id='course', source='local')],
            'groups': [dict(id='group', source='local', role='student', course_id='course', term_id='term')],
            'memberships': [dict(id='member-1', person_id='student-1', canonical_person_id=PERSON_1,
                group_id='group', course_id='course', term_id='term', role='student', source='local')],
            'workspaces': [dict(id='workspace', source='local', course_id='course', term_id='term',
                group_id='group', state=workspace_state, hub_user='workspace-owner', hub_server='group',
                profile='interactive-cpu')],
        }

    async def _actor(self, visitor):
        return dict(name='administrator', admin=True, groups=[])

    def _clean(self):
        pass

    async def _rpc(self, method):
        return {'state': 'active'}

    def _records(self, kind):
        return self.records.get(kind, [])

    async def _hub(self, path):
        return {'name': 'student-2', 'servers': {}}


def membership(*, canonical=None):
    row = dict(id='member-2', person_id='student-2', group_id='group', course_id='course',
        term_id='term', role='student', source='local', external_id=None)
    if canonical is not None:
        row['canonical_person_id'] = canonical
    return row


def test_add_source_derives_canonical_person_from_verified_directory_mapping():
    controller = Controller()
    lifecycle = object.__new__(SharedLifecycleController)
    lifecycle.controller = controller

    _, _, request, writers = asyncio.run(lifecycle.add_source(object(), 'workspace', membership()))

    assert writers == controller.records['workspaces']
    assert request['roster_after'] == [
        dict(membership_id='member-1', username='student-1', canonical_person_id=PERSON_1),
        dict(membership_id='member-2', username='student-2', canonical_person_id=PERSON_2),
    ]
    assert request['action'] == 'shared-student-add'


def test_add_source_rejects_unverified_or_conflicting_identity_mapping():
    lifecycle = object.__new__(SharedLifecycleController)
    lifecycle.controller = Controller(verified=False)
    with pytest.raises(ValueError, match='verified canonical directory mapping'):
        asyncio.run(lifecycle.add_source(object(), 'workspace', membership()))

    lifecycle.controller = Controller()
    with pytest.raises(ValueError, match='match the verified directory mapping'):
        asyncio.run(lifecycle.add_source(object(), 'workspace', membership(canonical=str(uuid.uuid4()))))


def test_add_source_requires_stopped_group_workspace_and_existing_hub_account():
    lifecycle = object.__new__(SharedLifecycleController)
    lifecycle.controller = Controller(workspace_state='running')
    with pytest.raises(ValueError, match='Only stopped'):
        asyncio.run(lifecycle.add_source(object(), 'workspace', membership()))

    controller = Controller()
    async def missing_account(path):
        return {'name': 'other-user'}
    controller._hub = missing_account
    lifecycle.controller = controller
    with pytest.raises(ValueError, match='Existing Hub student account'):
        asyncio.run(lifecycle.add_source(object(), 'workspace', membership()))


def test_add_journals_only_the_directory_derived_canonical_person():
    lifecycle = object.__new__(SharedLifecycleController)
    expected = dict(action='shared-student-add', membership_id='member-2',
        roster_after=[dict(membership_id='member-2', username='student-2', canonical_person_id=PERSON_2)])
    captured = {}

    async def add_source(visitor, workspace, record):
        return object(), {'source': 'preimage'}, expected, [{'id': 'workspace'}]

    async def prepare(visitor, current, source, request, writers, *, membership_record=None):
        captured['membership_record'] = membership_record
        return {'state': 'prepared'}

    lifecycle.add_source = add_source
    lifecycle._prepare = prepare
    row = {'id': 'member-2', 'person_id': 'student-2', 'source': 'local', 'external_id': None}
    assert asyncio.run(lifecycle.add(object(), 'workspace', row)) == {'state': 'prepared'}
    assert captured['membership_record'] == {**row, 'canonical_person_id': PERSON_2}


def test_remove_source_derives_exact_surviving_local_roster():
    controller = Controller()
    controller.records['memberships'].append(dict(id='member-2', person_id='student-2',
        canonical_person_id=PERSON_2, group_id='group', course_id='course', term_id='term',
        role='student', source='local'))
    lifecycle = object.__new__(SharedLifecycleController)
    lifecycle.controller = controller

    _, _, request, writers = asyncio.run(lifecycle.source(object(), 'workspace', 'member-1'))

    assert [writer['id'] for writer in writers] == ['workspace']
    assert request['roster_before'] == [
        dict(membership_id='member-1', username='student-1', canonical_person_id=PERSON_1),
        dict(membership_id='member-2', username='student-2', canonical_person_id=PERSON_2),
    ]
    assert request['roster_after'] == [
        dict(membership_id='member-2', username='student-2', canonical_person_id=PERSON_2),
    ]
