import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from tornado import web
from tornado.httpclient import HTTPClientError
from tornado.testing import AsyncHTTPTestCase

from e2x_course_hub.cps.platform_handlers import NativeGroupApprovalHandler, default_handlers
from e2x_course_hub.cps.providers import LocalCourseProvider


class NativeGroupApprovalHTTPTest(AsyncHTTPTestCase):
    def get_app(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = LocalCourseProvider(Path(self.tmp.name) / 'db', 'cps')
        self.admin = True
        self.calls = []
        self.failure = None
        outer = self

        class Hub:
            async def get_user(self, name):
                return {'name': name, 'admin': outer.admin, 'groups': ['lms.course.c.term.t.instructor']}

        class Handler(NativeGroupApprovalHandler):
            def get_current_user(self):
                name = self.request.headers.get('X-Test-User')
                return {'name': name} if name else None

        class Client:
            async def native_group_approvals(self):
                if outer.failure:
                    raise outer.failure
                return {'revision': 'sha256:' + 'a' * 64, 'ready': False,
                        'qualified_profiles': ['interactive-shared-5'], 'approvals': {}}

            async def mutate_native_group_approval(self, workspace, body, *, revoke=False):
                if outer.failure:
                    raise outer.failure
                outer.calls.append((workspace, body, revoke))
                return {'revision': 'sha256:' + 'b' * 64, 'ready': False}

        routes = [(pattern, Handler) for pattern, handler in default_handlers
                  if handler is NativeGroupApprovalHandler]
        return web.Application(routes, compute_policy=Client(),
            last_config_check=time.time(), course_provider=self.provider,
            api=SimpleNamespace(course_api=SimpleNamespace(hub_api=Hub())),
            login_url='/login', cookie_secret='test')

    def tearDown(self):
        self.provider.db.close()
        self.tmp.cleanup()
        super().tearDown()

    def request(self, body=None, *, action='approve', user='alice'):
        return self.fetch('/api/native-group-approvals' + ('/w/' + action if body is not None else ''),
            method='POST' if body is not None else 'GET',
            body=json.dumps(body) if body is not None else None,
            headers={'X-Test-User': user} if user else {}, follow_redirects=False)

    def test_admin_reads_scoped_registry_and_qualified_candidates(self):
        response = self.request()
        self.assertEqual(response.code, 200)
        self.assertEqual(json.loads(response.body)['qualified_profiles'], ['interactive-shared-5'])
        self.assertFalse(json.loads(response.body)['ready'])

    def test_admin_approves_with_exact_revision_and_no_resource_overrides(self):
        body = {'profile': 'interactive-shared-5', 'expected_revision': 'sha256:' + 'a' * 64}
        self.assertEqual(self.request(body).code, 200)
        self.assertEqual(self.calls, [('w', body, False)])
        entries = self.provider.db.execute("SELECT actor FROM audit WHERE kind='native-group-approvals'").fetchall()
        self.assertEqual([row[0] for row in entries], ['alice'])

    def test_revoke_forwards_only_revision(self):
        body = {'expected_revision': 'sha256:' + 'a' * 64}
        self.assertEqual(self.request(body, action='revoke').code, 200)
        self.assertEqual(self.calls, [('w', body, True)])

    def test_instructors_cannot_issue_native_qualification_approval(self):
        self.admin = False
        self.assertEqual(self.request().code, 403)
        self.assertEqual(self.request({'profile': 'interactive-shared-5', 'expected_revision': 'x'}).code, 403)
        self.assertEqual(self.calls, [])

    def test_login_required(self):
        self.assertEqual(self.request(user=None).code, 302)
        self.assertEqual(self.request({'profile': 'interactive-shared-5', 'expected_revision': 'x'}, user=None).code, 403)
        self.assertEqual(self.calls, [])

    def test_exact_body_rejects_actor_ref_and_resource_injection(self):
        base = {'profile': 'interactive-shared-5', 'expected_revision': 'sha256:' + 'a' * 64}
        for extra in ({'actor': 'admin'}, {'approval_ref': {}}, {'resources': {'gpu': 8}}, {'source': 'cit'}):
            with self.subTest(extra=extra):
                self.assertEqual(self.request(dict(base, **extra)).code, 400)
        for body in ({}, {'profile': 5, 'expected_revision': 'x'}, [], {'profile': 'interactive-shared-20'}):
            with self.subTest(body=body):
                self.assertEqual(self.request(body).code, 400)
        self.assertEqual(self.calls, [])

    def test_gateway_conflict_and_denial_remain_visible_and_audited(self):
        for status in (400, 403, 409, 503):
            with self.subTest(status=status):
                self.failure = HTTPClientError(status)
                self.assertEqual(self.request({'profile': 'interactive-shared-5', 'expected_revision': 'x'}).code, status)
        self.assertEqual(self.calls, [])
