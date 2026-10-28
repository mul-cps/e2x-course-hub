"""Focused Source integration prerequisites and private credential boundary."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from e2x_course_hub.cps.compute import ComputePolicyClient
from e2x_course_hub.cps.providers import LocalCourseProvider


class PrivateTransportTests(unittest.IsolatedAsyncioTestCase):
    def test_reject_ambiguous_private_destinations(self):
        for base in ('https://user:secret@host', 'https://HOST', 'https://host/',
                     'https://host/a/../b', 'https://host/%2f', 'https://host:99999'):
            with self.subTest(base=base), self.assertRaises(ValueError):
                ComputePolicyClient(base, 'secret', 'cps')

    async def test_closed_paths_quoted_ids_and_owned_transport(self):
        calls, closed = [], []
        class Client:
            async def fetch(self, request, **kwargs):
                calls.append(request)
                request.streaming_callback(b'{"ok":true}')
                return SimpleNamespace(code=200, body=b'')
            def close(self): closed.append(True)
        client = ComputePolicyClient('https://host', 'secret', 'cps',
                                     http_client_factory=lambda **kwargs: Client())
        self.assertEqual(await client.grants('a ?#%'), {'ok':True})
        self.assertIn('grants/a%20%3F%23%25', calls[0].url)
        self.assertFalse(calls[0].follow_redirects)
        self.assertEqual(await client._request('profiles'), {'ok':True})
        for path in ('https://evil', '../x', 'native-pool-head/target', 'grants/a/b'):
            with self.assertRaises(ValueError): await client._request(path)
        self.assertEqual(len(calls), 2)
        self.assertEqual(closed, [True,True])

    async def test_bounded_errors_sanitized_and_redirects_refused(self):
        for code, body in ((307,b'secret'), (403,b'secret'), (200,b'{"a":1,"a":2}'),
                           (200,b'x'*(8*1024*1024+1))):
            class Client:
                async def fetch(self, request, **kwargs):
                    request.streaming_callback(body)
                    return SimpleNamespace(code=code,body=b'')
                def close(self): pass
            client=ComputePolicyClient('https://host','secret','cps',
                                       http_client_factory=lambda **kwargs:Client())
            with self.assertRaises(ValueError) as raised: await client.grants('a')
            self.assertNotIn('secret',str(raised.exception))


class BindingValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.provider=LocalCourseProvider(Path(self.tmp.name)/'source.sqlite','cps')
        self.addCleanup(self.provider.db.close)
        await self.provider.put('courses',{'id':'c'},actor='seed')
        await self.provider.put('groups',{'id':'g','course_id':'c'},actor='seed')

    async def test_closed_bindings_preserved_and_duplicates_owned_refs_rejected(self):
        row={'group_id':'g','hub_group':'team','role':'student'}
        result=await self.provider.put('courses',{'id':'c','hub_group_bindings':[row]},actor='seed')
        self.assertEqual(result['hub_group_bindings'],[row])
        result=await self.provider.put('courses',{'id':'c','name':'Updated'},actor='seed')
        self.assertEqual(result['hub_group_bindings'],[row])
        for rows in ([row,row],[dict(row,role='admin')],[dict(row,extra=True)],
                     [dict(row,group_id='missing')],[dict(row,hub_group='../bad')]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                await self.provider.put('courses',{'id':'c','hub_group_bindings':rows},actor='seed')
        await self.provider.put('courses',{'id':'other'},actor='seed')
        await self.provider.put('groups',{'id':'other-g','course_id':'other'},actor='seed')
        with self.assertRaises(ValueError):
            await self.provider.put('courses',{'id':'other','hub_group_bindings':[
                dict(row,group_id='other-g')]},actor='seed')

    def test_disabled_installer_does_not_touch_inputs_or_import_sdk(self):
        from e2x_course_hub.cps.hub_permission_handlers import install_hub_permissions
        class Poison:
            def __getattribute__(self,name):raise AssertionError(name)
        self.assertIsNone(install_hub_permissions(Poison()))

    async def test_typed_generation6_open_validates_custody_without_initializing(self):
        from dataclasses import asdict
        import hashlib
        from e2x_course_hub.cps.hub_permissions import SourceJournalBinding
        from e2x_course_hub.cps.hub_journal import SOURCE_JOURNAL_SCHEMA
        from e2x_course_hub.cps.pool_heads import SOURCE_SCHEMA,PoolMutationRPCGuard
        client=ComputePolicyClient('https://coordinator.example','secret','cps')
        guard=PoolMutationRPCGuard(client,database_id='11111111-1111-4111-8111-111111111111')
        path=Path(self.tmp.name)/'source.sqlite'
        binding=SourceJournalBinding('cps',str(path.resolve()),'https://hub.example/hub/api',
            client.base_url,guard.database_id,'22222222-2222-4222-8222-222222222222',
            'operator','33333333-3333-4333-8333-333333333333')
        with self.provider.db:
            for sql in {**SOURCE_SCHEMA,**SOURCE_JOURNAL_SCHEMA}.values():self.provider.db.execute(sql)
            self.provider.db.execute('INSERT INTO pool_source_authority VALUES(?,?,?,?)',
                ('cps',client.base_url,guard.database_id,str(path.resolve())))
            self.provider.db.execute('INSERT INTO hub_journal_authority VALUES(?,?,?,?)',
                (1,1,json.dumps(asdict(binding),sort_keys=True,separators=(',',':')),
                'sha256:'+hashlib.sha256(b'').hexdigest()))
            self.provider.db.execute('PRAGMA user_version=6')
        with self.assertRaises(ValueError):LocalCourseProvider(path,'cps',v2_managed=True,pool_guard=guard)
        reopened=LocalCourseProvider(path,'cps',v2_managed=True,pool_guard=guard,journal_binding=binding)
        self.addCleanup(reopened.db.close)
        self.assertEqual(len(reopened.hub_journal.capture_source()['records']['rows']),2)
        from unittest.mock import patch
        import sqlite3
        opened=[];connect=sqlite3.connect
        def track(*args,**kwargs):
            db=connect(*args,**kwargs);opened.append(db);return db
        with patch('e2x_course_hub.cps.hub_permissions._sdk',side_effect=ValueError('Managed mode requires compute extra')):
            with patch('e2x_course_hub.cps.providers.sqlite3.connect',side_effect=track):
                with self.assertRaisesRegex(ValueError,'requires compute extra'):
                    LocalCourseProvider(path,'cps',v2_managed=True,pool_guard=guard,journal_binding=binding)
        with self.assertRaises(sqlite3.ProgrammingError):opened[0].execute('SELECT 1')
        with self.provider.db:self.provider.db.execute('DELETE FROM hub_journal_authority')
        with self.assertRaises(ValueError):
            LocalCourseProvider(path,'cps',v2_managed=True,pool_guard=guard,journal_binding=binding)
