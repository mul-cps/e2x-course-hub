"""Console-owned CRUD: instructors retain upstream course-scoped operations."""
import json
from tornado import web
from ..course_service.handlers.base import BaseAPIHandler

class RecordsHandler(BaseAPIHandler):
    async def authorize(self):
        user = await self.get_user()
        # Current Hub state, rather than login-time admin claims, determines authority.
        current = await self.course_api.hub_api.get_user(user.username)
        if not current.get('admin', False):
            if self.request.method in ('POST','DELETE','PUT'):
                self.settings['course_provider'].denial(user.username,'authorization',self.request.path,{},'denied')
            raise web.HTTPError(403, reason='Console administrator required')
        return user.username

    @web.authenticated
    async def get(self, kind):
        await self.authorize()
        provider = self.settings['course_provider']
        if kind == 'audit': self.write({'records': provider.audit()})
        else: self.write({'records': await provider.list(kind)})

    @web.authenticated
    async def post(self, kind):
        actor = await self.authorize()
        if kind in ('assignments','workspaces'):
            raise web.HTTPError(405,reason='Use controlled assignment/workspace lifecycle APIs')
        data = json.loads(self.request.body)
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
        actor = await self.authorize()
        if kind in ('assignments','workspaces','memberships'):
            raise web.HTTPError(405,reason='Membership removal requires controlled workspace stop/revoke')
        data = json.loads(self.request.body)
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
