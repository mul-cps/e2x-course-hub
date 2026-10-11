"""Normal Stop waits only for a typed, attempt-bound native cleanup response."""
import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tornado.httpclient import HTTPClientError, HTTPRequest, HTTPResponse
from e2x_course_hub.cps.compute import ComputePolicyClient
from e2x_course_hub.cps.providers import LocalCourseProvider
from e2x_course_hub.cps.workspaces import HubWorkspaceAdapter, WorkspaceService


DETAIL = {'version': 1, 'code': 'native_cleanup_pending', 'workspace': 'w', 'attempt': 'attempt-1'}


def error(detail, status=409, *, raw=None):
    response = HTTPResponse(HTTPRequest('https://gateway.invalid/internal/v1/reservations/release'),
        status, buffer=io.BytesIO(json.dumps({'detail': detail}).encode() if raw is None else raw))
    return HTTPClientError(status, response=response)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_exact_typed_scoped_pending_is_translated(self):
        client = ComputePolicyClient('https://gateway.invalid', 'test-token', 'cps')
        original = error(DETAIL)
        class HTTP:
            async def fetch(self, request): raise original
        with patch('e2x_course_hub.cps.compute.AsyncHTTPClient', return_value=HTTP()):
            try:
                await client.release_after_shutdown('w', confirmed_by_hub=True, actor='teacher', attempt='attempt-1')
            except HTTPClientError as exc:
                self.assertEqual(type(exc).__name__, 'NativeCleanupPending')
                self.assertEqual((exc.workspace, exc.attempt), ('w', 'attempt-1'))
            else: self.fail('missing cleanup may not be reported released')

    async def test_generic_foreign_malformed_and_other_status_remain_original(self):
        client = ComputePolicyClient('https://gateway.invalid', 'test-token', 'cps')
        cases = [('Shutdown unconfirmed or invalid release',409),
                 ({**DETAIL,'workspace':'other'},409),({**DETAIL,'attempt':'new'},409),
                 ({**DETAIL,'version':True},409),({**DETAIL,'extra':True},409),
                 ({**DETAIL,'code':'another_conflict'},409),(DETAIL,400),(None,409)]
        for detail,status in cases:
            with self.subTest(detail=detail,status=status):
                original=error(detail,status)
                class HTTP:
                    async def fetch(self, request):raise original
                with patch('e2x_course_hub.cps.compute.AsyncHTTPClient',return_value=HTTP()):
                    with self.assertRaises(HTTPClientError) as raised:
                        await client.release_after_shutdown('w',confirmed_by_hub=True,actor='teacher',attempt='attempt-1')
                self.assertIs(raised.exception,original)

    async def test_duplicate_nonfinite_truncated_or_oversized_wire_is_not_pending(self):
        client=ComputePolicyClient('https://gateway.invalid','test-token','cps')
        valid=json.dumps({'detail':DETAIL}).encode()
        bodies=[valid.replace(b'"code": "native_cleanup_pending"',
                    b'"code": "different", "code": "native_cleanup_pending"'),
                valid.replace(b'"version": 1',b'"version": true, "version": 1'),
                valid.replace(b'"workspace": "w"',b'"workspace": "foreign", "workspace": "w"'),
                valid.replace(b'"version": 1',b'"version": NaN'),
                valid[:-1], b'\xff', b' '*8193+valid]
        for raw in bodies:
            with self.subTest(raw=raw[:160]):
                original=error(None,raw=raw)
                class HTTP:
                    async def fetch(self,request):raise original
                with patch('e2x_course_hub.cps.compute.AsyncHTTPClient',return_value=HTTP()):
                    with self.assertRaises(HTTPClientError) as raised:
                        await client.release_after_shutdown('w',confirmed_by_hub=True,actor='teacher',attempt='attempt-1')
                self.assertIs(raised.exception,original)


class StopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.provider=LocalCourseProvider(Path(self.tmp.name)/'source.sqlite','cps')
        await self.provider.put('courses',{'id':'course'},actor='teacher')
        await self.provider.put('groups',{'id':'group','course_id':'course'},actor='teacher')
        await self.provider.put('workspaces',{'id':'w','course_id':'course','group_id':'group',
            'hub_user':'owner','hub_server':'rtc','profile':'native-5','state':'running',
            'namespace':'jupyterhub','pod':'exact-pod'},actor='teacher')
        self.calls=[];self.failures=[error(DETAIL)];self.active=True;self.attempt='attempt-1';self.server=None
        outer=self
        class API:
            api_url='https://hub.invalid/hub/api'
            async def request(self,url,method):outer.calls.append(('hub',method,url))
            async def get_user(self,user):
                outer.calls.append(('server',user));return {'servers':{} if outer.server is None else {'rtc':outer.server}}
        class HTTP:
            async def fetch(self,request):
                if request.method=='GET':
                    outer.calls.append(('reservation',outer.attempt))
                    return SimpleNamespace(body=json.dumps({'workspace':'w','active':outer.active,'attempt':outer.attempt}).encode())
                payload=json.loads(request.body);outer.calls.append(('release',payload))
                if outer.failures:raise outer.failures.pop(0)
                outer.active=False;return SimpleNamespace(body=b'{"released":true}')
        self.transport=patch('e2x_course_hub.cps.compute.AsyncHTTPClient',return_value=HTTP())
        self.transport.start()
        self.service=WorkspaceService(self.provider,ComputePolicyClient('https://gateway.invalid','test-token','cps'),
            HubWorkspaceAdapter(API()),native_cleanup_timeout=0.03)

    async def asyncTearDown(self):
        self.transport.stop();self.provider.db.close();self.tmp.cleanup()

    async def test_normal_stop_finishes_after_proof_without_sqlite_lock_during_wait(self):
        before=await self.service.get('w')
        async def wait(delay):
            self.assertFalse(self.provider.db.in_transaction)
            pending=await self.service.get('w')
            self.assertEqual(pending['state'],'stopping')
            self.assertIn('native GPU cleanup',pending['notice'])
            self.assertTrue(self.active)
        with patch('e2x_course_hub.cps.workspaces.asyncio.sleep',side_effect=wait):
            await self.service.close('w',actor='teacher')
        after=await self.service.get('w')
        self.assertEqual(after,{**before,'state':'stopped','notice':None,'archive_pending':False})
        releases=[c for c in self.calls if c[0]=='release']
        self.assertEqual(len(releases),2)
        self.assertTrue(all(c[1]['attempt']=='attempt-1' for c in releases))
        self.assertEqual(sum(c[0]=='server' for c in self.calls),2)
        self.assertFalse(self.active)

    async def test_timeout_retains_pending_notice_and_blocks_start_until_later_normal_stop(self):
        self.failures=[error(DETAIL)]*100
        with self.assertRaisesRegex(TimeoutError,'reservations retained'):
            await self.service.close('w',actor='teacher')
        pending=await self.service.get('w')
        self.assertEqual(pending['state'],'stopping');self.assertIn('reservations retained',pending['notice'])
        self.assertTrue(self.active)
        with self.assertRaisesRegex(ValueError,'unresolved'):await self.service.start('w',actor='teacher')
        self.failures=[]
        await self.service.close('w',actor='teacher')
        self.assertEqual((await self.service.get('w'))['state'],'stopped')

    async def test_other_conflict_is_not_retried(self):
        original=error('Shutdown unconfirmed or invalid release');self.failures=[original]
        with self.assertRaises(HTTPClientError) as raised:await self.service.close('w',actor='teacher')
        self.assertIs(raised.exception,original)
        self.assertEqual(sum(c[0]=='release' for c in self.calls),1)
        self.assertTrue(self.active)

    async def test_new_attempt_or_server_reappearance_stops_retry(self):
        for changed in ('attempt','server','workspace'):
            with self.subTest(changed=changed):
                self.calls.clear();self.failures=[error(DETAIL)];self.attempt='attempt-1';self.server=None
                row=await self.service.get('w');await self.provider.put('workspaces',{**row,'state':'running'},actor='teacher')
                async def wait(delay):
                    if changed=='attempt':self.attempt='attempt-2'
                    elif changed=='server':self.server={'pending':'spawn','ready':False}
                    else:
                        row=await self.service.get('w');await self.provider.put('workspaces',{**row,'profile':'foreign'},actor='teacher')
                with patch('e2x_course_hub.cps.workspaces.asyncio.sleep',side_effect=wait):
                    with self.assertRaises((ValueError,RuntimeError)):
                        await self.service.close('w',actor='teacher')
                self.assertEqual(sum(c[0]=='release' for c in self.calls),1)
                self.assertTrue(self.active)

    async def test_slow_recheck_is_bounded_and_never_marks_stopped(self):
        self.service.native_cleanup_timeout=0.25
        original=self.service.hub.assert_stopped
        async def slow(workspace):await asyncio.Event().wait()
        self.service.hub.assert_stopped=slow
        with self.assertRaisesRegex(TimeoutError,'cleanup is unconfirmed'):
            await asyncio.wait_for(self.service.close('w',actor='teacher'),0.5)
        self.assertEqual((await self.service.get('w'))['state'],'stopping')
        self.assertTrue(self.active)
        self.assertEqual(sum(c[0]=='release' for c in self.calls),1)
        self.service.hub.assert_stopped=original

    async def test_stopping_sibling_blocks_shared_group_start(self):
        row=await self.service.get('w')
        await self.provider.put('workspaces',{**row,'state':'stopping'},actor='teacher')
        await self.provider.put('workspaces',{**row,'id':'sibling','state':'stopped'},actor='teacher')
        with self.assertRaisesRegex(ValueError,'Shared group has an unresolved lifecycle'):
            await self.service.start('sibling',actor='teacher')

    async def test_cleanup_wait_configuration_is_closed_finite_bound(self):
        for timeout in (True,0,-1,121,float('inf'),float('nan'),'60'):
            with self.subTest(timeout=timeout),self.assertRaises(ValueError):
                WorkspaceService(self.provider,self.service.compute,self.service.hub,native_cleanup_timeout=timeout)
