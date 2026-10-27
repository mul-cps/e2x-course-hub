"""Source-authorized ordinary groups. No migration, roster or shared lifecycle writer."""
from dataclasses import asdict, dataclass
import json
import sys
from urllib.parse import quote
import uuid

from .compute import private_json
from .pool_heads import PoolMutationRPCGuard, digest

ACTIONS = frozenset(('group-create','group-users-add','group-users-remove','group-delete'))


def _sdk():
    if sys.version_info < (3,11):
        raise ValueError('Managed Hub permissions require Python 3.11 and the compute extra')
    try:
        from cps_compute import native_pool_hub_effects
    except ImportError:
        raise ValueError('Managed Hub permissions require the pinned compute extra') from None
    return native_pool_hub_effects


@dataclass(frozen=True)
class SourceJournalBinding:
    console: str
    source_path: str
    hub_api: str
    coordinator_url: str
    coordinator_database_id: str
    installation_id: str
    writer_actor: str
    writer_instance: str


def validate_bindings(courses, groups, terms, legacy_roles=None):
    """Validate every persisted binding; ownership is explicit, never a prefix."""
    codec = _sdk()
    by_id = {g['id']:g for g in groups}
    seen_groups, seen_names, bindings = set(),set(),{}
    for course in courses:
        rows = course.get('hub_group_bindings',[])
        if type(rows) is not list:raise ValueError('Closed Hub group bindings required')
        for row in rows:
            if type(row) is not dict or set(row)!={'group_id','hub_group','role'}:
                raise ValueError('Closed Hub group binding required')
            codec.name(row['group_id']);codec.name(row['hub_group'])
            group = by_id.get(row['group_id'])
            from e2x_hub_rbac import Role,RoleAssignment
            from .authorization import canonical_group
            semantic=RoleAssignment.from_group_name(canonical_group(row['hub_group'],legacy_roles))
            if row['role']=='student' and semantic is not None and (semantic.role is not Role.STUDENT
                or not group or semantic.course_id!=course['id'] or semantic.term_id!=group.get('term_id')):
                raise ValueError('Teaching or owner RBAC group cannot be a student binding')
            if (row['role'] not in ('student','teaching') or row['group_id'] in seen_groups
                or row['hub_group'] in seen_names or not group or group.get('source')!='local'
                or course.get('source')!='local' or group.get('course_id')!=course['id']):
                raise ValueError('Unique owned local Hub group binding required')
            term=group.get('term_id')
            if term is not None and not any(t.get('course_id')==course['id'] and t.get('source')=='local'
                    and term in (t['id'],t.get('term_id')) for t in terms):
                raise ValueError('Owned local term required')
            seen_groups.add(row['group_id']);seen_names.add(row['hub_group'])
            bindings[row['group_id']] = (course,group,row)
    return bindings


class HubPermissionController:
    """Internal API accepts the authenticated handler User; its claims are refreshed.

    Journal and coordinator/Hub observations are separate custody domains. Every
    network call checks that Source SQL is clean. No Source transaction spans an
    await. Recovery never rebuilds a retained request from current Hub membership.
    """
    def __init__(self, provider, guard, binding, hub_api, *, http_client_factory=None,
                 legacy_roles=None):
        from .providers import LocalCourseProvider
        from ..api.hub_api import HubAPI
        from .hub_journal import SourceHubJournal
        _sdk()
        if (type(provider) is not LocalCourseProvider or type(guard) is not PoolMutationRPCGuard
            or type(binding) is not SourceJournalBinding or not isinstance(hub_api,HubAPI)
            or guard.binding!=(binding.console,binding.coordinator_url,binding.coordinator_database_id)
            or provider.pool_guard is not guard or hub_api.api_url!=binding.hub_api):
            raise ValueError('Exact Source, coordinator and pinned Hub required')
        self.provider,self.guard,self.binding = provider,guard,binding
        self.hub_api=hub_api
        self._factory=http_client_factory
        self.legacy_roles=legacy_roles
        self.journal=SourceHubJournal(provider,**asdict(binding))

    def _clean(self):
        if self.provider.db.in_transaction:
            raise ValueError('Source connection must be clean before network I/O')

    def _records(self,kind):
        return [json.loads(r[0]) for r in self.provider.db.execute(
            'SELECT payload FROM records WHERE console=? AND kind=? ORDER BY id',
            (self.binding.console,kind))]

    async def _rpc(self,path,method='GET',payload=None):
        self._clean()
        b=self.binding
        if (self.guard.binding!=(b.console,b.coordinator_url,b.coordinator_database_id)
            or self.guard.client.base_url!=b.coordinator_url or self.guard.client.console!=b.console
            or self.guard.database_id!=b.coordinator_database_id):
            raise ValueError('Pinned coordinator binding changed')
        return await self.guard.client._request('native-pool-head/'+path,method,payload)

    async def _hub(self,path,*,payload=None,absent=False):
        self._clean()
        if self.hub_api.api_url!=self.binding.hub_api:
            raise ValueError('Pinned Hub origin changed')
        return await private_json(self.binding.hub_api+'/'+path,self.hub_api.api_token,
            method='GET' if payload is None else 'POST',payload=payload,
            factory=self._factory,absent=absent)

    def _visitor(self,visitor):
        from ..schema.user import User
        if type(visitor) is not User:raise PermissionError('Authenticated Hub visitor required')
        _sdk().name(visitor.username)
        return visitor.username

    async def _actor(self,visitor):
        username=self._visitor(visitor)
        current=await self._hub('users/'+quote(username,safe=''))
        if (type(current) is not dict or current.get('name')!=username
            or type(current.get('admin')) is not bool or type(current.get('groups')) is not list
            or any(type(g) is not str for g in current['groups'])):
            raise PermissionError('Fresh authenticated Hub visitor required')
        return current

    def _source(self,group_id,action,current):
        from .expiry import active,effective_groups
        from .authorization import checker,Permission
        bindings=validate_bindings(self._records('courses'),self._records('groups'),self._records('terms'),self.legacy_roles)
        if group_id not in bindings:raise PermissionError('Explicit owned Source group binding required')
        course,group,binding=bindings[group_id]
        if binding['role']=='student' and group.get('role') not in (None,'student'):
            raise PermissionError('Teaching Source group cannot use a student binding')
        if action not in ACTIONS:raise ValueError('Ordinary group action required')
        if group.get('assignment_id') is not None:
            raise ValueError('Assignment-bound group requires the qualified shared lifecycle adapter')
        if course.get('reconciliation_pending') or course.get('membership_removal_pending'):
            raise ValueError('Course reconciliation pending')
        if not current['admin']:
            groups=effective_groups(self.provider,current['name'],current['groups'],self.legacy_roles)
            check=checker(current['name'],groups,self.legacy_roles)
            permission=Permission.EDIT_TERM if group.get('term_id') else Permission.EDIT_COURSE
            if (action=='group-delete' or binding['role']!='student'
                or not check.has_permission(permission,course_id=course['id'],term_id=group.get('term_id'))):
                raise PermissionError('Owned course instructor or actual console administrator required')
        # Shared lifecycle is deliberately unsupported until Task 3C, even stopped refs.
        for kind in ('workspaces','assignments','groupings'):
            for record in self._records(kind):
                if (record.get('group_id')==group_id or group_id in record.get('group_ids',[])
                    or group_id in record.get('groups',{}) or record['id']==group.get('assignment_id')):
                    raise ValueError('Referenced group requires the qualified shared lifecycle adapter')
        if any(w.get('group_id')==group_id for c in self._records('courses') for w in c.get('workspace_bindings',[])):
            raise ValueError('Server-bound group requires the qualified shared lifecycle adapter')
        memberships=[m for m in self._records('memberships') if m.get('group_id')==group_id]
        if action=='group-delete' and (memberships or group.get('members')):
            raise ValueError('Group deletion requires no Source membership references')
        roster=[]
        for member in memberships:
            if (member.get('course_id')!=course['id'] or member.get('term_id')!=group.get('term_id')
                or member.get('source')!='local'):
                raise ValueError('Exact local course/term membership required')
            if not active(member):continue
            if binding['role']=='student' and member.get('role','student')!='student':
                raise PermissionError('Teaching membership cannot enter a student Hub binding')
            username=member.get('person_id');_sdk().name(username)
            if username in roster:raise ValueError('Duplicate Source account mapping')
            # person_id is the explicit local Hub username; canonical IDs are only
            # accepted with the actual reviewed account row, never guessed by email.
            if member.get('canonical_person_id') is not None:
                link=self.provider.db.execute('SELECT canonical_person_id,verified FROM email_links WHERE console=? AND username=?',
                    (self.binding.console,username)).fetchone()
                if link is None or tuple(link)!=(member['canonical_person_id'],1):
                    raise ValueError('Verified exact Hub account mapping required')
            roster.append(username)
        # Legacy group.members is not authority. Reject inconsistent duplicate rosters.
        if group.get('members') and sorted(group['members'])!=sorted(roster):
            raise ValueError('Source group roster conflicts with active memberships')
        return binding,sorted(roster)

    async def _fresh(self,visitor,group_id,action,source,expected_roster):
        current=await self._actor(visitor)
        self._clean()
        binding,roster=self._source(group_id,action,current)
        if self.journal.capture_source()!=source or roster!=expected_roster:
            raise ValueError('Source authority or active membership changed during request')
        return current,binding

    def _head(self,status,*,operation_id=None,input_sha256=None,effect_sha256=None,allow_completed=False,ticket=None):
        b=self.binding
        if type(status) is not dict or status.get('database_id')!=b.coordinator_database_id:
            raise ValueError('Pinned native database required')
        if operation_id is None:return status
        row=status.get('transition')
        if (status.get('state')!='fenced' or status.get('transition_id')!=operation_id
            or type(row) is not dict or row.get('transition_id')!=operation_id
            or status.get('revision')!=(row.get('predecessor') or 'pending:'+operation_id)
            or row.get('actor')!=b.writer_actor or row.get('instance')!=b.writer_instance
            or row.get('operation')!='admin.hub' or row.get('input_sha256')!=input_sha256
            or row.get('effect_sha256')!=effect_sha256 or row.get('target') is not None
            or row.get('receipt') is not None or not allow_completed and row.get('mutation_result') is not None):
            raise ValueError('Exact current unfinished fenced Hub transition required')
        if ticket is not None and any(row.get(key)!=ticket[key] for key in (
                'transition_id','predecessor','operation','input_sha256','actor','instance')):
            raise ValueError('Original current transition ticket required')
        return row

    def _effect(self,value,record):
        b=self.binding
        result=_sdk().validate_effect_record(value,transition_id=record['operation_id'],
            actor=b.writer_actor,instance=b.writer_instance,input_sha256=record['input_sha256'])
        if result['intent']!=record['intent']:raise ValueError('Retained effect intent changed')
        return result

    async def dispatch(self,visitor,group_id,action):
        _sdk().name(group_id)
        if action not in ACTIONS:raise ValueError('Ordinary group action required')
        source=self.journal.capture_source()
        current=await self._actor(visitor)
        binding,roster=self._source(group_id,action,current)
        before=await self._hub('groups/'+quote(binding['hub_group'],safe=''),absent=True)
        if before is not None:
            if (type(before) is not dict or before.get('name')!=binding['hub_group']
                or type(before.get('properties')) is not dict):raise ValueError('Exact Hub group projection required')
            for field in ('users','roles'):
                values=before.get(field)
                if type(values) is not list:raise ValueError('Complete actual Hub group model required')
                for value in values:_sdk().name(value)
                if len(values)!=len(set(values)):raise ValueError('Duplicate actual Hub group names')
                before[field]=sorted(values)
            _sdk().names(before['users']);_sdk().names(before['roles'])
            if before['roles']:raise PermissionError('Native role-bearing groups require a separate writer')
            before={key:before[key] for key in ('name','users','roles')} | {'properties_sha256':digest(before['properties'])}
        if action=='group-create' and before is not None or action!='group-create' and before is None:
            raise ValueError('Hub group existence differs from action')
        request={'action':action,'name':binding['hub_group']}
        if action in ('group-users-add','group-users-remove'):
            users=sorted(set(roster)-set(before['users'])) if action=='group-users-add' else sorted(set(before['users'])-set(roster))
            if not users:
                await self._fresh(visitor,group_id,action,source,roster)
                return {'state':'unchanged'}
            request['users']=users
        for username in roster:
            existing=await self._hub('users/'+quote(username,safe=''))
            if type(existing) is not dict or existing.get('name')!=username:
                raise ValueError('Exact existing mapped Hub account required')
        await self._fresh(visitor,group_id,action,source,roster)
        intent=_sdk().validate_hub_intent(dict(console=self.binding.console,actor=current['name'],
            hub_api=self.binding.hub_api,request=request,before=before,source_revision=digest(source)))
        input_hash=digest({'operation':'admin.hub','payload':intent})
        status=self._head(await self._rpc('current'))
        if status.get('state')=='fenced':
            row=status.get('transition',{})
            if row.get('effect_sha256') is not None or row.get('mutation_result') is not None:
                raise ValueError('Retained operation requires its exact recovery route')
            self._head(status,operation_id=status.get('transition_id'),input_sha256=input_hash)
            # Full local history validated above. No retained effect means a lost
            # prepare response may be read back from this exact pending head.
        await self._fresh(visitor,group_id,action,source,roster)
        self._clean()
        ticket=await self.guard.prepare('admin.hub',intent)
        record=self.journal.prepare(operation_id=ticket['transition_id'],input_sha256=input_hash,
            intent=intent,source_preimage=source,hub_preimage=before,actor=current['name'],ticket=ticket)
        return await self._run(visitor,record,group_id,roster)

    async def recover(self,visitor,operation_id,input_sha256):
        if type(operation_id) is not str or str(uuid.UUID(operation_id))!=operation_id:
            raise ValueError('Canonical operation UUID required')
        record=self.journal.read(operation_id,input_sha256)
        if record is None:raise ValueError('Original retained Source operation required')
        current=await self._actor(visitor)
        if current['name']!=record['actor'] and not current['admin']:
            raise PermissionError('Original visitor or actual console administrator required')
        if record['state']=='completed':
            await self._finish(record)
            return self.journal.read(operation_id,input_sha256)
        bindings=validate_bindings(self._records('courses'),self._records('groups'),self._records('terms'),self.legacy_roles)
        group_id=next((gid for gid,(_,_,b) in bindings.items() if b['hub_group']==record['intent']['request']['name']),None)
        if group_id is None:raise ValueError('Original Source binding missing')
        _,roster=self._source(group_id,record['intent']['request']['action'],current)
        request=record['intent']['request']
        if request['action'] in ('group-users-add','group-users-remove'):
            before=set(record['intent']['before']['users'])
            expected=sorted(set(roster)-before) if request['action']=='group-users-add' else sorted(before-set(roster))
            if request['users']!=expected:raise ValueError('Original active Source membership intent changed')
        return await self._run(visitor,record,group_id,roster)

    async def _run(self,visitor,record,group_id,roster):
        op,ih=record['operation_id'],record['input_sha256']
        action=record['intent']['request']['action']
        await self._fresh(visitor,group_id,action,record['source_preimage'],roster)
        status=await self._rpc('current')
        expected=record['effect_sha256']
        if record['state']=='prepared':
            row=status.get('transition',{})
            self._head(status,operation_id=op,input_sha256=ih,effect_sha256=row.get('effect_sha256'),ticket=record['ticket'])
            path='hub-effects/read' if row.get('effect_sha256') else 'hub-effects'
            effect=self._effect(await self._rpc(path,'POST',{'transition_id':op,'intent':record['intent']}),record)
            eh=digest(effect)
            if row.get('effect_sha256') not in (None,eh):raise ValueError('Retained effect digest differs')
            record=self.journal.arm(op,ih,effect=effect,effect_sha256=eh)
        else:
            self._head(status,operation_id=op,input_sha256=ih,effect_sha256=expected,ticket=record['ticket'])
            effect=self._effect(await self._rpc('hub-effects/read','POST',{'transition_id':op,'intent':record['intent']}),record)
            if effect!=record['effect'] or digest(effect)!=expected:raise ValueError('Original effect custody required')
        # Last awaited authority observation, then synchronous time/Source checks.
        await self._fresh(visitor,group_id,action,record['source_preimage'],roster)
        receipt=await self._hub('cps/permission-operations/'+op,payload={'effect':record['effect']})
        # Genuine atomic receiver receipt is the only completion evidence.
        record=self.journal.complete(op,ih,receipt=receipt)
        await self._finish(record)
        return record

    async def _finish(self,record):
        op,ih=record['operation_id'],record['input_sha256']
        status=self._head(await self._rpc('current'))
        if status.get('transition_id')!=op:
            # Historical receipt cannot act on a later current head.
            return
        row=self._head(status,operation_id=op,input_sha256=ih,effect_sha256=record['effect_sha256'],allow_completed=True,ticket=record['ticket'])
        effect=self._effect(await self._rpc('hub-effects/read','POST',{'transition_id':op,'intent':record['intent']}),record)
        if effect!=record['effect']:raise ValueError('Original retained effect required')
        result={'receipt':record['receipt']}
        if row.get('mutation_result') not in (None,digest(result)):
            raise ValueError('Global mutation completion differs')
        # Recheck the actual head after the effect await; never complete a new head.
        status=await self._rpc('current')
        row=self._head(status,operation_id=op,input_sha256=ih,effect_sha256=record['effect_sha256'],allow_completed=True,ticket=record['ticket'])
        if row.get('mutation_result')==digest(result):return
        if row.get('mutation_result') is not None:raise ValueError('Global mutation completion differs')
        self._clean()
        await self.guard.complete(record['ticket'],result)
