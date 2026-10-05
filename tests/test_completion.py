import asyncio
import tempfile
import unittest
from pathlib import Path
from e2x_course_hub.cps.providers import LocalCourseProvider, reconcile
from e2x_course_hub.cps.workspaces import WorkspaceService
from e2x_course_hub.cps.compute import ComputePolicyClient
from e2x_course_hub.cps.identity import reviewed_email_mapping


class CompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = LocalCourseProvider(Path(self.tmp.name)/'db','cps')
        self.calls = []
        self.stop_failure = False
        self.spawn_failure = False
        self.release_failure = False
        self.archive_verified = True
        outer = self
        class Compute:
            async def validate_workspace(self,*args):
                outer.calls.append('validate');return {'policy_hash':'hash'}
            async def register_workspace(self,workspace,members,validation):
                outer.calls.append('register')
                assert workspace['pod']=='preserved'
                assert validation['policy_hash']=='hash'
            async def reservation_state(self,*args):return {'active':True,'attempt':'bound-attempt'}
            async def release_after_shutdown(self,*args,**kwargs):
                assert kwargs['attempt']=='bound-attempt'
                outer.calls.append('release')
                if outer.release_failure:raise TimeoutError('Pod still exists')
        class Hub:
            async def assert_stopped(self,*args):pass
            async def stop_confirmed(self,workspace):
                outer.calls.append('stop:'+workspace['id'])
                if outer.stop_failure:raise TimeoutError('Hub active')
            async def start(self,*args):
                outer.calls.append('spawn')
                if outer.spawn_failure:raise RuntimeError('lost response')
            async def sync_group(self,workspace,members):outer.calls.append('sync')
            async def replace_shares(self,*args):outer.calls.append('shares')
            async def revoke_shares(self,*args):outer.calls.append('revoke')
            async def remove_users_from_group(self,*args):outer.calls.append('remove')
        class Filesystem:
            async def provision(self,*args,**kwargs):outer.calls.append('provision')
            async def archive(self,workspaces,**kwargs):
                outer.calls.append('archive')
                assert all('stop:'+w['id'] in outer.calls for w in workspaces)
                return {'read_only_verified':outer.archive_verified,'evidence':['operator://verified']}
        self.hub = Hub();self.hub.hub=self.hub
        self.service=WorkspaceService(self.provider,Compute(),self.hub,filesystem=Filesystem())
        p=self.provider
        await p.put('courses',{'id':'c','resource_ceiling':{},'workspace_bindings':[
            {'group_id':'g','hub_user':'neutral','hub_server':'rtc','namespace':'trusted','pod':'preserved'}]},actor='admin')
        await p.put('assignments',{'id':'a','course_id':'c','mode':'manual'},actor='admin')
        await p.put('groups',{'id':'g','course_id':'c','assignment_id':'a'},actor='admin')
        person='00000000-0000-4000-8000-000000000001'
        await p.link_identities([{'hub':'cps','username':'unchanged','email':'person@example.edu','person_id':person,'verified':True}],actor='admin')
        await p.put('memberships',{'id':'m','course_id':'c','group_id':'g','person_id':'unchanged','canonical_person_id':person},actor='admin')
        await p.put('workspaces',{'id':'w','course_id':'c','group_id':'g','hub_user':'neutral','hub_server':'rtc','profile':'cpu'},actor='admin')

    async def asyncTearDown(self):
        self.provider.db.close();self.tmp.cleanup()

    async def test_legacy_email_proofs_remain_blocked_until_verified(self):
        self.provider.db.execute('UPDATE email_links SET verified=0')
        with self.assertRaisesRegex(ValueError,'verification'):
            await self.service.start('w',actor='teacher')
        self.assertNotIn('spawn',self.calls)
        self.assertEqual((await self.provider.members('c'))[0]['person_id'],'unchanged')

    async def test_closing_assignment_blocks_new_starts_before_archive(self):
        assignment=(await self.provider.list('assignments'))[0]
        await self.provider.put('assignments',{**assignment,'archive_pending':True},actor='teacher')
        with self.assertRaisesRegex(ValueError,'Closed assignment'):
            await self.service.start('w',actor='teacher')
        self.assertEqual(self.calls,[])

    async def test_start_registers_without_console_reserving(self):
        await self.service.start('w',actor='teacher')
        self.assertEqual(self.calls,['provision','validate','register','sync','spawn','shares'])
        self.assertEqual((await self.provider.members('c'))[0]['person_id'],'unchanged')

    async def test_failed_spawn_requires_matching_observed_release(self):
        self.spawn_failure=True;self.release_failure=True
        with self.assertRaises(TimeoutError):await self.service.start('w',actor='teacher')
        self.assertEqual(self.calls[-2:],['stop:w','release'])
        self.assertTrue(any(a['kind']=='workspace-lifecycle' and a['outcome']=='failure' for a in self.provider.audit()))

    async def test_unqualified_provisioning_blocks_writer(self):
        service=WorkspaceService(self.provider,self.service.compute,self.hub)
        with self.assertRaisesRegex(RuntimeError,'provisioning'):await service.start('w',actor='teacher')
        self.assertEqual(self.calls,[])

    async def test_archive_requires_all_writers_and_verified_evidence(self):
        await self.provider.put('workspaces',{'id':'other','course_id':'c','group_id':'g'},actor='admin')
        assignment=(await self.provider.list('assignments'))[0]
        self.archive_verified=False
        with self.assertRaises(RuntimeError):await self.service.close_assignment(assignment,actor='teacher')
        self.assertIn('stop:w',self.calls);self.assertIn('stop:other',self.calls)
        self.assertTrue((await self.service.get('w'))['archive_pending'])
        self.assertFalse((await self.service.get('w')).get('archived',False))
        await self.service.close('w',actor='teacher')
        with self.assertRaises(ValueError):await self.service.start('w',actor='teacher')
        self.archive_verified=True
        await self.service.close_assignment(assignment,actor='teacher')
        self.assertTrue((await self.service.get('w'))['archived'])
        self.assertTrue((await self.provider.list('assignments'))[0]['archived'])

    async def test_membership_removal_stops_every_group_writer(self):
        await self.provider.put('workspaces',{'id':'other','course_id':'c','group_id':'g'},actor='admin')
        await self.service.remove_member('w','m',actor='teacher')
        self.assertLess(self.calls.index('stop:w'),self.calls.index('remove'))
        self.assertLess(self.calls.index('stop:other'),self.calls.index('remove'))
        self.assertEqual(await self.provider.members('c'),[])

    async def test_fake_source_uses_same_access_sink_without_provider_mutations(self):
        class Fake:
            async def courses(self):return [{'id':'c','source':'fake'}]
            async def members(self,course):return [{'group_id':'g','person_id':'unchanged','canonical_person_id':'00000000-0000-4000-8000-000000000001'}]
            async def groups(self,course):return [{'id':'g'}]
            async def groupings(self,course):return []
        await reconcile(Fake(),lambda snapshot:self.service.reconcile_snapshot(snapshot,actor='reconciler'))
        self.assertEqual(self.calls,['stop:w','release','validate','register','sync','shares'])


class BoundaryTests(unittest.TestCase):
    def test_administrator_review_does_not_replace_verification(self):
        with self.assertRaisesRegex(ValueError,'verification'):
            reviewed_email_mapping([{'hub':'cps','username':'old','email':'person@example.edu',
               'person_id':'00000000-0000-4000-8000-000000000001','administrator_reviewed':True}])

    def test_databases_are_separate_and_backups_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'cps.db';provider=LocalCourseProvider(path,'cps')
            asyncio.run(provider.put('courses',{'id':'retained'},actor='admin'))
            provider.backup(Path(tmp)/'backup.db');provider.db.close()
            with self.assertRaisesRegex(ValueError,'separate'):LocalCourseProvider(path,'cit')
            restored=LocalCourseProvider(Path(tmp)/'backup.db','cps')
            self.assertEqual(asyncio.run(restored.courses())[0]['id'],'retained');restored.db.close()

    def test_registry_payload_is_source_owned_fixed_current_policy(self):
        class Client(ComputePolicyClient):
            async def _request(self,path,method='GET',payload=None):return path,method,payload
        client=Client('https://internal.example','private','cit')
        workspace={'id':'w','hub_user':'neutral','hub_server':'rtc','namespace':'trusted',
                   'pod':'preserved','profile':'cpu','course_ceiling':{}}
        path,method,payload=asyncio.run(client.register_workspace(workspace,['canonical'],{'policy_hash':'hash'}))
        self.assertEqual((path,method),('workspaces','PUT'))
        self.assertEqual(payload['principal'],'workspace:cit:w')
        self.assertEqual(payload['policy_hash'],'hash')
        workspace.pop('pod')
        with self.assertRaises(ValueError):asyncio.run(client.register_workspace(workspace,['canonical'],{'policy_hash':'hash'}))
