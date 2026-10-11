"""Server-only shared policy client. Tokens must never enter rendered config."""
import json
from urllib.parse import quote, urlsplit
from tornado.httpclient import AsyncHTTPClient, HTTPRequest, HTTPClientError


class NativeCleanupPending(HTTPClientError):
    """A scoped gateway response: shutdown verified, node cleanup not published."""
    def __init__(self, workspace, attempt, *, response=None):
        super().__init__(409, 'Native GPU cleanup pending; reservations retained', response=response)
        self.workspace, self.attempt = workspace, attempt

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
        context = {}
        # Legacy local workspaces may have a course without a term. Emit the
        # qualified pair only from the owning console's stored workspace.
        if workspace.get('term_id') is not None:
            keys = ('course_id', 'term_id')
            if any(not isinstance(workspace.get(key), str) or
                   not 1 <= len(workspace[key]) <= 128 for key in keys):
                raise ValueError('course_id and term_id must be nonempty strings of at most 128 characters')
            context = {key: workspace[key] for key in keys}
        return await self._request('workspaces', 'PUT', {
            'workspace': workspace['id'], 'owner': workspace['hub_user'],
            'server': workspace['hub_server'], 'members': members,
            'principal': 'workspace:' + self.console + ':' + workspace['id'],
            'profiles': [workspace['profile']], 'ceiling': workspace['course_ceiling'],
            'namespace': workspace['namespace'], 'pod': workspace['pod'],
            'policy_hash': validation['policy_hash'],
            **context,
            **({'group_id': workspace['group_id']} if 'group_id' in workspace else {})})

    async def reservation_state(self, workspace):
        return await self._request("reservations/" + quote(workspace, safe=""))

    async def release_after_shutdown(self, workspace, *, confirmed_by_hub, actor, attempt=None):
        if confirmed_by_hub is not True:
            raise ValueError('Hub shutdown confirmation required')
        # Hub post-stop owns the persisted attempt. This idempotent probe cannot
        # force-release its reservation; the gateway observes actual Pod absence.
        try:
            return await self._request('reservations/release', 'POST', {'workspace': workspace, 'attempt': attempt})
        except HTTPClientError as error:
            raw = getattr(error.response, 'body', None)
            if error.code != 409 or not isinstance(raw, (bytes, str)) or len(raw) > 8192:
                raise
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('Duplicate cleanup response field')
                    result[key] = value
                return result
            def reject_constant(value):
                raise ValueError('Nonfinite cleanup response value')
            try:
                body = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
            except (ValueError, UnicodeError):
                raise error from None
            detail = body.get('detail') if isinstance(body, dict) else None
            if (isinstance(body, dict) and set(body) == {'detail'}
                    and isinstance(detail, dict) and set(detail) == {'version', 'code', 'workspace', 'attempt'}
                    and type(detail['version']) is int and detail['version'] == 1
                    and detail['code'] == 'native_cleanup_pending'
                    and isinstance(attempt, str) and bool(attempt)
                    and detail['workspace'] == workspace and detail['attempt'] == attempt):
                raise NativeCleanupPending(workspace, attempt, response=error.response) from None
            raise

    async def validate_workspace(self, workspace, members, profile, ceiling, registration=None):
        payload = {'workspace': workspace, 'members': members, 'profile': profile, 'ceiling': ceiling}
        if registration is None:
            payload['principal'] = 'workspace:' + self.console + ':' + workspace
            path = 'workspace-policy/preview'
        else:
            # Provisioning updates the central document with verified storage.
            # Validate that current registration, including its exact Hub/Pod
            # binding, rather than the earlier PUT response or local draft.
            current = await self._request('workspaces/' + quote(workspace, safe=''))
            payload = {**current, 'profile': profile}
            path = 'workspace-policy'
        return await self._request(path, 'POST', payload)
