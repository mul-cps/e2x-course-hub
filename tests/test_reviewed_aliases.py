import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from e2x_course_hub.cps.providers import LocalCourseProvider
from e2x_course_hub.cps.workspaces import WorkspaceService

PERSON = '00000000-0000-4000-8000-000000000001'
OTHER = '00000000-0000-4000-8000-000000000002'


def alias(username='old-name', person=PERSON, **changes):
    return dict(hub='cps', username=username, person_id=person,
                authority='reviewed_account_alias', administrator_reviewed=True,
                review_reason='Reviewed against the existing Hub account inventory', **changes)


class ReviewedAliasTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'db'
        self.provider = LocalCourseProvider(self.path, 'cps')
        await self.provider.put('courses', {'id': 'course-original'}, actor='admin')
        await self.provider.put('groups', {'id': 'group-original', 'course_id': 'course-original'}, actor='admin')
        self.workspace = dict(course_id='course-original', group_id='group-original')
        self.service = WorkspaceService(self.provider, None, None)

    async def asyncTearDown(self):
        self.provider.db.close()
        self.tmp.cleanup()

    def membership(self, username='old-name', person=PERSON, identifier='membership-original'):
        return dict(id=identifier, course_id='course-original', group_id='group-original',
                    person_id=username, canonical_person_id=person)

    async def test_reviewed_alias_with_unverified_email_resolves_real_membership_and_workspace(self):
        await self.provider.link_identities([alias(email='person@example.edu', verified=False)], actor='console-admin')
        record = await self.provider.put('memberships', self.membership(), actor='teacher')
        self.assertEqual(record['person_id'], 'old-name')
        self.assertEqual(await self.service.members(self.workspace), [PERSON])
        self.assertEqual(self.provider.db.execute('SELECT count(*) FROM email_links').fetchone()[0], 0)
        proof = dict(self.provider.db.execute('SELECT * FROM reviewed_account_aliases').fetchone())
        self.assertEqual(proof['review_actor'], 'console-admin')
        self.assertTrue(proof['review_reason'])
        self.assertTrue(proof['reviewed_at'].endswith('+00:00'))
        audit = [r for r in self.provider.audit() if r['kind'] == 'identity-aliases'][-1]
        self.assertEqual(audit['actor'], 'console-admin')
        self.assertEqual(json.loads(audit['current'])['person_id'], PERSON)

    async def test_canonical_uuid_forms_never_escape_to_workspace_member_identifiers(self):
        person = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        await self.provider.link_identities([alias(person=person)], actor='admin')
        record = await self.provider.put('memberships', self.membership(person=person.upper()), actor='teacher')
        self.assertEqual(record['canonical_person_id'], person)
        with self.provider.db:
            record['canonical_person_id'] = person.replace('-', '').upper()
            self.provider.db.execute('UPDATE records SET payload=? WHERE kind=? AND id=?',
                                     (json.dumps(record), 'memberships', record['id']))
        self.assertEqual(await self.service.members(self.workspace), [person])

    async def test_alias_without_email_is_sufficient_but_matching_unverified_email_does_not_join(self):
        await self.provider.link_identities([alias()], actor='admin')
        await self.provider.put('memberships', self.membership(), actor='teacher')
        await self.provider.link_identities([alias('another-name', OTHER, email='person@example.edu', verified=False)], actor='admin')
        await self.provider.link_identities([alias(email='person@example.edu', verified=False)], actor='admin')
        await self.provider.put('memberships', self.membership('another-name', OTHER, 'another-membership'), actor='teacher')
        self.assertEqual(await self.service.members(self.workspace), [PERSON, OTHER])

    async def test_unverified_legacy_email_rows_remain_ineffective_after_additive_migration(self):
        self.provider.db.close()
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TABLE reviewed_account_aliases')
            db.execute('PRAGMA user_version=5')
            db.execute('INSERT INTO email_links VALUES (?,?,?,?,?)', ('cps', 'old-name', 'person@example.edu', PERSON, 0))
            db.execute('INSERT INTO records VALUES (?,?,?,?)', ('cps', 'memberships', 'membership-original', json.dumps({**self.membership(), 'source': 'local'})))
        self.provider = LocalCourseProvider(self.path, 'cps')
        self.service = WorkspaceService(self.provider, None, None)
        with self.assertRaises(ValueError):
            await self.provider.put('memberships', self.membership(), actor='teacher')
        with self.assertRaises(ValueError):
            await self.service.members(self.workspace)
        self.assertEqual((await self.provider.members('course-original'))[0]['id'], 'membership-original')
        self.assertEqual(self.provider.db.execute('SELECT verified FROM email_links').fetchone()[0], 0)

    async def test_explicit_alias_migration_preserves_legacy_ids_names_and_references(self):
        old = self.membership(person='person@example.edu')
        old['legacy_marker'] = 'keep'
        with self.provider.db:
            self.provider.db.execute('INSERT INTO email_links VALUES (?,?,?,?,?)', ('cps', 'old-name', 'person@example.edu', None, 0))
            self.provider.db.execute('INSERT INTO records VALUES (?,?,?,?)', ('cps', 'memberships', old['id'], json.dumps(old)))
        await self.provider.link_identities([alias()], actor='admin')
        records = await self.provider.members('course-original')
        self.assertEqual(records, [{**old, 'canonical_person_id': PERSON}])
        self.assertEqual(await self.service.members(self.workspace), [PERSON])
        self.assertEqual(self.provider.db.execute('SELECT verified FROM email_links').fetchone()[0], 0)
        self.assertTrue(any(r['kind'] == 'identity-membership' and r['target'] == old['id'] for r in self.provider.audit()))

    async def test_foreign_console_duplicate_alias_reassignment_and_cross_authority_conflicts_deny(self):
        await self.provider.link_identities([alias()], actor='admin')
        for row in [dict(alias(), hub='cit'), alias(person=OTHER), alias('other-name'),
                    dict(hub='cps', username='old-name', person_id=OTHER, email='verified@example.edu', verified=True)]:
            with self.subTest(row=row), self.assertRaises(ValueError):
                await self.provider.link_identities([row], actor='admin')
        with self.assertRaises(ValueError):
            await self.provider.link_identities([alias('new-name', OTHER), alias('new-name', OTHER)], actor='admin')
        self.assertEqual(self.provider.db.execute('SELECT count(*) FROM reviewed_account_aliases').fetchone()[0], 1)
        self.assertTrue(any(r['outcome'] == 'failure' for r in self.provider.audit()))

    async def test_existing_unverified_uuid_conflict_is_not_overwritten(self):
        with self.provider.db:
            self.provider.db.execute('INSERT INTO email_links VALUES (?,?,?,?,?)', ('cps', 'old-name', 'person@example.edu', OTHER, 0))
        with self.assertRaises(ValueError):
            await self.provider.link_identities([alias()], actor='admin')
        self.assertEqual(self.provider.db.execute('SELECT canonical_person_id FROM email_links').fetchone()[0], OTHER)

    async def test_review_provenance_and_explicit_uuid_are_required_and_cannot_be_spoofed(self):
        for changes in [dict(administrator_reviewed=False), dict(review_reason=' '), dict(person_id='person@example.edu'),
                        dict(review_actor='someone-else'), dict(reviewed_at='2000-01-01T00:00:00Z'), dict(verified=True)]:
            row = {**alias(), **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                await self.provider.link_identities([row], actor='admin')
        with self.assertRaises(ValueError):
            await self.provider.link_identities([alias()], actor='')

    async def test_membership_and_workspace_share_conflict_detection(self):
        await self.provider.link_identities([alias()], actor='admin')
        await self.provider.put('memberships', self.membership(), actor='teacher')
        with self.provider.db:
            self.provider.db.execute('INSERT INTO email_links VALUES (?,?,?,?,?)', ('cps', 'old-name', 'other@example.edu', OTHER, 1))
        with self.assertRaises(ValueError):
            await self.provider.put('memberships', self.membership(), actor='teacher')
        with self.assertRaises(ValueError):
            await self.service.members(self.workspace)

    async def test_alias_migration_cannot_change_active_workspace_members_or_conflicting_memberships(self):
        old = self.membership(person=None)
        with self.provider.db:
            self.provider.db.execute('INSERT INTO records VALUES (?,?,?,?)', ('cps', 'memberships', old['id'], json.dumps(old)))
        await self.provider.put('workspaces', {'id': 'w', **self.workspace, 'state': 'running'}, actor='admin')
        with self.assertRaises(ValueError):
            await self.provider.link_identities([alias()], actor='admin')
        self.assertEqual(self.provider.db.execute('SELECT count(*) FROM reviewed_account_aliases').fetchone()[0], 0)
        await self.provider.put('workspaces', {'id': 'w', **self.workspace, 'state': 'stopped'}, actor='admin')
        with self.provider.db:
            old['canonical_person_id'] = OTHER
            self.provider.db.execute('UPDATE records SET payload=? WHERE kind=? AND id=?', (json.dumps(old), 'memberships', old['id']))
        with self.assertRaises(ValueError):
            await self.provider.link_identities([alias()], actor='admin')

    async def test_batch_collision_rolls_back_prior_alias_and_preserves_failure_audit(self):
        await self.provider.link_identities([alias()], actor='admin')
        with self.assertRaises(ValueError):
            await self.provider.link_identities([alias('new-name', OTHER), alias('collision', PERSON)], actor='admin')
        rows = self.provider.db.execute('SELECT username FROM reviewed_account_aliases').fetchall()
        self.assertEqual([row['username'] for row in rows], ['old-name'])
        self.assertFalse(any(r['kind'] == 'identity-aliases' and r['target'] == 'new-name' for r in self.provider.audit()))
        self.assertEqual(self.provider.audit()[-1]['outcome'], 'failure')

    async def test_invalid_stored_review_provenance_denies_both_consumers(self):
        await self.provider.link_identities([alias()], actor='admin')
        await self.provider.put('memberships', self.membership(), actor='teacher')
        for column, value in [('reviewed_at', 'not-a-time'), ('review_actor', ' '), ('review_reason', ' ')]:
            await self.provider.link_identities([alias()], actor='admin')
            with self.provider.db:
                self.provider.db.execute('UPDATE reviewed_account_aliases SET '+column+'=?', (value,))
            with self.subTest(column=column), self.assertRaises(ValueError):
                await self.provider.put('memberships', self.membership(), actor='teacher')
            with self.subTest(column=column), self.assertRaises(ValueError):
                await self.service.members(self.workspace)

    async def test_backup_reopen_preserves_alias_authority_and_rejects_foreign_owner(self):
        await self.provider.link_identities([alias()], actor='admin')
        await self.provider.put('memberships', self.membership(), actor='teacher')
        backup = Path(self.tmp.name) / 'backup'
        self.provider.backup(backup)
        restored = LocalCourseProvider(backup, 'cps')
        try:
            self.assertEqual(await WorkspaceService(restored, None, None).members(self.workspace), [PERSON])
            self.assertEqual(restored.db.execute('PRAGMA user_version').fetchone()[0], 6)
        finally:
            restored.db.close()
        with self.assertRaises(ValueError):
            LocalCourseProvider(backup, 'cit')
