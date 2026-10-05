"""Service-controlled shared GPU workspace lifecycle and access reconciliation."""
import asyncio
import json
from urllib.parse import quote
from tornado.httpclient import HTTPClientError

class HubWorkspaceAdapter:
    def __init__(self, hub_api, *, timeout=120):
        self.hub = hub_api
        self.timeout = timeout

    def path(self, workspace, suffix):
        user = quote(workspace['hub_user'],safe='')
        server = quote(workspace['hub_server'],safe='')
        return self.hub.api_url.rstrip('/') + f'/users/{user}/servers/{server}' + suffix

    async def stop_confirmed(self, workspace):
        try:
            await self.hub.request(self.path(workspace,''), 'DELETE')
        except HTTPClientError as error:
            if error.code != 404: raise
        deadline = asyncio.get_running_loop().time() + self.timeout
        while True:
            user = await self.hub.get_user(workspace['hub_user'])
            if workspace['hub_server'] not in user.get('servers',{}): return True
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError('Hub shutdown not confirmed; GPU reservations retained')
            await asyncio.sleep(0.2)

    async def replace_shares(self, workspace, group):
        user = quote(workspace['hub_user'],safe='')
        server = quote(workspace['hub_server'],safe='')
        url = self.hub.api_url.rstrip('/') + f'/shares/{user}/{server}'
        # Revoke all old access while server is confirmed stopped; then grant group access.
        try: await self.hub.request(url,'DELETE')
        except HTTPClientError as error:
            if error.code != 404: raise
        await self.hub.request(url,'POST',json.dumps({'group':group,'scopes':['access:servers']}))

    async def sync_group(self, workspace, memberships):
        from ..api.errors import GroupNotFoundError
        group = workspace['group_id']
        usernames = sorted({m['person_id'] for m in memberships if m.get('group_id') == group})
        try: current = await self.hub.get_group(group)
        except GroupNotFoundError:
            await self.hub.create_group(group)
            current = {'users':[]}
        remove = sorted(set(current.get('users',[]))-set(usernames))
        if remove: await self.hub.remove_users_from_group(group,remove)
        if usernames: await self.hub.add_users_to_group(group,usernames)

    async def start(self, workspace):
        # Only the selected fixed policy profile enters spawn; no user Pod/resource overrides.
        await self.hub.request(self.path(workspace,''),'POST',json.dumps({'profile':workspace['profile']}))

class WorkspaceService:
    def __init__(self, provider, compute, hub):
        self.provider, self.compute, self.hub = provider,compute,hub
        self.locks = {}

    async def get(self, identifier):
        for record in await self.provider.list('workspaces'):
            if record['id'] == identifier: return record
        raise KeyError(identifier)

    async def members(self, workspace):
        records = await self.provider.members(workspace['course_id'])
        people = [r.get('canonical_person_id') for r in records if r.get('group_id') == workspace['group_id']]
        if not people or None in people:
            raise ValueError('explicit canonical person mappings required for every workspace member')
        return sorted(set(people))

    async def start(self, identifier, *, actor):
        async with self.locks.setdefault(identifier,asyncio.Lock()):
            workspace = await self.get(identifier)
            if workspace.get('archive_pending') or workspace.get('archived'):
                raise ValueError('Archived workspace cannot restart a writer')
            members = await self.members(workspace)
            await self.compute.validate_workspace(identifier,members,workspace['profile'],workspace['course_ceiling'])
            await self.compute.acquire(identifier,members,profile=workspace['profile'],ceiling=workspace['course_ceiling'],actor=actor)
            try:
                await self.hub.sync_group(workspace,await self.provider.members(workspace['course_id']))
                await self.hub.start(workspace)
                await self.hub.replace_shares(workspace,workspace['group_id'])
            except Exception:
                # A failed response can still leave a starting server; confirm shutdown first.
                await self.hub.stop_confirmed(workspace)
                await self.compute.release_after_shutdown(identifier,confirmed_by_hub=True,actor=actor)
                raise
            await self.provider.put('workspaces',{**workspace,'state':'running'},actor=actor)

    async def remove_member(self, identifier, membership_id, *, actor):
        async with self.locks.setdefault(identifier,asyncio.Lock()):
            workspace = await self.get(identifier)
            membership = next((r for r in await self.provider.members(workspace['course_id']) if r['id']==membership_id),None)
            if not membership or membership.get('group_id') != workspace['group_id']: raise ValueError('membership is not in this workspace')
            if membership.get('source') != 'local': raise PermissionError('external membership cannot be edited')
            await self.hub.stop_confirmed(workspace)
            # Close existing visitors by stopping the server before revoking group access.
            await self.hub.hub.remove_users_from_group(workspace['group_id'],[membership['person_id']])
            await self.hub.replace_shares(workspace,workspace['group_id'])
            await self.compute.release_after_shutdown(identifier,confirmed_by_hub=True,actor=actor)
            await self.provider.delete('memberships',membership_id,actor=actor)
            await self.provider.put('workspaces',{**workspace,'state':'stopped','notice':'Membership changed; kernel interrupted. Files retained. Profile must be revalidated before restart.'},actor=actor)

    async def close(self, identifier, *, actor, archive=False):
        async with self.locks.setdefault(identifier,asyncio.Lock()):
            workspace = await self.get(identifier)
            await self.hub.stop_confirmed(workspace)
            await self.compute.release_after_shutdown(identifier,confirmed_by_hub=True,actor=actor)
            # No filesystem permission claim: external archive operation must stop all other writers too.
            await self.provider.put('workspaces',{**workspace,'state':'stopped','archive_pending':archive},actor=actor)
