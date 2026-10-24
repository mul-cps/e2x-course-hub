"""Default-off and persisted Source guards; no remote service is deployed."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from e2x_course_hub.cps.providers import LocalCourseProvider
from e2x_course_hub.cps.compute import ComputePolicyClient


class SourceGuardTests(unittest.TestCase):
    def test_managed_source_requires_exact_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):LocalCourseProvider(Path(tmp)/'db','cps',v2_managed=True)

    def test_direct_import_identity_and_legacy_group_writers_are_denied(self):
        from e2x_course_hub.cps.pool_heads import PoolMutationRPCGuard
        from e2x_course_hub.api.hub_api import HubAPI
        with tempfile.TemporaryDirectory() as tmp:
            client=ComputePolicyClient('https://gateway.invalid','private-test-token','cps')
            guard=PoolMutationRPCGuard(client,database_id='11111111-1111-4111-8111-111111111111')
            from e2x_course_hub.cps.pool_heads import migrate_pool_source_guard
            client._request=AsyncMock(return_value={'database_id':guard.database_id,'state':'absent'})
            p=LocalCourseProvider(Path(tmp)/'db','cps')
            asyncio.run(migrate_pool_source_guard(p,guard))
            with self.assertRaises(ValueError):LocalCourseProvider(Path(tmp)/'db','cps')
            async def run():
                with self.assertRaises(ValueError):await p.migrate_local({},actor='admin')
                with self.assertRaises(ValueError):await p.link_identities([],actor='admin')
                hub=HubAPI(api_token='test',api_url='https://hub.invalid/hub/api')
                hub.audit_provider=p
                hub.client.fetch=AsyncMock(side_effect=AssertionError('No network mutation permitted'))
                with self.assertRaises(ValueError):await hub.delete_group('course.group')
                hub.client.fetch.assert_not_awaited()
            asyncio.run(run())
            self.assertEqual(p.db.execute('SELECT * FROM records').fetchall(),[])
            p.db.close()

    def test_failed_managed_constructor_never_installs_marker(self):
        from e2x_course_hub.cps.pool_heads import PoolMutationRPCGuard
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'db'
            p=LocalCourseProvider(path,'cps');p.db.close()
            client=ComputePolicyClient('https://gateway.invalid','test','cps')
            guard=PoolMutationRPCGuard(client,database_id='11111111-1111-4111-8111-111111111111')
            with self.assertRaises(ValueError):LocalCourseProvider(path,'cps',v2_managed=True,pool_guard=guard)
            reopened=LocalCourseProvider(path,'cps')
            self.assertEqual(reopened.db.execute("SELECT name FROM sqlite_master WHERE name GLOB 'pool_source_*'").fetchall(),[])
            reopened.db.close()

    def test_runtime_enable_with_missing_operator_coverage_fails_before_creating_source(self):
        from e2x_course_hub.cps.providers import configure_provider
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'db'
            with self.assertRaisesRegex(ValueError,'operator'):
                configure_provider({'nativePoolHeads':{'enabled':True}},path,'cps')
            self.assertFalse(path.exists())

    def test_existing_handle_rejects_lost_authority_row_before_any_rpc(self):
        from e2x_course_hub.cps.pool_heads import PoolMutationRPCGuard,migrate_pool_source_guard
        with tempfile.TemporaryDirectory() as tmp:
            client=ComputePolicyClient('https://gateway.invalid','test','cps')
            guard=PoolMutationRPCGuard(client,database_id='11111111-1111-4111-8111-111111111111')
            client._request=AsyncMock(return_value={'database_id':guard.database_id,'state':'absent'})
            p=LocalCourseProvider(Path(tmp)/'db','cps')
            async def run():
                await p.put('courses',{'id':'c','name':'Original'},actor='admin')
                await migrate_pool_source_guard(p,guard)
                p.db.execute('DELETE FROM pool_source_authority');p.db.commit()
                client._request.reset_mock()
                with self.assertRaises(ValueError):await p.delete('courses','c',actor='admin')
                with self.assertRaises(ValueError):await p.put('courses',{'id':'c','name':'Changed'},actor='admin')
                client._request.assert_not_awaited()
                self.assertEqual((await p.courses())[0]['name'],'Original')
                self.assertEqual(p.db.execute('SELECT COUNT(*) FROM pool_source_authority').fetchone()[0],0)
            asyncio.run(run());p.db.close()

    def test_delete_rechecks_preimage_and_references_after_remote_fence(self):
        import json
        from e2x_course_hub.cps.pool_heads import PoolMutationRPCGuard,migrate_pool_source_guard,digest
        for fault in ('preimage','child','authority'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as tmp:
                p=LocalCourseProvider(Path(tmp)/'db','cps')
                client=ComputePolicyClient('https://gateway.invalid','test','cps')
                guard=PoolMutationRPCGuard(client,database_id='11111111-1111-4111-8111-111111111111')
                completed=[]
                async def rpc(path,method='GET',payload=None):
                    self.assertFalse(p.db.in_transaction)
                    if path.endswith('migration-permit'):return {'database_id':guard.database_id,'state':'absent'}
                    if path.endswith('prepare-write'):
                        with p.db:
                            if fault=='preimage':p.db.execute("UPDATE records SET payload=? WHERE kind='courses'",(json.dumps({'id':'c','name':'Changed','source':'local'}),))
                            elif fault=='child':p.db.execute('INSERT INTO records VALUES(?,?,?,?)',('cps','terms','term',json.dumps({'id':'term','course_id':'c','source':'local'})))
                            else:p.db.execute('DELETE FROM pool_source_authority')
                        return {'database_id':guard.database_id,'operation':payload['operation'],
                            'input_sha256':digest(payload),'transition_id':'22222222-2222-4222-8222-222222222222',
                            'receipt':None,'mutation_result':None}
                    completed.append(path);raise AssertionError('Completion must not follow a failed local CAS')
                client._request=rpc
                async def run():
                    await p.put('courses',{'id':'c','name':'Original'},actor='seed')
                    await migrate_pool_source_guard(p,guard)
                    with self.assertRaises(ValueError):await p.delete('courses','c',actor='admin')
                    self.assertEqual(len(await p.courses()),1)
                    self.assertEqual(p.db.execute('SELECT COUNT(*) FROM pool_source_receipt').fetchone()[0],0)
                    self.assertEqual(p.db.execute("SELECT COUNT(*) FROM audit WHERE outcome='success'").fetchone()[0],1)
                    self.assertFalse(completed)
                asyncio.run(run());p.db.close()

    def test_changed_client_origin_is_rejected_before_rpc(self):
        from e2x_course_hub.cps.pool_heads import PoolMutationRPCGuard,migrate_pool_source_guard
        with tempfile.TemporaryDirectory() as tmp:
            p=LocalCourseProvider(Path(tmp)/'db','cps')
            client=ComputePolicyClient('https://gateway.invalid','test','cps')
            guard=PoolMutationRPCGuard(client,database_id='11111111-1111-4111-8111-111111111111')
            client._request=AsyncMock(return_value={'database_id':guard.database_id,'state':'absent'})
            async def run():
                await p.put('courses',{'id':'c','name':'Original'},actor='seed')
                await migrate_pool_source_guard(p,guard)
                client.base_url='https://different.invalid';client._request.reset_mock()
                with self.assertRaises(ValueError):await p.delete('courses','c',actor='admin')
                client._request.assert_not_awaited()
                self.assertEqual(len(await p.courses()),1)
            asyncio.run(run());p.db.close()
