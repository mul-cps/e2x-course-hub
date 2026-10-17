"""A failed DELETE is uncertain; only authoritative Hub absence confirms Stop."""
import asyncio
import json
import unittest
from types import SimpleNamespace

from tornado.httpclient import HTTPClientError
from e2x_course_hub.api.errors import HubAPIError
from e2x_course_hub.api.hub_api import HubAPI
from e2x_course_hub.cps.workspaces import HubWorkspaceAdapter
import test_workspace_pending_cancel as pending


class StopObservationTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, observations, *, delete=500, timeout=.5):
        calls = []
        class Client:
            async def fetch(self, request):
                calls.append(request.method)
                if request.method == 'DELETE':
                    if isinstance(delete, BaseException): raise delete
                    raise HTTPClientError(delete)
                value = observations.pop(0) if len(observations) > 1 else observations[0]
                if isinstance(value, BaseException): raise value
                return SimpleNamespace(body=json.dumps(value).encode())
        hub = HubAPI(api_url='https://hub.example/hub/api', api_token='synthetic')
        hub.client = Client()
        return HubWorkspaceAdapter(hub, timeout=timeout), calls

    def workspace(self):
        return {'hub_user':'neutral-new', 'hub_server':'rtc'}

    async def test_actual_wrapped_delete_500_observes_pending_then_absent(self):
        adapter, calls = self.adapter([
            {'servers':{'rtc':{'pending':'spawn','ready':False}}}, {'servers':{}},
        ])
        self.assertTrue(await adapter.stop_confirmed(self.workspace()))
        self.assertEqual(calls, ['DELETE', 'GET', 'GET'])

    async def test_delete_failure_with_server_present_never_confirms(self):
        adapter, calls = self.adapter([{'servers':{'rtc':{'pending':'spawn'}}}], timeout=.01)
        with self.assertRaisesRegex(TimeoutError, 'reservations retained'):
            await adapter.stop_confirmed(self.workspace())
        self.assertEqual(calls.count('DELETE'), 1)

    async def test_actual_wrapped_delete_auth_and_other_client_errors_are_not_ignored(self):
        for code in (400, 401, 403, 409, 422):
            with self.subTest(code=code):
                adapter, calls = self.adapter([{'servers':{}}], delete=code)
                with self.assertRaises(HubAPIError): await adapter.stop_confirmed(self.workspace())
                self.assertEqual(calls, ['DELETE'])

    async def test_untyped_wrapper_is_not_ignored(self):
        adapter, calls = self.adapter([{'servers':{}}], delete=HubAPIError('Unknown failure'))
        with self.assertRaises(HubAPIError): await adapter.stop_confirmed(self.workspace())
        self.assertEqual(calls, ['DELETE'])

    async def test_failed_or_scope_missing_status_cannot_prove_absence(self):
        for status in (HTTPClientError(503), HTTPClientError(401), {'name':'neutral-new'}):
            with self.subTest(status=type(status).__name__):
                adapter, calls = self.adapter([status])
                with self.assertRaises((HubAPIError, RuntimeError)):
                    await adapter.stop_confirmed(self.workspace())
                self.assertEqual(calls, ['DELETE', 'GET'])

    async def test_delete_404_and_transport_timeout_still_require_absence(self):
        for failure in (404, 599, TimeoutError('Request timed out')):
            with self.subTest(failure=type(failure).__name__):
                adapter, calls = self.adapter([{'servers':{}}], delete=failure)
                self.assertTrue(await adapter.stop_confirmed(self.workspace()))
                self.assertEqual(calls, ['DELETE', 'GET'])

    async def test_stalled_status_read_is_bounded_and_cannot_confirm(self):
        adapter, calls = self.adapter([{'servers':{}}], timeout=.01)
        async def stalled(_): await asyncio.Event().wait()
        adapter.hub.get_user = stalled
        with self.assertRaisesRegex(TimeoutError, 'reservations retained'):
            await asyncio.wait_for(adapter.stop_confirmed(self.workspace()), .5)
        self.assertEqual(calls, ['DELETE'])

    async def test_stalled_delete_uses_the_same_deadline_and_never_confirms(self):
        adapter, calls = self.adapter([{'servers':{}}], timeout=.01)
        async def stalled(*args): await asyncio.Event().wait()
        adapter.hub.request = stalled
        with self.assertRaisesRegex(TimeoutError, 'reservations retained'):
            await asyncio.wait_for(adapter.stop_confirmed(self.workspace()), .5)
        self.assertEqual(calls, [])


class ServiceStopObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await pending.PendingCancelTests.asyncSetUp(self)

    async def asyncTearDown(self):
        await pending.PendingCancelTests.asyncTearDown(self)

    async def test_normal_service_release_follows_observed_absence_after_delete_error(self):
        self.active = True
        self.server = {'pending':'spawn', 'ready':False}
        absent = asyncio.Event()
        self.assertions = []
        outer = self
        class Client:
            reads = 0
            async def fetch(self, request):
                if request.method == 'DELETE':
                    outer.assertions.append('delete')
                    raise HTTPClientError(500, 'Spawner still pending')
                self.reads += 1
                if self.reads == 1:
                    return SimpleNamespace(body=json.dumps({'servers':{'rtc':outer.server}}).encode())
                await absent.wait()
                outer.server = None
                return SimpleNamespace(body=b'{"servers":{}}')
        api = HubAPI(api_url='https://hub.example/hub/api', api_token='synthetic')
        api.client = Client()
        self.hub.hub = api
        stop = asyncio.create_task(self.service.close('w', actor='instructor'))
        self.tasks.append(stop)
        await asyncio.sleep(.02)
        self.assertFalse(stop.done())
        self.assertTrue(self.active)
        self.assertEqual(self.releases, 0)
        absent.set()
        await asyncio.wait_for(stop, 1)
        self.assertFalse(self.active)
        self.assertEqual(self.releases, 1)
        self.assertEqual(self.assertions, ['delete'])
