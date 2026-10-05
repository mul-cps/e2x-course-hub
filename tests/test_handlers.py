import time
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from tornado import web
from tornado.testing import AsyncHTTPTestCase
from e2x_course_hub.cps.proxy import ComputeVisitorProxy
from e2x_course_hub.cps.handlers import RecordsHandler
from e2x_course_hub.cps.providers import LocalCourseProvider
from e2x_course_hub.schema.user import User

class HandlersTest(AsyncHTTPTestCase):
    def get_app(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = LocalCourseProvider(Path(self.tmp.name)/'db','cps')
        self.admin = True
        outer = self
        class Hub:
            async def get_user(self,name): return {'admin':outer.admin}
        class Auth:
            def get_token(self,handler): return handler.request.headers.get('X-Visitor')
        class Proxy(ComputeVisitorProxy):
            _hub_auth = Auth()
            def get_current_user(self): return {'name':self.request.headers.get('X-Visitor')}
        class Records(RecordsHandler):
            def get_current_user(self): return {'name':'admin','admin':True}
            async def get_user(self): return User(username='admin',admin=True,groups=[])
        return web.Application([(r'/proxy/(.*)',Proxy),(r'/records/(.*)',Records)],
            last_config_check=time.time(),course_provider=self.provider,api=NS(course_api=NS(hub_api=Hub())),
            compute_gateway_url='https://gateway.example',console_owner='cps',cookie_secret='test')

    def tearDown(self):
        self.provider.db.close()
        self.tmp.cleanup()
        super().tearDown()

    def test_two_actual_visitors_have_distinct_identity(self):
        calls=[]
        class Client:
            async def fetch(self,request,**kwargs):
                calls.append(request)
                return NS(code=200,headers={'Content-Type':'application/json'},
                    body=json.dumps({'person':request.headers['Authorization'].split()[1]}).encode())
        with patch('e2x_course_hub.cps.proxy.AsyncHTTPClient',return_value=Client()):
            for visitor in ['alice','bob']:
                response=self.fetch('/proxy/me',headers={'X-Visitor':visitor,'Idempotency-Key':'request-'+visitor})
                self.assertEqual(json.loads(response.body)['person'],visitor)
        self.assertEqual([c.headers['X-CPS-Hub'] for c in calls],['cps','cps'])
        self.assertEqual([c.headers['Idempotency-Key'] for c in calls],['request-alice','request-bob'])
        self.assertTrue(all(c.url=='https://gateway.example/v1/me' for c in calls))

    def test_internal_and_encoded_routes_cannot_be_proxied(self):
        for path in ['internal/v1/grants','../internal/v1/grants','workflows/x%2Flogs']:
            self.assertEqual(self.fetch('/proxy/'+path,headers={'X-Visitor':'alice'}).code,404)

    def test_referenced_group_cannot_be_changed_or_deleted(self):
        async def seed():
            await self.provider.put('courses',{'id':'c'},actor='admin')
            await self.provider.put('groups',{'id':'g','course_id':'c','assignment_id':'a'},actor='admin')
            await self.provider.put('workspaces',{'id':'w','course_id':'c','group_id':'g','state':'running'},actor='admin')
        self.io_loop.run_sync(seed)
        response=self.fetch('/records/groups',method='POST',body=json.dumps({'id':'g','course_id':'c'}))
        self.assertEqual(response.code,409)
        response=self.fetch('/records/groups',method='DELETE',body=json.dumps({'id':'g'}),allow_nonstandard_methods=True)
        self.assertEqual(response.code,409)

    def test_membership_move_cannot_bypass_old_workspace_shutdown(self):
        async def seed():
            await self.provider.put('courses',{'id':'a'},actor='admin')
            await self.provider.put('courses',{'id':'b'},actor='admin')
            await self.provider.put('memberships',{'id':'m','course_id':'a','group_id':'ga','person_id':'alice','canonical_person_id':'p1'},actor='admin')
            await self.provider.put('workspaces',{'id':'w','course_id':'a','group_id':'ga','state':'running'},actor='admin')
        self.io_loop.run_sync(seed)
        response=self.fetch('/records/memberships',method='POST',body=json.dumps({'id':'m','course_id':'b','group_id':'gb','person_id':'bob','canonical_person_id':'p2'}))
        self.assertEqual(response.code,409)
        records=self.io_loop.run_sync(lambda:self.provider.list('memberships'))
        self.assertEqual(records[0]['person_id'],'alice')
        self.assertEqual(records[0]['course_id'],'a')

    def test_latest_admin_state_authoritative_not_stale_login(self):
        self.admin=False
        self.assertEqual(self.fetch('/records/courses').code,403)
        self.admin=True
        self.assertEqual(self.fetch('/records/courses').code,200)

    def test_external_edits_rejected_and_cit_records_hidden(self):
        response=self.fetch('/records/courses',method='POST',body=json.dumps({'id':'c','source':'moodle'}))
        self.assertEqual(response.code,403)
        response=self.fetch('/records/courses',method='POST',body=json.dumps({'id':'c'}))
        self.assertEqual(response.code,200)
        self.provider.console='cit'
        response=self.fetch('/records/courses')
        self.assertEqual(json.loads(response.body),{'records':[]})

from e2x_course_hub.cps.proxy import ComputeCSRFHandler
from e2x_course_hub.cps.oauth import CPSHubOAuth

class CSRFBootstrapTest(AsyncHTTPTestCase):
    def get_app(self):
        class Auth(CPSHubOAuth):
            def _get_token_cookie(self,handler):return handler.request.headers.get('X-Visitor')
            def get_token(self,handler,**kwargs):return self._get_token_cookie(handler)
        auth=Auth(base_url='/services/console/')
        class Bootstrap(ComputeCSRFHandler):
            _hub_auth=auth
            def get_current_user(self):return {'name':self.request.headers.get('X-Visitor')}
        class Proxy(ComputeVisitorProxy):
            _hub_auth=auth
            def get_current_user(self):return {'name':self.request.headers.get('X-Visitor')}
        return web.Application([(r'/services/console/api/compute/xsrf',Bootstrap),
            (r'/services/console/api/compute/v1/(.*)',Proxy)],cookie_secret='test',xsrf_cookies=True,
            compute_gateway_url='https://gateway.example',console_owner='cps')

    def test_bootstrap_token_enables_write_and_is_visitor_bound(self):
        bootstrap=self.fetch('/services/console/api/compute/xsrf',headers={'X-Visitor':'alice'})
        self.assertEqual(bootstrap.code,200)
        self.assertEqual(bootstrap.headers['Cache-Control'],'no-store')
        token=json.loads(bootstrap.body)['xsrf_token']
        self.assertIn('Path=/services/console/',bootstrap.headers['Set-Cookie'])
        self.assertEqual(self.fetch('/services/console/api/compute/v1/workflows',method='POST',body='{}',headers={'X-Visitor':'alice'}).code,403)
        class Client:
            async def fetch(self,*args,**kwargs):return NS(code=200,headers={},body=b'{}')
        with patch('e2x_course_hub.cps.proxy.AsyncHTTPClient',return_value=Client()):
            result=self.fetch('/services/console/api/compute/v1/workflows',method='POST',body='{}',headers={'X-Visitor':'alice','X-XSRFToken':token})
            self.assertEqual(result.code,200)
            result=self.fetch('/services/console/api/compute/v1/workflows',method='POST',body='{}',headers={'X-Visitor':'bob','X-XSRFToken':token})
            self.assertEqual(result.code,403)
