import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from e2x_course_hub.cps.providers import LocalCourseProvider
from e2x_course_hub.cps.workspaces import WorkspaceService
from e2x_course_hub.cps.assignments import allocate

class WorkspaceTests(unittest.TestCase):
    def test_csv_manual_and_seeded_random(self):
        members=['p1','p2','p3','p4']
        self.assertEqual(allocate(members,mode='random',group_size=2,seed='42'),allocate(members[::-1],mode='random',group_size=2,seed='42'))
        self.assertEqual(allocate(members,mode='csv',rows='person_id,group_id\np1,g\np2,g\n'),{'g':['p1','p2']})
        with self.assertRaises(ValueError):allocate(members,mode='manual',rows={'g':['p1'],'h':['p1']})

    def test_stop_revoke_release_then_delete_and_restart_validation(self):
        self.run_lifecycle()

    def test_unconfirmed_shutdown_preserves_member_and_reservation(self):
        self.run_lifecycle(stop_failure=True)

    def run_lifecycle(self,stop_failure=False):
        with tempfile.TemporaryDirectory() as tmp:
            p=LocalCourseProvider(Path(tmp)/'db','cps')
            calls=[]
            class Compute:
                async def validate_workspace(self,*args):
                    calls.append('validate'); return {'policy_hash':'test-hash'}
                async def register_workspace(self,*args,**kwargs):calls.append('register')
                async def reservation_state(self,*args):return {'attempt':'attempt'}
                async def release_after_shutdown(self,*args,**kwargs):calls.append('release')
            class Hub:
                async def assert_stopped(self,*args):pass
                async def stop_confirmed(self,w):
                    calls.append('stop')
                    if stop_failure:raise TimeoutError()
                async def remove_users_from_group(self,*args):calls.append('revoke')
                async def replace_shares(self,*args):calls.append('shares')
                async def sync_group(self,*args):calls.append('sync')
                async def start(self,*args):calls.append('spawn')
            hub=Hub();hub.hub=hub
            class Filesystem:
                async def provision(self,*args,**kwargs):pass
            service=WorkspaceService(p,Compute(),hub,filesystem=Filesystem())
            async def run():
                await p.put('courses',{'id':'c','resource_ceiling':{},'workspace_bindings':[{'group_id':'g','hub_user':'neutral','hub_server':'rtc','namespace':'trusted','pod':'preserved'}]},actor='admin')
                await p.put('groups',{'id':'g','course_id':'c'},actor='admin')
                await p.link_identities([{'hub':'cps','username':'old-name','email':'person1@example.edu','person_id':'00000000-0000-4000-8000-000000000001','administrator_reviewed':True,'verified':True}],actor='admin')
                await p.put('memberships',{'id':'m','course_id':'c','group_id':'g','person_id':'old-name','canonical_person_id':'00000000-0000-4000-8000-000000000001'},actor='admin')
                await p.put('workspaces',{'id':'w','course_id':'c','group_id':'g','profile':'shared-5','course_ceiling':{},'hub_user':'neutral','hub_server':'rtc'},actor='admin')
                if stop_failure:
                    with self.assertRaises(TimeoutError):await service.remove_member('w','m',actor='admin')
                    self.assertEqual(len(await p.members('c')),1)
                    self.assertEqual(calls,['stop'])
                else:
                    await service.start('w',actor='admin')
                    self.assertEqual(calls[:5],['validate','register','sync','spawn','shares'])
                    await service.remove_member('w','m',actor='admin')
                    self.assertEqual(calls[5:],['stop','release','revoke','shares'])
                    self.assertEqual(await p.members('c'),[])
                    self.assertIn('Files retained',(await service.get('w'))['notice'])
                    with self.assertRaises(ValueError):await service.start('w',actor='admin')
            asyncio.run(run());p.db.close()
