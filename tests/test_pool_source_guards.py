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
