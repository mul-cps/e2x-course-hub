"""Course-service compatibility adapter for the installed compute storage SDK."""

import json

import httpx

from cps_compute.storage import HTTPFilesystemAdapter, StorageError

_ARCHIVE_VERIFICATION_FAILED = 'Filesystem archive verification failed; assignment remains pending'


class CourseServiceFilesystemAdapter(HTTPFilesystemAdapter):
    """Keep provisioning on the SDK adapter and archive one trusted group per call."""

    async def archive(self, workspaces, actor, assignment=None):
        if type(workspaces) is not list or not workspaces:
            raise StorageError('Exact retained workspace archive targets required')
        groups = {}
        seen = set()
        for workspace in workspaces:
            if (type(workspace) is not dict or type(workspace.get('id')) is not str
                    or not workspace['id'].strip() or type(workspace.get('group_id')) is not str
                    or not workspace['group_id'].strip() or len(workspace['group_id']) > 256):
                raise StorageError('Exact retained workspace archive targets required')
            identifier = workspace['id']
            if identifier in seen:
                raise StorageError('Unique retained workspace archive targets required')
            seen.add(identifier)
            groups.setdefault(workspace['group_id'], []).append(identifier)

        results = {}
        for group_id, names in groups.items():
            try:
                response = await self.client.post('/internal/v1/workspaces/archive', json={
                    'workspaces': names, 'actor': actor, 'assignment': assignment,
                })
                response.raise_for_status()
            except httpx.HTTPError:
                raise RuntimeError(_ARCHIVE_VERIFICATION_FAILED) from None
            try:
                result = response.json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise RuntimeError(_ARCHIVE_VERIFICATION_FAILED) from None
            if (type(result) is not dict or result.get('read_only_verified') is not True
                    or type(result.get('evidence')) is not dict or not result['evidence']):
                raise RuntimeError(_ARCHIVE_VERIFICATION_FAILED)
            results[group_id] = result

        if len(results) == 1:
            return next(iter(results.values()))
        return {
            'state': 'archived',
            'read_only_verified': True,
            'evidence': {
                'groups': {group_id: result['evidence'] for group_id, result in results.items()},
            },
        }
