"""Prepare stopped Source-owned CPU storage without a Hub spawn/GPU lease."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path

from e2x_course_hub.cps.providers import LocalCourseProvider
from e2x_course_hub.cps.workspaces import WorkspaceService


class NativePreparationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.provider=LocalCourseProvider(Path(self.tmp.name)/'db','cps')
        self.calls=[];self.race=None;self.storage_failure=False;outer=self
        class Compute:
            console='cps'
            async def _request(self,path):
                assert path=='profiles';return {'interactive-cpu':{'enabled':True,'kind':'interactive','gpu':'0'},
                    'batch-cpu':{'enabled':True,'kind':'batch','gpu':'0'}}
            async def reservation_state(self,identifier):return {'active':False,'attempt':None}
            async def release_after_shutdown(self,*args,**kwargs):
                # Actual gateway shutdown observation needs the central mapping.
                if not hasattr(outer,'registered'):raise RuntimeError('No registered shutdown mapping')
                outer.calls.append('cleanup')
            async def validate_workspace(self,identifier,members,profile,ceiling,registration=None):
                outer.calls.append('validate-current' if registration is not None else 'validate-cpu')
                return {'policy_hash':'sha256:'+'a'*64}
            async def register_workspace(self,workspace,members,validation):
                outer.calls.append('register');outer.registered=copy.deepcopy(workspace)
                if outer.race:await outer.race()
                return {'workspace':workspace['id'],'members':members}
        class Hub:
            async def assert_stopped(self,workspace):outer.calls.append('stopped')
            async def start(self,*args):raise AssertionError('Preparation must not spawn')
            async def sync_group(self,*args):raise AssertionError('Preparation must not grant access')
            async def replace_shares(self,*args):raise AssertionError('Preparation must not change Shares')
        class Storage:
            async def provision(self,workspace,*,actor):
                outer.calls.append('provision')
                if outer.storage_failure:raise RuntimeError('Storage unavailable')
        self.compute=Compute();self.hub=Hub()
        self.service=WorkspaceService(self.provider,self.compute,self.hub,filesystem=Storage())
        ceiling={'cpu':'4','memory':'16Gi','gpuMemoryGiB':5}
        await self.provider.put('courses',{'id':'c','resource_ceiling':ceiling,'workspace_bindings':[
            {'group_id':'g','hub_user':'neutral-new','hub_server':'rtc','namespace':'jupyterhub','pod':'jupyter-neutral-new--rtc'}]},actor='admin')
        await self.provider.put('terms',{'id':'t','course_id':'c'},actor='admin')
        await self.provider.put('groups',{'id':'g','course_id':'c','term_id':'t'},actor='admin')
        # Explicit reviewed account alias; no email matching or new person inference.
        await self.provider.link_identities([{'hub':'cps','username':'existing-account',
            'person_id':'00000000-0000-4000-8000-000000000001','authority':'reviewed_account_alias',
            'administrator_reviewed':True,'review_reason':'Unit fixture reviewed account'}],actor='admin')
        await self.provider.put('memberships',{'id':'m','course_id':'c','term_id':'t','group_id':'g',
            'person_id':'existing-account','canonical_person_id':'00000000-0000-4000-8000-000000000001'},actor='admin')
        await self.provider.put('workspaces',{'id':'w','course_id':'c','term_id':'t','group_id':'g',
            'hub_user':'neutral-new','hub_server':'rtc','profile':'interactive-cpu','course_ceiling':ceiling,
            'state':'stopped'},actor='admin')

    async def asyncTearDown(self):self.provider.db.close();self.tmp.cleanup()

    async def test_prepare_registers_provisions_revalidates_and_keeps_stopped_audited(self):
        result=await self.service.prepare_native_approval('w',actor='admin')
        self.assertEqual(self.calls,['validate-cpu','stopped','register','cleanup','provision','validate-current'])
        self.assertEqual(self.registered['namespace'],'jupyterhub')
        self.assertEqual(self.registered['pod'],'jupyter-neutral-new--rtc')
        self.assertEqual(result['state'],'stopped');self.assertEqual(result['profile'],'interactive-cpu')
        self.assertEqual((await self.service.get('w'))['namespace'],'jupyterhub')
        self.assertTrue(any(row['actor']=='admin' and row['kind']=='workspaces' for row in self.provider.audit()))

    async def test_running_archived_pending_binding_and_gpu_profile_fail_before_registration(self):
        original=await self.service.get('w')
        for changes in ({'state':'running'},{'archived':True},{'archive_pending':True},
                        {'hub_user':'human'},{'course_ceiling':{'cpu':'400'}},{'profile':'interactive-shared-5'},
                        {'namespace':'foreign','pod':'forged'}):
            with self.subTest(changes=changes):
                await self.provider.put('workspaces',{**original,**changes},actor='admin')
                self.calls.clear()
                with self.assertRaises((ValueError,PermissionError)):
                    await self.service.prepare_native_approval('w',actor='admin')
                self.assertNotIn('register',self.calls);self.assertNotIn('provision',self.calls)
        await self.provider.put('workspaces',original,actor='admin')

    async def test_unconfirmed_stop_or_active_reservation_never_registers(self):
        async def pending(*args):raise TimeoutError('Hub still stopping')
        original=self.hub.assert_stopped;self.hub.assert_stopped=pending
        with self.assertRaises(TimeoutError):await self.service.prepare_native_approval('w',actor='admin')
        self.assertNotIn('register',self.calls)
        self.hub.assert_stopped=original
        async def active(*args):return {'active':True,'attempt':'live'}
        self.compute.reservation_state=active
        with self.assertRaises(ValueError):await self.service.prepare_native_approval('w',actor='admin')
        self.assertNotIn('register',self.calls)

    async def test_storage_failure_retains_stopped_retryable_record_and_denial_audit(self):
        self.storage_failure=True
        with self.assertRaises(RuntimeError):await self.service.prepare_native_approval('w',actor='admin')
        self.assertEqual((await self.service.get('w'))['state'],'stopped')
        self.assertNotIn('validate-current',self.calls)
        self.assertTrue(any(row.get('outcome')=='failure' for row in self.provider.audit()))
        self.storage_failure=False;self.calls.clear()
        self.assertEqual((await self.service.prepare_native_approval('w',actor='admin'))['state'],'stopped')

    async def test_member_or_binding_change_during_registration_blocks_storage(self):
        async def changed():
            course=(await self.provider.courses())[0]
            course['workspace_bindings'][0]['pod']='changed-pod'
            await self.provider.put('courses',course,actor='admin')
        self.race=changed
        with self.assertRaises(ValueError):await self.service.prepare_native_approval('w',actor='admin')
        self.assertNotIn('provision',self.calls)
        self.assertEqual((await self.service.get('w'))['state'],'stopped')

    async def test_canonical_link_absence_blocks_preparation(self):
        with self.provider.db:self.provider.db.execute('DELETE FROM reviewed_account_aliases')
        with self.assertRaises(ValueError):await self.service.prepare_native_approval('w',actor='admin')
        self.assertNotIn('register',self.calls)

    async def test_structured_cpu_mode_is_accepted_but_disabled_or_batch_are_not(self):
        async def structured(path):
            return {'interactive-cpu':{'enabled':True,'kind':'interactive','gpu':{'mode':'none'}}}
        self.compute._request=structured
        await self.service.prepare_native_approval('w',actor='admin')
        for entry in ({'enabled':False,'kind':'interactive','gpu':'0'},
                      {'enabled':True,'kind':'batch','gpu':'0'},
                      {'enabled':True,'kind':'interactive','gpu':{'mode':'shared'}},
                      {'enabled':True,'kind':'interactive','gpu':False}):
            with self.subTest(entry=entry):
                async def forbidden(path):return {'interactive-cpu':entry}
                self.compute._request=forbidden;self.calls.clear()
                with self.assertRaises(ValueError):await self.service.prepare_native_approval('w',actor='admin')
                self.assertNotIn('register',self.calls)

    async def test_cleanup_error_after_registration_never_provisions(self):
        async def unresolved(*args,**kwargs):raise RuntimeError('Released lease has no cleanup proof')
        self.compute.release_after_shutdown=unresolved
        with self.assertRaises(RuntimeError):await self.service.prepare_native_approval('w',actor='admin')
        self.assertIn('register',self.calls);self.assertNotIn('provision',self.calls)
        self.assertEqual((await self.service.get('w'))['state'],'stopped')

    async def test_membership_change_after_registration_is_rejected(self):
        async def changed():await self.provider.delete('memberships','m',actor='admin')
        self.race=changed
        with self.assertRaises(ValueError):await self.service.prepare_native_approval('w',actor='admin')
        self.assertNotIn('provision',self.calls)
        self.assertEqual((await self.service.get('w'))['state'],'stopped')

    async def test_membership_edit_is_guarded_during_provision_and_late_binding_change_denied(self):
        outer=self
        class Storage:
            async def provision(self,workspace,*,actor):
                outer.assertEqual((await outer.service.get('w'))['state'],'preparing')
                course=(await outer.provider.courses())[0]
                course['workspace_bindings'][0]['pod']='late-change'
                await outer.provider.put('courses',course,actor='admin')
        self.service.filesystem=Storage()
        with self.assertRaises(ValueError):await self.service.prepare_native_approval('w',actor='admin')
        self.assertIn('validate-current',self.calls)
        self.assertEqual((await self.service.get('w'))['state'],'stopped')

    async def test_foreign_workspace_edit_is_not_reverted_after_failure(self):
        async def changed():
            workspace=await self.service.get('w')
            await self.provider.put('workspaces',{**workspace,'notice':'Foreign change'},actor='other-admin')
        self.race=changed
        with self.assertRaises(ValueError):await self.service.prepare_native_approval('w',actor='admin')
        current=await self.service.get('w')
        self.assertEqual(current['notice'],'Foreign change')
        self.assertEqual(current['state'],'preparing')
        self.assertNotIn('provision',self.calls)
