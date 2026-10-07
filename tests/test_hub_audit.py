import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from tornado.httpclient import HTTPClientError
from e2x_course_hub.api.hub_api import HubAPI
from e2x_course_hub.api.errors import HubAPIError
from e2x_course_hub.cps.audit import actor_context
from e2x_course_hub.cps.providers import LocalCourseProvider
from e2x_course_hub.course_service.handlers.base import BaseAPIHandler

class HubAuditTest(unittest.TestCase):
    def test_upstream_handler_uses_configured_alias_when_filtering_expired_grants(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = LocalCourseProvider(Path(tmp)/'db', 'cps')
            class Hub:
                async def get_user(self, username):
                    return {'name':username, 'admin':False, 'groups':['c.t.teacher']}
            handler = NS(get_current_user=lambda:{'name':'instructor'},
                course_api=NS(hub_api=Hub()), settings={'course_provider':provider,
                'legacy_rbac_roles':{'teacher':'instructor'}})
            async def run():
                await provider.put('courses', {'id':'c'}, actor='admin')
                await provider.put('terms', {'id':'term','course_id':'c','term_id':'t'}, actor='admin')
                await provider.put('groups', {'id':'lms.course.c.term.t.instructor','course_id':'c','term_id':'t'}, actor='admin')
                grant = {'id':'grant','course_id':'c','term_id':'t','person_id':'instructor',
                    'group_id':'lms.course.c.term.t.instructor','expires':'9999-01-01T00:00:00Z'}
                await provider.put('memberships', grant, actor='admin')
                active_user = await BaseAPIHandler.get_user(handler)
                self.assertEqual(active_user.get_roles_in_course('c','t'), ['teacher'])
                await provider.put('memberships', {**grant,'expires':'2020-01-01T00:00:00Z'}, actor='admin')
                expired_user = await BaseAPIHandler.get_user(handler)
                self.assertEqual(expired_user.groups, [])
                self.assertEqual(expired_user.get_roles_in_course('c','t'), [])
            try: asyncio.run(run())
            finally: provider.db.close()

    def test_hub_mutations_record_actual_actor_before_after_and_denials(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider=LocalCourseProvider(Path(tmp)/'db','cps')
            hub=HubAPI(api_url='https://hub.example/hub/api',api_token='private-token')
            hub.audit_provider=provider
            class Client:
                users=['old']
                deny=False
                async def fetch(self,request):
                    if request.method=='GET':return NS(body=json.dumps({'name':'g','users':self.users}).encode())
                    if self.deny:raise HTTPClientError(403)
                    self.users=['old','new']
                    return NS(body=b'{}')
            client=Client();hub.client=client
            async def run():
                reset=actor_context.set('actual-instructor')
                try:
                    await hub.add_users_to_group('g',['new'])
                    client.deny=True
                    with self.assertRaises(HubAPIError):await hub.remove_users_from_group('g',['old'])
                finally:actor_context.reset(reset)
            asyncio.run(run())
            entries=provider.audit()
            self.assertEqual([e['outcome'] for e in entries],['success','denied'])
            self.assertEqual(json.loads(entries[0]['previous'])['users'],['old'])
            self.assertEqual(json.loads(entries[0]['current'])['observed']['users'],['old','new'])
            self.assertTrue(all(e['actor']=='actual-instructor' for e in entries))
            self.assertNotIn('private-token',json.dumps(entries))
            provider.db.close()
