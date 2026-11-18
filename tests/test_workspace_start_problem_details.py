"""Human browser Start failures remain actionable without weakening membership or XSRF."""
import json,time,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from tornado import web
from tornado.testing import AsyncHTTPTestCase
from e2x_course_hub.cps.platform_handlers import WorkspaceHandler
from e2x_course_hub.cps.workspaces import WorkspaceService
from e2x_course_hub.cps.providers import LocalCourseProvider
from e2x_course_hub.schema.user import User

EMPTY='Workspace has no active group members. Update its course roster before starting.'
class Roster(IsolatedAsyncioTestCase):
    async def test_empty_and_expired_group_rosters_remain_denied_with_precise_reason(self):
        for records in [[],[{'group_id':'group','canonical_person_id':'person','expires':'2000-01-01T00:00:00Z'}]]:
            async def members(course):return records
            service=SimpleNamespace(provider=SimpleNamespace(members=members))
            with self.subTest(records=records),self.assertRaisesRegex(ValueError,'no active group members'):
                await WorkspaceService.members(service,{'course_id':'course','group_id':'group'})
    async def test_active_member_without_canonical_identity_is_still_rejected(self):
        async def members(course):return [{'group_id':'group'}]
        service=SimpleNamespace(provider=SimpleNamespace(members=members))
        with self.assertRaisesRegex(ValueError,'canonical person mappings'):
            await WorkspaceService.members(service,{'course_id':'course','group_id':'group'})

class BrowserStart(AsyncHTTPTestCase):
    def get_app(self):
        outer=self;self.start_calls=0;self.records=[];self.failure=None
        self.tmp=tempfile.TemporaryDirectory();self.audit_provider=LocalCourseProvider(Path(self.tmp.name)/"offline-audit", "cps")
        class Hub:
            async def get_user(self,name):return {'name':name,'admin':True,'groups':[]}
        class Handler(WorkspaceHandler):
            def get_current_user(self):
                return {'name':'admin'}if self.get_secure_cookie('fixture-user')==b'admin'else None
            async def get_user(self):return User(username='admin',admin=True,groups=[])
        class Bootstrap(Handler):
            def get(self):self.xsrf_token;self.write('browser')
        class Provider:
            async def members(self,course):return outer.records
        class Service:
            provider=Provider()
            async def get(self,identifier):return {'id':'w','course_id':'course','group_id':'group'}
            async def start(self,identifier,*,actor):
                outer.start_calls+=1
                if outer.failure:raise outer.failure
                await WorkspaceService.members(self,await self.get(identifier))
                raise AssertionError('No real compute/Hub/job action may follow an empty roster')
        return web.Application([(r'/services/cps-admin/app',Bootstrap),(r'/services/cps-admin/api/workspaces/([^/]+)/(start|stop|profile|remove-member)',Handler)],
            xsrf_cookies=True,cookie_secret='offline-fixture-only',login_url='/login',
            workspace_service=Service(),course_provider=self.audit_provider,last_config_check=time.time(),
            api=SimpleNamespace(course_api=SimpleNamespace(hub_api=Hub())))
    def tearDown(self):
        self.audit_provider.db.close();self.tmp.cleanup();super().tearDown()
    def headers(self):
        user=web.create_signed_value('offline-fixture-only','fixture-user','admin').decode()
        bootstrap=self.fetch('/services/cps-admin/app',headers={'Cookie':'fixture-user='+user});cookie=bootstrap.headers.get_list('Set-Cookie')[0].split(';',1)[0];token=cookie.split('=',1)[1]
        return {'Cookie':cookie+'; fixture-user='+user,'X-XSRFToken':token,'Content-Type':'application/json'}
    def test_real_cookie_xsrf_start_returns_rfc9457_roster_reason_instead_of_html400(self):
        r=self.fetch('/services/cps-admin/api/workspaces/w/start',method='POST',body='{}',headers=self.headers())
        self.assertEqual(r.code,400)
        self.assertTrue(r.headers['Content-Type'].startswith('application/problem+json'),r.body)
        self.assertEqual(json.loads(r.body),{'type':'about:blank','title':'Bad Request','status':400,'detail':EMPTY})
        self.assertEqual(self.start_calls,1)
    def test_unknown_post_fields_are_precise_400_before_start(self):
        r=self.fetch('/services/cps-admin/api/workspaces/w/start',method='POST',body='{"profile":"injected"}',headers=self.headers())
        self.assertEqual(r.code,400)
        self.assertTrue(r.headers['Content-Type'].startswith('application/problem+json'),r.body)
        self.assertEqual(json.loads(r.body)['detail'],'Resource overrides and lifecycle assertions are not accepted')
        self.assertEqual(self.start_calls,0)
    def test_browser_missing_xsrf_remains_403_and_never_calls_start(self):
        headers=self.headers();headers.pop('X-XSRFToken')
        r=self.fetch('/services/cps-admin/api/workspaces/w/start',method='POST',body='{}',headers=headers)
        self.assertEqual(r.code,403);self.assertEqual(self.start_calls,0)

    def test_existing_typed_api_problem_preserves_status_type_and_extensions(self):
        from e2x_course_hub.api.errors import APIError
        class Conflict(APIError):
            status_code=409
            type_uri='urn:fixture:conflict'
            title='Scoped conflict'
        self.failure=Conflict('Public existing problem',scope='course',retryable=False)
        r=self.fetch('/services/cps-admin/api/workspaces/w/start',method='POST',body='{}',headers=self.headers())
        self.assertEqual(r.code,409)
        self.assertTrue(r.headers['Content-Type'].startswith('application/problem+json'))
        self.assertEqual(json.loads(r.body),{'type':'urn:fixture:conflict','title':'Scoped conflict','status':409,'detail':'Public existing problem','scope':'course','retryable':False})
    def test_unexpected_internal_exception_retains_safe_default_500_without_details(self):
        self.failure=TypeError('PRIVATE_INTERNAL_URL_OR_CREDENTIAL')
        r=self.fetch('/services/cps-admin/api/workspaces/w/start',method='POST',body='{}',headers=self.headers())
        self.assertEqual(r.code,500)
        self.assertNotIn(b'PRIVATE_INTERNAL',r.body)
        self.assertNotIn(b'Traceback',r.body)
        self.assertFalse(r.headers['Content-Type'].startswith('application/problem+json'))
