"""Server-only shared policy client. Tokens must never enter rendered config."""
import json
from urllib.parse import quote, urlsplit
from tornado.httpclient import AsyncHTTPClient, HTTPRequest

class ComputePolicyClient:
    def __init__(self, base_url, token, console):
        parsed = urlsplit(base_url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError('compute policy base_url must use HTTPS')
        if console not in ('cps', 'cit') or not token:
            raise ValueError('console and private service token required')
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.console = console

    async def _request(self, path, method='GET', payload=None):
        response = await AsyncHTTPClient().fetch(HTTPRequest(
            self.base_url + '/internal/v1/' + path,
            method=method,
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'},
            body=json.dumps(payload).encode() if payload is not None else None,
            request_timeout=20))
        return json.loads(response.body) if response.body else None

    async def grants(self, person):
        return await self._request('grants/' + quote(person, safe=''))

    async def put_grant(self, grant, *, actor):
        if grant.get('source', self.console) != self.console:
            raise PermissionError('console cannot mutate another console grant')
        payload = {key: value for key, value in grant.items() if key != 'source'}
        return await self._request('grants', 'PUT', payload)

    async def register_workspace(self, workspace, members, validation):
        required = ('namespace', 'pod')
        if any(not workspace.get(key) for key in required):
            raise ValueError('Operator-qualified namespace and preserved Pod name required')
        return await self._request('workspaces', 'PUT', {
            'workspace': workspace['id'], 'owner': workspace['hub_user'],
            'server': workspace['hub_server'], 'members': members,
            'principal': 'workspace:' + self.console + ':' + workspace['id'],
            'profiles': [workspace['profile']], 'ceiling': workspace['course_ceiling'],
            'namespace': workspace['namespace'], 'pod': workspace['pod'],
            'policy_hash': validation['policy_hash']})

    async def reservation_state(self, workspace):
        return await self._request("reservations/" + quote(workspace, safe=""))

    async def release_after_shutdown(self, workspace, *, confirmed_by_hub, actor, attempt=None):
        if confirmed_by_hub is not True:
            raise ValueError('Hub shutdown confirmation required')
        # Hub post-stop owns the persisted attempt. This idempotent probe cannot
        # force-release its reservation; the gateway observes actual Pod absence.
        return await self._request('reservations/release', 'POST', {'workspace': workspace, 'attempt': attempt})

    async def validate_workspace(self, workspace, members, profile, ceiling):
        return await self._request('workspace-policy', 'POST', {'workspace': workspace,
            'members': members, 'profile': profile, 'ceiling': ceiling})
