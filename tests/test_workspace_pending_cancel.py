"""Normal console Stop interrupts readiness, keeping observed cleanup serialized."""
import asyncio
import unittest
from types import SimpleNamespace

from e2x_course_hub.cps.workspaces import HubWorkspaceAdapter
import test_workspace_native_prepare as preparation


class PendingCancelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await preparation.NativePreparationTests.asyncSetUp(self)
        self.tasks = []
        self.posting = asyncio.Event()
        self.pending = asyncio.Event()
        self.deleting = asyncio.Event()
        self.delete_allowed = asyncio.Event()
        self.post_allowed = asyncio.Event()
        self.post_allowed.set()
        self.server = None
        self.ready = False
        self.fail_delete = False
        self.fail_release = False
        self.posts = self.releases = 0
        self.active = False
        outer = self

        class API:
            api_url = 'http://hub/hub/api'

            async def request(self, url, method, body=None):
                if method == 'POST':
                    outer.posts += 1
                    outer.posting.set()
                    await outer.post_allowed.wait()
                    outer.active = True
                    outer.server = {'ready': outer.ready, 'pending': None if outer.ready else 'spawn'}
                    return SimpleNamespace(code=202)
                assert method == 'DELETE'
                outer.calls.append('delete')
                outer.deleting.set()
                await outer.delete_allowed.wait()
                if outer.fail_delete:
                    raise TimeoutError('Hub shutdown not confirmed')
                outer.server = None
                return SimpleNamespace(code=204)

            async def get_user(self, user):
                if outer.server and outer.server['pending'] == 'spawn':
                    outer.pending.set()
                return {'servers': {'rtc': outer.server} if outer.server else {}}

        self.hub = HubWorkspaceAdapter(API(), timeout=2)
        async def allowed(*args): pass
        self.hub.sync_group = self.hub.replace_shares = self.hub.revoke_shares = allowed
        self.service.hub = self.hub
        async def reservation(*args):
            return {'active': self.active, 'attempt': 'exact-attempt' if self.active else None}
        async def release(*args, **kwargs):
            self.assertIsNone(self.server, 'No capacity release before actual server absence')
            if self.fail_release:
                raise RuntimeError('Trusted cleanup held')
            self.releases += 1
            self.active = False
        self.compute.reservation_state = reservation
        self.compute.release_after_shutdown = release

    async def asyncTearDown(self):
        self.delete_allowed.set()
        self.post_allowed.set()
        for task in self.tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await preparation.NativePreparationTests.asyncTearDown(self)

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def start_pending(self):
        start = self.task(self.service.start('w', actor='instructor'))
        await asyncio.wait_for(self.pending.wait(), 1)
        return start

    async def test_stop_wakes_pending_start_and_waits_for_absence_before_release(self):
        start = await self.start_pending()
        stop = self.task(self.service.close('w', actor='instructor'))
        await asyncio.wait_for(self.deleting.wait(), .5)
        self.assertFalse(stop.done())
        self.assertTrue(self.active)
        self.assertEqual(self.releases, 0)
        self.delete_allowed.set()
        with self.assertRaisesRegex(TimeoutError, 'cancelled'):
            await start
        await asyncio.wait_for(stop, 1)
        self.assertFalse(self.active)
        self.assertEqual(self.posts, 1)
        self.assertEqual((await self.service.get('w'))['state'], 'stopped')
        self.assertTrue(any(row['outcome'] == 'failure' for row in self.provider.audit()
                            if row['kind'] == 'workspace-lifecycle'))

    async def test_unconfirmed_shutdown_retains_capacity_and_unresolved_state(self):
        start = await self.start_pending()
        self.fail_delete = True
        self.delete_allowed.set()
        stop = self.task(self.service.close('w', actor='instructor'))
        results = await asyncio.wait_for(asyncio.gather(start, stop, return_exceptions=True), 1)
        self.assertTrue(all(isinstance(result, TimeoutError) for result in results))
        self.assertTrue(self.active)
        self.assertEqual(self.releases, 0)
        self.assertEqual((await self.service.get('w'))['state'], 'reconciling')

    async def test_previous_cancel_does_not_cancel_next_start(self):
        start = await self.start_pending()
        self.delete_allowed.set()
        stop = self.task(self.service.close('w', actor='instructor'))
        await asyncio.gather(start, stop, return_exceptions=True)
        self.ready = True
        await asyncio.wait_for(self.service.start('w', actor='instructor'), 1)
        self.assertEqual(self.posts, 2)
        self.assertEqual((await self.service.get('w'))['state'], 'running')
        await self.service.close('w', actor='instructor')
        self.assertFalse(self.active)

    async def test_stop_does_not_cancel_inflight_spawn_post(self):
        self.post_allowed.clear()
        start = self.task(self.service.start('w', actor='instructor'))
        await asyncio.wait_for(self.posting.wait(), 1)
        stop = self.task(self.service.close('w', actor='instructor'))
        await asyncio.sleep(.02)
        self.assertFalse(start.done())
        self.assertFalse(self.deleting.is_set())
        self.post_allowed.set()
        self.delete_allowed.set()
        with self.assertRaisesRegex(TimeoutError, 'cancelled'):
            await start
        await asyncio.wait_for(stop, 1)
        self.assertFalse(self.active)
        self.assertEqual(self.posts, 1)

    async def test_failed_central_cleanup_retains_capacity_after_pod_absence(self):
        start = await self.start_pending()
        self.fail_release = True
        self.delete_allowed.set()
        stop = self.task(self.service.close('w', actor='instructor'))
        results = await asyncio.wait_for(asyncio.gather(start, stop, return_exceptions=True), 1)
        self.assertTrue(all(isinstance(result, RuntimeError) for result in results))
        self.assertIsNone(self.server)
        self.assertTrue(self.active)
        self.assertEqual(self.releases, 0)
        self.assertEqual((await self.service.get('w'))['state'], 'reconciling')
