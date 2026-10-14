"""Fresh neutral Hub ownership must never be inferred from an existing account."""
import asyncio
import copy
import hashlib
import json
import tempfile
import sqlite3
import unittest
from pathlib import Path

from tornado.httpclient import HTTPClientError

from e2x_course_hub.api.errors import GroupNotFoundError, UserNotFoundError
from e2x_course_hub.cps.providers import LocalCourseProvider
from e2x_course_hub.cps.accounts import WorkspaceAccountAdapter


GROUP = 'cps-workspace-kernel-accounts'
OWNER = 'cps-ws-' + hashlib.sha256(b'cps\0w').hexdigest()[:32]


class HubFixture:
    """The Hub boundary, including immutable creation time and effective roles."""
    def __init__(self, group='neutral-kernels'):
        self.api_url = 'https://hub.invalid/hub/api'
        self.users = {}
        self.group = {'name': group, 'users': [], 'roles': ['cps-workspace-kernel']}
        self.created = []
        self.added = []
        self.create_failure = False
        self.grant_failure = False
        self.create_gate = None
        self.receipts = {}
        self.posts = []
        self.auth_header = {"Authorization": "token fixture-source-service"}

        class UnenrolledHubClient:
            async def fetch(self, request):
                # Existing fixture owners predate optional runtime enrollment.
                assert '/cps/legacy-workspace-runtime-custody?' in request.url
                raise HTTPClientError(404)

        self.client = UnenrolledHubClient()

    async def get_group(self, name):
        if self.group is None:
            raise GroupNotFoundError(name)
        assert name == self.group['name']
        return copy.deepcopy(self.group)

    async def get_user(self, name):
        if name not in self.users:
            raise UserNotFoundError(name)
        return copy.deepcopy(self.users[name])

    async def create_user(self, name):
        if name in self.users:
            raise RuntimeError('409 user exists')
        self.created.append(name)
        self.users[name] = {'name': name, 'admin': False, 'roles': ['user'], 'groups': [],
                            'servers': {}, 'created': '2026-10-13T12:00:00Z'}
        if self.create_gate:
            await self.create_gate.wait()
        if self.create_failure:
            raise TimeoutError('POST outcome unknown')
        return copy.deepcopy(self.users[name])

    async def create_workspace_account(self, body):
        self.posts.append(copy.deepcopy(body))
        name = 'cps-ws-' + hashlib.sha256(('cps\0' + body['workspace']).encode()).hexdigest()[:32]
        digest = hashlib.sha256(json.dumps(dict(source='cps', **body), sort_keys=True,
            separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
        if name in self.receipts:
            receipt = self.receipts[name]
            user = self.users.get(name, {})
            if (receipt['descriptor_hash'] != digest or user.get('created') != receipt['identity']['created']
                    or user.get('admin') is not False or user.get('groups') != [self.group['name']]
                    or user.get('roles') != ['user'] or name not in self.group['users']):
                raise PermissionError('Permanent Hub identity/binding revoked or changed')
            return copy.deepcopy(receipt)
        if name in self.users:
            raise PermissionError('Existing user has no permanent Hub receipt')
        if self.grant_failure:
            raise RuntimeError('Atomic group grant unavailable')
        self.created.append(name)
        self.users[name] = dict(name=name, admin=False, roles=['user'],
            groups=[self.group['name']], servers={}, created='2026-10-13T12:00:00Z')
        self.group['users'].append(name)
        self.receipts[name] = dict(version=1, kind='hub_neutral_creation', source='cps',
            workspace=body['workspace'], owner=name, descriptor_hash=digest,
            identity=dict(id=len(self.created), name=name, created=self.users[name]['created']),
            group=self.group['name'], role='cps-workspace-kernel')
        if self.create_gate:
            await self.create_gate.wait()
        if self.create_failure:
            raise TimeoutError('POST outcome unknown')
        return copy.deepcopy(self.receipts[name])

    async def add_users_to_group(self, name, users):
        assert name == self.group['name']
        if self.grant_failure:
            raise RuntimeError('group grant unavailable')
        self.added.extend(users)
        for user in users:
            self.users[user]['groups'] = [self.group['name']]
            # JupyterHub's user model lists direct roles, not inherited group roles.
            self.users[user]['roles'] = ['user']
            self.group['users'].append(user)


class WorkspaceAccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'source.sqlite'
        self.provider = LocalCourseProvider(self.path, 'cps')
        self.hub = HubFixture(group=GROUP)
        self.workspace = {'id': 'w', 'course_id': 'c', 'term_id': 't', 'group_id': 'g',
                          'hub_user': OWNER, 'hub_server': 'rtc',
                          'namespace': 'jupyterhub', 'pod': 'jupyter-fresh-owner--rtc'}
        self.adapter = WorkspaceAccountAdapter(self.provider, self.hub, group=GROUP)

    async def asyncTearDown(self):
        self.provider.db.close()
        self.tmp.cleanup()

    async def test_fresh_account_gets_only_fixed_kernel_role_and_retry_survives_restart(self):
        await self.adapter.ensure(self.workspace, actor='instructor')
        self.assertEqual(self.hub.users[OWNER]['roles'], ['user'])
        self.assertEqual(self.hub.users[OWNER]['groups'], [GROUP])
        self.assertFalse(self.hub.users[OWNER]['admin'])
        self.provider.db.close()
        self.provider = LocalCourseProvider(self.path, 'cps')
        adapter = WorkspaceAccountAdapter(self.provider, self.hub, group=GROUP)
        await adapter.ensure(self.workspace, actor='instructor')
        self.assertEqual(self.hub.created, [OWNER])
        self.assertEqual(self.hub.added, [])
        self.assertTrue(any(row['kind'] == 'workspace_accounts' and row['actor'] == 'instructor'
                            for row in self.provider.audit()))

    async def test_existing_neutral_or_personal_account_is_never_adopted_or_modified(self):
        for roles in (['user'], ['user', 'cps-workspace-kernel']):
            with self.subTest(roles=roles):
                self.hub.users[OWNER] = {'name': OWNER, 'admin': False,
                    'roles': roles, 'groups': [], 'servers': {}, 'created': '2026-10-12T12:00:00Z'}
                before = copy.deepcopy(self.hub.users)
                with self.assertRaises(PermissionError):
                    await self.adapter.ensure(self.workspace, actor='instructor')
                self.assertEqual(self.hub.users, before)
                self.assertEqual(self.hub.added, [])
                self.assertEqual(self.hub.created, [])

    async def test_account_owner_and_workspace_binding_are_reserved_forever(self):
        await self.adapter.ensure(self.workspace, actor='instructor')
        for changed in ({'id': 'another'}, {'hub_user': 'another-owner'},
                        {'hub_server': 'other'}, {'pod': 'another-pod'}, {'course_id': 'other'}):
            with self.subTest(changed=changed), self.assertRaises(PermissionError):
                await self.adapter.ensure({**self.workspace, **changed}, actor='instructor')
        self.assertEqual(self.hub.created, [OWNER])

    async def test_unknown_post_outcome_retries_exact_permanent_hub_receipt(self):
        self.hub.create_failure = True
        with self.assertRaises(TimeoutError):
            await self.adapter.ensure(self.workspace, actor='instructor')
        self.hub.create_failure = False
        await self.adapter.ensure(self.workspace, actor='instructor')
        self.assertIn(OWNER, self.hub.users)
        self.assertEqual(self.hub.added, [])
        self.assertEqual(self.hub.created, [OWNER])

    async def test_failed_atomic_role_grant_retries_permanent_creation(self):
        self.hub.grant_failure = True
        with self.assertRaises(RuntimeError):
            await self.adapter.ensure(self.workspace, actor='instructor')
        self.hub.grant_failure = False
        await self.adapter.ensure(self.workspace, actor='instructor')
        self.assertEqual(self.hub.created, [OWNER])
        self.assertEqual(self.hub.users[OWNER]['groups'], [GROUP])

    async def test_recreated_or_privileged_or_unrelated_running_identity_is_denied(self):
        await self.adapter.ensure(self.workspace, actor='instructor')
        original = copy.deepcopy(self.hub.users[OWNER])
        for changed in ({'created': '2026-10-14T12:00:00Z'}, {'admin': True},
                        {'roles': ['user', 'cps-workspace-kernel', 'admin']},
                        {'servers': {'personal': {'ready': True}}},
                        {'servers': {'rtc': {'ready': True, 'state': {'pod_name': 'other-pod'}}}}):
            with self.subTest(changed=changed):
                self.hub.users[OWNER] = {**copy.deepcopy(original), **changed}
                with self.assertRaises((PermissionError, ValueError)):
                    await self.adapter.ensure(self.workspace, actor='instructor')
        self.assertEqual(self.hub.added, [])

    async def test_missing_configured_group_or_kernel_role_fails_before_creation(self):
        for group in (None, {'name': GROUP, 'roles': [], 'users': []}):
            self.hub.group = group
            with self.assertRaises(RuntimeError):
                await self.adapter.ensure(self.workspace, actor='instructor')
        adapter = WorkspaceAccountAdapter(self.provider, self.hub, group='')
        with self.assertRaises(RuntimeError):
            await adapter.ensure(self.workspace, actor='instructor')
        self.assertEqual(self.hub.created, [])

    async def test_simultaneous_source_instances_cannot_adopt_the_same_owner(self):
        self.hub.create_gate = asyncio.Event()
        second = LocalCourseProvider(self.path, 'cps')
        adapter = WorkspaceAccountAdapter(second, self.hub, group=GROUP)
        task = asyncio.create_task(self.adapter.ensure(self.workspace, actor='instructor'))
        try:
            for _ in range(10):
                if self.hub.created:
                    break
                await asyncio.sleep(0)
            self.assertEqual(self.hub.created, [OWNER])
            await adapter.ensure(self.workspace, actor='instructor')
            self.hub.create_gate.set()
            await task
        finally:
            self.hub.create_gate.set()
            await task
            second.db.close()
        self.assertEqual(self.hub.created, [OWNER])

    async def test_receipt_persistence_failure_retries_same_hub_receipt_and_preserves_user(self):
        self.provider.db.execute("CREATE TRIGGER fail_receipt BEFORE UPDATE OF receipt ON workspace_accounts BEGIN SELECT RAISE(ABORT, 'receipt unavailable'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.adapter.ensure(self.workspace, actor='instructor')
        self.provider.db.execute('DROP TRIGGER fail_receipt')
        await self.adapter.ensure(self.workspace, actor='instructor')
        self.assertIn(OWNER, self.hub.users)
        self.assertEqual(self.hub.created, [OWNER])
        self.assertEqual(self.hub.added, [])

    async def test_v6_database_migrates_without_claiming_any_existing_hub_owner(self):
        await self.provider.put('courses', {'id': 'legacy-course'}, actor='admin')
        before = await self.provider.courses()
        self.provider.db.execute('DROP TABLE workspace_accounts')
        self.provider.db.execute('PRAGMA user_version=6')
        self.provider.db.commit()
        self.provider.db.close()
        self.provider = LocalCourseProvider(self.path, 'cps')
        self.assertEqual(await self.provider.courses(), before)
        self.assertEqual(self.provider.db.execute('PRAGMA user_version').fetchone()[0], 7)
        self.assertEqual(self.provider.db.execute('SELECT COUNT(*) FROM workspace_accounts').fetchone()[0], 0)

    async def test_backup_preserves_receipt_and_foreign_console_cannot_reopen(self):
        await self.adapter.ensure(self.workspace, actor='instructor')
        backup = Path(self.tmp.name) / 'backup.sqlite'
        self.provider.backup(backup)
        restored = LocalCourseProvider(backup, 'cps')
        try:
            self.assertEqual(restored.db.execute('PRAGMA user_version').fetchone()[0], 7)
            await WorkspaceAccountAdapter(restored, self.hub, group=GROUP).ensure(
                self.workspace, actor='instructor')
        finally:
            restored.db.close()
        with self.assertRaises(ValueError):
            LocalCourseProvider(backup, 'cit')
        self.assertEqual(self.hub.created, [OWNER])


if __name__ == '__main__':
    unittest.main()
