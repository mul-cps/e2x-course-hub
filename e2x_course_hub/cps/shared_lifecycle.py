"""Managed shared student removal: fresh Source authority and retained semantic intent."""
import json
from .authorization import checker,Permission
from .expiry import active,effective_groups

ACTION='shared-student-remove'
ADD_ACTION='shared-student-add'
SHARED_ACTIONS=frozenset((ACTION,ADD_ACTION))


class SharedLifecycleController:
    def __init__(self,controller):
        from .hub_permissions import HubPermissionController
        if type(controller) is not HubPermissionController:
            raise ValueError('Actual installed Source permission controller required')
        self.controller=controller

    async def source(self,visitor,workspace,membership_id):
        c=self.controller
        current=await c._actor(visitor)
        c._clean()
        records={kind:c._records(kind) for kind in ('courses','terms','groups','memberships','workspaces','assignments','groupings','projects')}
        target=next((w for w in records['workspaces'] if w['id']==workspace),None)
        if not target:raise PermissionError('Owned existing shared workspace required')
        course=next((r for r in records['courses'] if r['id']==target['course_id']),None)
        group=next((r for r in records['groups'] if r['id']==target['group_id']),None)
        if not course or not group or any(r.get('source')!='local' for r in (target,course,group)):
            raise PermissionError('Explicit owned local Source objects required')
        if not current['admin']:
            groups=effective_groups(c.provider,current['name'],current['groups'],c.legacy_roles)
            authority=checker(current['name'],groups,c.legacy_roles)
            if not authority.has_permission(Permission.EDIT_TERM,course_id=course['id'],term_id=target['term_id']):
                raise PermissionError('Current own-course instructor authority required')
        if group.get('role','student')!='student' or group.get('course_id')!=course['id'] or group.get('term_id')!=target['term_id']:
            raise PermissionError('Exact owned student group required')
        if not any(t.get('source')=='local' and t.get('course_id')==course['id']
            and target['term_id'] in (t['id'],t.get('term_id')) for t in records['terms']):
            raise PermissionError('Owned current course term required')
        if course.get('reconciliation_pending') or course.get('membership_removal_pending'):
            raise ValueError('Original course lifecycle must complete first')
        roster=[]
        for member in records['memberships']:
            if member.get('group_id')!=group['id']:continue
            if member.get('source')!='local' or member.get('course_id')!=course['id'] or member.get('term_id')!=target['term_id']:
                raise PermissionError('Exact local student membership required')
            if member.get('role','student')!='student':raise PermissionError('Teaching membership cannot use student lifecycle')
            if not active(member):raise ValueError('Expired roster requires its explicit reconciliation')
            link=c.provider.db.execute('SELECT canonical_person_id,verified FROM email_links WHERE console=? AND username=?',
                (c.binding.console,member['person_id'])).fetchone()
            if link is None or tuple(link)!=(member.get('canonical_person_id'),1):
                raise ValueError('Actual verified canonical student mapping required')
            roster.append(dict(membership_id=member['id'],username=member['person_id'],canonical_person_id=member['canonical_person_id']))
        roster.sort(key=lambda r:r['membership_id'])
        from cps_compute.native_pool_shared_effects import roster as validate_roster
        validate_roster(roster)
        if not any(r['membership_id']==membership_id for r in roster):raise PermissionError('Existing own student membership required')
        surviving=[r for r in roster if r['membership_id']!=membership_id]
        validate_roster(surviving)
        affected=sorted([w for w in records['workspaces'] if w.get('group_id')==group['id']],key=lambda w:w['id'])
        for writer in affected:
            if any(writer.get(k)!=target.get(k) for k in ('course_id','term_id')) or writer.get('source')!='local':
                raise PermissionError('Complete owned workspace census required')
            bindings=[b for b in course.get('workspace_bindings',[]) if all(b.get(k)==writer.get(k)
                for k in ('group_id','hub_user','hub_server'))]
            if len(bindings)!=1:raise ValueError('Exact operator-owned workspace binding required')
            if writer.get('archived') or writer.get('archive_pending'):raise ValueError('Original archive lifecycle must complete first')
        source=c.journal.capture_source()
        return current,source,dict(workspace=workspace,membership_id=membership_id,course=course['id'],
            term=target['term_id'],group=group['id'],roster_before=roster,roster_after=surviving),affected

    async def start(self,visitor,workspace):
        """Existing roster/share only; final installed Hub hooks own admission."""
        from urllib.parse import quote
        c=self.controller;current=await c._actor(visitor);c._clean()
        c.provider.managed_installation.check_source(c.provider)
        head=await c._rpc('current')
        if head.get('state')!='active':raise ValueError('Managed shared Start is fenced')
        target=next((w for w in c._records('workspaces') if w['id']==workspace),None)
        if target is None or target.get('source')!='local':raise PermissionError('Owned shared workspace required')
        course=next((r for r in c._records('courses') if r['id']==target['course_id']),None)
        group=next((r for r in c._records('groups') if r['id']==target['group_id']),None)
        if not course or not group or course.get('source')!='local' or group.get('source')!='local' or group.get('role')!='student':
            raise PermissionError('Existing genuine local student workspace required')
        if not current['admin']:
            groups=effective_groups(c.provider,current['name'],current['groups'],c.legacy_roles)
            if not checker(current['name'],groups,c.legacy_roles).has_permission(Permission.EDIT_TERM,
                    course_id=course['id'],term_id=target['term_id']):raise PermissionError('Current own-course Start authority required')
        if (target.get('archived') or target.get('archive_pending') or course.get('reconciliation_pending')
                or course.get('membership_removal_pending') or target.get('state')!='stopped'):
            raise ValueError('Original workspace lifecycle must complete first')
        assignment=target.get('assignment_id') or group.get('assignment_id')
        if assignment:
            row=next((a for a in c._records('assignments') if a['id']==assignment),None)
            if row is None or row.get('state','open')!='open' or row.get('archive_pending'):
                raise ValueError('Closed assignment cannot restart')
        for state,raw in c.provider.db.execute('SELECT state,record FROM hub_journal_operation'):
            record=json.loads(raw)
            if record['intent']['request']['action'] in SHARED_ACTIONS and state!='effective' and any(
                    w['workspace']==workspace for w in record['intent']['request']['writers']):
                raise ValueError('Original removal requires effective operator handoff')
        path='users/'+quote(target['hub_user'],safe='')+'/servers/'+quote(target['hub_server'],safe='')
        before=c.journal.capture_source()
        user=await c._hub('users/'+quote(target['hub_user'],safe=''))
        if target['hub_server'] in user.get('servers',{}):raise ValueError('Existing writer must be observed stopped')
        latest=await c._actor(visitor)
        if latest!=current or c.journal.capture_source()!=before:raise ValueError('Start authority changed')
        # This is the one exact ordinary Hub server API. Installed shared hooks
        # independently validate the current Source, roster, grants and final Pod.
        await c._hub(path,payload={'profile':target['profile']})
        return dict(state='start-requested',workspace=workspace)

    async def stop(self,visitor,workspace):
        # The ordinary authenticated Hub DELETE remains independent of runtime
        # SQL, via the installed UID-safe Spawner. This Source operation needs
        # its separate retained standalone audit transition before exposure.
        await self.controller._actor(visitor)
        raise ValueError('Managed standalone Stop audit transition is not installed; use the authorized Hub Stop')

    async def remove(self,visitor,workspace,membership_id):
        current,source,request,writers=await self.source(visitor,workspace,membership_id)
        # The next boundary must retain original writer observations before any
        # stop; neither a Hub absence nor a caller flag can supply that evidence.
        return await self._prepare(visitor,current,source,request,writers)

    async def add(self,visitor,workspace,membership_record):
        current,source,request,writers=await self.add_source(visitor,workspace,membership_record)
        added=next(row for row in request['roster_after'] if row['membership_id']==request['membership_id'])
        # `add_source` derives this from the verified directory row. Carry that
        # value into the journaled Source postimage even when the UI omitted it.
        normalized=dict(membership_record,canonical_person_id=added['canonical_person_id'])
        return await self._prepare(visitor,current,source,request,writers,membership_record=normalized)

    async def add_source(self,visitor,workspace,membership_record):
        """Build a one-member addition only from a fresh verified directory link."""
        import uuid
        from urllib.parse import quote
        c=self.controller;current=await c._actor(visitor);c._clean()
        installation=getattr(c.provider,'managed_installation',None)
        if installation is None:raise ValueError('Actual managed Source installation required')
        installation.check_source(c.provider)
        status=await c._rpc('current')
        if status.get('state')!='active':raise ValueError('Managed shared addition is fenced')
        records={kind:c._records(kind) for kind in ('courses','terms','groups','memberships','workspaces')}
        target=next((w for w in records['workspaces'] if w['id']==workspace),None)
        if not target or target.get('source')!='local':raise PermissionError('Owned existing local shared workspace required')
        course=next((r for r in records['courses'] if r['id']==target['course_id']),None)
        group=next((r for r in records['groups'] if r['id']==target['group_id']),None)
        if not course or not group or any(r.get('source')!='local' for r in (course,group)):
            raise PermissionError('Explicit owned local Source objects required')
        if not current['admin']:
            groups=effective_groups(c.provider,current['name'],current['groups'],c.legacy_roles)
            authority=checker(current['name'],groups,c.legacy_roles)
            if not authority.has_permission(Permission.EDIT_TERM,course_id=course['id'],term_id=target['term_id']):
                raise PermissionError('Current own-course instructor authority required')
        if group.get('role','student')!='student' or group.get('course_id')!=course['id'] or group.get('term_id')!=target['term_id']:
            raise PermissionError('Exact owned student group required')
        if not any(t.get('source')=='local' and t.get('course_id')==course['id']
            and target['term_id'] in (t['id'],t.get('term_id')) for t in records['terms']):
            raise PermissionError('Owned current course term required')
        if course.get('reconciliation_pending') or course.get('membership_removal_pending'):
            raise ValueError('Original course lifecycle must complete first')
        if target.get('archived') or target.get('archive_pending') or target.get('state')!='stopped':
            raise ValueError('Only stopped, unarchived shared workspaces may change roster')
        row=dict(membership_record)
        if (set(row)-{'id','course_id','term_id','group_id','person_id','canonical_person_id','role','assignment_id','starts','expires','source','external_id'}
                or row.get('source')!='local' or row.get('external_id') is not None
                or row.get('course_id')!=course['id'] or row.get('term_id')!=target['term_id']
                or row.get('group_id')!=group['id'] or row.get('role','student')!='student'):
            raise PermissionError('Exact new local student membership required')
        if any(r['id']==row.get('id') for r in records['memberships']):raise ValueError('Membership identifier already exists')
        if not active(row):raise ValueError('New shared membership must be currently active')
        link=c.provider.db.execute('SELECT canonical_person_id,verified FROM email_links WHERE console=? AND username=?',
            (c.binding.console,row.get('person_id'))).fetchone()
        if link is None or link['verified']!=1 or not link['canonical_person_id']:
            raise ValueError('Actual verified canonical directory mapping required')
        try:canonical=str(uuid.UUID(link['canonical_person_id']))
        except (ValueError,TypeError,AttributeError):raise ValueError('Verified canonical person UUID required') from None
        if canonical!=link['canonical_person_id'] or row.get('canonical_person_id') not in (None,canonical):
            raise ValueError('Membership identity must match the verified directory mapping')
        row['canonical_person_id']=canonical
        roster=[]
        for member in records['memberships']:
            if member.get('group_id')!=group['id']:continue
            if member.get('source')!='local' or member.get('course_id')!=course['id'] or member.get('term_id')!=target['term_id']:
                raise PermissionError('Exact local student membership required')
            if member.get('role','student')!='student':raise PermissionError('Teaching membership cannot use student lifecycle')
            if not active(member):raise ValueError('Expired roster requires explicit reconciliation')
            mapping=c.provider.db.execute('SELECT canonical_person_id,verified FROM email_links WHERE console=? AND username=?',
                (c.binding.console,member['person_id'])).fetchone()
            if mapping is None or tuple(mapping)!=(member.get('canonical_person_id'),1):
                raise ValueError('Actual verified canonical student mapping required')
            roster.append(dict(membership_id=member['id'],username=member['person_id'],canonical_person_id=member['canonical_person_id']))
        roster.sort(key=lambda r:r['membership_id'])
        from cps_compute.native_pool_shared_effects import roster as validate_roster
        validate_roster(roster)
        candidate=dict(membership_id=row['id'],username=row['person_id'],canonical_person_id=canonical)
        validate_roster([*roster,candidate])
        affected=sorted((w for w in records['workspaces'] if w.get('group_id')==group['id']),key=lambda w:w['id'])
        if not affected or any(w.get('state')!='stopped' or w.get('archived') or w.get('archive_pending')
                or any(w.get(k)!=target.get(k) for k in ('course_id','term_id')) or w.get('source')!='local' for w in affected):
            raise ValueError('Complete stopped owned group workspace census required')
        # Hub usernames must already resolve to real accounts. A membership row
        # alone never manufactures a Hub user or a direct Share.
        account=await c._hub('users/'+quote(row['person_id'],safe=''))
        if type(account) is not dict or account.get('name')!=row['person_id']:
            raise ValueError('Existing Hub student account required before shared access')
        source=c.journal.capture_source()
        return current,source,dict(action=ADD_ACTION,workspace=workspace,membership_id=row['id'],course=course['id'],
            term=target['term_id'],group=group['id'],roster_before=roster,
            roster_after=sorted([*roster,candidate],key=lambda r:r['membership_id'])),affected

    async def _fresh(self,visitor,record):
        c=self.controller;request=record['intent']['request'];current=await c._actor(visitor)
        if current['name']!=record['actor'] and not current['admin']:raise PermissionError('Original actor or actual administrator required')
        if not current['admin']:
            groups=effective_groups(c.provider,current['name'],current['groups'],c.legacy_roles)
            if not checker(current['name'],groups,c.legacy_roles).has_permission(Permission.EDIT_TERM,
                course_id=request['course'],term_id=request['term']):raise PermissionError('Current own-course authority required')
        from cps_compute.native_pool_shared_effects import PHASES,source_postimage
        expected=source_postimage(record['source_preimage'],request) if PHASES.index(record['state'])>=PHASES.index('source-completed') else record['source_preimage']
        if c.journal.capture_source()!=expected:raise ValueError('Original shared Source census changed')

    async def _prepare(self,visitor,current,source,request,writers,*,prepare_only=False,membership_record=None):
        from .pool_heads import digest
        c=self.controller
        if not prepare_only and c.provider.db.execute('PRAGMA user_version').fetchone()[0]!=7:
            raise ValueError('Explicit operator generation7 maintenance required before shared actions')
        observed=await c._hub('cps/shared-lifecycle/preimage',payload={'workspace':request['workspace']})
        if type(observed) is not dict or set(observed)!={'writers','before'}:raise ValueError('Original installed writer observation required')
        if [w['workspace'] for w in observed['writers']]!=[w['id'] for w in writers]:raise ValueError('Complete original Source/Hub writer census required')
        request=dict(request,action=request.get('action',ACTION),writers=observed['writers'],source_changes=[])
        if request['action']==ADD_ACTION:
            if type(membership_record) is not dict or membership_record.get('id')!=request['membership_id']:
                raise ValueError('Exact normalized new membership row required')
            request['source_changes'].append(dict(kind='memberships',id=request['membership_id'],before=None,
                after=json.dumps(membership_record,sort_keys=True)))
        for console,kind,identifier,raw in source['records']['rows']:
            original=json.loads(raw);after=None
            if kind=='memberships' and identifier==request['membership_id']:
                if request['action']==ACTION:pass
                else:continue
            elif kind=='workspaces' and identifier in {w['id'] for w in writers}:
                if request['action']==ACTION:
                    after=dict(original,state='stopped',notice='Membership changed; kernel interrupted. Files retained. Profile must be revalidated before restart.')
                else:continue
            elif kind=='groups' and identifier==request['group'] and 'members' in original:
                after=dict(original,members=[r['username'] for r in request['roster_after']])
            else:continue
            request['source_changes'].append(dict(kind=kind,id=identifier,before=raw,after=json.dumps(after,sort_keys=True) if after is not None else None))
        request['source_changes'].sort(key=lambda r:(r['kind'],r['id']))
        intent=dict(console=c.binding.console,actor=current['name'],hub_api=c.binding.hub_api,
            source_revision=digest(source),request=request,before=observed['before'])
        from cps_compute.native_pool_hub_effects import validate_hub_intent
        intent=validate_hub_intent(intent)
        # Read-only preparation is reauthorized after every external preimage call.
        if request['action']==ADD_ACTION:
            _,latest,_,_=await self.add_source(visitor,request['workspace'],membership_record)
        else:
            _,latest,_,_=await self.source(visitor,request['workspace'],request['membership_id'])
        if latest!=source:raise ValueError('Original Source changed during shared preparation')
        if prepare_only:return intent
        ticket=await c.guard.prepare('admin.hub',intent)
        record=c.journal.prepare(operation_id=ticket['transition_id'],input_sha256=digest(dict(operation='admin.hub',payload=intent)),
            intent=intent,source_preimage=source,hub_preimage=observed['before'],actor=current['name'],ticket=ticket)
        return await self.run(visitor,record)

    async def _stop_writer(self,record,writer):
        """Only the original armed, server-derived writer; no legacy audit writer."""
        import asyncio
        from urllib.parse import quote
        from .compute import private_json
        from .shared_journal import stop_event
        c=self.controller;op=record['operation_id'];ih=record['input_sha256']
        event=next(e for e in record['lifecycle']['stops'] if e['writer']==writer)
        if event['outcome']=='hub-stopped':return
        observed=await c._hub('cps/shared-lifecycle/stop-binding',payload={'effect':record['effect']})
        if observed!=dict(state='original-binding',writers=[w['spawn'] for w in record['intent']['request']['writers']]):
            raise ValueError('Exact current original shared Stop bindings required')
        if record['intent']['request']['action']==ADD_ACTION:
            # Addition requires an already-stopped workspace. Verify the exact
            # Hub server postimage and record it without issuing another DELETE.
            user=await c._hub('users/'+quote(writer['owner'],safe='')+'?include_stopped_servers=true')
            server=user.get('servers',{}).get(writer['descriptor']['server']) if type(user) is dict else None
            if (type(server) is not dict or server.get('name')!=writer['descriptor']['server']
                    or server.get('stopped') is not True or server.get('ready') is not False
                    or server.get('pending') is not None or server.get('started') is not None):
                raise ValueError('Actual original Hub server must already be stopped before roster addition')
            after=dict(name=user['name'],server={key:server[key] for key in
                ('name','full_name','stopped','ready','pending','started')})
            stop_event(c.journal,op,ih,writer,after=after)
            return
        c._clean()
        if c.hub_api.api_url!=c.binding.hub_api:raise ValueError('Pinned Hub origin changed')
        path='users/'+quote(writer['owner'],safe='')+'/servers/'+quote(writer['descriptor']['server'],safe='')
        # UID-bound termination is independently performed by the installed Hub
        # observer. This ordinary endpoint also clears the original Hub spawner.
        await private_json(c.binding.hub_api+'/'+path,c.hub_api.api_token,method='DELETE',
            factory=c._factory,absent=True)
        deadline=asyncio.get_running_loop().time()+120
        while True:
            observed=await c._hub('users/'+quote(writer['owner'],safe='')+'?include_stopped_servers=true')
            server=observed.get('servers',{}).get(writer['descriptor']['server']) if type(observed) is dict else None
            if type(server) is not dict:raise ValueError('Exact scoped original Hub server model required')
            if server.get('stopped') is True and server.get('ready') is False and server.get('pending') is None and server.get('started') is None:
                after=dict(name=observed['name'],server={key:server[key] for key in ('name','full_name','stopped','ready','pending','started')})
                stop_event(c.journal,op,ih,writer,after=after);return
            if asyncio.get_running_loop().time()>=deadline:raise ValueError('Original Hub stop unconfirmed; retain custody')
            await asyncio.sleep(.2)

    async def run(self,visitor,record):
        from .pool_heads import digest
        from .shared_journal import advance
        c=self.controller;op,ih=record['operation_id'],record['input_sha256']
        if record['state']=='effective':
            # Historical receipt never mutates or rewinds a later roster.
            current=await c._actor(visitor)
            if current['name']!=record['actor'] and not current['admin']:raise PermissionError('Original visitor or administrator required')
            return record
        await self._fresh(visitor,record)
        status=await c._rpc('current')
        if record['state']=='source-completed' and status.get('state')=='active' and status.get('transition_id')==op:
            return advance(c.journal,op,ih,'effective',finalized=json.loads(status['transition']['receipt']) if isinstance(status['transition']['receipt'],str) else status['transition']['receipt'])
        if record['state']=='prepared':
            row=status.get('transition',{})
            c._head(status,operation_id=op,input_sha256=ih,effect_sha256=row.get('effect_sha256'),ticket=record['ticket'])
            effect=c._effect(await c._rpc('hub-effects/read' if row.get('effect_sha256') else 'hub-effects','POST',
                {'transition_id':op,'intent':record['intent']}),record)
            record=c.journal.arm(op,ih,effect=effect,effect_sha256=digest(effect))
        else:
            c._head(status,operation_id=op,input_sha256=ih,effect_sha256=record['effect_sha256'],
                allow_completed=record['state']=='source-completed',ticket=record['ticket'])
        if record['state']=='armed':
            await self._fresh(visitor,record)
            from .shared_journal import stop_event
            for writer in record['intent']['request']['writers']:
                record=stop_event(c.journal,op,ih,writer)
            # A held runtime SQL successor cannot prevent independent ordinary
            # authorized Hub termination. No permission mutation follows a hold.
            try:
                await c._hub('cps/shared-lifecycle/stop',payload={'effect':record['effect']})
            finally:
                failures=[]
                for writer in record['intent']['request']['writers']:
                    try:await self._stop_writer(record,writer)
                    except Exception as error:failures.append(error)
                if failures:raise failures[0]
            observed=await c._hub('cps/shared-lifecycle/stopped',payload={'effect':record['effect']})
            record=advance(c.journal,op,ih,'stopped',stopped=observed['stopped'])
        if record['state']=='stopped':
            await self._fresh(visitor,record)
            receipt=await c._hub('cps/permission-operations/'+op,payload={'effect':record['effect']})
            record=advance(c.journal,op,ih,'hub-completed',receipt=receipt)
        if record['state']=='hub-completed':
            await self._fresh(visitor,record)
            result=await c._hub('cps/shared-lifecycle/control',payload={'effect':record['effect']})
            record=advance(c.journal,op,ih,'control-completed',control=result['control'])
        if record['state']=='control-completed':
            await self._fresh(visitor,record)
            record=advance(c.journal,op,ih,'source-completed')
        if record['state']=='source-completed':await c._finish(record)
        return record
