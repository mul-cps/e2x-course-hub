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
        try:
            result = await self.settings['course_provider'].put(kind, json.loads(self.request.body), actor=actor)
        except (ValueError, KeyError, TypeError) as error:
            raise web.HTTPError(400, reason=str(error))
        except PermissionError as error:
            raise web.HTTPError(403, reason=str(error))
        self.write(result)

    @web.authenticated
    async def delete(self, kind):
        actor = await self.authorize()
        try:
            await self.settings['course_provider'].delete(kind, json.loads(self.request.body)['id'], actor=actor)
        except (ValueError, KeyError) as error:
            raise web.HTTPError(400, reason=str(error))
        except PermissionError as error:
            raise web.HTTPError(403, reason=str(error))
        self.set_status(204)

default_handlers = [(r'/api/local/(courses|terms|memberships|groups|groupings|audit)', RecordsHandler)]
