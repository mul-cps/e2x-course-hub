"""Closed normalized record shapes and owned relational reference validation."""
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, List, Dict, Any

class Record(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    id: str = Field(min_length=1)
    source: str = 'local'
    external_id: Optional[str] = None

class WorkspaceBinding(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    group_id:str
    hub_user:str
    hub_server:str

class Course(Record):
    name: str = ''
    description: str = ''
    resource_ceiling: Optional[Dict[str,Any]] = None
    workspace_bindings: List[WorkspaceBinding] = Field(default_factory=list)

class Child(Record):
    course_id: str = Field(min_length=1)
    term_id: Optional[str] = None

class Term(Child):
    name: str = ''

class Membership(Child):
    person_id: str = Field(min_length=1)
    canonical_person_id: Optional[str] = None
    role: str = 'student'
    group_id: Optional[str] = None
    assignment_id: Optional[str] = None
    starts: Optional[str] = None
    expires: Optional[str] = None

class Group(Child):
    name: str = ''
    role: Optional[str] = None
    assignment_id: Optional[str] = None
    members: List[str] = Field(default_factory=list)

class Grouping(Child):
    name: str = ''
    group_ids: List[str] = Field(default_factory=list)

class Project(Record):
    name: str = ''
    description: str = ''
    course_id: Optional[str] = None

class Assignment(Child):
    mode: str
    rows: Optional[Any] = None
    group_size: Optional[int] = None
    seed: Optional[Any] = None
    groups: Dict[str,List[str]] = Field(default_factory=dict)
    state: str = 'open'
    archive_pending: bool = False

class Workspace(Child):
    group_id: str
    hub_user: Optional[str] = None
    hub_server: Optional[str] = None
    profile: Optional[str] = None
    course_ceiling: Dict[str,Any] = Field(default_factory=dict)
    state: str = 'stopped'
    archive_pending: bool = False
    archived: bool = False
    notice: Optional[str] = None

MODELS = dict(courses=Course,terms=Term,memberships=Membership,groups=Group,groupings=Grouping,
              projects=Project,assignments=Assignment,workspaces=Workspace)

async def validate(provider,kind,record):
    # Preserve absent legacy fields, while rejecting unknown or wrong-type supplied values.
    result=MODELS[kind].model_validate(record).model_dump(exclude_unset=True)
    if kind=='memberships':
        from .expiry import instant
        start,end=instant(result.get('starts')),instant(result.get('expires'))
        if start and end and start>=end:raise ValueError('membership expiry must follow start')
    course_id=result.get('course_id')
    if course_id is not None:
        courses=await provider.list('courses')
        course=next((r for r in courses if r['id']==course_id),None)
        if not course:raise ValueError('owned course reference does not exist')
        if course.get('source','local')!='local':raise PermissionError('external course is read-only')
    term_id=result.get('term_id')
    if term_id and kind!='terms':
        if not any(t.get('term_id',t['id'])==term_id or t['id']==term_id for t in await provider.terms(course_id)):
            raise ValueError('term reference does not exist in owned course')
    for group_id in ([result['group_id']] if result.get('group_id') else result.get('group_ids',[])):
        group=next((g for g in await provider.groups(course_id) if g['id']==group_id),None)
        if not group:
            raise ValueError('group reference does not exist in owned course')
        if term_id and group.get('term_id') and group['term_id']!=term_id:
            raise ValueError('group reference belongs to another term')
    if kind=='groups' and result.get('members'):
        people={m['person_id'] for m in await provider.members(course_id)}
        if set(result['members'])-people:raise ValueError('group contains unknown course membership')
        if len(result['members'])!=len(set(result['members'])):raise ValueError('duplicate group members')
    assignment_id=result.get('assignment_id')
    if assignment_id and not any(a['id']==assignment_id for a in await provider.list('assignments',course_id)):
        raise ValueError('assignment reference does not exist in owned course')
    if len(result.get('group_ids',[]))!=len(set(result.get('group_ids',[]))):
        raise ValueError('duplicate group references')
    if kind=='memberships' and result.get('canonical_person_id') is not None:
        linked=provider.db.execute('SELECT canonical_person_id FROM email_links WHERE console=? AND username=?',
            (provider.console,result['person_id'])).fetchone()
        if not linked or linked['canonical_person_id']!=result['canonical_person_id']:
            raise ValueError('membership canonical UUID requires an explicit verified/reviewed account mapping')
    return result
