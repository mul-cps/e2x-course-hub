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
    """Only the existing central provision route; no local mount/permission writes."""
    def __init__(self, compute):self.compute=compute

    async def provision(self, workspace, *, actor):
        return await self.compute.provision_workspace(workspace['id'])

    async def archive(self, *args, **kwargs):
        raise ValueError('Managed shared archive lifecycle is not qualified')
