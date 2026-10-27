"""Authenticated Hub requests must never escape the configured API boundary."""
import unittest
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from e2x_course_hub.api.hub_api import HubAPI
from e2x_course_hub.api.errors import HubAPIError


class HubRequestBoundaryTest(unittest.IsolatedAsyncioTestCase):
    def hub(self):
        hub = HubAPI(api_url='https://hub.example/hub/api', api_token='private-token')
        hub.client = SimpleNamespace(fetch=AsyncMock(return_value=SimpleNamespace(body=b'{}')))
        return hub

    async def test_rejects_foreign_or_ambiguous_urls_before_sending_credentials(self):
        urls = [
            'https://other.example/hub/api/users/alice/server',
            'http://hub.example/hub/api/users/alice/server',
            'https://hub.example:444/hub/api/users/alice/server',
            'https://user@hub.example/hub/api/users/alice/server',
            'https://hub.example/hub/api-evil/users/alice/server',
            'https://hub.example/hub/api/../users/alice/server',
            'https://hub.example/hub/api/%2e%2e/users/alice/server',
            'https://hub.example/hub/api/users/alice/server#fragment',
            'https://hub.example/hub/api/users/alice%2f..%2fgroups/g',
        ]
        for url in urls:
            with self.subTest(url=url):
                hub = self.hub()
                with self.assertRaises(HubAPIError):
                    await hub.request(url)
                hub.client.fetch.assert_not_awaited()

    async def test_requests_never_follow_authenticated_redirects(self):
        hub = self.hub()
        await hub.request('https://hub.example/hub/api/users?offset=0')
        self.assertFalse(hub.client.fetch.call_args.args[0].follow_redirects)

    async def test_stop_exception_requires_exact_server_route(self):
        for suffix in ['groups/users/alice/server', 'users/alice/servers/x/extra',
                       'users/alice/roles/servers/x', 'users/alice/server/extra']:
            with self.subTest(suffix=suffix):
                hub = self.hub()
                def deny(operation):
                    raise ValueError('uncovered mutation')
                hub.audit_provider = SimpleNamespace(pool_guard=SimpleNamespace(deny_uncovered=deny))
                with self.assertRaisesRegex(ValueError, 'uncovered'):
                    await hub.request('https://hub.example/hub/api/'+suffix, method='DELETE')
                hub.client.fetch.assert_not_awaited()

    async def test_normal_server_stops_remain_available_with_audit(self):
        for suffix in ['users/alice/server', 'users/alice/servers/team-1']:
            with self.subTest(suffix=suffix):
                hub = self.hub()
                db = sqlite3.connect(':memory:')
                db.execute('CREATE TABLE audit(actor, console, action, target, previous, current, time, outcome)')
                def deny(operation):
                    raise ValueError('uncovered mutation')
                hub.audit_provider = SimpleNamespace(
                    console='cps', db=db, pool_guard=SimpleNamespace(deny_uncovered=deny))
                try:
                    await hub.request('https://hub.example/hub/api/'+suffix, method='DELETE')
                    self.assertEqual(db.execute('SELECT outcome FROM audit').fetchall(), [('success',)])
                    requests = [call.args[0] for call in hub.client.fetch.call_args_list]
                    self.assertEqual([r.method for r in requests], ['GET', 'DELETE', 'GET'])
                    self.assertTrue(all(not r.follow_redirects for r in requests))
                finally:
                    db.close()

    async def test_lowercase_mutation_cannot_skip_managed_write_guard(self):
        hub = self.hub()
        def deny(operation):
            raise ValueError('uncovered mutation')
        hub.audit_provider = SimpleNamespace(pool_guard=SimpleNamespace(deny_uncovered=deny))
        with self.assertRaisesRegex(ValueError, 'uncovered'):
            await hub.request('https://hub.example/hub/api/groups/g', method='post', body='{}')
        hub.client.fetch.assert_not_awaited()

    async def test_audit_snapshots_preserve_custom_api_prefix(self):
        prefix = 'https://hub.example/users/service/servers/console/hub/api'
        hub = self.hub()
        hub.api_url = prefix
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE audit(actor, console, action, target, previous, current, time, outcome)')
        hub.audit_provider = SimpleNamespace(console='cps', db=db)
        try:
            await hub.request(prefix+'/users/alice/servers/team-1', method='DELETE')
            urls = [call.args[0].url for call in hub.client.fetch.call_args_list]
            self.assertEqual(urls, [prefix+'/users/alice',
                                   prefix+'/users/alice/servers/team-1',
                                   prefix+'/users/alice'])
        finally:
            db.close()
