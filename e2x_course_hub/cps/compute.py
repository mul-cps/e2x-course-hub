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
        return await self._request('grants', 'PUT', {**grant, 'source': self.console, 'actor': actor})

    async def acquire(self, workspace, canonical_members, *, actor):
        return await self._request('reservations/acquire', 'POST', {
            'workspace': workspace, 'members': canonical_members, 'actor': actor})

    async def release_after_shutdown(self, workspace, *, confirmed_by_hub, actor):
        # This boolean must come from a trusted Hub adapter poll, never a browser request.
        if confirmed_by_hub is not True:
            raise ValueError('Hub shutdown confirmation required before releasing reservations')
        return await self._request('reservations/release', 'POST', {
            'workspace': workspace, 'shutdown_confirmed': True, 'actor': actor})
