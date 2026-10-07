"""Console-owned CRUD: instructors retain upstream course-scoped operations."""
import json
from tornado import web
from ..course_service.handlers.base import BaseAPIHandler

class RecordsHandler(BaseAPIHandler):
    async def authorize(self, course_id=None, term_id=None, *, read=False):
        user = await self.get_user()
        self._audit_actor = user.username
        from .audit import actor_context
        actor_context.set(user.username)
        current = await self.course_api.hub_api.get_user(user.username)
        if current.get('admin', False): return user.username
        from .expiry import effective_groups
        groups=effective_groups(self.settings["course_provider"],user.username,current.get("groups",user.groups),
                                self.settings.get('legacy_rbac_roles'))
        if course_id:
            from .authorization import checker, Permission
            check = checker(user.username,groups,self.settings.get('legacy_rbac_roles'))
            permission = (Permission.VIEW_TERM if term_id else Permission.VIEW_COURSE) if read else Permission.EDIT_TERM if term_id else Permission.EDIT_COURSE
            if check.has_permission(permission,course_id=course_id,term_id=term_id):return user.username
        raise web.HTTPError(403, reason='Owned course scope or console administrator required')

    def body(self):
        try: data = json.loads(self.request.body)
        except (ValueError,UnicodeDecodeError): raise web.HTTPError(400,reason='Valid JSON object required')
        if not isinstance(data,dict):raise web.HTTPError(400,reason='JSON object required')
        return data

    async def authorize_record(self, kind, data, *, read=False):
        course = data.get('id') if kind=='courses' else data.get('course_id')
        actor = await self.authorize(course,data.get('term_id'),read=read)
        previous = next((r for r in await self.settings['course_provider'].list(kind) if r['id']==data.get('id')),None)
        if previous:
            old_course=previous['id'] if kind=='courses' else previous.get('course_id')
            await self.authorize(old_course,previous.get('term_id'),read=read)
        return actor

    async def check_reconciliation_barrier(self, kind, data):
        course_id = data.get('id') if kind=='courses' else data.get('course_id')
        course=next((c for c in await self.settings['course_provider'].courses() if c['id']==course_id),None)
        if course and course.get('reconciliation_pending'):
            raise web.HTTPError(409,reason='Course reconciliation is pending')

    @web.authenticated
    async def get(self, kind):
        provider=self.settings['course_provider']
        if kind=='audit':
            await self.authorize()
            self.write({'records':provider.audit()})
            return
        course_id=self.get_argument('course_id',None)
        term_id=self.get_argument('term_id',None)
        user=await self.get_user()
        latest=await self.course_api.hub_api.get_user(user.username)
        records=await provider.list(kind)
        if course_id:records=[r for r in records if (r['id'] if kind=='courses' else r.get('course_id'))==course_id]
        if term_id:records=[r for r in records if r.get('term_id')==term_id]
        if not latest.get('admin',False):
            from .authorization import checker,Permission
            from .expiry import effective_groups
            groups=effective_groups(provider,user.username,latest.get('groups',user.groups),
                                    self.settings.get('legacy_rbac_roles'))
            check=checker(user.username,groups,self.settings.get('legacy_rbac_roles'))
            def visible(record):
                course=record['id'] if kind=='courses' else record.get('course_id')
                term=record.get('term_id')
                if not course:return False
                permission=Permission.VIEW_TERM if term else Permission.VIEW_COURSE
                return check.has_permission(permission,course_id=course,term_id=term)
            records=[r for r in records if visible(r)]
            if not check.get_course_ids():raise web.HTTPError(403,reason='Owned course scope required')
        self.write({'records':records})

    @web.authenticated
    async def post(self, kind):
        data = self.body()
        if {'reconciliation_pending','membership_removal_pending','membership_removal_group_id'} & data.keys():
            raise web.HTTPError(400,reason='Reconciliation barriers are service-controlled')
        actor = await self.authorize_record(kind,data)
        await self.check_reconciliation_barrier(kind,data)
        if kind=='courses' and {'resource_ceiling','workspace_bindings'} & set(data):await self.authorize()
        if kind in ('assignments','workspaces'):
            raise web.HTTPError(405,reason='Use controlled assignment/workspace lifecycle APIs')
        if kind == 'groups' and any(w.get('group_id') == data.get('id') for w in await self.settings['course_provider'].list('workspaces')):
            raise web.HTTPError(409,reason='Referenced workspace group requires controlled lifecycle update')
        if kind == 'memberships':
            records = await self.settings['course_provider'].list('memberships')
            previous = next((r for r in records if r['id']==data.get('id')), {})
            groups = {data.get('group_id'),previous.get('group_id')}
            workspaces = await self.settings['course_provider'].list('workspaces')
            identity_fields = ('course_id','group_id','person_id','canonical_person_id')
            if previous and any(w.get('group_id') == previous.get('group_id') for w in workspaces) and any(data.get(k) != previous.get(k) for k in identity_fields):
                raise web.HTTPError(409,reason='Referenced membership identity changes require controlled removal/reassignment')
            if any(w.get('group_id') in groups and w.get('state') != 'stopped' for w in workspaces):
                raise web.HTTPError(409,reason='Stop affected shared workspace before changing membership')
        try:
            result = await self.settings['course_provider'].put(kind, data, actor=actor)
        except (ValueError, KeyError, TypeError) as error:
            self.settings['course_provider'].denial(actor,kind,str(data.get('id','')),data,'invalid')
            raise web.HTTPError(400, reason=str(error))
        except PermissionError as error:
            self.settings['course_provider'].denial(actor,kind,str(data.get('id','')),data,'denied')
            raise web.HTTPError(403, reason=str(error))
        self.write(result)

    @web.authenticated
    async def delete(self, kind):
        data = self.body()
        existing=next((r for r in await self.settings['course_provider'].list(kind) if r['id']==data.get('id')),None)
        if existing is None:raise web.HTTPError(404)
        actor = await self.authorize_record(kind,existing)
        await self.check_reconciliation_barrier(kind,existing)
        if kind in ('assignments','workspaces','memberships'):
            raise web.HTTPError(405,reason='Membership removal requires controlled workspace stop/revoke')
        if kind == 'groups' and any(w.get('group_id') == data.get('id') for w in await self.settings['course_provider'].list('workspaces')):
            raise web.HTTPError(409,reason='Referenced workspace group cannot be deleted')
        try:
            await self.settings['course_provider'].delete(kind, data['id'], actor=actor)
        except (ValueError, KeyError) as error:
            raise web.HTTPError(400, reason=str(error))
        except PermissionError as error:
            raise web.HTTPError(403, reason=str(error))
        self.set_status(204)

default_handlers = [(r'/api/local/(courses|terms|memberships|groups|groupings|projects|assignments|workspaces|audit)', RecordsHandler)]
