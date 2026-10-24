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


class ManagedImportTests(unittest.TestCase):
    def test_import_is_one_fenced_atomic_batch_with_reference_validation(self):
        from e2x_course_hub.cps.pool_heads import PoolMutationRPCGuard,migrate_pool_source_guard,digest
        for fault in ('none','bad-reference','duplicate','preimage','authority','mid-commit'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                async def run():
                    p=LocalCourseProvider(Path(tmp)/'source','cps')
                    await p.put('courses',{'id':'seed','name':'Original'},actor='seed')
                    client=ComputePolicyClient('https://gateway.invalid','test','cps')
                    guard=PoolMutationRPCGuard(client,database_id='11111111-1111-4111-8111-111111111111')
                    calls=[]
                    async def rpc(path,method='GET',body=None):
                        self.assertFalse(p.db.in_transaction)
                        calls.append(path)
                        if path.endswith('migration-permit'):return {'database_id':guard.database_id,'state':'absent'}
                        if path.endswith('/current'):return {'database_id':guard.database_id,'transition_id':'pending'}
                        if path.endswith('prepare-write'):
                            self.assertEqual(body['operation'],'admin.record.import')
                            self.assertEqual([item['record']['id'] for item in body['payload']['records']],['new','second','term'])
                            if fault=='preimage':
                                p.db.execute("UPDATE records SET payload=? WHERE id='seed'",('{"id":"seed","name":"Concurrent","source":"local","external_id":null}',));p.db.commit()
                            elif fault=='authority':p.db.execute('DELETE FROM pool_source_authority');p.db.commit()
                            return {'database_id':guard.database_id,'transition_id':'pending','operation':body['operation'],
                                'input_sha256':digest(body),'receipt':None,'mutation_result':None}
                        if path.endswith('complete-write'):return {'state':'pending','transition_id':'pending'}
                        raise AssertionError(path)
                    client._request=rpc
                    await migrate_pool_source_guard(p,guard)
                    records={'courses':[{'id':'new','name':'Physics'},{'id':'second','name':'Mathematics'}],
                        'terms':[{'id':'term','course_id':'new','name':'Winter'}]}
                    if fault=='bad-reference':records['terms'][0]['course_id']='missing'
                    if fault=='duplicate':records['courses'][1]['id']='new'
                    if fault=='mid-commit':
                        original=p._audit
                        def audit(*args):
                            if args[2]=='second':raise OSError('controlled commit failure')
                            return original(*args)
                        p._audit=audit
                    if fault=='none':await p.migrate_local(records,actor='reviewed-admin')
                    else:
                        with self.assertRaises((ValueError,OSError)):await p.migrate_local(records,actor='reviewed-admin')
                    rows=await p.courses()
                    self.assertEqual({r['id'] for r in rows},{'seed','new','second'} if fault=='none' else {'seed'})
                    self.assertEqual(len(await p.terms('new')),1 if fault=='none' else 0)
                    self.assertEqual(p.db.execute('SELECT COUNT(*) FROM pool_source_receipt').fetchone()[0],1 if fault=='none' else 0)
                    self.assertEqual(p.db.execute("SELECT COUNT(*) FROM audit WHERE outcome='success'").fetchone()[0],4 if fault=='none' else 1)
                    self.assertEqual(sum(path.endswith('complete-write') for path in calls),1 if fault=='none' else 0)
                    if fault in ('bad-reference','duplicate'):
                        self.assertFalse(any(path.endswith('prepare-write') for path in calls))
                    if fault=='preimage':self.assertEqual(rows[0]['name'],'Concurrent')
                    if fault=='authority':self.assertEqual(p.db.execute('SELECT COUNT(*) FROM pool_source_authority').fetchone()[0],0)
                    p.db.close()
                asyncio.run(run())


class ManagedIdentityTests(unittest.TestCase):
    def test_verified_mapping_is_atomic_and_preserves_local_ids(self):
        from e2x_course_hub.cps.pool_heads import PoolMutationRPCGuard,migrate_pool_source_guard,digest
        person='00000000-0000-4000-8000-000000000001'
        for fault in ('none','unverified','reassignment','collision','running','preimage','authority','mid-commit'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                async def run():
                    p=LocalCourseProvider(Path(tmp)/'source','cps')
                    await p.put('courses',{'id':'course','name':'Original'},actor='seed')
                    await p.put('groups',{'id':'group','course_id':'course'},actor='seed')
                    await p.put('memberships',{'id':'member-original','person_id':'student','course_id':'course','group_id':'group'},actor='seed')
                    await p.put('workspaces',{'id':'workspace','course_id':'course','group_id':'group','state':'running' if fault=='running' else 'stopped'},actor='seed')
                    if fault in ('reassignment','collision'):
                        await p.link_identities([{'hub':'cps','username':'student' if fault=='reassignment' else 'another',
                            'email':'old@example.edu','person_id':'00000000-0000-4000-8000-000000000002' if fault=='reassignment' else person,
                            'verified':True,'administrator_reviewed':True}],actor='seed')
                    success_before=p.db.execute("SELECT COUNT(*) FROM audit WHERE outcome='success'").fetchone()[0]
                    membership_before=(await p.members('course'))[0]
                    links_before=[dict(row) for row in p.db.execute('SELECT * FROM email_links')]
                    client=ComputePolicyClient('https://gateway.invalid','test','cps')
                    guard=PoolMutationRPCGuard(client,database_id='11111111-1111-4111-8111-111111111111')
                    calls=[]
                    async def rpc(path,method='GET',body=None):
                        self.assertFalse(p.db.in_transaction);calls.append(path)
                        if path.endswith('migration-permit'):return {'database_id':guard.database_id,'state':'absent'}
                        if path.endswith('/current'):return {'database_id':guard.database_id,'transition_id':'pending'}
                        if path.endswith('prepare-write'):
                            self.assertEqual(body['operation'],'admin.identity.link')
                            if fault=='preimage':
                                p.db.execute("UPDATE records SET payload=? WHERE kind='courses'",('{"id":"course","name":"Concurrent","source":"local","external_id":null}',));p.db.commit()
                            elif fault=='authority':p.db.execute('DELETE FROM pool_source_authority');p.db.commit()
                            return {'database_id':guard.database_id,'transition_id':'pending','operation':body['operation'],
                                'input_sha256':digest(body),'receipt':None,'mutation_result':None}
                        if path.endswith('complete-write'):return {'state':'pending','transition_id':'pending'}
                        raise AssertionError(path)
                    client._request=rpc;await migrate_pool_source_guard(p,guard)
                    if fault=='mid-commit':
                        original=p._audit
                        def audit(*args):
                            if args[1]=='identities':raise OSError('controlled commit failure')
                            return original(*args)
                        p._audit=audit
                    rows=[{'hub':'cps','username':'student','email':' Student@Example.edu ','person_id':person,
                        'verified':fault!='unverified','administrator_reviewed':True}]
                    if fault=='none':
                        self.assertEqual(await p.link_identities(rows,actor='admin'),{'student':{'email':'student@example.edu','person_id':person}})
                    else:
                        with self.assertRaises((ValueError,OSError)):await p.link_identities(rows,actor='admin')
                    membership=(await p.members('course'))[0]
                    self.assertEqual(membership['id'],'member-original');self.assertEqual(membership['person_id'],'student')
                    if fault=='none':self.assertEqual(membership['canonical_person_id'],person)
                    else:
                        self.assertEqual(membership,membership_before)
                        self.assertEqual([dict(row) for row in p.db.execute('SELECT * FROM email_links')],links_before)
                    self.assertEqual(p.db.execute("SELECT COUNT(*) FROM audit WHERE outcome='success'").fetchone()[0],success_before+(2 if fault=='none' else 0))
                    self.assertEqual(p.db.execute('SELECT COUNT(*) FROM pool_source_receipt').fetchone()[0],1 if fault=='none' else 0)
                    self.assertEqual(sum(path.endswith('complete-write') for path in calls),1 if fault=='none' else 0)
                    if fault in ('unverified','reassignment','collision','running'):
                        self.assertFalse(any(path.endswith('prepare-write') for path in calls))
                    p.db.close()
                asyncio.run(run())

    def test_retry_never_repairs_changed_identity_effects_from_a_receipt(self):
        from e2x_course_hub.cps.pool_heads import PoolMutationRPCGuard,migrate_pool_source_guard,digest
        import json
        for fault in ('verified','email','membership'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                async def run():
                    p=LocalCourseProvider(Path(tmp)/'source','cps')
                    await p.put('courses',{'id':'c'},actor='seed')
                    await p.put('memberships',{'id':'m','person_id':'student','course_id':'c'},actor='seed')
                    client=ComputePolicyClient('https://gateway.invalid','test','cps')
                    guard=PoolMutationRPCGuard(client,database_id='11111111-1111-4111-8111-111111111111')
                    completed=[]
                    async def rpc(path,method='GET',body=None):
                        self.assertFalse(p.db.in_transaction)
                        if path.endswith('migration-permit'):return {'database_id':guard.database_id,'state':'absent'}
                        if path.endswith('/current'):return {'database_id':guard.database_id,'transition_id':'pending'}
                        if path.endswith('prepare-write'):return {'database_id':guard.database_id,'transition_id':'pending',
                            'operation':body['operation'],'input_sha256':digest(body),'receipt':None,'mutation_result':None}
                        if path.endswith('complete-write'):
                            completed.append(body);return {'state':'pending','transition_id':'pending'}
                        raise AssertionError(path)
                    client._request=rpc;await migrate_pool_source_guard(p,guard)
                    rows=[{'hub':'cps','username':'student','email':'student@example.edu','person_id':'00000000-0000-4000-8000-000000000001',
                        'verified':True,'administrator_reviewed':True}]
                    await p.link_identities(rows,actor='admin')
                    success=p.db.execute("SELECT COUNT(*) FROM audit WHERE outcome='success'").fetchone()[0]
                    if fault=='verified':p.db.execute('UPDATE email_links SET verified=0')
                    elif fault=='email':p.db.execute("UPDATE email_links SET email='changed@example.edu'")
                    else:
                        record=(await p.members('c'))[0];record['canonical_person_id']=None
                        p.db.execute("UPDATE records SET payload=? WHERE kind='memberships'",(json.dumps(record),))
                    p.db.commit();before=p._identity_projection({'student'})
                    with self.assertRaises(ValueError):await p.link_identities(rows,actor='admin')
                    self.assertEqual(p._identity_projection({'student'}),before)
                    self.assertEqual(len(completed),1)
                    self.assertEqual(p.db.execute("SELECT COUNT(*) FROM audit WHERE outcome='success'").fetchone()[0],success)
                    self.assertEqual(p.db.execute('SELECT COUNT(*) FROM pool_source_receipt').fetchone()[0],1)
                    p.db.close()
                asyncio.run(run())
