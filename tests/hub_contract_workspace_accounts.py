"""Explicit compatibility gate against the installed reviewed Hub wheel.

Run with both Source and cps-compute-hub installed, never an equivalent DTO.
All databases below are isolated fictional test fixtures.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from jupyterhub import orm

from cps_compute_hub.neutral_accounts import (
    AccountProvisioningSettings, _descriptor, initialize_registry, provision_account,
)
from cps_compute_hub.roles import WORKSPACE_KERNEL_GROUP as GROUP
from cps_compute_hub.roles import WORKSPACE_KERNEL_ROLE as ROLE
from e2x_course_hub.api.errors import UserNotFoundError
from e2x_course_hub.cps.accounts import WorkspaceAccountAdapter, _creation_descriptor
from e2x_course_hub.cps.providers import LocalCourseProvider


class HubContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.provider = LocalCourseProvider(root / 'source.sqlite', 'cps')
        self.factory = orm.new_session_factory('sqlite:///' + str(root / 'hub.sqlite'))
        self.settings = AccountProvisioningSettings({'cps-admin': 'cps', 'cit-admin': 'cit'})
        with self.factory() as db:
            marker = orm.Role(name=ROLE, scopes=[])
            db.add_all([orm.Role(name='user', scopes=['self']), marker,
                        orm.Group(name=GROUP, roles=[marker])])
            db.commit()
            initialize_registry(db, AccountProvisioningSettings({'cps-admin': 'cps'},
                allow_registry_migration=True, registry_backup_sha256='a' * 64))
        self.body = dict(workspace='assignment-1', course='course-1', term='ws26',
            group='group-1', server='rtc', namespace='jupyterhub', pod='jupyter-group--rtc')
        owner = _descriptor('cps', self.body)[0]
        self.workspace = dict(id=self.body['workspace'], course_id=self.body['course'],
            term_id=self.body['term'], group_id=self.body['group'], hub_user=owner,
            hub_server=self.body['server'], namespace=self.body['namespace'], pod=self.body['pod'])
        self.posts = []
        self.response_lost = False
        outer = self

        class RealHub:
            api_url = 'https://fixture.invalid/hub/api'

            async def create_workspace_account(self, body):
                outer.posts.append(copy.deepcopy(body))
                result = provision_account(outer.factory.kw['bind'], outer.settings, 'cps', body)
                if outer.response_lost:
                    raise TimeoutError('Committed Hub response lost')
                return result

            async def get_user(self, name):
                with outer.factory() as db:
                    user = orm.User.find(db, name)
                    if user is None:
                        raise UserNotFoundError(name)
                    return dict(name=user.name, admin=user.admin,
                        created=user.created.isoformat() + 'Z', roles=[r.name for r in user.roles],
                        groups=[g.name for g in user.groups], servers={})

            async def get_group(self, name):
                with outer.factory() as db:
                    group = orm.Group.find(db, name)
                    return dict(name=group.name, roles=[r.name for r in group.roles],
                                users=[u.name for u in group.users])

            async def create_user(self, name):
                raise AssertionError('Ordinary users POST forbidden')

            async def add_users_to_group(self, name, users):
                raise AssertionError('Source group regrant forbidden')

        self.hub = RealHub()
        self.adapter = WorkspaceAccountAdapter(self.provider, self.hub, group=GROUP)

    async def asyncTearDown(self):
        self.provider.db.close()
        self.factory.kw['bind'].dispose()
        self.tmp.cleanup()

    async def test_actual_hub_receipt_persists_complete_identity_and_stopped_owner(self):
        await self.adapter.ensure(self.workspace, actor='fictional-instructor')
        row = self.adapter._row(self.adapter._binding(self.workspace))
        expected = provision_account(self.factory.kw['bind'], self.settings, 'cps', self.body)
        self.assertEqual(json.loads(row['receipt']), expected)
        with self.factory() as db:
            user = orm.User.find(db, self.workspace['hub_user'])
            self.assertEqual(expected['identity']['id'], user.id)
            self.assertEqual(user.orm_spawners, {})
            self.assertFalse(user.admin)

    async def test_exact_retry_after_timeout_and_source_db_reopen_returns_same_hub_identity(self):
        self.response_lost = True
        with self.assertRaises(TimeoutError):
            await self.adapter.ensure(self.workspace, actor='fictional-instructor')
        committed = provision_account(self.factory.kw['bind'], self.settings, 'cps', self.body)
        self.response_lost = False
        path = Path(self.tmp.name) / 'source.sqlite'
        self.provider.db.close()
        self.provider = LocalCourseProvider(path, 'cps')
        self.adapter = WorkspaceAccountAdapter(self.provider, self.hub, group=GROUP)
        await self.adapter.ensure(self.workspace, actor='fictional-instructor')
        self.assertEqual(json.loads(self.adapter._row(self.adapter._binding(self.workspace))['receipt']), committed)
        self.assertEqual(self.posts, [self.body, self.body])
        with self.factory() as db:
            self.assertEqual(db.query(orm.User).count(), 1)

    async def test_actual_hub_revocation_cannot_be_restored_by_source_ensure(self):
        await self.adapter.ensure(self.workspace, actor='fictional-instructor')
        with self.factory() as db:
            user = orm.User.find(db, self.workspace['hub_user'])
            user.groups.clear()
            db.commit()
        with self.assertRaises(PermissionError):
            await self.adapter.ensure(self.workspace, actor='fictional-instructor')
        with self.factory() as db:
            self.assertEqual(orm.User.find(db, self.workspace['hub_user']).groups, [])

    async def test_descriptor_and_owner_match_actual_hub_contract_for_both_sources(self):
        for source in ('cps', 'cit'):
            body, owner, digest = _creation_descriptor(source, self.workspace)
            expected_owner, serialized, expected_digest = _descriptor(source, body)
            self.assertEqual(owner, expected_owner)
            self.assertEqual(digest, expected_digest)
            self.assertEqual(json.loads(serialized), dict(source=source, **body))
        for field, value in [('id', '-bad'), ('term_id', 'x' * 129),
                             ('hub_server', 'UPPER'), ('namespace', 'a' * 64), ('pod', 'bad..pod')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                _creation_descriptor('cps', dict(self.workspace, **{field: value}))
