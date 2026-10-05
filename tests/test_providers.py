import asyncio
import tempfile
import unittest
from pathlib import Path
from e2x_course_hub.cps.providers import LocalCourseProvider, configure_provider, reconcile

class ProvidersTest(unittest.TestCase):
    def test_disabled_moodle_and_enable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = configure_provider({'moodle': {'enabled': False, 'authSecretRef': 'never-read'}}, Path(tmp)/'db', 'cps')
            self.assertIsInstance(provider, LocalCourseProvider)
            with self.assertRaisesRegex(ValueError, 'planned / not deployed'):
                configure_provider({'moodle': {'enabled': True}}, Path(tmp)/'other', 'cps')
            self.assertFalse((Path(tmp)/'other').exists())

    def test_crud_provenance_audit_and_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = LocalCourseProvider(Path(tmp)/'db', 'cps')
            async def run():
                await p.put('courses', {'id': 'existing', 'name': 'Physics'}, actor='admin')
                await p.put('memberships', {'id': 'm1', 'course_id': 'existing', 'person_id': 'p1'}, actor='admin')
                self.assertEqual((await p.courses())[0]['source'], 'local')
                self.assertEqual(len(p.audit()), 2)
                with self.assertRaises(PermissionError):
                    await p.put('courses', {'id': 'existing', 'name': 'Changed', 'source': 'moodle'}, actor='admin')
                p.console = 'cit'
                self.assertEqual(await p.courses(), [])
            asyncio.run(run())

    def test_source_neutral_reconciliation(self):
        class Fake:
            async def courses(self): return [{'id': 'c', 'source': 'fake'}]
            async def members(self, course_id): return [{'person_id': 'p'}]
            async def groups(self, course_id): return []
            async def groupings(self, course_id): return []
        seen = []
        async def apply(snapshot): seen.append(snapshot)
        asyncio.run(reconcile(Fake(), apply))
        self.assertEqual(seen[0]['members'][0]['person_id'], 'p')

class UpstreamMigrationTest(unittest.TestCase):
    def test_old_configuration_and_membership_references_survive_import(self):
        from types import SimpleNamespace as NS
        from e2x_course_hub.schema.course import CourseMetadata, TermConfig
        from e2x_course_hub.cps.migration import import_upstream
        metadata = CourseMetadata(course_id='existing', course_name='Same Name')
        term = TermConfig(allowed_profiles=[])
        course = NS(metadata=metadata, terms={'2026': {}}, config=NS(terms={'2026':term}))
        server = NS(courses={'existing':course}, roles=NS(root={'student':{}}))
        class Hub:
            async def get_group(self, name): return {'users':['old-user']}
        with tempfile.TemporaryDirectory() as tmp:
            provider = LocalCourseProvider(Path(tmp)/'db', 'cps')
            async def run():
                await import_upstream(server, Hub(), provider, actor='migration')
                self.assertEqual((await provider.courses())[0]['id'], 'existing')
                self.assertEqual((await provider.groups('existing'))[0]['id'], 'existing.2026.student')
                self.assertEqual((await provider.members('existing'))[0]['person_id'], 'old-user')
            asyncio.run(run())
        self.assertEqual(metadata.source, 'local')
        self.assertEqual(term.source, 'local')

class ImportAtomicityTest(unittest.TestCase):
    def test_invalid_child_rolls_back_entire_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = LocalCourseProvider(Path(tmp)/'db', 'cps')
            async def run():
                with self.assertRaises(ValueError):
                    await provider.migrate_local({'courses': [{'id':'c'}], 'memberships':[{'id':'m','course_id':'missing'}]}, actor='migration')
                self.assertEqual(await provider.courses(), [])
                self.assertEqual(provider.audit(), [])
            asyncio.run(run())
            provider.db.close()

class UpstreamCapabilityTest(unittest.TestCase):
    def test_external_membership_edits_rejected_by_existing_backend(self):
        from types import SimpleNamespace as NS
        from e2x_course_hub.api.course_api import CourseAPI
        api = CourseAPI.__new__(CourseAPI)
        api.server = NS(courses={'c':NS(metadata=NS(source='moodle'), terms={'t':{'profile':{}}}, config=NS(terms={'t':NS(source='local')}))})
        with self.assertRaisesRegex(PermissionError, 'read-only'):
            api.ensure_local_editable('c','t')
        api.server.courses['c'].metadata.source = 'local'
        api.ensure_local_editable('c','t')
