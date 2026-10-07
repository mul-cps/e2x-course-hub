import unittest
from unittest.mock import AsyncMock

from e2x_course_hub.cps.compute import ComputePolicyClient


class WorkspaceRegistrationTests(unittest.IsolatedAsyncioTestCase):
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
