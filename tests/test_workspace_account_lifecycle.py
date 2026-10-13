"""Exercise fresh-account provisioning through normal Source lifecycle methods."""
import json
import unittest

from e2x_course_hub.cps.accounts import WorkspaceAccountAdapter
from e2x_course_hub.cps.providers import LocalCourseProvider
from test_workspace_accounts import HubFixture
import test_workspace_native_prepare as preparation


class AccountLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await preparation.NativePreparationTests.asyncSetUp(self)
        self.api = HubFixture()
        self.service.accounts = WorkspaceAccountAdapter(self.provider, self.api, group='neutral-kernels')

    async def asyncTearDown(self):
        await preparation.NativePreparationTests.asyncTearDown(self)

    async def test_normal_preparation_creates_fresh_neutral_owner_before_registration_and_storage(self):
        original = self.compute.register_workspace
        async def register(workspace, members, validation):
            self.assertEqual(self.api.users['neutral-new']['groups'], ['neutral-kernels'])
            return await original(workspace, members, validation)
        self.compute.register_workspace = register
        result = await self.service.prepare_native_approval('w', actor='instructor')
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(self.api.created, ['neutral-new'])
        self.assertIn('provision', self.calls)
        await self.service.prepare_native_approval('w', actor='instructor')
        self.assertEqual(self.api.created, ['neutral-new'])

    async def test_external_or_missing_term_or_nonlocal_group_has_no_account_side_effect(self):
        original_course = (await self.provider.courses())[0]
        original_group = (await self.provider.groups('c'))[0]
        original_workspace = await self.service.get('w')
        for kind, record in (
            ('courses', {**original_course, 'source': 'external'}),
            ('groups', {**original_group, 'source': 'external'}),
            ('workspaces', {**original_workspace, 'term_id': 'missing'}),
        ):
            with self.subTest(kind=kind):
                self.provider.db.execute('UPDATE records SET payload=? WHERE console=? AND kind=? AND id=?',
                    (json.dumps(record), 'cps', kind, record['id']))
                self.provider.db.commit()
                with self.assertRaises((ValueError, PermissionError)):
                    await self.service.prepare_native_approval('w', actor='instructor')
                for restore_kind, restore in (('courses', original_course), ('groups', original_group),
                                               ('workspaces', original_workspace)):
                    self.provider.db.execute('UPDATE records SET payload=? WHERE console=? AND kind=? AND id=?',
                        (json.dumps(restore), 'cps', restore_kind, restore['id']))
                self.provider.db.commit()
        self.assertEqual(self.api.created, [])
        self.assertNotIn('provision', self.calls)

    async def test_central_policy_denial_precedes_account_mutation(self):
        async def denied(*args, **kwargs):
            raise PermissionError('central policy denied')
        self.compute.validate_workspace = denied
        with self.assertRaises(PermissionError):
            await self.service.prepare_native_approval('w', actor='instructor')
        self.assertEqual(self.api.created, [])
        self.assertNotIn('register', self.calls)

    async def test_source_binding_change_during_account_creation_preserves_account_but_blocks_storage(self):
        original = self.api.create_user
        async def changed(name):
            created = await original(name)
            course = (await self.provider.courses())[0]
            course['workspace_bindings'][0]['pod'] = 'changed-pod'
            await self.provider.put('courses', course, actor='admin')
            return created
        self.api.create_user = changed
        with self.assertRaises(ValueError):
            await self.service.prepare_native_approval('w', actor='instructor')
        self.assertIn('neutral-new', self.api.users)
        self.assertNotIn('register', self.calls)
        self.assertNotIn('provision', self.calls)
        self.assertEqual((await self.service.get('w'))['state'], 'stopped')

    async def test_normal_cpu_start_bootstraps_account_and_uses_existing_lifecycle(self):
        async def allow(*args):
            pass
        self.hub.sync_group = self.hub.start = self.hub.replace_shares = allow
        await self.service.start('w', actor='instructor')
        self.assertEqual(self.api.created, ['neutral-new'])
        self.assertEqual((await self.service.get('w'))['state'], 'running')
        self.assertIn('register', self.calls)
        self.assertIn('provision', self.calls)

    async def _start_assignment_fixture(self):
        await self.provider.put('assignments', {'id': 'a', 'course_id': 'c', 'term_id': 't',
                                               'state': 'open', 'mode': 'group'}, actor='admin')
        await self.provider.put('workspaces', {**await self.service.get('w'), 'assignment_id': 'a'},
                                actor='admin')
        async def allow(*args):
            pass
        self.hub.sync_group = self.hub.start = self.hub.replace_shares = allow

    async def _assert_start_denied_with_account_preserved(self):
        with self.assertRaises(ValueError):
            await self.service.start('w', actor='instructor')
        self.assertIn('neutral-new', self.api.users)
        self.assertEqual((await self.service.get('w'))['state'], 'stopped')
        self.assertEqual((await self.provider.list('assignments'))[0]['state'], 'closed')
        self.assertNotIn('register', self.calls)
        self.assertNotIn('provision', self.calls)

    async def test_assignment_closure_during_account_creation_blocks_start(self):
        await self._start_assignment_fixture()
        original = self.api.create_user
        async def close_assignment(name):
            result = await original(name)
            await self.provider.put('assignments', {'id': 'a', 'state': 'closed'}, actor='admin')
            return result
        self.api.create_user = close_assignment
        await self._assert_start_denied_with_account_preserved()

    async def test_assignment_closure_during_final_hub_stop_check_blocks_start(self):
        await self._start_assignment_fixture()
        async def close_assignment(workspace):
            await self.provider.put('assignments', {'id': 'a', 'state': 'closed'}, actor='admin')
        self.hub.assert_stopped = close_assignment
        await self._assert_start_denied_with_account_preserved()

    async def test_assignment_closure_after_final_check_blocks_conditional_guard_across_instances(self):
        await self._start_assignment_fixture()
        other = LocalCourseProvider(self.provider.db.execute('PRAGMA database_list').fetchone()[2], 'cps')
        original = self.provider.put
        async def race(kind, record, **kwargs):
            if kind == 'workspaces' and record.get('state') == 'starting':
                await other.put('assignments', {'id': 'a', 'state': 'closed'}, actor='admin')
            return await original(kind, record, **kwargs)
        self.provider.put = race
        try:
            await self._assert_start_denied_with_account_preserved()
        finally:
            other.db.close()

    async def test_same_snapshot_cannot_persist_two_cross_instance_start_guards(self):
        await self._start_assignment_fixture()
        other = LocalCourseProvider(self.provider.db.execute('PRAGMA database_list').fetchone()[2], 'cps')
        workspace = await self.service.get('w')
        expected = self.provider.lifecycle_snapshot('c')
        try:
            await self.provider.put('workspaces', {**workspace, 'state': 'starting'}, actor='instructor',
                                    expected_lifecycle=expected)
            with self.assertRaises(ValueError):
                await other.put('workspaces', {**workspace, 'state': 'starting'}, actor='other-instructor',
                                expected_lifecycle=expected)
            self.assertEqual((await self.service.get('w'))['state'], 'starting')
            self.assertFalse(other.db.in_transaction)
            self.assertTrue(any(row['actor'] == 'other-instructor' and row['outcome'] == 'failure'
                                for row in other.audit()))
        finally:
            other.db.close()
