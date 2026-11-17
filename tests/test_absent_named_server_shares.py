"""Real Source lifecycle/Hub client with a deterministic native Hub HTTP boundary."""
import json
import unittest
from types import SimpleNamespace
from urllib.parse import urlsplit
from tornado.httpclient import HTTPClientError
from e2x_course_hub.api.hub_api import HubAPI
from e2x_course_hub.cps.workspaces import HubWorkspaceAdapter
import test_completion as completion


class NativeHub:
    def __init__(self):
        self.servers = {}
        self.group = ['remaining']  # A was removed before the historical Shares failure.
        self.calls = []
        self.shares = []

    async def fetch(self, request):
        path = urlsplit(request.url).path
        self.calls.append((request.method, request.url, request.body))
        if path == '/hub/api/users/neutral':
            return SimpleNamespace(body=json.dumps({'name':'neutral','servers':self.servers}).encode())
        if path == '/hub/api/users/neutral/servers/rtc':
            if request.method == 'DELETE': self.servers = {}; return SimpleNamespace(code=204,body=b'')
            self.servers = {'rtc':{'ready':True,'pending':None}}
            return SimpleNamespace(code=201,body=b'')
        if path == '/hub/api/shares/neutral/rtc':
            if 'rtc' not in self.servers:
                body=json.dumps({'status':404,'message':'No such server: neutral/rtc'}).encode()
                raise HTTPClientError(404,response=SimpleNamespace(body=body))
            if request.method == 'POST': self.shares.append(json.loads(request.body))
            return SimpleNamespace(body=b'{}')
        if path == '/hub/api/groups/g/users':
            users=json.loads(request.body)['users']
            if request.method == 'DELETE': self.group=[p for p in self.group if p not in users]
            else: self.group=sorted(set(self.group+users))
            return SimpleNamespace(body=json.dumps({'name':'g','users':self.group}).encode())
        if path == '/hub/api/groups/g':
            return SimpleNamespace(body=json.dumps({'name':'g','users':self.group}).encode())
        raise AssertionError((request.method,path))


class AbsentSharesLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await completion.CompletionTests.asyncSetUp(self)
        person='00000000-0000-4000-8000-000000000002'
        await self.provider.link_identities([{'hub':'cps','username':'remaining','email':'remaining@example.edu','person_id':person,'verified':True}],actor='admin')
        await self.provider.put('memberships',{'id':'m2','course_id':'c','group_id':'g','person_id':'remaining','canonical_person_id':person},actor='admin')
        self.native=NativeHub()
        api=HubAPI(api_url='https://hub.example/hub/api',api_token='synthetic')
        api.client=self.native
        self.service.hub=HubWorkspaceAdapter(api)
        course=(await self.provider.courses())[0]
        await self.provider.put('courses',{**course,'reconciliation_pending':True,'membership_removal_pending':'m','membership_removal_group_id':'g'},actor='teacher')

    async def asyncTearDown(self):
        await completion.CompletionTests.asyncTearDown(self)

    async def test_retry_partial_removal_of_absent_named_server_then_start_regrants_remaining_group(self):
        with self.assertRaisesRegex(ValueError,'barrier'):
            await self.service.start('w',actor='teacher')
        try:
            await self.service.remove_member('w','m',actor='teacher')
        except HTTPClientError as error:
            self.fail('Confirmed stopped absent rtc must allow original membership-removal retry: '+str(error))
        self.assertEqual([m['person_id']for m in await self.provider.members('c')],['remaining'])
        self.assertFalse((await self.provider.courses())[0]['reconciliation_pending'])
        self.assertEqual(self.native.group,['remaining'])
        self.assertEqual(self.native.shares,[])
        self.assertIn(('GET','https://hub.example/hub/api/users/neutral?include_stopped_servers=1',None),self.native.calls)
        await self.service.start('w',actor='teacher')
        self.assertEqual((await self.service.get('w'))['state'],'running')
        self.assertEqual(self.native.group,['remaining'])
        self.assertEqual(self.native.shares,[{'group':'g','scopes':['access:servers']}])
        methods=[(method,urlsplit(url).path)for method,url,_ in self.native.calls]
        self.assertLess(methods.index(('POST','/hub/api/users/neutral/servers/rtc')),max(i for i,item in enumerate(methods)if item==('POST','/hub/api/shares/neutral/rtc')))


class AbsentSharesGuardTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, *, model=None, post_error=None, delete_error=None, read_error=None):
        calls=[]
        native=json.dumps({'status':404,'message':'No such server: neutral/rtc'}).encode()
        class Client:
            async def fetch(self, request):
                calls.append((request.method,request.url))
                if '/shares/' in request.url:
                    if request.method=='DELETE':
                        if delete_error is not None: raise delete_error
                        return SimpleNamespace(body=b'{}')
                    raise post_error if post_error is not None else HTTPClientError(404,response=SimpleNamespace(body=native))
                if read_error is not None: raise read_error
                return SimpleNamespace(body=model if isinstance(model,bytes)else json.dumps(model).encode())
        api=HubAPI(api_url='https://hub.example/hub/api',api_token='synthetic');api.client=Client()
        return HubWorkspaceAdapter(api),calls

    async def test_only_exact_absent_named_key_with_stopped_rows_included_can_defer(self):
        adapter,calls=self.adapter(model={'name':'neutral','servers':{}})
        await adapter.replace_shares({'hub_user':'neutral','hub_server':'rtc'},'g',allow_absent=True)
        self.assertEqual(calls[-1],('GET','https://hub.example/hub/api/users/neutral?include_stopped_servers=1'))

    async def test_start_default_remains_strict_even_for_genuine_absence(self):
        adapter,calls=self.adapter(model={'name':'neutral','servers':{}})
        with self.assertRaises(HTTPClientError):
            await adapter.replace_shares({'hub_user':'neutral','hub_server':'rtc'},'g')
        self.assertEqual([c[0]for c in calls],['DELETE','POST'])

    async def test_unknown_malformed_active_pending_or_stopped_row_refuses(self):
        for model in [None,[],{}, {'name':'other','servers':{}}, {'name':'neutral'},
                      {'name':'neutral','servers':[]}, {'name':'neutral','servers':{'rtc':None}},
                      {'name':'neutral','servers':{'rtc':{'stopped':True}}},
                      {'name':'neutral','servers':{'rtc':{'pending':'spawn','ready':False}}},
                      {'name':'neutral','servers':{'rtc':{'pending':None,'ready':True}}},b'not-json']:
            with self.subTest(model=model):
                adapter,_=self.adapter(model=model)
                with self.assertRaises((RuntimeError,ValueError)):
                    await adapter.replace_shares({'hub_user':'neutral','hub_server':'rtc'},'g',allow_absent=True)

    async def test_other_404_bodies_and_missing_response_are_not_absence(self):
        bodies=[{}, {'status':404,'message':'Unknown route'}, {'status':404,'message':'No such server: other/rtc'},
                {'status':404,'message':'No such server: neutral/rtc','unexpected':'field'},[],None]
        errors=[HTTPClientError(404)]+[HTTPClientError(404,response=SimpleNamespace(body=json.dumps(body).encode()))for body in bodies]
        for error in errors:
            with self.subTest(error=repr(error.response)):
                adapter,calls=self.adapter(model={'name':'neutral','servers':{}},post_error=error)
                with self.assertRaises(HTTPClientError):
                    await adapter.replace_shares({'hub_user':'neutral','hub_server':'rtc'},'g',allow_absent=True)
                self.assertNotIn('GET',[c[0]for c in calls])

    async def test_post_auth_server_transport_errors_never_enter_absence_fallback(self):
        from e2x_course_hub.api.errors import HubAPIError
        for error in [HTTPClientError(code)for code in (400,401,403,409,500,599)]+[TimeoutError('network timeout')]:
            with self.subTest(error=repr(error)):
                adapter,calls=self.adapter(model={'name':'neutral','servers':{}},post_error=error)
                with self.assertRaises((HubAPIError,TimeoutError)):
                    await adapter.replace_shares({'hub_user':'neutral','hub_server':'rtc'},'g',allow_absent=True)
                self.assertEqual([c[0]for c in calls],['DELETE','POST'])

    async def test_delete_errors_do_not_enter_post_absence_fallback(self):
        from e2x_course_hub.api.errors import HubAPIError
        for error in [HTTPClientError(code)for code in (401,403,500)]+[TimeoutError('network timeout')]:
            adapter,calls=self.adapter(model={'name':'neutral','servers':{}},delete_error=error)
            with self.assertRaises((HubAPIError,TimeoutError)):
                await adapter.replace_shares({'hub_user':'neutral','hub_server':'rtc'},'g',allow_absent=True)
            self.assertEqual([c[0]for c in calls],['DELETE'])

    async def test_unknown_owner_auth_or_transport_error_in_fresh_get_stays_closed(self):
        from e2x_course_hub.api.errors import HubAPIError
        for error in [HTTPClientError(code)for code in (404,401,403,500)]+[TimeoutError('read timeout')]:
            adapter,calls=self.adapter(read_error=error)
            with self.assertRaises((HTTPClientError,HubAPIError,TimeoutError)):
                await adapter.replace_shares({'hub_user':'neutral','hub_server':'rtc'},'g',allow_absent=True)
            self.assertEqual([c[0]for c in calls],['DELETE','POST','GET'])
