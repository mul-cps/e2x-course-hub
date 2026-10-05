import asyncio
import tempfile
import unittest
from pathlib import Path
from e2x_course_hub.cps.identity import reviewed_email_mapping
from e2x_course_hub.cps.providers import LocalCourseProvider

class IdentityTest(unittest.TestCase):
    def test_explicit_verified_email_links_cross_hub_aliases(self):
        mapping=reviewed_email_mapping([
            {'hub':'cps','username':'old-cps','email':' Person@Example.edu ','person_id':'00000000-0000-4000-8000-000000000001','verified':True},
            {'hub':'cit','username':'old-cit','email':'person@example.edu','person_id':'00000000-0000-4000-8000-000000000001','administrator_reviewed':True,'verified':True}])
        self.assertEqual(mapping[('cps','old-cps')],mapping[('cit','old-cit')])

    def test_missing_unverified_and_duplicate_maps_rejected(self):
        bad=[{'hub':'cps','username':'person@example.edu'},
             {'hub':'cps','username':'person','email':'person@example.edu'},
             {'hub':'cps','username':'person','email':'missing','person_id':'00000000-0000-4000-8000-000000000001','verified':True}]
        for row in bad:
            with self.assertRaises(ValueError):reviewed_email_mapping([row])
        with self.assertRaises(ValueError):reviewed_email_mapping([
            {'hub':'cps','username':'one','email':'person@example.edu','person_id':'00000000-0000-4000-8000-000000000001','verified':True},
            {'hub':'cps','username':'two','email':'PERSON@example.edu','person_id':'00000000-0000-4000-8000-000000000001','verified':True}])

    def test_membership_requires_reviewed_mapping_and_canonical_reassignment_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider=LocalCourseProvider(Path(tmp)/'db','cps')
            async def run():
                await provider.put('courses',{'id':'c'},actor='admin')
                record={'id':'old-membership','course_id':'c','person_id':'old-user','canonical_person_id':'00000000-0000-4000-8000-000000000001'}
                with self.assertRaises(ValueError):await provider.put('memberships',record,actor='teacher')
                await provider.link_identities([{'hub':'cps','username':'old-user','email':'Person@Example.edu','person_id':'00000000-0000-4000-8000-000000000001','administrator_reviewed':True,'verified':True}],actor='admin')
                await provider.put('memberships',record,actor='teacher')
                self.assertEqual((await provider.members('c'))[0]['person_id'],'old-user')
                with self.assertRaises(ValueError):await provider.link_identities([{'hub':'cps','username':'old-user','email':'other@example.edu','person_id':'00000000-0000-4000-8000-000000000001','verified':True}],actor='admin')
                self.assertTrue(any(r['outcome']=='failure' for r in provider.audit()))
            asyncio.run(run());provider.db.close()

    def test_email_only_schema_requires_explicit_reviewed_uuid_handover(self):
        import sqlite3,json
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'db'
            with sqlite3.connect(path) as db:
                db.executescript('CREATE TABLE email_links(console TEXT,username TEXT,email TEXT,PRIMARY KEY(console,username),UNIQUE(console,email)); CREATE TABLE records(console TEXT,kind TEXT,id TEXT,payload TEXT,PRIMARY KEY(console,kind,id));')
                db.execute('INSERT INTO email_links VALUES (?,?,?)',('cps','old-user','person@example.edu'))
                db.execute('INSERT INTO records VALUES (?,?,?,?)',('cps','courses','course-original',json.dumps({'id':'course-original','source':'local'})))
                db.execute('INSERT INTO records VALUES (?,?,?,?)',('cps','memberships','membership-original',json.dumps({'id':'membership-original','course_id':'course-original','person_id':'old-user','canonical_person_id':'person@example.edu','source':'local'})))
            provider=LocalCourseProvider(path,'cps')
            person='00000000-0000-4000-8000-000000000001'
            async def run():
                old=provider.db.execute('SELECT canonical_person_id FROM email_links').fetchone()
                self.assertIsNone(old['canonical_person_id'])
                with self.assertRaises(ValueError):await provider.link_identities([{'hub':'cps','username':'old-user','email':'person@example.edu','person_id':person,'verified':True}],actor='admin')
                await provider.link_identities([{'hub':'cps','username':'old-user','email':'person@example.edu','person_id':person,'administrator_reviewed':True,'verified':True}],actor='admin')
                record=(await provider.members('course-original'))[0]
                self.assertEqual(record['id'],'membership-original')
                self.assertEqual(record['person_id'],'old-user')
                self.assertEqual(record['canonical_person_id'],person)
                linked=provider.db.execute('SELECT email FROM email_links').fetchone()
                self.assertEqual(linked['email'],'person@example.edu')
            asyncio.run(run());provider.db.close()

    def test_uuid_collision_across_separate_requests_and_email_handover(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider=LocalCourseProvider(Path(tmp)/'db','cps')
            person='00000000-0000-4000-8000-000000000001'
            async def run():
                await provider.link_identities([{'hub':'cps','username':'alice','email':'alice@example.edu','person_id':person,'administrator_reviewed':True,'verified':True}],actor='admin')
                with self.assertRaisesRegex(ValueError,'duplicate canonical'):
                    await provider.link_identities([{'hub':'cps','username':'bob','email':'bob@example.edu','person_id':person,'administrator_reviewed':True,'verified':True}],actor='admin')
                self.assertIsNone(provider.db.execute('SELECT username FROM email_links WHERE username=?',('bob',)).fetchone())
                await provider.link_identities([{'hub':'cps','username':'alice','email':'new-alice@example.edu','person_id':person,'administrator_reviewed':True,'verified':True}],actor='admin')
                linked=provider.db.execute('SELECT email,canonical_person_id FROM email_links WHERE username=?',('alice',)).fetchone()
                self.assertEqual(linked['canonical_person_id'],person)
                self.assertEqual(linked['email'],'new-alice@example.edu')
            asyncio.run(run());provider.db.close()
