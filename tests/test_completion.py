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
                return {'principal':'workspace:cps:'+workspace['id'],'policy_hash':validation['policy_hash']}
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
                assert (await outer.service.get('w'))['state']=='starting'
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
        self.assertEqual(self.calls,['validate','register','provision','validate','sync','spawn','shares'])
        self.assertEqual((await self.provider.members('c'))[0]['person_id'],'unchanged')

    async def test_failed_spawn_requires_matching_observed_release(self):
        self.spawn_failure=True;self.release_failure=True
        with self.assertRaises(TimeoutError):await self.service.start('w',actor='teacher')
        self.assertEqual(self.calls[-2:],['stop:w','release'])
        self.assertTrue(any(a['kind']=='workspace-lifecycle' and a['outcome']=='failure' for a in self.provider.audit()))
        workspace = await self.service.get('w')
        self.assertEqual(workspace['state'],'reconciling')
        self.assertIn('reservations retained',workspace['notice'])

    async def test_asynchronous_spawn_failure_is_not_marked_running(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from e2x_course_hub.cps.workspaces import HubWorkspaceAdapter
        api = SimpleNamespace(api_url='http://hub/hub/api',
            request=AsyncMock(return_value=SimpleNamespace(code=202)),
            get_user=AsyncMock(return_value={'servers':{}}))
        self.hub.start = HubWorkspaceAdapter(api).start
        with self.assertRaisesRegex(RuntimeError,'spawn failed'):
            await self.service.start('w',actor='teacher')
        workspace=await self.service.get('w')
        self.assertEqual(workspace['state'],'stopped')
        self.assertIn('start failed',workspace['notice'])
        self.assertNotIn('shares',self.calls)
        self.assertEqual(self.calls[-2:],['stop:w','release'])

    async def test_workspace_remains_starting_until_hub_is_ready(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from e2x_course_hub.cps.workspaces import HubWorkspaceAdapter
        waiting=asyncio.Event();ready=asyncio.Event();observations=0
        async def get_user(username):
            nonlocal observations
            observations+=1
            if observations==1:
                return {'servers':{'rtc':{'pending':'spawn','ready':False}}}
            waiting.set()
            await ready.wait()
            return {'servers':{'rtc':{'pending':None,'ready':True}}}
        api=SimpleNamespace(api_url='http://hub/hub/api',get_user=get_user,
            request=AsyncMock(return_value=SimpleNamespace(code=202)))
        self.hub.start=HubWorkspaceAdapter(api).start
        with patch('e2x_course_hub.cps.workspaces.asyncio.sleep',new=AsyncMock()):
            starting=asyncio.create_task(self.service.start('w',actor='teacher'))
            try:
                await asyncio.wait_for(waiting.wait(),2)
                self.assertEqual((await self.service.get('w'))['state'],'starting')
                self.assertNotIn('shares',self.calls)
                self.assertFalse(starting.done())
            finally:
                ready.set()
                await asyncio.wait_for(starting,2)
        self.assertEqual((await self.service.get('w'))['state'],'running')
        self.assertIn('shares',self.calls)

    async def test_failed_spawn_keeps_guard_when_hub_shutdown_is_unconfirmed(self):
        self.spawn_failure=True;self.stop_failure=True
        with self.assertRaises(TimeoutError):
            await self.service.start('w',actor='teacher')
        workspace=await self.service.get('w')
        self.assertEqual(workspace['state'],'reconciling')
        self.assertIn('reservations retained',workspace['notice'])
        self.assertNotIn('release',self.calls)

    async def test_successful_stop_clears_obsolete_cleanup_failure_notice(self):
        workspace=await self.service.get('w')
        await self.provider.put('workspaces',{**workspace,'state':'reconciling',
            'notice':'Workspace start failed; shutdown/cleanup is unresolved. GPU reservations retained until confirmed cleanup.'},actor='teacher')
        await self.service.close('w',actor='teacher')
        stopped=await self.service.get('w')
        self.assertEqual(stopped['state'],'stopped')
        self.assertIsNone(stopped.get('notice'))
        self.assertEqual(self.calls,['stop:w','release','revoke'])

    async def test_failed_stop_preserves_cleanup_notice_until_confirmed_retry(self):
        notice='Workspace start failed; shutdown/cleanup is unresolved. GPU reservations retained until confirmed cleanup.'
        for failed_step in ('stop','release'):
            with self.subTest(failed_step=failed_step):
                self.calls.clear()
                self.stop_failure=failed_step=='stop';self.release_failure=failed_step=='release'
                workspace=await self.service.get('w')
                await self.provider.put('workspaces',{**workspace,'state':'reconciling','notice':notice},actor='teacher')
                with self.assertRaises(TimeoutError):await self.service.close('w',actor='teacher')
                unresolved=await self.service.get('w')
                self.assertEqual(unresolved['state'],'reconciling')
                self.assertEqual(unresolved['notice'],notice)
                self.assertEqual(self.calls,['stop:w'] if failed_step=='stop' else ['stop:w','release'])
                self.stop_failure=False;self.release_failure=False
                self.calls.clear()
                await self.service.close('w',actor='teacher')
                stopped=await self.service.get('w')
                self.assertEqual(stopped['state'],'stopped')
                self.assertIsNone(stopped.get('notice'))
                self.assertEqual(self.calls,['stop:w','release','revoke'])

    async def test_unqualified_provisioning_blocks_writer(self):
        service=WorkspaceService(self.provider,self.service.compute,self.hub)
        with self.assertRaisesRegex(RuntimeError,'provisioning'):await service.start('w',actor='teacher')
        self.assertEqual(self.calls,['validate','register'])
        self.assertEqual((await service.get('w'))['state'],'stopped')

    async def test_registration_rejection_is_retryable_without_releasing_unknown_reservation(self):
        original_register=self.service.compute.register_workspace
        async def rejected(*args):raise ValueError('registration denied')
        self.service.compute.register_workspace=rejected
        with self.assertRaisesRegex(ValueError,'registration denied'):
            await self.service.start('w',actor='teacher')
        self.assertEqual((await self.service.get('w'))['state'],'stopped')
        self.assertNotIn('release',self.calls)
        self.assertNotIn('spawn',self.calls)
        self.service.compute.register_workspace=original_register
        await self.service.start('w',actor='teacher')
        self.assertIn('spawn',self.calls)

    async def test_strict_policy_rejection_is_retryable_without_spawning_or_releasing(self):
        original_validate=self.service.compute.validate_workspace
        async def rejected(*args):
            if len(args)==5:raise ValueError('registered policy denied')
            return await original_validate(*args)
        self.service.compute.validate_workspace=rejected
        with self.assertRaisesRegex(ValueError,'registered policy denied'):
            await self.service.start('w',actor='teacher')
        self.assertEqual((await self.service.get('w'))['state'],'stopped')
        self.assertNotIn('release',self.calls)
        self.assertNotIn('spawn',self.calls)
        self.service.compute.validate_workspace=original_validate
        await self.service.start('w',actor='teacher')
        self.assertIn('spawn',self.calls)

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

    async def test_partial_membership_removal_blocks_restart_until_retry(self):
        person='00000000-0000-4000-8000-000000000002'
        await self.provider.link_identities([{'hub':'cps','username':'remaining','email':'remaining@example.edu',
            'person_id':person,'verified':True}],actor='admin')
        await self.provider.put('memberships',{'id':'m2','course_id':'c','group_id':'g',
            'person_id':'remaining','canonical_person_id':person},actor='admin')
        original_shares=self.hub.replace_shares
        async def failed_shares(*args):
            raise RuntimeError('lost Shares response after group removal')
        self.hub.replace_shares=failed_shares
        with self.assertRaisesRegex(RuntimeError,'lost Shares'):
            await self.service.remove_member('w','m',actor='teacher')
        self.assertEqual(len(await self.provider.members('c')),2)
        with self.assertRaises(ValueError):
            await self.service.start('w',actor='teacher')
        await self.service.close('w',actor='teacher')
        with self.assertRaisesRegex(ValueError,'barrier'):
            await self.service.start('w',actor='teacher')
        course=(await self.provider.courses())[0]
        with self.assertRaisesRegex(ValueError,'Membership removal'):
            await self.service.reconcile_snapshot({'course':course,'groups':[{'id':'g'}],
                'members':await self.provider.members('c'),'groupings':[]},actor='reconciler')
        self.assertNotIn('spawn',self.calls)
        self.hub.replace_shares=original_shares
        await self.service.remove_member('w','m',actor='teacher')
        self.assertEqual([m['person_id'] for m in await self.provider.members('c')],['remaining'])
        await self.service.start('w',actor='teacher')
        self.assertIn('spawn',self.calls)

    async def removal_after_delete_failure(self, stage):
        stale_members = await self.provider.members('c')
        original_put = self.provider.put
        failed = False
        async def failed_put(kind, record, *, actor):
            nonlocal failed
            terminal = (kind == 'workspaces' and record.get('state') == 'stopped') if stage == 'workspace' else (kind == 'courses' and not record.get('reconciliation_pending'))
            if terminal and not failed:
                failed = True
                raise RuntimeError('post-delete persistence failure')
            return await original_put(kind, record, actor=actor)
        self.provider.put = failed_put
        with self.assertRaisesRegex(RuntimeError, 'post-delete'):
            await self.service.remove_member('w', 'm', actor='teacher')
        self.assertEqual(await self.provider.members('c'), [])
        course = (await self.provider.courses())[0]
        self.assertTrue(course['reconciliation_pending'])
        await original_put('groups', {'id':'different', 'course_id':'c'}, actor='admin')
        await original_put('workspaces', {'id':'wrong-group', 'course_id':'c', 'group_id':'different'}, actor='admin')
        with self.assertRaises(ValueError):
            await self.service.remove_member('wrong-group', 'm', actor='teacher')
        with self.assertRaises(ValueError):
            await self.service.remove_member('w', 'other-membership', actor='teacher')
        if stage == 'workspace':
            with self.assertRaisesRegex(ValueError, 'Membership removal'):
                await self.service.reconcile_snapshot({'course':course, 'groups':[{'id':'g'}],
                    'members':stale_members, 'groupings':[]}, actor='reconciler')
        await self.service.remove_member('w', 'm', actor='teacher')
        course = (await self.provider.courses())[0]
        self.assertFalse(course['reconciliation_pending'])
        self.assertIsNone(course['membership_removal_pending'])
        self.assertIsNone(course['membership_removal_group_id'])
        self.assertEqual((await self.service.get('w'))['state'], 'stopped')
        self.assertEqual(await self.provider.members('c'), [])
        self.assertEqual(self.calls.count('remove'), 1)
        self.assertNotIn('spawn', self.calls)

    async def test_last_member_removal_recovers_after_stopped_state_failure(self):
        await self.removal_after_delete_failure('workspace')

    async def test_last_member_removal_recovers_after_barrier_clear_failure(self):
        await self.removal_after_delete_failure('course')

    async def test_two_writer_reconciliation_stops_both_before_group_mutation(self):
        workspace=await self.service.get('w')
        await self.provider.put('workspaces',{**workspace,'id':'w2','hub_server':'rtc2'},actor='admin')
        course=(await self.provider.courses())[0]
        binding={**course['workspace_bindings'][0],'hub_server':'rtc2'}
        await self.provider.put('courses',{**course,'workspace_bindings':course['workspace_bindings']+[binding]},actor='admin')
        await self.service.reconcile_snapshot({'course':course,'groups':[{'id':'g'}],
            'members':await self.provider.members('c'),'groupings':[]},actor='reconciler')
        self.assertLess(self.calls.index('stop:w2'),self.calls.index('sync'))
        self.assertLess(self.calls.index('stop:w'),self.calls.index('sync'))
        self.assertEqual(self.calls.count('sync'),1)
        self.assertEqual(self.calls.count('shares'),2)

    async def test_partial_reconciliation_blocks_sibling_restart_even_after_direct_stop(self):
        workspace=await self.service.get('w')
        await self.provider.put('workspaces',{**workspace,'id':'w2','hub_server':'rtc2'},actor='admin')
        course=(await self.provider.courses())[0]
        binding={**course['workspace_bindings'][0],'hub_server':'rtc2'}
        await self.provider.put('courses',{**course,'workspace_bindings':course['workspace_bindings']+[binding]},actor='admin')
        snapshot={'course':course,'groups':[{'id':'g'}],
                  'members':await self.provider.members('c'),'groupings':[]}
        original_stop=self.hub.stop_confirmed
        async def delayed_failure(workspace):
            await original_stop(workspace)
            if workspace['id']=='w2':
                await asyncio.sleep(0)
                raise TimeoutError('second writer remains active')
        self.hub.stop_confirmed=delayed_failure
        with self.assertRaises(TimeoutError):
            await self.service.reconcile_snapshot(snapshot,actor='reconciler')
        self.assertTrue((await self.provider.courses())[0]['reconciliation_pending'])
        self.assertNotIn('sync',self.calls)
        with self.assertRaises(ValueError):await self.service.start('w',actor='teacher')
        await self.service.close('w',actor='teacher')
        self.assertEqual((await self.service.get('w'))['state'],'stopped')
        with self.assertRaisesRegex(ValueError,'barrier'):
            await self.service.start('w',actor='teacher')
        self.assertNotIn('spawn',self.calls)
        self.hub.stop_confirmed=original_stop
        await self.service.reconcile_snapshot(snapshot,actor='reconciler')
        self.assertFalse((await self.provider.courses())[0]['reconciliation_pending'])
        await self.service.start('w',actor='teacher')
        self.assertIn('spawn',self.calls)

    async def test_late_sibling_create_start_waits_for_blocked_course_reconciliation(self):
        course=(await self.provider.courses())[0]
        binding={**course['workspace_bindings'][0],'hub_server':'rtc2'}
        await self.provider.put('courses',{**course,'workspace_bindings':course['workspace_bindings']+[binding]},actor='admin')
        snapshot={'course':course,'groups':[{'id':'g'}],
                  'members':await self.provider.members('c'),'groupings':[]}
        workspace_lock=self.service.locks.setdefault('w',asyncio.Lock())
        await workspace_lock.acquire()
        reconciliation=asyncio.create_task(self.service.reconcile_snapshot(snapshot,actor='reconciler'))
        try:
            for _ in range(20):
                await asyncio.sleep(0)
                if (await self.provider.courses())[0].get('reconciliation_pending'):break
            self.assertTrue((await self.provider.courses())[0]['reconciliation_pending'])
            async def spawn(workspace):
                self.assertEqual((await self.service.get(workspace['id']))['state'],'starting')
                self.calls.append('spawn:'+workspace['id'])
            self.hub.start=spawn
            async def late_writer():
                await self.service.create({'id':'w2','course_id':'c','group_id':'g',
                    'hub_user':'neutral','hub_server':'rtc2','profile':'cpu','course_ceiling':{}},actor='admin')
                await self.service.start('w2',actor='teacher')
            late=asyncio.create_task(late_writer())
            await asyncio.sleep(0)
            self.assertFalse(late.done())
            self.assertFalse(any(w['id']=='w2' for w in await self.provider.list('workspaces')))
            self.assertEqual(self.calls,[])
        finally:
            workspace_lock.release()
        await asyncio.wait_for(asyncio.gather(reconciliation,late),2)
        self.assertLess(self.calls.index('sync'),self.calls.index('spawn:w2'))
        self.assertIn('stop:w',self.calls)
        self.assertFalse((await self.provider.courses())[0]['reconciliation_pending'])

    async def test_writer_snapshot_is_collected_after_course_serialization(self):
        lock=self.service.course_locks.setdefault('c',asyncio.Lock())
        await lock.acquire()
        course=(await self.provider.courses())[0]
        snapshot={'course':course,'groups':[{'id':'g'}],
                  'members':await self.provider.members('c'),'groupings':[]}
        reconciliation=asyncio.create_task(self.service.reconcile_snapshot(snapshot,actor='reconciler'))
        await asyncio.sleep(0)
        workspace=await self.service.get('w')
        await self.provider.put('workspaces',{**workspace,'id':'w2','hub_server':'rtc2'},actor='admin')
        await self.provider.put('courses',{**course,'workspace_bindings':course['workspace_bindings']+
            [{**course['workspace_bindings'][0],'hub_server':'rtc2'}]},actor='admin')
        lock.release()
        await asyncio.wait_for(reconciliation,2)
        self.assertLess(self.calls.index('stop:w2'),self.calls.index('sync'))

    async def test_empty_assignment_cannot_claim_read_only_archive(self):
        await self.provider.put('assignments',{'id':'empty','course_id':'c','mode':'manual'},actor='admin')
        assignment=next(a for a in await self.provider.list('assignments') if a['id']=='empty')
        with self.assertRaisesRegex(RuntimeError,'storage targets'):
            await self.service.close_assignment(assignment,actor='teacher')
        self.assertNotIn('archive',self.calls)

    async def test_fake_source_uses_same_access_sink_without_provider_mutations(self):
        before = {kind: await self.provider.list(kind)
                  for kind in ('courses', 'memberships', 'groups', 'groupings', 'workspaces')}
        class Fake:
            async def courses(self):return [{'id':'c','source':'fake'}]
            async def members(self,course):return [{'group_id':'g','person_id':'unchanged','canonical_person_id':'00000000-0000-4000-8000-000000000001'}]
            async def groups(self,course):return [{'id':'g'}]
            async def groupings(self,course):return []
        await reconcile(Fake(),lambda snapshot:self.service.reconcile_snapshot(snapshot,actor='reconciler'))
        self.assertEqual(self.calls,['stop:w','release','revoke','validate','register','provision','validate','sync','shares'])
        # Reconciliation changes access/lifecycle, never hands local record ownership
        # to the external source or silently replaces its roster.
        for kind in ('memberships', 'groups', 'groupings'):
            self.assertEqual(await self.provider.list(kind), before[kind])
        course = (await self.provider.courses())[0]
        self.assertEqual(course['source'], 'local')
        self.assertFalse(course['reconciliation_pending'])
        workspace = await self.service.get('w')
        self.assertEqual(workspace['id'], before['workspaces'][0]['id'])
        self.assertEqual(workspace['hub_user'], before['workspaces'][0]['hub_user'])
        self.assertEqual(workspace['hub_server'], before['workspaces'][0]['hub_server'])
        self.assertEqual(workspace['state'], 'stopped')


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
