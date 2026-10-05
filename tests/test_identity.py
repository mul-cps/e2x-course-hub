import asyncio
import tempfile
import unittest
from pathlib import Path
from e2x_course_hub.cps.identity import reviewed_email_mapping
from e2x_course_hub.cps.providers import LocalCourseProvider

class IdentityTest(unittest.TestCase):
    def test_explicit_verified_email_links_cross_hub_aliases(self):
        mapping=reviewed_email_mapping([
            {'hub':'cps','username':'old-cps','email':' Person@Example.edu ','verified':True},
            {'hub':'cit','username':'old-cit','email':'person@example.edu','administrator_reviewed':True}])
        self.assertEqual(mapping[('cps','old-cps')],mapping[('cit','old-cit')])

    def test_missing_unverified_and_duplicate_maps_rejected(self):
        bad=[{'hub':'cps','username':'person@example.edu'},
             {'hub':'cps','username':'person','email':'person@example.edu'},
             {'hub':'cps','username':'person','email':'missing','verified':True}]
        for row in bad:
            with self.assertRaises(ValueError):reviewed_email_mapping([row])
        with self.assertRaises(ValueError):reviewed_email_mapping([
            {'hub':'cps','username':'one','email':'person@example.edu','verified':True},
            {'hub':'cps','username':'two','email':'PERSON@example.edu','verified':True}])

    def test_membership_requires_reviewed_mapping_and_canonical_reassignment_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider=LocalCourseProvider(Path(tmp)/'db','cps')
            async def run():
                await provider.put('courses',{'id':'c'},actor='admin')
                record={'id':'old-membership','course_id':'c','person_id':'old-user','canonical_person_id':'person@example.edu'}
                with self.assertRaises(ValueError):await provider.put('memberships',record,actor='teacher')
                await provider.link_identities([{'hub':'cps','username':'old-user','email':'Person@Example.edu','administrator_reviewed':True}],actor='admin')
                await provider.put('memberships',record,actor='teacher')
                self.assertEqual((await provider.members('c'))[0]['person_id'],'old-user')
                with self.assertRaises(ValueError):await provider.link_identities([{'hub':'cps','username':'old-user','email':'other@example.edu','verified':True}],actor='admin')
                self.assertTrue(any(r['outcome']=='failure' for r in provider.audit()))
            asyncio.run(run());provider.db.close()
