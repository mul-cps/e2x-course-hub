import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from e2x_course_hub.cps.workspaces import HubWorkspaceAdapter


class HubReadinessTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, observations, *, code=202, timeout=120):
        api = SimpleNamespace(
            api_url='http://hub/hub/api',
            request=AsyncMock(return_value=SimpleNamespace(code=code)),
            get_user=AsyncMock(side_effect=observations),
        )
        return HubWorkspaceAdapter(api, timeout=timeout), api

    def workspace(self):
        return {'hub_user':'neutral', 'hub_server':'rtc', 'profile':'shared-5'}

    async def test_accepted_spawn_waits_for_observed_readiness(self):
        adapter, api = self.adapter([
            {'servers':{'rtc':{'pending':'spawn','ready':False,'stopped':False}}},
            {'servers':{'rtc':{'pending':None,'ready':True,'stopped':False}}},
        ])
        with patch('e2x_course_hub.cps.workspaces.asyncio.sleep', new=AsyncMock()):
            await adapter.start(self.workspace())
        self.assertEqual(api.get_user.await_count, 2)
        self.assertEqual(json.loads(api.request.call_args.args[2]), {'profile':'shared-5'})

    async def test_accepted_spawn_that_disappears_fails(self):
        adapter, _ = self.adapter([
            {'servers':{'rtc':{'pending':'spawn','ready':False}}},
            {'servers':{}},
        ])
        with patch('e2x_course_hub.cps.workspaces.asyncio.sleep', new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, 'spawn failed'):
                await adapter.start(self.workspace())

    async def test_created_response_alone_does_not_prove_readiness(self):
        adapter, _ = self.adapter([{'servers':{'rtc':{'pending':None,'ready':False}}}], code=201)
        with self.assertRaisesRegex(RuntimeError, 'spawn failed'):
            await adapter.start(self.workspace())

    async def test_pending_spawn_has_a_bounded_deadline(self):
        adapter, _ = self.adapter([
            {'servers':{'rtc':{'pending':'spawn','ready':False}}},
        ], timeout=0)
        with self.assertRaisesRegex(TimeoutError, 'readiness not confirmed'):
            await adapter.start(self.workspace())

    async def test_deadline_also_bounds_a_stalled_status_read(self):
        import asyncio
        adapter, api = self.adapter([],timeout=0.02)
        cancelled=asyncio.Event()
        async def stalled(username):
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        api.get_user=stalled
        with self.assertRaisesRegex(TimeoutError,'readiness not confirmed'):
            await asyncio.wait_for(adapter.start(self.workspace()),1)
        self.assertTrue(cancelled.is_set())

    async def test_unexpected_post_response_cannot_publish_running(self):
        adapter, api=self.adapter([],code=200)
        with self.assertRaisesRegex(RuntimeError,'did not accept'):
            await adapter.start(self.workspace())
        self.assertEqual(api.get_user.await_count,0)

    async def test_missing_server_scope_is_not_shutdown_evidence(self):
        adapter, _ = self.adapter([{'name':'neutral'}])
        with self.assertRaisesRegex(RuntimeError, 'server status unavailable'):
            await adapter.stop_confirmed(self.workspace())

    async def test_ready_flag_during_stop_is_not_a_successful_spawn(self):
        adapter, _ = self.adapter([
            {'servers':{'rtc':{'pending':'stop','ready':True,'stopped':False}}},
        ])
        with self.assertRaisesRegex(RuntimeError, 'spawn failed'):
            await adapter.start(self.workspace())
