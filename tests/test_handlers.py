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
        self.groups = []
        self.hub_failure = False
        outer = self
        class Hub:
            async def get_user(self,name): return {'admin':outer.admin,'groups':outer.groups}
        class Auth:
            def get_token(self,handler): return handler.request.headers.get('X-Visitor')
        class Proxy(ComputeVisitorProxy):
            _hub_auth = Auth()
            def get_current_user(self): return {'name':self.request.headers.get('X-Visitor')}
        class Records(RecordsHandler):
            def get_current_user(self): return {'name':'admin','admin':True}
            async def get_user(self): return User(username='admin',admin=True,groups=[])
        from e2x_course_hub.cps.platform_handlers import AssignmentHandler, WorkspaceHandler, ComputeHandler
        class Assignment(AssignmentHandler):
            get_current_user=Records.get_current_user
            get_user=Records.get_user
        class Workspace(WorkspaceHandler):
            get_current_user=Records.get_current_user
            get_user=Records.get_user
        class Compute(ComputeHandler):
            get_current_user=Records.get_current_user
            get_user=Records.get_user
        class Service:
            async def get(self,identifier):
                return next(r for r in await outer.provider.list('workspaces') if r['id']==identifier)
            async def start(self,identifier,**kwargs):
                if outer.hub_failure:raise RuntimeError('Hub unavailable')
                record=await self.get(identifier)
                await outer.provider.put('workspaces',{**record,'state':'running'},actor=kwargs['actor'])
        return web.Application([(r'/proxy/(.*)',Proxy),(r'/records/(.*)',Records),
            (r'/assignment',Assignment),(r'/workspace/([^/]+)/(start|stop|remove-member)',Workspace),
            (r'/compute',Compute)],workspace_service=Service(),
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
            await self.provider.put('assignments',{'id':'a','course_id':'c','mode':'manual'},actor='admin')
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
            await self.provider.put('groups',{'id':'ga','course_id':'a'},actor='admin')
            await self.provider.link_identities([{'hub':'cps','username':'alice','email':'alice@example.edu','person_id':'00000000-0000-4000-8000-000000000001','administrator_reviewed':True,'verified':True}],actor='admin')
            await self.provider.put('memberships',{'id':'m','course_id':'a','group_id':'ga','person_id':'alice','canonical_person_id':'00000000-0000-4000-8000-000000000001'},actor='admin')
            await self.provider.put('workspaces',{'id':'w','course_id':'a','group_id':'ga','state':'running'},actor='admin')
        self.io_loop.run_sync(seed)
        response=self.fetch('/records/memberships',method='POST',body=json.dumps({'id':'m','course_id':'b','group_id':'gb','person_id':'bob','canonical_person_id':'bob@example.edu'}))
        self.assertEqual(response.code,409)
        records=self.io_loop.run_sync(lambda:self.provider.list('memberships'))
        self.assertEqual(records[0]['person_id'],'alice')
        self.assertEqual(records[0]['course_id'],'a')

    def test_instructor_course_term_crud_assignment_and_workspace_scope(self):
        async def seed():
            await self.provider.put('courses',{'id':'c','resource_ceiling':{'cpu':'2','memory':'4Gi','gpuMemoryGiB':5},'workspace_bindings':[{'group_id':'g','hub_user':'neutral','hub_server':'shared'}]},actor='admin')
            await self.provider.put('terms',{'id':'term-old','course_id':'c','term_id':'t'},actor='admin')
            await self.provider.put('terms',{'id':'other-term','course_id':'c','term_id':'other'},actor='admin')
            await self.provider.put('groups',{'id':'g','course_id':'c','term_id':'t'},actor='admin')
            await self.provider.put('memberships',{'id':'m','course_id':'c','term_id':'t','person_id':'student'},actor='admin')
            await self.provider.put('workspaces',{'id':'w','course_id':'c','term_id':'t','group_id':'g'},actor='admin')
        self.io_loop.run_sync(seed)
        self.admin=False
        self.groups=['lms.course.c.term.t.instructor']
        response=self.fetch('/records/courses',method='POST',body=json.dumps({'id':'c','name':'Updated'}))
        self.assertEqual(response.code,200)
        course=self.io_loop.run_sync(lambda:self.provider.courses())[0]
        self.assertEqual(course['resource_ceiling'],{'cpu':'2','memory':'4Gi','gpuMemoryGiB':5})
        self.assertEqual(course['workspace_bindings'],[{'group_id':'g','hub_user':'neutral','hub_server':'shared'}])
        visible=self.fetch('/records/workspaces')
        self.assertEqual([r['id'] for r in json.loads(visible.body)['records']],['w'])
        self.assertEqual(self.fetch('/records/audit?course_id=c').code,403)
        response=self.fetch('/records/terms',method='POST',body=json.dumps({'id':'other-term','course_id':'c','term_id':'other'}))
        self.assertEqual(response.code,403)
        response=self.fetch('/assignment',method='POST',body=json.dumps({'id':'a','course_id':'c','term_id':'t','mode':'random','group_size':2,'seed':'42'}))
        self.assertEqual(response.code,200)
        response=self.fetch('/workspace/w/start',method='POST',body='{}')
        self.assertEqual(response.code,200)
        self.assertEqual(self.fetch('/compute',method='POST',body='{}').code,403)
        self.assertEqual(self.fetch('/records/courses',method='POST',body=json.dumps({'id':'c','resource_ceiling':{'cpu':'100'}})).code,403)
        self.groups=['lms.course.another.term.t.instructor']
        self.assertEqual(self.fetch('/workspace/w/start',method='POST',body='{}').code,403)

    def test_every_http_mutation_audits_invalid_json_delete_denial_and_hub_failure(self):
        async def seed():
            await self.provider.put('courses',{'id':'c'},actor='admin')
            await self.provider.put('groups',{'id':'g','course_id':'c'},actor='admin')
            await self.provider.put('workspaces',{'id':'w','course_id':'c','group_id':'g'},actor='admin')
        self.io_loop.run_sync(seed)
        self.assertEqual(self.fetch('/records/courses',method='POST',body='{invalid').code,400)
        self.assertEqual(self.fetch('/records/courses',method='DELETE',body=json.dumps({'id':'c'}),allow_nonstandard_methods=True).code,400)
        self.hub_failure=True
        self.assertEqual(self.fetch('/workspace/w/start',method='POST',body='{}').code,503)
        self.admin=False
        self.assertEqual(self.fetch('/records/groups',method='POST',body=json.dumps({'id':'bad','course_id':'c'})).code,403)
        entries=[r for r in self.provider.audit() if r['kind'].startswith('http:')]
        self.assertEqual(len(entries),4)
        self.assertEqual([r['outcome'] for r in entries],['invalid','invalid','failure','denied'])
        self.assertTrue(all(r['actor']=='admin' and r['time'] and r['previous'] is not None and r['current'] is not None for r in entries))

    def test_expired_teaching_membership_surviving_login_is_no_longer_effective(self):
        group='lms.course.c.term.t.instructor'
        async def seed():
            await self.provider.put('courses',{'id':'c'},actor='admin')
            await self.provider.put('terms',{'id':'term','course_id':'c','term_id':'t'},actor='admin')
            await self.provider.put('groups',{'id':group,'course_id':'c','term_id':'t'},actor='admin')
            await self.provider.put('memberships',{'id':'grant','course_id':'c','term_id':'t','person_id':'admin','group_id':group,'role':'instructor','expires':'2020-01-01T00:00:00Z'},actor='admin')
        self.io_loop.run_sync(seed)
        self.admin=False
        self.groups=[group]
        self.assertEqual(self.fetch('/records/courses',method='POST',body=json.dumps({'id':'c','name':'Denied'})).code,403)
        self.assertEqual(self.fetch('/records/courses').code,403)

    def test_membership_mutations_blocked_while_starting_or_reconciling(self):
        async def seed(state):
            await self.provider.put('courses',{'id':'c'},actor='admin')
            await self.provider.put('groups',{'id':'g','course_id':'c'},actor='admin')
            await self.provider.put('workspaces',{'id':'w','course_id':'c','group_id':'g','state':state},actor='admin')
        for state in ('starting','reconciling'):
            self.io_loop.run_sync(lambda:seed(state))
            response=self.fetch('/records/memberships',method='POST',body=json.dumps(
                {'id':'new','course_id':'c','group_id':'g','person_id':'existing-name'}))
            self.assertEqual(response.code,409)
            self.assertEqual(self.io_loop.run_sync(lambda:self.provider.members('c')),[])

    def test_closed_record_shapes_and_cross_course_references_rejected(self):
        async def seed():
            await self.provider.put('courses',{'id':'a'},actor='admin')
            await self.provider.put('courses',{'id':'b'},actor='admin')
            await self.provider.put('groups',{'id':'g','course_id':'a'},actor='admin')
        self.io_loop.run_sync(seed)
        for kind,data in [('memberships',{'id':'m','course_id':'b','person_id':'x','group_id':'g'}),
                          ('groups',{'id':'evil','course_id':'a','kubernetes_scopes':['admin']}),
                          ('groupings',{'id':'gg','course_id':'b','group_ids':['g']}),
                          ('memberships',{'id':'m','course_id':'a','person_id':'x','canonical_person_id':'unreviewed@example.edu'})]:
            self.assertEqual(self.fetch('/records/'+kind,method='POST',body=json.dumps(data)).code,400)
        entries=[r for r in self.provider.audit() if r['kind'].startswith('http:')]
        self.assertEqual(len(entries),4)
        self.assertTrue(all(r['outcome']=='invalid' for r in entries))

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
