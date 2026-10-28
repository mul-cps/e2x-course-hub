"""Authenticated console operations. Central policy remains the resource authority."""
import sqlite3
import json
from tornado import web
from tornado.httpclient import HTTPClientError
from .handlers import RecordsHandler
from .assignments import allocate

class ComputeHandler(RecordsHandler):
    @web.authenticated
    async def get(self):
        await self.authorize()
        client = self.settings.get('compute_policy')
        if client is None: raise web.HTTPError(503,reason='Shared compute service not configured')
        person = self.get_argument('person',None)
        if person: self.write({'grants':await client.grants(person)})
        else: self.write({'profiles':await client._request('profiles')})

    @web.authenticated
    async def post(self):
        actor = await self.authorize()
        client = self.settings.get('compute_policy')
        if client is None: raise web.HTTPError(503,reason='Shared compute service not configured')
        data = self.body()
        allowed = {'id','person','profiles','projects','allowance','expires','starts','reason'}
        if set(data)-allowed: raise web.HTTPError(400,reason='Unknown grant fields')
        provider = self.settings['course_provider']
        previous = await client.grants(data.get('person',''))
        try: result = await client.put_grant(data,actor=actor)
        except HTTPClientError as error:
            provider.denial(actor,'compute-grants',str(data.get('id','')),data,'denied')
            raise web.HTTPError(error.code,reason='Shared compute policy rejected request')
        with provider.db:
            provider._audit(actor,'compute-grants',str(data['id']),json.dumps(previous),json.dumps(result))
        if getattr(provider,'pool_guard',None) is not None:self.set_status(202)
        self.write(result or {})

class WorkspaceHandler(RecordsHandler):
    def on_finish(self):
        if getattr(self.settings.get('course_provider'),'pool_guard',None) is not None:
            # These closed managed service methods retain their semantic journal
            # and denial successor. The legacy request writer would escape custody.
            from ..course_service.handlers.base import BaseHandler
            return BaseHandler.on_finish(self)
        return super().on_finish()

    @web.authenticated
    async def get(self,identifier,action):
        if action not in ('status','operations'):raise web.HTTPError(405)
        service=self.settings.get('workspace_service')
        if service is None:raise web.HTTPError(503)
        workspace=await service.get(identifier)
        await self.authorize(workspace['course_id'],workspace.get('term_id'),read=True)
        controller=getattr(getattr(service,'accounts',None),'controller',None)
        if controller is None:raise web.HTTPError(503,reason='Managed runtime observer unavailable')
        if action=='operations':
            if self.request.query:raise web.HTTPError(400,reason='No operation discovery query fields accepted')
            try:
                operations=await controller.pending_workspace_removals(await self.get_user(),identifier)
            except PermissionError:
                raise web.HTTPError(403,reason='Fresh authenticated Hub identity required') from None
            except (ValueError,KeyError,TypeError):
                raise web.HTTPError(409,reason='Retained workspace operation unavailable') from None
            self.set_header('Cache-Control','no-store')
            self.write({'operations':operations})
            return
        from urllib.parse import quote
        user=await controller._hub('users/'+quote(workspace['hub_user'],safe='')+'?include_stopped_servers=true')
        server=user.get('servers',{}).get(workspace['hub_server'])
        if type(server) is not dict:raise web.HTTPError(409,reason='Exact scoped runtime observation unavailable')
        self.set_header('Cache-Control','no-store')
        self.write({'workspace':identifier,'runtime':{key:server[key] for key in ('name','ready','stopped','pending','started')}})

    @web.authenticated
    async def post(self, identifier, action):
        if action not in ('start','stop','remove-member'):raise web.HTTPError(405)
        service = self.settings.get('workspace_service')
        if service is None: raise web.HTTPError(503,reason='Shared workspace lifecycle not configured')
        workspace=await service.get(identifier)
        actor=await self.authorize(workspace['course_id'],workspace.get('term_id'))
        data=self.body() if self.request.body else {}
        allowed = {'membership_id'} if action == 'remove-member' else set()
        if set(data)!=allowed: raise web.HTTPError(400,reason='Exact lifecycle fields required')
        managed=getattr(service.provider,'pool_guard',None) is not None
        try:
            if action == 'start': result=await service.start(identifier,actor=actor)
            elif action == 'stop': result=await service.close(identifier,actor=actor)
            else: result=await service.remove_member(identifier,data['membership_id'],actor=actor)
        except (ValueError,KeyError) as error:
            if managed and action=='remove-member':
                c=service.accounts.controller
                # Only original validated, retained operations may become recovery
                # responses. A corrupt/unavailable journal still refuses.
                matches=[]
                for op,ih in c.provider.db.execute('SELECT operation_id,input_sha256 FROM hub_journal_operation'):
                    record=c.journal.read(op,ih);request=record['intent']['request']
                    if request['action']=='shared-student-remove' and request['workspace']==identifier and request['membership_id']==data['membership_id']:
                        matches.append(record)
                if len(matches)==1 and matches[0]['actor']==actor:
                    result=matches[0]
                else:raise web.HTTPError(409,reason='Original removal unavailable; retain recovery context') from None
            else:raise web.HTTPError(409 if managed else 400,reason=str(error)) from None
        except PermissionError as error: raise web.HTTPError(403,reason=str(error))
        except TimeoutError as error: raise web.HTTPError(409,reason=str(error))
        except RuntimeError as error: raise web.HTTPError(503,reason=str(error))
        except HTTPClientError as error: raise web.HTTPError(error.code,reason='Shared compute policy rejected request')
        if managed:
            self.set_status(202);self.set_header('Cache-Control','no-store')
            self.write({key:result[key] for key in ('state','workspace','operation_id','input_sha256') if key in result})
        else:self.write({'workspace':await service.get(identifier)})

class AssignmentHandler(RecordsHandler):
    @web.authenticated
    async def post(self):
        provider = self.settings['course_provider']
        data = self.body()
        actor = await self.authorize(data.get("course_id"),data.get("term_id"))
        allowed = {'id','course_id','term_id','mode','rows','group_size','seed'}
        if set(data)-allowed: raise web.HTTPError(400,reason='Unknown assignment fields')
        try:
            courses = await provider.list('courses')
            course = next((c for c in courses if c['id']==data['course_id']),None)
            if not course or course.get('source') != 'local': raise PermissionError('Local course required')
            members = [m['person_id'] for m in await provider.members(data['course_id']) if m.get('term_id')==data['term_id']]
            identifier = data['id']
            existing = await provider.list('assignments')
            if any(a['id']==identifier for a in existing):
                retained=await provider.retained_import('assignments',identifier) if getattr(provider,'pool_guard',None) is not None else None
                assignment=next((item['record'] for item in retained or [] if item['kind']=='assignments' and item['record']['id']==identifier),None)
                if assignment is None or {key:value for key,value in assignment.items() if key in allowed}!=data:
                    raise ValueError('Existing assignment requires reviewed stop/reallocation; create a new assignment ID')
                retry={}
                for item in retained:retry.setdefault(item['kind'],[]).append(item['record'])
                await provider.migrate_local(retry,actor=actor)
                self.set_status(202)
                self.write({'assignment':identifier,'groups':assignment['groups'],'state':'pending'})
                return
            groups = allocate(members,mode=data['mode'],rows=data.get('rows'),group_size=data.get('group_size'),seed=data.get('seed'))
            records = {'groups':[], 'groupings':[], 'assignments':[], 'memberships':[]}
            originals = {m['person_id']:m for m in await provider.members(data['course_id']) if m.get('term_id')==data['term_id']}
            for group,people in groups.items():
                group_id = f'{identifier}-{group}'
                records['groups'].append({'id':group_id, 'course_id':data['course_id'],'term_id':data['term_id'],'assignment_id':identifier,'members':people})
                for person in people:
                    records['memberships'].append({**originals[person], 'id':f'{group_id}:{person}', 'group_id':group_id, 'assignment_id':identifier})
            records['groupings'].append({'id':identifier,'course_id':data['course_id'],'term_id':data['term_id'],'group_ids':[r['id'] for r in records['groups']]})
            records['assignments'].append({**data,'groups':groups,'source':'local'})
            await provider.migrate_local(records,actor=actor)
        except (ValueError,KeyError,TypeError) as error: raise web.HTTPError(400,reason=str(error))
        except PermissionError as error: raise web.HTTPError(403,reason=str(error))
        if getattr(provider,'pool_guard',None) is not None:
            self.set_status(202)
            self.write({'assignment':identifier,'groups':groups,'state':'pending'})
        else:self.write({'assignment':identifier,'groups':groups})

default_handlers = [
    (r'/api/compute',ComputeHandler),
    (r'/api/assignments/allocate',AssignmentHandler),
    (r'/api/workspaces/([^/]+)/(start|stop|remove-member|status|operations)',WorkspaceHandler),
]

class WorkspaceCreateHandler(RecordsHandler):
    @web.authenticated
    async def post(self):
        service = self.settings.get('workspace_service')
        if service is None: raise web.HTTPError(503,reason='Shared workspace lifecycle not configured')
        data = self.body()
        actor=await self.authorize(data.get("course_id"),data.get("term_id"))
        required = {'id','course_id','term_id','group_id','hub_user','hub_server','profile','course_ceiling'}
        if set(data) != required: raise web.HTTPError(400,reason='Exact workspace fields required; resource overrides forbidden')
        import re
        if any(not isinstance(data[k],str) or not re.fullmatch(r'[a-zA-Z0-9_.-]+',data[k]) for k in ('id','group_id','hub_user','hub_server')):
            raise web.HTTPError(400,reason='Workspace/Hub identifiers must be explicit safe identifiers')
        provider = self.settings['course_provider']
        if any(w['id']==data['id'] for w in await provider.list('workspaces')):
            raise web.HTTPError(409,reason='Workspace already exists')
        course=next((c for c in await provider.courses() if c['id']==data['course_id']),None)
        bindings=[b for b in (course or {}).get('workspace_bindings',[]) if all(b.get(k)==data[k] for k in ('group_id','hub_user','hub_server'))]
        binding=bindings[0] if len(bindings)==1 else None
        if not course or course.get('resource_ceiling') != data['course_ceiling'] or binding is None:
            raise web.HTTPError(403,reason='Use administrator-controlled course ceiling and neutral-account binding')
        groups = await provider.groups(data['course_id'])
        group=next((g for g in groups if g['id']==data['group_id']),None)
        if group is None: raise web.HTTPError(400,reason='Unknown owned course group')
        if group.get('assignment_id'):
            assignment=next((a for a in await provider.list('assignments') if a['id']==group['assignment_id']),None)
            if not assignment or assignment.get('state','open')!='open' or assignment.get('archive_pending'):
                raise web.HTTPError(409,reason='Assignment is closing or closed')
        try:
            await service.create(data,actor=actor)
        except (ValueError,KeyError) as error: raise web.HTTPError(400,reason=str(error))
        except PermissionError as error: raise web.HTTPError(403,reason=str(error))
        except HTTPClientError as error: raise web.HTTPError(error.code,reason='Shared compute policy rejected profile')
        self.write({'workspace':data['id']})

default_handlers.append((r'/api/workspaces/create',WorkspaceCreateHandler))

class AssignmentCloseHandler(RecordsHandler):
    @web.authenticated
    async def post(self, identifier):
        service = self.settings.get('workspace_service')
        if service is None: raise web.HTTPError(503,reason='Shared workspace lifecycle not configured')
        provider = self.settings['course_provider']
        assignment = next((a for a in await provider.list('assignments') if a['id']==identifier),None)
        if not assignment: raise web.HTTPError(404)
        actor=await self.authorize(assignment['course_id'],assignment.get('term_id'))
        if assignment.get('source') != 'local': raise web.HTTPError(403,reason='External assignment is read-only')
        try:
            result = await service.close_assignment(assignment,actor=actor)
        except RuntimeError as error:
            raise web.HTTPError(503,reason=str(error))
        except (TimeoutError,HTTPClientError) as error:
            raise web.HTTPError(409,reason='Writer shutdown or reservation release not confirmed')
        self.write({'assignment':result,'notice':'Verified read-only archive; files retained.'})

default_handlers.append((r'/api/assignments/([^/]+)/close',AssignmentCloseHandler))

class IdentityMappingHandler(RecordsHandler):
    @web.authenticated
    async def post(self):
        actor=await self.authorize()
        data=self.body()
        if set(data)!={'mappings'} or not isinstance(data['mappings'],list):raise web.HTTPError(400,reason='Explicit reviewed mappings list required')
        if any(not isinstance(row,dict) for row in data['mappings']):raise web.HTTPError(400,reason='Mapping rows must be objects')
        if any(row.get('hub')!=self.settings['console_owner'] for row in data['mappings']):raise web.HTTPError(403,reason='Console can link only its own Hub accounts')
        try:result=await self.settings['course_provider'].link_identities(data['mappings'],actor=actor)
        except (ValueError,TypeError,sqlite3.IntegrityError) as error:raise web.HTTPError(400,reason=str(error))
        if getattr(self.settings['course_provider'],'pool_guard',None) is not None:
            self.set_status(202)
            self.write({'linked':result,'state':'pending'})
        else:self.write({'linked':result})

default_handlers.append((r'/api/identities',IdentityMappingHandler))
