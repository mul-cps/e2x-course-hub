import unittest
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from tornado.httpclient import HTTPClientError

from e2x_course_hub.cps.compute import ComputePolicyClient


class WorkspaceRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_prospective_policy_uses_read_only_preview(self):
        client = ComputePolicyClient('https://internal.example', 'fixture-token', 'cps')
        client._request = AsyncMock(return_value={'policy_hash': 'current-hash'})
        await client.validate_workspace('workspace-old', ['person'], 'cpu', {'cpu': '2'})
        client._request.assert_awaited_once_with('workspace-policy/preview', 'POST', {
            'workspace': 'workspace-old', 'members': ['person'], 'profile': 'cpu',
            'ceiling': {'cpu': '2'}, 'principal': 'workspace:cps:workspace-old',
        })

    async def test_registered_policy_refreshes_complete_provisioned_registration(self):
        client = ComputePolicyClient('https://internal.example', 'fixture-token', 'cit')
        current = {
            'workspace': 'workspace/old +&', 'source': 'cit',
            'owner': 'neutral-old', 'server': 'rtc-old',
            'namespace': 'cit-jhub', 'pod': 'preserved-old',
            'members': ['current-person'], 'profiles': ['cpu'],
            'ceiling': {'cpu': '4', 'memory': '8Gi'},
            'principal': 'workspace:cit:workspace/old +&',
            'policy_hash': 'current-hash',
            'storage': {'state': 'verified', 'claim': 'workspace-pvc', 'generation': 'new'},
        }
        calls = []

        class FakeHTTP:
            async def fetch(self, request):
                calls.append(request)
                return SimpleNamespace(body=json.dumps(
                    current if request.method == 'GET' else {'policy_hash': 'current-hash'}
                ).encode())

        with patch('e2x_course_hub.cps.compute.AsyncHTTPClient', return_value=FakeHTTP()):
            result = await client.validate_workspace('workspace/old +&', ['stale-person'],
                'cpu', {'cpu': '2'}, {'principal': 'stale', 'policy_hash': 'stale-hash'})
        self.assertEqual(result, {'policy_hash': 'current-hash'})
        self.assertEqual([request.method for request in calls], ['GET', 'POST'])
        self.assertEqual(calls[0].url,
            'https://internal.example/internal/v1/workspaces/workspace%2Fold%20%2B%26')
        self.assertEqual(calls[1].url, 'https://internal.example/internal/v1/workspace-policy')
        self.assertEqual(json.loads(calls[1].body), {**current, 'profile': 'cpu'})
        self.assertTrue(all(request.headers['Authorization'] == 'Bearer fixture-token'
            for request in calls))
        self.assertNotIn('profile', current)

    async def test_registered_policy_does_not_validate_a_stale_document_if_refresh_fails(self):
        client = ComputePolicyClient('https://internal.example', 'fixture-token', 'cps')
        calls = []

        class FakeHTTP:
            async def fetch(self, request):
                calls.append(request)
                raise HTTPClientError(409, 'Registration changed')

        with patch('e2x_course_hub.cps.compute.AsyncHTTPClient', return_value=FakeHTTP()):
            with self.assertRaises(HTTPClientError):
                await client.validate_workspace('workspace-old', ['person'], 'cpu', {'cpu': '2'},
                    {'principal': 'stale', 'policy_hash': 'stale-hash'})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].method, 'GET')

    async def test_stable_group_reaches_production_storage_registration(self):
        client = ComputePolicyClient('https://internal.example', 'fixture-token', 'cps')
        client._request = AsyncMock(return_value={'registered': True})
        workspace = {
            'id': 'workspace-old', 'group_id': 'course-group-old',
            'hub_user': 'neutral-old', 'hub_server': 'rtc-old',
            'namespace': 'jupyterhub', 'pod': 'preserved-old',
            'profile': 'cpu', 'course_ceiling': {'cpu': '2', 'memory': '4Gi'},
        }
        await client.register_workspace(workspace, ['canonical-person'], {'policy_hash': 'reviewed-hash'})
        client._request.assert_awaited_once_with('workspaces', 'PUT', {
            'workspace': 'workspace-old', 'group_id': 'course-group-old',
            'owner': 'neutral-old', 'server': 'rtc-old',
            'members': ['canonical-person'], 'principal': 'workspace:cps:workspace-old',
            'profiles': ['cpu'], 'ceiling': {'cpu': '2', 'memory': '4Gi'},
            'namespace': 'jupyterhub', 'pod': 'preserved-old',
            'policy_hash': 'reviewed-hash',
        })
