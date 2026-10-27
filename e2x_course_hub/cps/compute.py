"""Server-only fixed-destination policy client. No browser credential exposure."""
import json
from urllib.parse import quote, urlsplit, unquote
from tornado.httpclient import AsyncHTTPClient, HTTPRequest


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    def constant(value): raise ValueError('Non JSON constant')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)


def coordinator_base(value):
    try:
        if type(value) is not str or any(c.isspace() or ord(c)<32 or ord(c)==127 or c in '\\%' for c in value):
            raise ValueError()
        u=urlsplit(value)
        if (u.scheme!='https' or not u.hostname or u.username is not None or u.password is not None
            or u.query or u.fragment or u.geturl()!=value or u.netloc!=u.netloc.lower()
            or u.path.endswith('/') or '//' in u.path or any(p in ('.','..') for p in u.path.split('/'))
            or u.port is not None and not 0<u.port<65536): raise ValueError()
    except (ValueError,TypeError):
        raise ValueError('Canonical credential-free coordinator HTTPS base required') from None
    return value


async def private_json(url, token, *, method='GET', payload=None, factory=None, absent=False, paginated=False):
    """Own bounded transport, disable redirects, suppress secret-bearing failures."""
    client=None
    try:
        raw=None if payload is None else json.dumps(payload,allow_nan=False,separators=(',',':')).encode()
        if raw is not None and len(raw)>8*1024*1024:raise ValueError()
        body=bytearray()
        def receive(chunk):
            body.extend(chunk)
            if len(body)>8*1024*1024:raise ValueError()
        client=(factory or AsyncHTTPClient)(force_instance=True,max_buffer_size=8*1024*1024)
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}
        if paginated:
            headers['Accept']='application/jupyterhub-pagination+json'
        response=await client.fetch(HTTPRequest(url, method=method,
            headers=headers,body=raw,
            follow_redirects=False,connect_timeout=5,request_timeout=20,streaming_callback=receive),
            raise_error=False)
        if response.code==404 and absent:return None
        if response.code not in (200,201,202,204):raise ValueError()
        if response.body:
            receive(response.body)
        return strict_json(body) if body else None
    except Exception:
        raise ValueError('Private authenticated service request unavailable or invalid') from None
    finally:
        if client is not None:client.close()


class ComputePolicyClient:
    def __init__(self, base_url, token, console, *, http_client_factory=None):
        self.base_url=coordinator_base(base_url)
        if (console not in ('cps','cit') or type(token) is not str or not token
            or any(ord(c)<33 or ord(c)>126 for c in token)):
            raise ValueError('Console and private service credential required')
        self.token=token
        self.console=console
        self._http_client_factory=http_client_factory

    async def _request(self,path,method='GET',payload=None,*,absent=False):
        fixed={('profiles','GET'),('grants','PUT'),('workspaces','PUT'),('workspaces/provision','POST'),('reservations/release','POST'),
            ('workspace-policy/preview','POST'),('workspace-policy','POST'),
            ('native-pool-head/current','GET'),('native-pool-head/migration-permit','GET'),
            ('native-pool-head/prepare-write','POST'),('native-pool-head/complete-write','POST'),
            ('native-pool-head/hub-effects','POST'),('native-pool-head/hub-effects/read','POST')}
        dynamic=False
        if type(path) is str and method=='GET':
            pieces=path.split('/')
            if len(pieces)==2 and pieces[0] in ('grants','reservations','workspaces'):
                part=pieces[1]
                decoded=unquote(part,errors='strict')
                dynamic=bool(decoded) and decoded not in ('.','..') and quote(decoded,safe='')==part
        if not dynamic and (path,method) not in fixed:
            raise ValueError('Closed internal policy path required')
        if absent and not (dynamic and pieces[0]=='workspaces'):
            raise ValueError('Absence applies only to exact workspace lookup')
        return await private_json(self.base_url+'/internal/v1/'+path,self.token,
            method=method,payload=payload,factory=self._http_client_factory,absent=absent)

    async def grants(self, person):
        return await self._request('grants/' + quote(person, safe=''))

    async def put_grant(self, grant, *, actor):
        if grant.get('source', self.console) != self.console:
            raise PermissionError('console cannot mutate another console grant')
        payload = {key: value for key, value in grant.items() if key != 'source'}
        return await self._request('grants', 'PUT', payload)

    async def workspace_registration(self, workspace):
        return await self._request('workspaces/'+quote(workspace,safe=''),absent=True)

    async def provision_workspace(self, workspace):
        return await self._request('workspaces/provision','POST',{'workspace':workspace})

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
            'policy_hash': validation['policy_hash'],
            **({'group_id': workspace['group_id']} if 'group_id' in workspace else {}),
            **({'course_id':workspace['course_id'],'term_id':workspace['term_id']} if workspace.get('term_id') else {})})

    async def reservation_state(self, workspace):
        return await self._request("reservations/" + quote(workspace, safe=""))

    async def release_after_shutdown(self, workspace, *, confirmed_by_hub, actor, attempt=None):
        if confirmed_by_hub is not True:
            raise ValueError('Hub shutdown confirmation required')
        # Hub post-stop owns the persisted attempt. This idempotent probe cannot
        # force-release its reservation; the gateway observes actual Pod absence.
        return await self._request('reservations/release', 'POST', {'workspace': workspace, 'attempt': attempt})

    async def validate_workspace(self, workspace, members, profile, ceiling, registration=None):
        payload = {'workspace': workspace, 'members': members, 'profile': profile, 'ceiling': ceiling}
        if registration is None:
            payload['principal'] = 'workspace:' + self.console + ':' + workspace
            path = 'workspace-policy/preview'
        else:
            payload.update(principal=registration['principal'], policy_hash=registration['policy_hash'])
            path = 'workspace-policy'
        return await self._request(path, 'POST', payload)
