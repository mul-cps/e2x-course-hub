"""Service-controlled shared GPU workspace lifecycle and access reconciliation."""
import asyncio
import json
from functools import wraps
from contextlib import AsyncExitStack
from urllib.parse import quote
from tornado.httpclient import HTTPClientError

def audited_lifecycle(method):
    @wraps(method)
    async def wrapped(self, identifier, *args, actor, **kwargs):
        try:
            return await method(self,identifier,*args,actor=actor,**kwargs)
        except Exception:
            if hasattr(self.provider,'denial'):
                target = identifier.get('id','') if isinstance(identifier,dict) else identifier
                self.provider.denial(actor,'workspace-lifecycle',target,{'operation':method.__name__},'failure')
            raise
    return wrapped

def course_serialized(method):
    @wraps(method)
    async def wrapped(self, identifier, *args, **kwargs):
        if isinstance(identifier,dict):
            course_id = identifier.get('course_id') or identifier.get('course',{}).get('id')
        else:
            course_id = (await self.get(identifier))['course_id']
        if not course_id:
            raise ValueError('Owned course required for lifecycle serialization')
        async with self.course_locks.setdefault(course_id,asyncio.Lock()):
            return await method(self,identifier,*args,**kwargs)
    return wrapped

class HubWorkspaceAdapter:
    def __init__(self, hub_api, *, timeout=120):
        self.hub = hub_api
        self.timeout = timeout

    def path(self, workspace, suffix):
        user = quote(workspace['hub_user'],safe='')
        server = quote(workspace['hub_server'],safe='')
        return self.hub.api_url.rstrip('/') + f'/users/{user}/servers/{server}' + suffix

    async def assert_stopped(self, workspace):
        user = await self.hub.get_user(workspace['hub_user'])
        if workspace['hub_server'] in user.get('servers',{}):
            raise ValueError('Stop existing server before workspace policy/access reconciliation')

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

    async def revoke_shares(self, workspace):
        user = quote(workspace['hub_user'],safe='')
        server = quote(workspace['hub_server'],safe='')
        try:
            await self.hub.request(self.hub.api_url.rstrip('/') + f'/shares/{user}/{server}', 'DELETE')
        except HTTPClientError as error:
            if error.code != 404: raise

    async def sync_group(self, workspace, memberships):
        from ..api.errors import GroupNotFoundError
        group = workspace['group_id']
        from .expiry import active
        usernames = sorted({m['person_id'] for m in memberships if m.get('group_id') == group and active(m)})
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

class UnconfiguredFilesystemAdapter:
    async def provision(self, workspace, *, actor):
        raise RuntimeError("Workspace filesystem provisioning adapter is not qualified")

    async def archive(self, workspaces, *, actor, assignment=None):
        raise RuntimeError('Read-only filesystem archive adapter is not qualified')


class WorkspaceService:
    def __init__(self, provider, compute, hub, *, filesystem=None):
        self.provider, self.compute, self.hub = provider,compute,hub
        self.locks = {}
        self.course_locks = {}
        self.filesystem = filesystem or UnconfiguredFilesystemAdapter()

    async def get(self, identifier):
        for record in await self.provider.list('workspaces'):
            if record['id'] == identifier: return record
        raise KeyError(identifier)

    async def members(self, workspace):
        from .expiry import active
        records = [r for r in await self.provider.members(workspace['course_id']) if active(r)]
        people = [r.get('canonical_person_id') for r in records if r.get('group_id') == workspace['group_id']]
        if hasattr(self.provider,'db'):
            for record in records:
                if record.get('group_id') != workspace['group_id']: continue
                linked = self.provider.db.execute('SELECT canonical_person_id,verified FROM email_links WHERE console=? AND username=?',
                    (self.provider.console,record['person_id'])).fetchone()
                if not linked or linked['verified'] != 1 or linked['canonical_person_id'] != record.get('canonical_person_id'):
                    raise ValueError('Email verification required before workspace member linkage')
        if not people or None in people:
            raise ValueError('explicit canonical person mappings required for every workspace member')
        return sorted(set(people))

    @audited_lifecycle
    @course_serialized
    async def create(self, data, *, actor):
        # Re-read authoritative records under the same lock used by starts/reconcile.
        course = next((c for c in await self.provider.courses() if c['id']==data['course_id']),None)
        if not course or course.get('reconciliation_pending'):
            raise ValueError('Course is missing or has an unresolved reconciliation barrier')
        if any(w['id']==data['id'] for w in await self.provider.list('workspaces')):
            raise ValueError('Workspace already exists')
        bindings = [b for b in course.get('workspace_bindings',[]) if
                    all(b.get(k)==data.get(k) for k in ('group_id','hub_user','hub_server'))]
        if len(bindings)!=1 or course.get('resource_ceiling') != data.get('course_ceiling'):
            raise PermissionError('Exact administrator-controlled workspace binding and ceiling required')
        group = next((g for g in await self.provider.groups(data['course_id']) if g['id']==data['group_id']),None)
        if group is None:
            raise ValueError('Owned group required')
        if group.get('assignment_id'):
            assignment=next((a for a in await self.provider.list('assignments') if a['id']==group['assignment_id']),None)
            if not assignment or assignment.get('state','open')!='open' or assignment.get('archive_pending'):
                raise ValueError('Assignment is closing or closed')
        members = await self.members(data)
        await self.compute.validate_workspace(data['id'],members,data['profile'],data['course_ceiling'])
        return await self.provider.put('workspaces',{**data,'state':'stopped'},actor=actor)

    @audited_lifecycle
    @course_serialized
    async def start(self, identifier, *, actor):
        async with self.locks.setdefault(identifier,asyncio.Lock()):
            workspace = await self.get(identifier)
            if workspace.get('state','stopped') != 'stopped':
                raise ValueError('Workspace lifecycle is unresolved; observed Stop/reconciliation required')
            if workspace.get('archive_pending') or workspace.get('archived'):
                raise ValueError('Archived workspace cannot restart a writer')
            group=next((g for g in await self.provider.groups(workspace['course_id']) if g['id']==workspace['group_id']),{})
            assignment_id=workspace.get('assignment_id') or group.get('assignment_id')
            if assignment_id:
                assignment=next((a for a in await self.provider.list('assignments') if a['id']==assignment_id),None)
                if not assignment or assignment.get('state','open') != 'open' or assignment.get('archive_pending'):
                    raise ValueError('Closed assignment cannot restart a writer')
            course=next((c for c in await self.provider.courses() if c['id']==workspace['course_id']),None)
            if course and course.get('reconciliation_pending'):
                raise ValueError('Course reconciliation barrier is unresolved; reconcile every writer before restart')
            siblings = [w for w in await self.provider.list('workspaces')
                        if w.get('course_id') == workspace['course_id'] and w.get('group_id') == workspace['group_id']]
            if any(w.get('state') in ('reconciling','starting') for w in siblings):
                raise ValueError('Shared group has an unresolved lifecycle')
            if not course or course.get('resource_ceiling') is None:
                raise ValueError('Administrator-controlled course resource ceiling required')
            bindings = [b for b in course.get('workspace_bindings', [])
                        if all(b.get(k) == workspace.get(k) for k in ('group_id','hub_user','hub_server'))]
            if len(bindings) != 1:
                raise ValueError('Exact operator-owned workspace binding required')
            workspace={**workspace,'course_ceiling':course['resource_ceiling'],
                       'namespace':bindings[0].get('namespace'), 'pod':bindings[0].get('pod')}
            await self.hub.assert_stopped(workspace)
            # Persist the guard before asynchronous provisioning/registration/Hub calls.
            await self.provider.put('workspaces',{**workspace,'state':'starting'},actor=actor)
            await self.filesystem.provision(workspace,actor=actor)
            members = await self.members(workspace)
            validation = await self.compute.validate_workspace(identifier,members,workspace['profile'],workspace['course_ceiling'])
            await self.compute.register_workspace(workspace,members,validation)
            try:
                await self.hub.sync_group(workspace,await self.provider.members(workspace['course_id']))
                await self.hub.start(workspace)
                await self.hub.replace_shares(workspace,workspace['group_id'])
            except Exception:
                # A failed response can still leave a starting server; confirm shutdown first.
                reservation = await self.compute.reservation_state(identifier)
                await self.hub.stop_confirmed(workspace)
                await self.compute.release_after_shutdown(identifier,confirmed_by_hub=True,actor=actor,attempt=reservation.get("attempt"))
                await self.provider.put('workspaces',{**workspace,'state':'stopped'},actor=actor)
                raise
            await self.provider.put('workspaces',{**workspace,'state':'running'},actor=actor)

    @audited_lifecycle
    @course_serialized
    async def remove_member(self, identifier, membership_id, *, actor):
        target = await self.get(identifier)
        affected = sorted([w for w in await self.provider.list('workspaces')
                           if w.get('course_id') == target['course_id'] and
                           w.get('group_id') == target['group_id']],key=lambda w:w['id'])
        async with AsyncExitStack() as stack:
            for workspace in affected:
                await stack.enter_async_context(self.locks.setdefault(workspace['id'],asyncio.Lock()))
            membership = next((r for r in await self.provider.members(target['course_id']) if r['id']==membership_id),None)
            if not membership or membership.get('group_id') != target['group_id']:
                raise ValueError('membership is not in this workspace')
            if membership.get('source') != 'local':
                raise PermissionError('external membership cannot be edited')
            course = next(c for c in await self.provider.courses() if c['id']==target['course_id'])
            if course.get('reconciliation_pending') and course.get('membership_removal_pending') != membership_id:
                raise ValueError('Course reconciliation barrier requires its original operation to finish')
            # Persist the guard before external mutations. A failed Shares response
            # must not let Start restore the still-persisted membership, even after Stop.
            await self.provider.put('courses',{**course,'reconciliation_pending':True,
                'membership_removal_pending':membership_id},actor=actor)
            affected = [await self.get(w['id']) for w in affected]
            for workspace in affected:
                await self.provider.put('workspaces',{**workspace,'state':'reconciling'},actor=actor)
            for workspace in affected:
                reservation = await self.compute.reservation_state(workspace['id'])
                await self.hub.stop_confirmed(workspace)
                await self.compute.release_after_shutdown(workspace['id'],confirmed_by_hub=True,
                                                         actor=actor,attempt=reservation.get('attempt'))
            await self.hub.hub.remove_users_from_group(target['group_id'],[membership['person_id']])
            for workspace in affected:
                await self.hub.replace_shares(workspace,target['group_id'])
            await self.provider.delete('memberships',membership_id,actor=actor)
            for workspace in affected:
                current = await self.get(workspace['id'])
                await self.provider.put('workspaces',{**current,'state':'stopped',
                    'notice':'Membership changed; kernel interrupted. Files retained. Profile must be revalidated before restart.'},actor=actor)
            current_course = next(c for c in await self.provider.courses() if c['id']==target['course_id'])
            await self.provider.put('courses',{**current_course,'reconciliation_pending':False,
                'membership_removal_pending':None},actor=actor)

    @audited_lifecycle
    async def close(self, identifier, *, actor, archive=False):
        async with self.locks.setdefault(identifier,asyncio.Lock()):
            workspace = await self.get(identifier)
            reservation = await self.compute.reservation_state(identifier)
            await self.hub.stop_confirmed(workspace)
            await self.compute.release_after_shutdown(identifier,confirmed_by_hub=True,actor=actor,attempt=reservation.get("attempt"))
            # No filesystem permission claim: external archive operation must stop all other writers too.
            await self.hub.revoke_shares(workspace)
            await self.provider.put('workspaces',{**workspace,'state':'stopped','archive_pending':archive or workspace.get('archive_pending',False)},actor=actor)

    @audited_lifecycle
    @course_serialized
    async def close_assignment(self, assignment, *, actor):
        await self.provider.put('assignments',{**assignment,'archive_pending':True},actor=actor)
        groups = {g['id'] for g in await self.provider.groups(assignment['course_id'])
                  if g.get('assignment_id') == assignment['id']}
        workspaces = [w for w in await self.provider.list('workspaces')
                      if w.get('course_id') == assignment['course_id'] and
                      (w.get('group_id') in groups or w.get('assignment_id') == assignment['id'])]
        # Block restarts before stopping any writer. Retry remains safe after failures.
        for workspace in workspaces:
            async with self.locks.setdefault(workspace['id'],asyncio.Lock()):
                current = await self.get(workspace['id'])
                await self.provider.put('workspaces',{**current,'archive_pending':True},actor=actor)
        for workspace in workspaces:
            await self.close(workspace['id'],actor=actor,archive=True)
        await self.provider.put('assignments',{**assignment,'state':'closed','archive_pending':True},actor=actor)
        try:
            if not workspaces:
                raise RuntimeError('No qualified workspace storage targets; assignment archive requires operator reconciliation')
            evidence = await self.filesystem.archive(workspaces,actor=actor,assignment=assignment)
            if not isinstance(evidence,dict) or evidence.get('read_only_verified') is not True or not evidence.get('evidence'):
                raise RuntimeError('Filesystem adapter must verify read-only archive and return evidence')
        except Exception:
            if hasattr(self.provider,'denial'):
                self.provider.denial(actor,'assignments',assignment['id'],{'operation':'filesystem-archive'},'failure')
            raise
        for workspace in workspaces:
            await self.provider.put('workspaces',{**workspace,'state':'stopped','archived':True,
                                     'archive_pending':False,'archive_evidence':evidence},actor=actor)
        return await self.provider.put('assignments',{**assignment,'state':'closed','archived':True,
                                      'archive_pending':False,'archive_evidence':evidence},actor=actor)

    @audited_lifecycle
    @course_serialized
    async def reconcile_snapshot(self, snapshot, *, actor):
        """The same normalized provider sink, with no external provider writes."""
        course = snapshot['course']
        groups = {group['id']: group for group in snapshot['groups']}
        # Course serialization excludes every controlled create/start while collecting
        # writers. Persist the barrier before waiting for any workspace lock.
        configured_course = next((c for c in await self.provider.courses() if c['id']==course['id']),None)
        if configured_course is None:
            raise ValueError('Owned course required for reconciliation barrier')
        pending_removal = configured_course.get('membership_removal_pending')
        if pending_removal and any(m['id']==pending_removal for m in await self.provider.members(course['id'])):
            raise ValueError('Membership removal barrier requires the original removal to finish')
        await self.provider.put('courses',{**configured_course,'reconciliation_pending':True},actor=actor)
        workspaces = sorted([w for w in await self.provider.list('workspaces')
                             if w.get('course_id') == course['id']],key=lambda w:w['id'])
        async with AsyncExitStack() as stack:
            for workspace in workspaces:
                await stack.enter_async_context(self.locks.setdefault(workspace['id'],asyncio.Lock()))
            # Direct Stop may update state while we wait; refresh under all locks.
            workspaces = [await self.get(w['id']) for w in workspaces]
            # Persist mutation guards across every awaited stop and group update.
            for workspace in workspaces:
                await self.provider.put('workspaces',{**workspace,'state':'reconciling'},actor=actor)
            # Stop every associated writer before touching any shared group access.
            for workspace in workspaces:
                reservation = await self.compute.reservation_state(workspace['id'])
                await self.hub.stop_confirmed(workspace)
                await self.compute.release_after_shutdown(workspace['id'],confirmed_by_hub=True,
                                                         actor=actor,attempt=reservation.get('attempt'))
            for group_id in sorted({w['group_id'] for w in workspaces}):
                grouped = [w for w in workspaces if w['group_id']==group_id]
                eligible = [w for w in grouped if group_id in groups and
                            not w.get('archive_pending') and not w.get('archived')]
                for workspace in grouped:
                    await self.hub.revoke_shares(workspace)
                if not eligible:
                    continue
                from .expiry import active
                members = [m for m in snapshot['members'] if m.get('group_id')==group_id and active(m)]
                people = sorted({m.get('canonical_person_id') for m in members if m.get('canonical_person_id')})
                if not people or any(not m.get('canonical_person_id') for m in members):
                    raise ValueError('Explicit verified canonical members required for reconciliation')
                configured = next((c for c in await self.provider.courses() if c['id']==course['id']),None)
                for workspace in eligible:
                    bindings = [b for b in (configured or {}).get('workspace_bindings',[])
                                if all(b.get(k)==workspace.get(k) for k in ('group_id','hub_user','hub_server'))]
                    if len(bindings)!=1 or configured.get('resource_ceiling') is None:
                        raise ValueError('Operator-qualified course binding required for reconciliation')
                    registered={**workspace,'namespace':bindings[0].get('namespace'),
                                'pod':bindings[0].get('pod'),'course_ceiling':configured['resource_ceiling']}
                    validation = await self.compute.validate_workspace(workspace['id'],people,
                                        workspace['profile'],registered['course_ceiling'])
                    await self.compute.register_workspace(registered,people,validation)
                await self.hub.sync_group(eligible[0],members)
                for workspace in eligible:
                    await self.hub.replace_shares(workspace,group_id)
            for workspace in workspaces:
                await self.provider.put('workspaces',{**workspace,'state':'stopped'},actor=actor)
            current_course = next(c for c in await self.provider.courses() if c['id']==course['id'])
            await self.provider.put('courses',{**current_course,'reconciliation_pending':False,
                'membership_removal_pending':None},actor=actor)
