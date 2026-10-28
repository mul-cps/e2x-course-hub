"""Opt-in console handlers. OAuth visitor/XSRF come from the existing BaseAPIHandler."""
import re
import uuid

from tornado import web
from tornado.routing import PathMatches
from ..course_service.handlers.base import BaseAPIHandler, BaseHandler
from .compute import strict_json

PATH = r'/api/local/hub-groups/operations'
SETTING = 'hub_permission_controller'


class HubGroupOperationHandler(BaseAPIHandler):
    def _boundary(self):
        controller=self.settings[SETTING]
        try:
            controller._clean()
            if (self.course_api.hub_api is not controller.hub_api
                or controller.hub_api.api_url!=controller.binding.hub_api
                or controller.guard.client.base_url!=controller.binding.coordinator_url
                or self.settings.get('course_provider') is not controller.provider):
                raise ValueError()
        except ValueError:
            raise web.HTTPError(409,'Pinned service context and clean Source required') from None

    def prepare(self):
        self._boundary()
        return super().prepare()

    def check_xsrf_cookie(self):
        self._boundary()
        return super().check_xsrf_cookie()

    async def get_user(self):
        self._boundary()
        current=self.get_current_user()
        if type(current) is not dict or current.get('kind')!='user':
            raise web.HTTPError(403,'Authenticated Hub User visitor required')
        # Keep BaseAPIHandler's actual OAuth identity, but obtain the fresh
        # model through the pinned quoted reader: legacy HubAPI.get_user joins
        # raw names into URLs and cannot represent all canonical Hub names.
        from ..schema.user import User
        from .expiry import effective_groups
        from .audit import actor_context
        try:
            candidate=User(username=current['name'],admin=False,groups=[])
            latest=await self.settings[SETTING]._actor(candidate)
        except (ValueError,KeyError,TypeError,PermissionError):
            raise web.HTTPError(403,'Fresh authenticated Hub User required') from None
        groups=effective_groups(self.settings['course_provider'],current['name'],latest['groups'],
            self.settings.get('legacy_rbac_roles'))
        self._audit_actor=current['name']
        actor_context.set(current['name'])
        return User(username=current['name'],admin=latest['admin'],groups=groups)

    def on_finish(self):
        # SourceHubJournal owns the atomic operation audit. The generic request
        # audit commits its connection and must not touch a caller's dirty SQL.
        BaseHandler.on_finish(self)

    @web.authenticated
    async def post(self, operation_id=None):
        if self.request.query or len(self.request.body)>4096:
            raise web.HTTPError(400,'Closed bounded operation body required')
        try:
            data=strict_json(self.request.body)
            fields=({'input_sha256'} if operation_id is not None else
                {'action','workspace','membership_id'} if type(data) is dict and data.get('action')=='shared-student-remove' else
                {'action','workspace'} if type(data) is dict and data.get('action') in ('workspace-account-create','workspace-prepare') else
                {'action','name'} if type(data) is dict and data.get('action')=='user-create' else {'group_id','action'})
            if type(data) is not dict or set(data)!=fields:
                raise ValueError()
            if any(type(value) is not str for value in data.values()):raise ValueError()
            if operation_id is not None and str(uuid.UUID(operation_id))!=operation_id:raise ValueError()
        except (ValueError,TypeError,UnicodeError,RecursionError):
            raise web.HTTPError(400,'Exact operation fields required') from None
        visitor=await self.get_user()
        controller=self.settings[SETTING]
        try:
            if operation_id is None:
                if data['action']=='shared-student-remove':
                    from .shared_lifecycle import SharedLifecycleController
                    result=await SharedLifecycleController(controller).remove(visitor,data['workspace'],data['membership_id'])
                elif data['action']=='workspace-prepare':
                    service=self.settings.get('workspace_service')
                    if service is None:raise ValueError('Workspace service unavailable')
                    result=await service.prepare_native_approval(data['workspace'],visitor=visitor)
                elif data['action']=='workspace-account-create':result=await controller.create_workspace_account(visitor,data['workspace'])
                elif data['action']=='user-create':result=await controller.create_user(visitor,data['name'])
                else:result=await controller.dispatch(visitor,data['group_id'],data['action'])
            else:
                result=await controller.recover(visitor,operation_id,data['input_sha256'])
        except PermissionError:
            raise web.HTTPError(403,'Current owned course authority required') from None
        except (ValueError,KeyError,TypeError):
            raise web.HTTPError(409,'Permission operation unavailable or conflicted; retain exact recovery context') from None
        # Do not expose retained SQL observations or server installation metadata.
        response={key:result[key] for key in ('state','operation_id','input_sha256','receipt','phase','workspace','account_operation','account_input_sha256') if key in result}
        self.set_header('Cache-Control','no-store')
        self.set_status(200 if result['state']=='unchanged' else 202)
        self.write(response)


def install_hub_permissions(app, *, enabled=False, controller=None, service_prefix='/'):
    """Merge explicit routes only. Disabled mode does not inspect any other input."""
    if type(enabled) is not bool:raise ValueError('Explicit enablement boolean required')
    if not enabled:return None
    from .hub_permissions import HubPermissionController
    if type(controller) is not HubPermissionController:
        raise ValueError('Typed Source-authorized Hub controller required')
    if (type(service_prefix) is not str or not service_prefix.startswith('/')
        or not service_prefix.endswith('/') or '//' in service_prefix
        or any(p in ('.','..') for p in service_prefix.split('/'))
        or re.search(r'[^A-Za-z0-9_./-]',service_prefix)):
        raise ValueError('Canonical service prefix required')
    path=service_prefix.rstrip('/')+PATH
    if (SETTING in app.settings or app.settings.get('xsrf_cookies') is not True
        or app.settings.get('disable_check_xsrf')
        or 'course_provider' in app.settings and app.settings['course_provider'] is not controller.provider):
        raise ValueError('Conflicting permission settings or missing XSRF protection')
    pending=[app.default_router]
    visited=set()
    while pending:
        router=pending.pop()
        if id(router) in visited:continue
        visited.add(id(router))
        for rule in router.rules:
            if isinstance(rule.matcher,PathMatches) and any(rule.matcher.regex.fullmatch(probe)
                for probe in (path,path+'/11111111-1111-4111-8111-111111111111')):
                raise ValueError('Conflicting permission handler')
            if hasattr(rule.target,'rules'):pending.append(rule.target)
    service=app.settings.get('workspace_service')
    if service is not None:
        from .accounts import WorkspaceAccountAdapter,CentralWorkspaceFilesystemAdapter
        if service.provider is not controller.provider or service.compute is not controller.guard.client:
            raise ValueError('Exact managed workspace service binding required')
    app.add_handlers(r'.*$',[(re.escape(path),HubGroupOperationHandler),
        (re.escape(path)+r'/([^/]+)',HubGroupOperationHandler)])
    app.settings[SETTING]=controller
    if service is not None:
        service.accounts=WorkspaceAccountAdapter(controller)
        service.filesystem=CentralWorkspaceFilesystemAdapter(service.compute)
    return controller
