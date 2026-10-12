"""Stopped workspace profile edits use central policy and audited local persistence."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tornado.httpclient import HTTPClientError

from e2x_course_hub.cps.providers import LocalCourseProvider
from e2x_course_hub.cps.workspaces import WorkspaceService


class ProfileManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = LocalCourseProvider(Path(self.tmp.name) / 'db', 'cps')
        self.compute = SimpleNamespace(
            reservation_state=AsyncMock(return_value={'active': False, 'attempt': None}),
            release_after_shutdown=AsyncMock(return_value={'released': True}),
            validate_workspace=AsyncMock(return_value={'policy_hash': 'current'}),
            register_workspace=AsyncMock(return_value={'workspace': 'w', 'storage': {'verified': True}}),
        )
        self.hub = SimpleNamespace(assert_stopped=AsyncMock())
        self.service = WorkspaceService(self.provider, self.compute, self.hub)
        await self.provider.put('courses', {'id': 'c', 'resource_ceiling': {},
            'workspace_bindings': [{'group_id': 'g', 'hub_user': 'neutral',
                'hub_server': 'rtc', 'namespace': 'trusted', 'pod': 'preserved'}]}, actor='admin')
        await self.provider.put('groups', {'id': 'g', 'course_id': 'c'}, actor='admin')
        person = '00000000-0000-4000-8000-000000000001'
        await self.provider.link_identities([{'hub': 'cps', 'username': 'student',
            'email': 'student@example.edu', 'person_id': person,
            'administrator_reviewed': True, 'verified': True}], actor='admin')
        await self.provider.put('memberships', {'id': 'm', 'course_id': 'c', 'group_id': 'g',
            'person_id': 'student', 'canonical_person_id': person}, actor='admin')
        await self.provider.put('workspaces', {'id': 'w', 'course_id': 'c', 'group_id': 'g',
            'hub_user': 'neutral', 'hub_server': 'rtc', 'namespace': 'trusted', 'pod': 'preserved',
            'profile': 'interactive-native-qualification-5', 'course_ceiling': {},
            'state': 'stopped'}, actor='admin')
        self.before = await self.service.get('w')
        self.members_before = await self.provider.members('c')

    async def asyncTearDown(self):
        self.provider.db.close()
        self.tmp.cleanup()

    async def test_profile_migration_preserves_identity_members_storage_and_audits(self):
        await self.service.change_profile('w', 'interactive-shared-5', actor='manager')
        after = await self.service.get('w')
        self.assertEqual(after, {**self.before, 'profile': 'interactive-shared-5'})
        self.assertEqual(await self.provider.members('c'), self.members_before)
        proposed = self.compute.register_workspace.call_args.args[0]
        self.assertEqual(proposed, {**self.before, 'profile': 'interactive-shared-5'})
        self.assertEqual(self.compute.validate_workspace.await_count, 2)
        self.assertEqual(self.compute.validate_workspace.call_args.args[-1],
                         self.compute.register_workspace.return_value)
        audit = self.provider.audit()[-1]
        self.assertEqual(audit['actor'], 'manager')
        self.assertEqual(audit['kind'], 'workspaces')

    async def test_lifecycle_barriers_leave_previous_profile(self):
        for field, value in [('state', 'running'), ('state', 'starting'), ('state', 'stopping'),
                             ('state', 'reconciling'), ('archive_pending', True), ('archived', True)]:
            with self.subTest(field=field, value=value):
                await self.provider.put('workspaces', {**self.before, field: value}, actor='admin')
                with self.assertRaises(ValueError):
                    await self.service.change_profile('w', 'interactive-shared-5', actor='manager')
                self.assertEqual((await self.service.get('w'))['profile'], self.before['profile'])
        self.compute.register_workspace.assert_not_awaited()

    async def test_unconfirmed_hub_policy_or_native_shutdown_preserves_local_record(self):
        for dependency, error in [(self.hub.assert_stopped, ValueError('writer exists')),
            (self.compute.validate_workspace, HTTPClientError(422)),
            (self.compute.register_workspace, HTTPClientError(422)),
            (self.compute.release_after_shutdown, HTTPClientError(409))]:
            with self.subTest(error=error):
                dependency.side_effect = error
                with self.assertRaises(type(error)):
                    await self.service.change_profile('w', 'interactive-shared-5', actor='manager')
                self.assertEqual(await self.service.get('w'), self.before)
                dependency.side_effect = None

    async def test_active_or_unknown_reservation_cannot_be_reconfigured(self):
        for state in [{'active': True, 'attempt': 'a'}, {},
                      {'active': False, 'attempt': 'a'}, {'active': 0, 'attempt': None}]:
            with self.subTest(state=state):
                self.compute.reservation_state.return_value = state
                with self.assertRaises(ValueError):
                    await self.service.change_profile('w', 'interactive-shared-5', actor='manager')
                self.assertEqual(await self.service.get('w'), self.before)
        self.compute.register_workspace.assert_not_awaited()

    async def test_external_workspace_rejected_before_central_mutation(self):
        # A provider exposing externally owned metadata must not get as far as registration.
        original_list = self.provider.list
        async def external_list(kind, *args, **kwargs):
            rows = await original_list(kind, *args, **kwargs)
            return [{**r, 'source': 'moodle'} for r in rows] if kind == 'workspaces' else rows
        self.provider.list = external_list
        with self.assertRaises(PermissionError):
            await self.service.change_profile('w', 'interactive-shared-5', actor='manager')
        self.compute.register_workspace.assert_not_awaited()

    async def test_registered_policy_failure_preserves_local_record(self):
        self.compute.validate_workspace.side_effect = [{'policy_hash': 'current'}, HTTPClientError(422)]
        with self.assertRaises(HTTPClientError):
            await self.service.change_profile('w', 'interactive-shared-5', actor='manager')
        self.assertEqual(await self.service.get('w'), self.before)

    async def test_course_barrier_or_changed_operator_binding_cannot_change_profile(self):
        original = (await self.provider.courses())[0]
        for change in [{'reconciliation_pending': True}, {'resource_ceiling': {'cpu': 'changed'}},
                       {'workspace_bindings': []}]:
            with self.subTest(change=change):
                await self.provider.put('courses', {**original, **change}, actor='admin')
                with self.assertRaises((ValueError, PermissionError)):
                    await self.service.change_profile('w', 'interactive-shared-5', actor='manager')
                self.assertEqual(await self.service.get('w'), self.before)
        self.compute.register_workspace.assert_not_awaited()

    async def test_other_group_writer_blocks_profile_change(self):
        await self.provider.put('workspaces', {**self.before, 'id': 'other', 'state': 'running'}, actor='admin')
        with self.assertRaises(ValueError):
            await self.service.change_profile('w', 'interactive-shared-5', actor='manager')
        self.assertEqual(await self.service.get('w'), self.before)
        self.compute.register_workspace.assert_not_awaited()
