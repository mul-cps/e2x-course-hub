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
                async def validate_workspace(self,*args):calls.append('validate')
                async def acquire(self,*args,**kwargs):calls.append('acquire')
                async def release_after_shutdown(self,*args,**kwargs):calls.append('release')
            class Hub:
                async def stop_confirmed(self,w):
                    calls.append('stop')
                    if stop_failure:raise TimeoutError()
                async def remove_users_from_group(self,*args):calls.append('revoke')
                async def replace_shares(self,*args):calls.append('shares')
                async def sync_group(self,*args):calls.append('sync')
                async def start(self,*args):calls.append('spawn')
            hub=Hub();hub.hub=hub
            service=WorkspaceService(p,Compute(),hub)
            async def run():
                await p.put('courses',{'id':'c'},actor='admin')
                await p.put('memberships',{'id':'m','course_id':'c','group_id':'g','person_id':'old-name','canonical_person_id':'p1'},actor='admin')
                await p.put('workspaces',{'id':'w','course_id':'c','group_id':'g','profile':'shared-5','course_ceiling':{}},actor='admin')
                if stop_failure:
                    with self.assertRaises(TimeoutError):await service.remove_member('w','m',actor='admin')
                    self.assertEqual(len(await p.members('c')),1)
                    self.assertEqual(calls,['stop'])
                else:
                    await service.start('w',actor='admin')
                    self.assertEqual(calls[:5],['validate','acquire','sync','spawn','shares'])
                    await service.remove_member('w','m',actor='admin')
                    self.assertEqual(calls[5:],['stop','revoke','shares','release'])
                    self.assertEqual(await p.members('c'),[])
                    self.assertIn('Files retained',(await service.get('w'))['notice'])
                    with self.assertRaises(ValueError):await service.start('w',actor='admin')
            asyncio.run(run());p.db.close()
