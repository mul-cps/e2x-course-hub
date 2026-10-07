"""Course authorization uses the reviewed pinned e2x RBAC implementation."""
from enum import Enum
from types import SimpleNamespace
from e2x_hub_rbac import Role, Scope, RoleAssignment
from e2x_hub_rbac.auth.rbac import PermissionChecker

class Permission(Enum):
    VIEW_COURSE=('view:course',Scope.COURSE)
    VIEW_TERM=('view:term',Scope.TERM)
    EDIT_COURSE=('edit:course',Scope.COURSE)
    EDIT_TERM=('edit:term',Scope.TERM)
    def __init__(self,code,required_scope):self.code,self.required_scope=code,required_scope

ROLE_PERMISSIONS={role:frozenset() for role in Role}
ROLE_PERMISSIONS[Role.COURSE_OWNER]=frozenset(Permission)
ROLE_PERMISSIONS[Role.INSTRUCTOR]=frozenset(Permission)
ROLE_PERMISSIONS[Role.TEACHING_ASSISTANT]=frozenset({Permission.VIEW_COURSE,Permission.VIEW_TERM})

def canonical_group(group,legacy_roles=None):
    # Interpret configured legacy aliases without renaming any existing Hub group or path.
    aliases=legacy_roles or {'instructor':'instructor'}
    known={r.role_name:r for r in Role}
    parts=group.split('.')
    if len(parts)==3 and parts[2] in aliases:
        role=known.get(aliases[parts[2]])
        if role and role.scope is Scope.TERM:
            return RoleAssignment.term(role,parts[0],parts[1]).group_name
    return group

def checker(username,groups,legacy_roles=None):
    translated=list(groups)
    for group in groups:
        canonical=canonical_group(group,legacy_roles)
        if canonical != group:translated.append(canonical)
    return PermissionChecker(SimpleNamespace(username=username,groups=translated),ROLE_PERMISSIONS)
