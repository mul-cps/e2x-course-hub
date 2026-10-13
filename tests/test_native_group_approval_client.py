import json

from tornado import web
from tornado.testing import AsyncHTTPTestCase, gen_test

from e2x_course_hub.cps.compute import ComputePolicyClient


class NativeGroupApprovalClientTest(AsyncHTTPTestCase):
    def get_app(self):
        self.requests = []
        outer = self

        class Policy(web.RequestHandler):
            async def delete(self, workspace):
                outer.requests.append((self.request.method, self.request.uri,
                    self.request.headers['Authorization'], json.loads(self.request.body)))
                self.write({'revision': 'sha256:' + 'b' * 64, 'ready': False})

        return web.Application([(r'/internal/v1/native-group-approvals/(.+)', Policy)])

    @gen_test
    async def test_real_http_delete_retains_revision_body_and_server_credential(self):
        client = ComputePolicyClient('https://policy.invalid', 'server-only-test-token', 'cps')
        # Use a local HTTP listener for the transport test; production constructor requires TLS.
        client.base_url = self.get_url('').rstrip('/')
        body = {'expected_revision': 'sha256:' + 'a' * 64}
        result = await client.mutate_native_group_approval('w/slash', body, revoke=True)
        self.assertFalse(result['ready'])
        self.assertEqual(self.requests, [('DELETE', '/internal/v1/native-group-approvals/w%2Fslash',
                                         'Bearer server-only-test-token', body)])
