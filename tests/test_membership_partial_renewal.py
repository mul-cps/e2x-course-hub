"""The actual Members editor omits hidden canonical identity on partial updates."""
import json
from tornado.testing import AsyncHTTPTestCase
import test_handlers as handler_fixtures

PERSON='00000000-0000-4000-8000-000000000001'
class PartialMembershipRenewal(AsyncHTTPTestCase):
    def get_app(self):return handler_fixtures.HandlersTest.get_app(self)
    def tearDown(self):
        self.provider.db.close();self.tmp.cleanup();super().tearDown()
    def seed(self,state='stopped'):
        async def create():
            await self.provider.put('courses',{'id':'a'},actor='admin')
            await self.provider.put('groups',{'id':'ga','course_id':'a'},actor='admin')
            await self.provider.link_identities([{'hub':'cps','username':'alice','email':'alice@example.edu','person_id':PERSON,'administrator_reviewed':True,'verified':True}],actor='admin')
            await self.provider.put('memberships',{'id':'m','course_id':'a','group_id':'ga','person_id':'alice','canonical_person_id':PERSON,'role':'student','expires':'2000-01-01T00:00:00Z'},actor='admin')
            await self.provider.put('workspaces',{'id':'w','course_id':'a','group_id':'ga','state':state},actor='admin')
        self.io_loop.run_sync(create)
    def payload(self):
        # EXACT editor fields: canonical_person_id is intentionally not an editable field.
        return {'id':'m','course_id':'a','group_id':'ga','person_id':'alice','role':'student','expires':'2027-01-01T00:00:00Z'}
    def post(self,payload):return self.fetch('/records/memberships',method='POST',body=json.dumps(payload))
    def record(self):return self.io_loop.run_sync(lambda:self.provider.list('memberships'))[0]
    def test_editor_expiry_only_update_preserves_hidden_canonical_identity_and_provenance(self):
        self.seed();before=self.record();r=self.post(self.payload())
        self.assertEqual(r.code,200,r.body)
        after=self.record();self.assertEqual(after['expires'],'2027-01-01T00:00:00Z')
        self.assertEqual({k:v for k,v in before.items()if k!='expires'},{k:v for k,v in after.items()if k!='expires'})
        self.assertEqual(after['canonical_person_id'],PERSON)
    def test_explicit_none_or_changed_identity_cannot_reassign_referenced_membership(self):
        self.seed();before=self.record()
        for changes in [{'canonical_person_id':None},{'canonical_person_id':'00000000-0000-4000-8000-000000000002'},{'person_id':'bob'},{'group_id':'other'},{'course_id':'other'}]:
            with self.subTest(changes=changes):self.assertEqual(self.post({**self.payload(),**changes}).code,409)
        self.assertEqual(self.record(),before)
    def test_running_workspace_and_unowned_user_still_cannot_renew(self):
        self.seed('running');before=self.record();self.assertEqual(self.post(self.payload()).code,409);self.assertEqual(self.record(),before)
        self.admin=False;self.groups=[];self.assertEqual(self.post(self.payload()).code,403);self.assertEqual(self.record(),before)
