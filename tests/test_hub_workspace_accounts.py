"""Source consumes the closed permanent Hub creation contract."""
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tornado.httpclient import HTTPRequest, HTTPResponse

from e2x_course_hub.api.hub_api import HubAPI
from e2x_course_hub.cps.accounts import WorkspaceAccountAdapter
from e2x_course_hub.cps.providers import LocalCourseProvider
from test_workspace_accounts import HubFixture


GROUP = 'cps-workspace-kernel-accounts'
OWNER = 'cps-ws-' + hashlib.sha256(b'cps\0w').hexdigest()[:32]
BODY = dict(workspace='w', course='c', term='t', group='g', server='rtc',
            namespace='jupyterhub', pod='jupyter-group--rtc')


def receipt():
    digest = hashlib.sha256(json.dumps(dict(source='cps', **BODY), sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    return dict(version=1, kind='hub_neutral_creation', source='cps', workspace='w',
        owner=OWNER, descriptor_hash=digest,
        identity=dict(id=1, name=OWNER, created='2026-10-13T12:00:00Z'),
        group=GROUP, role='cps-workspace-kernel')


class PermanentHub(HubFixture):
    def __init__(self):
        super().__init__()
        self.group['name'] = GROUP
        self.posts = []
        self.result = receipt()

    async def get_group(self, name):
        assert name == GROUP
        return copy.deepcopy(self.group)

    async def create_user(self, name):
        raise AssertionError('Ordinary Hub user creation is forbidden')

    async def create_workspace_account(self, body):
        self.posts.append(copy.deepcopy(body))
        if OWNER not in self.users:
            self.users[OWNER] = dict(name=OWNER, created=self.result['identity']['created'],
                admin=False, roles=['user'], groups=[GROUP], servers={})
            self.group['users'].append(OWNER)
        if self.create_failure:
            raise TimeoutError('Committed response lost')
        return copy.deepcopy(self.result)

    async def add_users_to_group(self, name, users):
        raise AssertionError('Source cannot restore permanent Hub membership')


class PermanentSourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = LocalCourseProvider(Path(self.tmp.name) / 'source.sqlite', 'cps')
        self.hub = PermanentHub()
        self.adapter = WorkspaceAccountAdapter(self.provider, self.hub, group=GROUP)
        self.workspace = dict(id='w', course_id='c', term_id='t', group_id='g',
            hub_user=OWNER, hub_server='rtc', namespace=BODY['namespace'], pod=BODY['pod'])

    async def asyncTearDown(self):
        self.provider.db.close()
        self.tmp.cleanup()

    async def test_persists_complete_authoritative_receipt_and_closed_body(self):
        await self.adapter.ensure(self.workspace, actor='instructor')
        row = self.adapter._row(self.adapter._binding(self.workspace))
        self.assertEqual(json.loads(row['receipt']), receipt())
        self.assertEqual(row['state'], 'ready')
        self.assertEqual(self.hub.posts, [BODY])
        self.assertEqual(self.hub.users[OWNER]['servers'], {})

    async def test_timeout_and_restart_retry_same_permanent_endpoint(self):
        self.hub.create_failure = True
        with self.assertRaises(TimeoutError):
            await self.adapter.ensure(self.workspace, actor='instructor')
        self.hub.create_failure = False
        adapter = WorkspaceAccountAdapter(self.provider, self.hub, group=GROUP)
        await adapter.ensure(self.workspace, actor='instructor')
        self.assertEqual(self.hub.posts, [BODY, BODY])
        self.assertEqual(len(self.hub.users), 1)

    async def test_wrong_computed_owner_fails_before_hub_creation(self):
        with self.assertRaises(PermissionError):
            await self.adapter.ensure(dict(self.workspace, hub_user='chosen-owner'), actor='admin')
        self.assertEqual(self.hub.posts, [])

    async def test_typed_receipt_requires_full_binding_and_identity(self):
        variants = [dict(receipt(), version=True), dict(receipt(), descriptor_hash='0' * 64),
            dict(receipt(), source='cit'), dict(receipt(), workspace='other'),
            dict(receipt(), owner='other'), dict(receipt(), group='other'),
            dict(receipt(), role='admin'), dict(receipt(), extra=True)]
        for identity in (dict(id=True, name=OWNER, created='2026-10-13T12:00:00Z'),
                dict(id=0, name=OWNER, created='2026-10-13T12:00:00Z'),
                dict(id=1, name=OWNER, created='invalid'),
                dict(id=1, name='other', created='2026-10-13T12:00:00Z')):
            variants.append(dict(receipt(), identity=identity))
        for value in variants:
            with self.subTest(receipt=value):
                self.hub.result = value
                with self.assertRaises(PermissionError):
                    await self.adapter.ensure(self.workspace, actor='instructor')
        self.assertIsNone(self.adapter._row(self.adapter._binding(self.workspace))['receipt'])

    async def test_removed_group_membership_is_not_regranted(self):
        await self.adapter.ensure(self.workspace, actor='instructor')
        self.hub.users[OWNER]['groups'] = []
        self.hub.group['users'] = []
        with self.assertRaises(PermissionError):
            await self.adapter.ensure(self.workspace, actor='instructor')
        self.assertEqual(self.hub.users[OWNER]['groups'], [])

    async def test_changed_permanent_hub_identity_id_cannot_replace_source_receipt(self):
        await self.adapter.ensure(self.workspace, actor='instructor')
        self.hub.result['identity']['id'] = 2
        with self.assertRaises(PermissionError):
            await self.adapter.ensure(self.workspace, actor='instructor')

    async def test_date_only_value_is_not_an_actual_hub_creation_timestamp(self):
        value = receipt()
        value['identity']['created'] = '2026-10-13Z'
        with self.assertRaises(PermissionError):
            self.adapter._creation_receipt(value, self.adapter._binding(self.workspace))

    async def test_successful_v7_and_source_creation_receipts_keep_legacy_owner(self):
        workspace = dict(self.workspace, hub_user='retained-legacy-owner')
        binding = self.adapter._binding(workspace)
        identity = dict(name=workspace['hub_user'], created='2026-10-13T12:00:00Z')
        self.hub.users[workspace['hub_user']] = dict(identity, admin=False, roles=['user'],
                                                    groups=[GROUP], servers={})
        self.hub.group['users'].append(workspace['hub_user'])
        for value in (identity, dict(version=1, kind='source_creation', identity=identity)):
            with self.subTest(receipt=value):
                self.provider.db.execute('DELETE FROM workspace_accounts')
                self.provider.db.execute('INSERT INTO workspace_accounts VALUES (?, ?, ?, ?, ?, ?)',
                    ('cps', workspace['hub_user'], 'w', json.dumps(binding, sort_keys=True),
                     'ready', json.dumps(value)))
                self.provider.db.commit()
                await self.adapter.ensure(workspace, actor='instructor')
                self.assertEqual(json.loads(self.adapter._row(binding)['receipt']), value)
        self.assertEqual(self.hub.posts, [])

    async def test_old_unknown_post_intent_stays_fenced(self):
        binding = self.adapter._binding(self.workspace)
        self.provider.db.execute('INSERT INTO workspace_accounts VALUES (?, ?, ?, ?, ?, ?)',
            ('cps', OWNER, 'w', json.dumps(binding, sort_keys=True), 'intent', None))
        self.provider.db.commit()
        with self.assertRaises(PermissionError):
            await self.adapter.ensure(self.workspace, actor='instructor')
        self.assertEqual(self.hub.posts, [])
        self.assertEqual(self.adapter._row(binding)['state'], 'intent')


class HubEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_api_posts_only_closed_descriptor(self):
        api = HubAPI(api_url='https://hub.invalid/prefix/hub/api', api_token='fictional-service')
        requests = []
        async def request(url, method='GET', body=None):
            requests.append((url, method, json.loads(body)))
            return HTTPResponse(HTTPRequest(url), 200, buffer=io.BytesIO(json.dumps(receipt()).encode()))
        api.request = request
        self.assertEqual(await api.create_workspace_account(BODY), receipt())
        self.assertEqual(requests, [('https://hub.invalid/prefix/hub/api/cps/workspace-accounts', 'POST', BODY)])
        with self.assertRaises(ValueError):
            await api.create_workspace_account(dict(BODY, source='cps'))

    async def test_audited_post_uses_service_credential_without_endpoint_get(self):
        with tempfile.TemporaryDirectory() as root:
            provider = LocalCourseProvider(Path(root) / 'source.sqlite', 'cps')
            try:
                api = HubAPI(api_url='https://hub.invalid/hub/api', api_token='fictional-service')
                api.audit_provider = provider
                requests = []
                class Client:
                    async def fetch(self, request):
                        requests.append((request.method, request.headers['Authorization']))
                        return HTTPResponse(request, 200, buffer=io.BytesIO(json.dumps(receipt()).encode()))
                api.client = Client()
                await api.create_workspace_account(BODY)
                self.assertEqual(requests, [('POST', 'token fictional-service')])
                audit = provider.audit()[0]
                self.assertEqual(audit['outcome'], 'success')
                self.assertEqual(json.loads(audit['current'])['observed'], receipt())
            finally:
                provider.db.close()
