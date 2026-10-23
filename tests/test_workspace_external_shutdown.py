"""External Hub shutdown must converge Source state without TTL-based release."""
import unittest
import test_workspace_native_prepare as preparation

class ExternalShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await preparation.NativePreparationTests.asyncSetUp(self)
        self.server=None;self.reservation={'workspace':'w','active':False,'attempt':None}
        self.released=0;self.after_release=None
        async def status(workspace):return self.server
        async def stopped(workspace):
            if self.server is not None:raise ValueError('Server still present')
        async def reservation(identifier):return dict(self.reservation)
        async def release(*args,**kwargs):
            self.assertIs(kwargs['confirmed_by_hub'],True);self.assertIsNone(kwargs['attempt'])
            self.released+=1
            if self.after_release:await self.after_release()
        self.hub.server_status=status;self.hub.assert_stopped=stopped
        self.compute.reservation_state=reservation;self.compute.release_after_shutdown=release
        w=await self.service.get('w');await self.provider.put('workspaces',{**w,'state':'running'},actor='admin')

    async def asyncTearDown(self):await preparation.NativePreparationTests.asyncTearDown(self)

    async def test_confirmed_external_stop_persists_audited_state_and_retains_files(self):
        await self.service.reconcile_stopped('w',actor='system:hub-lifecycle')
        w=await self.service.get('w');self.assertEqual(w['state'],'stopped');self.assertIn('Files retained',w['notice'])
        self.assertEqual(self.released,1)
        self.assertTrue(any(a['actor']=='system:hub-lifecycle'and a['target']=='w'for a in self.provider.audit()))

    async def test_present_server_or_active_or_malformed_reservation_cannot_reconcile(self):
        for server,reservation in [({'ready':True},self.reservation),({'pending':'spawn'},self.reservation),
                (None,{'workspace':'w','active':True,'attempt':'held'}),
                (None,{'workspace':'foreign','active':False,'attempt':None}),
                (None,{'workspace':'w','active':False,'attempt':'old'}),
                (None,{'workspace':'w','active':0,'attempt':None}),
                (None,{'workspace':'w','active':False}),(None,{})]:
            with self.subTest(server=server,reservation=reservation):
                self.server=server;self.reservation=reservation
                await self.service.reconcile_stopped('w',actor='system:hub-lifecycle')
                self.assertEqual((await self.service.get('w'))['state'],'running')
        self.assertEqual(self.released,0)

    async def test_new_reservation_after_cleanup_prevents_stopped_publication(self):
        async def acquire():self.reservation={'workspace':'w','active':True,'attempt':'new'}
        self.after_release=acquire
        await self.service.reconcile_stopped('w',actor='system:hub-lifecycle')
        self.assertEqual((await self.service.get('w'))['state'],'running')

    async def test_hub_or_source_change_during_cleanup_prevents_stopped_publication(self):
        async def hub_changed():self.server={'ready':True}
        self.after_release=hub_changed
        with self.assertRaises(ValueError):await self.service.reconcile_stopped('w',actor='system:hub-lifecycle')
        self.assertEqual((await self.service.get('w'))['state'],'running')
        self.server=None
        async def source_changed():
            w=await self.service.get('w');await self.provider.put('workspaces',{**w,'notice':'Concurrent edit'},actor='admin')
        self.after_release=source_changed
        with self.assertRaises(ValueError):await self.service.reconcile_stopped('w',actor='system:hub-lifecycle')
        self.assertEqual((await self.service.get('w'))['notice'],'Concurrent edit')
        self.assertEqual((await self.service.get('w'))['state'],'running')

    async def test_unavailable_or_pending_cleanup_never_changes_source_state(self):
        async def unavailable(*args,**kwargs):raise ConnectionError('Unavailable')
        self.compute.release_after_shutdown=unavailable
        with self.assertRaises(ConnectionError):await self.service.reconcile_stopped('w',actor='system:hub-lifecycle')
        self.assertEqual((await self.service.get('w'))['state'],'running')

    async def test_background_cycle_continues_after_one_failure(self):
        calls=[]
        async def failed(identifier,*,actor):calls.append((identifier,actor));raise ConnectionError()
        self.service.reconcile_stopped=failed
        await self.service.reconcile_running()
        self.assertEqual(calls,[('w','system:hub-lifecycle')])

    async def test_busy_and_archived_states_are_not_reconciled(self):
        for state in ('stopped','starting','stopping','reconciling','preparing'):
            w=await self.service.get('w');await self.provider.put('workspaces',{**w,'state':state},actor='admin')
            await self.service.reconcile_stopped('w',actor='system:hub-lifecycle')
            self.assertEqual((await self.service.get('w'))['state'],state)
        w=await self.service.get('w');await self.provider.put('workspaces',{**w,'state':'running','archive_pending':True},actor='admin')
        await self.service.reconcile_stopped('w',actor='system:hub-lifecycle')
        self.assertEqual(self.released,0)

class ReconcilerStartupTests(unittest.TestCase):
    def test_real_application_start_schedules_and_finally_stops_reconciler(self):
        from types import SimpleNamespace
        from unittest.mock import Mock,patch
        from e2x_course_hub.course_service.app import CourseServiceApp
        app=CourseServiceApp();service=SimpleNamespace(reconcile_running=Mock())
        app.tornado_settings={'workspace_service':service};app.tornado_application=SimpleNamespace(listen=Mock())
        loop=Mock()
        with patch('asyncio.get_event_loop',return_value=loop),patch('tornado.ioloop.PeriodicCallback')as callback:
            app.start()
        callback.assert_called_once_with(service.reconcile_running,15000)
        callback.return_value.start.assert_called_once();callback.return_value.stop.assert_called_once()
        loop.run_forever.assert_called_once()
