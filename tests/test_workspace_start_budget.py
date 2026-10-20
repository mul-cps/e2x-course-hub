"""Start timing uses a single budget; Hub status/cleanup remain authoritative."""
import asyncio
import os
import socket
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from e2x_course_hub.cps.workspaces import HubWorkspaceAdapter, WorkspaceService
import test_workspace_native_prepare as preparation


class StartBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await preparation.NativePreparationTests.asyncSetUp(self)
        loop = asyncio.get_running_loop()
        self.slow_clock = patch.object(loop, 'slow_callback_duration', float('inf'))
        self.slow_clock.start()
        self.begin = self.now = loop.time()
        self.ready_after = 140
        self.spawned = None
        self.posts = self.deletes = 0
        outer = self

        class API:
            api_url = 'https://hub.invalid/hub/api'

            async def request(self, url, method, body=None):
                if method == 'POST':
                    outer.posts += 1
                    outer.spawned = outer.now
                    return SimpleNamespace(code=202)
                assert method == 'DELETE'
                outer.deletes += 1
                outer.spawned = None
                return SimpleNamespace(code=204)

            async def get_user(self, user):
                if outer.spawned is None:
                    return {'servers': {}}
                ready = outer.now - outer.spawned >= outer.ready_after
                return {'servers': {'rtc': {'ready': ready, 'pending': None if ready else 'spawn'}}}

        self.hub = HubWorkspaceAdapter(API(), start_timeout=300)
        async def allowed(*args): pass
        self.hub.sync_group = self.hub.replace_shares = allowed
        self.service.hub = self.hub
        original_sleep = asyncio.sleep

        async def sleep(seconds):
            self.now += seconds
            await original_sleep(0)

        original_wait_for = asyncio.wait_for

        async def wait_for(awaitable, timeout):
            if getattr(awaitable, 'cr_code', None) is asyncio.Event.wait.__code__:
                event = awaitable.cr_frame.f_locals['self']
                if not event.is_set():
                    awaitable.close()
                    await sleep(timeout)
                    raise TimeoutError()
            return await original_wait_for(awaitable, timeout)

        self.clock = patch.object(loop, 'time', side_effect=lambda: self.now)
        self.sleep = patch('e2x_course_hub.cps.workspaces.asyncio.sleep', side_effect=sleep)
        self.wait = patch('e2x_course_hub.cps.workspaces.asyncio.wait_for', side_effect=wait_for)
        self.clock.start()
        self.sleep.start()
        self.wait.start()

    async def asyncTearDown(self):
        self.wait.stop()
        self.sleep.stop()
        self.clock.stop()
        self.slow_clock.stop()
        await preparation.NativePreparationTests.asyncTearDown(self)

    async def test_ready_at_140_seconds_succeeds_within_default_start_budget(self):
        await self.service.start('w', actor='instructor')
        self.assertEqual((await self.service.get('w'))['state'], 'running')
        self.assertGreaterEqual(self.now - self.begin, 140)
        self.assertLess(self.now - self.begin, 141)
        self.assertEqual((self.posts, self.deletes), (1, 0))
        self.assertEqual(self.hub.timeout, 120)

    async def test_preparation_consumes_start_budget_instead_of_getting_another_300_seconds(self):
        provision = self.service.filesystem.provision
        async def delayed(*args, **kwargs):
            self.now += 200
            await provision(*args, **kwargs)
        self.service.filesystem.provision = delayed
        with self.assertRaises(TimeoutError):
            await self.service.start('w', actor='instructor')
        self.assertLess(self.now - self.begin, 301)
        self.assertEqual((self.posts, self.deletes), (1, 1))
        self.assertEqual((await self.service.get('w'))['state'], 'stopped')
        self.assertIn('cleanup', self.calls)

    async def test_expired_preparation_never_posts_a_spawn(self):
        provision = self.service.filesystem.provision
        async def delayed(*args, **kwargs):
            self.now += 301
            await provision(*args, **kwargs)
        self.service.filesystem.provision = delayed
        with self.assertRaisesRegex(TimeoutError, 'budget exhausted'):
            await self.service.start('w', actor='instructor')
        self.assertEqual(self.posts, 0)
        self.assertEqual((await self.service.get('w'))['state'], 'stopped')

    async def test_course_lock_wait_is_charged_to_start_budget(self):
        @asynccontextmanager
        async def read():
            self.now += 301
            yield
        self.service.course_locks['c'] = SimpleNamespace(read=read)
        with self.assertRaisesRegex(TimeoutError, 'budget exhausted'):
            await self.service.start('w', actor='instructor')
        self.assertEqual(self.posts, 0)
        self.assertNotIn('register', self.calls)

    async def test_shares_must_complete_within_same_budget_before_running(self):
        self.ready_after = 290
        async def late_shares(*args):
            await asyncio.sleep(11)
        self.hub.replace_shares = late_shares
        with self.assertRaises(TimeoutError):
            await self.service.start('w', actor='instructor')
        self.assertEqual((await self.service.get('w'))['state'], 'stopped')
        self.assertEqual((self.posts, self.deletes), (1, 1))
        self.assertIn('cleanup', self.calls)

    async def test_start_specific_budget_does_not_extend_stop_default(self):
        self.spawned = self.now
        async def uncertain_delete(*args):
            return SimpleNamespace(code=204)
        self.hub.hub.request = uncertain_delete
        with self.assertRaisesRegex(TimeoutError, 'shutdown not confirmed'):
            await self.hub.stop_confirmed({'hub_user':'neutral-new', 'hub_server':'rtc'})
        self.assertGreaterEqual(self.now - self.begin, 120)
        self.assertLess(self.now - self.begin, 121)

    async def test_terminal_hub_states_are_not_retried_under_longer_budget(self):
        get_user = self.hub.hub.get_user
        for status in [None, {'stopped': True}, {'ready': False, 'pending': None},
                       {'ready': True, 'pending': 'stop'}]:
            with self.subTest(status=status):
                async def observed(user):
                    if self.spawned is None:
                        return await get_user(user)
                    return {'servers': {'rtc': status} if status is not None else {}}
                self.hub.hub.get_user = observed
                self.posts = self.deletes = 0
                with self.assertRaisesRegex(RuntimeError, 'spawn failed'):
                    await self.service.start('w', actor='instructor')
                self.assertEqual(self.now, self.begin)
                self.assertEqual((self.posts, self.deletes), (1, 1))

    async def test_shares_error_is_preserved_and_never_reports_running(self):
        self.ready_after = 0
        async def denied(*args):
            raise PermissionError('synthetic Shares denial')
        self.hub.replace_shares = denied
        with self.assertRaisesRegex(PermissionError, 'synthetic Shares denial'):
            await self.service.start('w', actor='instructor')
        self.assertEqual(self.now, self.begin)
        self.assertEqual((await self.service.get('w'))['state'], 'stopped')
        self.assertEqual((self.posts, self.deletes), (1, 1))


class StartBudgetConfigurationTests(unittest.TestCase):
    def test_application_wires_same_operator_budget_without_changing_stop(self):
        from test_provider_startup import ProviderStartup
        for owner, seconds in [('cps', 300), ('cit', 180)]:
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as temporary:
                app = ProviderStartup().app(Path(temporary), owner, False)
                app.workspace_start_timeout = seconds
                app.compute_policy_url = 'https://policy.invalid'
                app.compute_policy_token = 'synthetic-start-budget-service-token'
                with patch.object(socket.socket, 'connect', side_effect=AssertionError('unexpected network')):
                    app.initialize([])
                try:
                    service = app.tornado_settings['workspace_service']
                    self.assertEqual(service.start_timeout, seconds)
                    self.assertEqual(service.hub.start_timeout, seconds)
                    self.assertEqual(service.hub.timeout, 120)
                finally:
                    app.tornado_settings['course_provider'].db.close()

    def test_operator_configuration_default_environment_and_python_override(self):
        from traitlets.config import Config
        from e2x_course_hub.course_service.app import CourseServiceApp
        with patch.dict(os.environ, {'E2X_WORKSPACE_START_TIMEOUT_SECONDS':'300'}):
            self.assertEqual(CourseServiceApp().workspace_start_timeout, 300)
        with patch.dict(os.environ, {'E2X_WORKSPACE_START_TIMEOUT_SECONDS':'180'}):
            self.assertEqual(CourseServiceApp().workspace_start_timeout, 180)
            self.assertEqual(CourseServiceApp(config=Config({'CourseServiceApp':{
                'workspace_start_timeout':240}})).workspace_start_timeout, 240)

    def test_operator_configuration_rejects_invalid_seconds(self):
        from traitlets import TraitError
        from e2x_course_hub.course_service.app import CourseServiceApp
        for value in ['0', '-1', '301', 'nan', 'inf', '1.5', 'invalid']:
            with self.subTest(value=value), patch.dict(os.environ, {
                    'E2X_WORKSPACE_START_TIMEOUT_SECONDS':value}):
                with self.assertRaises(TraitError):
                    _ = CourseServiceApp().workspace_start_timeout
        for value in [True, False, 0, 301, float('inf'), float('nan')]:
            with self.subTest(config=value), self.assertRaises(TraitError):
                CourseServiceApp(workspace_start_timeout=value)

    def test_direct_service_and_adapter_reject_invalid_start_budgets(self):
        for value in [True, False, 0, -1, 301, float('inf'), float('nan'), '300']:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    WorkspaceService(None, None, None, start_timeout=value)
                with self.assertRaises(ValueError):
                    HubWorkspaceAdapter(None, start_timeout=value)
