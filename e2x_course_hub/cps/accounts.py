"""Genuine neutral accounts through the installed managed Hub writer only."""


class WorkspaceAccountAdapter:
    def __init__(self, controller):
        from .hub_permissions import HubPermissionController
        if type(controller) is not HubPermissionController:
            raise ValueError('Original installed Hub permission controller required')
        self.controller=controller

    async def ensure(self, workspace, *, visitor):
        return await self.controller.create_workspace_account(visitor,workspace['id'])

    async def head(self):
        controller=self.controller
        controller._clean()
        controller.provider.managed_installation.check_source(controller.provider)
        return controller._head(await controller._rpc('current'))


class CentralWorkspaceFilesystemAdapter:
    """Use central storage routes only; never mount, delete, or rewrite files locally."""
    def __init__(self, compute):self.compute=compute

    async def provision(self, workspace, *, actor):
        return await self.compute.provision_workspace(workspace['id'])

    async def archive(self, workspaces, *, actor, assignment=None):
        if (type(workspaces) is not list or not workspaces
                or any(type(workspace) is not dict or type(workspace.get('id')) is not str
                    or not workspace['id'].strip() for workspace in workspaces)):
            raise ValueError('Exact retained workspace archive targets required')
        result = await self.compute.archive_workspaces(
            [workspace['id'] for workspace in workspaces], actor=actor, assignment=assignment)
        if (type(result) is not dict or result.get('read_only_verified') is not True
                or not result.get('evidence')):
            raise ValueError('Read-only archive verification required')
        return result
