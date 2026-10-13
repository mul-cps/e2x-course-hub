"""Fresh neutral Hub accounts, with permanent Source ownership receipts.

A POST with an unknown outcome is deliberately not retried by adopting the user.
No failure path removes a Hub identity or its files.
"""
import json
import re
import sqlite3

from ..api.errors import GroupNotFoundError, UserNotFoundError


class WorkspaceAccountAdapter:
    def __init__(self, provider, hub_api, *, group, role='cps-workspace-kernel'):
        self.provider, self.hub = provider, hub_api
        self.group, self.role = group, role

    def _binding(self, workspace):
        fields = ('id', 'course_id', 'term_id', 'group_id', 'hub_user', 'hub_server', 'namespace', 'pod')
        result = {key: workspace.get(key) for key in fields}
        if any(not isinstance(result[key], str) or not re.fullmatch(r'[a-zA-Z0-9_.-]+', result[key])
               for key in fields):
            raise ValueError('Explicit safe Source workspace and Hub/Pod binding required for fresh account creation')
        return {**result, "hub_api_url": self.hub.api_url.rstrip("/"),
                "kernel_group": self.group, "kernel_role": self.role}

    def _row(self, binding):
        rows = self.provider.db.execute(
            'SELECT * FROM workspace_accounts WHERE console=? AND (hub_user=? OR workspace_id=?)',
            (self.provider.console, binding['hub_user'], binding['id'])).fetchall()
        if not rows:
            return None
        if len(rows) != 1 or json.loads(rows[0]['binding']) != binding:
            raise PermissionError('Neutral account is permanently owned by a different workspace binding')
        return dict(rows[0])

    def _model(self, user, binding, *, receipt=None):
        if (not isinstance(user, dict) or user.get('name') != binding['hub_user']
                or user.get('admin') is not False or not isinstance(user.get('created'), str)
                or not user['created'] or not isinstance(user.get('roles'), list)
                or any(not isinstance(role, str) for role in user['roles'])
                or set(user['roles']) - {'user', self.role}
                or not isinstance(user.get('groups'), list)
                or any(not isinstance(group, str) for group in user['groups'])
                or set(user['groups']) - {self.group}
                or not isinstance(user.get('servers'), dict)):
            raise PermissionError('Hub neutral identity model is unavailable, privileged, or not dedicated to this workspace')
        fingerprint = {'name': user['name'], 'created': user['created']}
        if receipt is not None and fingerprint != receipt:
            raise PermissionError('Hub identity was recreated or differs from the successful Source creation receipt')
        for name, server in user['servers'].items():
            if name != binding['hub_server'] or not isinstance(server, dict):
                raise PermissionError('Neutral identity has an unrelated Hub server')
            state = server.get('state', {})
            if (not isinstance(state, dict) or
                    state.get('pod_name', binding['pod']) != binding['pod'] or
                    state.get('namespace', binding['namespace']) != binding['namespace']):
                raise PermissionError('Hub server differs from the owned Source workspace binding')
            if server.get('stopped') is not True or server.get('pending') is not None:
                raise ValueError('Stop the owned workspace before account role reconciliation')
        return fingerprint

    async def ensure(self, workspace, *, actor):
        binding = self._binding(workspace)
        previous = None
        try:
            previous = self._row(binding)
            await self._ensure(binding, previous)
        except Exception as error:
            self.provider.denial(actor, 'workspace_accounts', binding['hub_user'], binding,
                                 'denied' if isinstance(error, PermissionError) else 'failure')
            raise
        with self.provider.db:
            self.provider._audit(actor, 'workspace_accounts', binding['hub_user'],
                                 json.dumps(previous, sort_keys=True) if previous else None,
                                 json.dumps(self._row(binding), sort_keys=True))

    async def _ensure(self, binding, row):
        if (not isinstance(self.group, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]+', self.group)
                or not isinstance(self.role, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]+', self.role)
                or self.role in {'admin', 'user', 'server'}):
            raise RuntimeError('Configure the trusted neutral workspace group and minimum kernel role before provisioning')
        if binding['group_id'] == self.group or any(
                group['id'] == self.group for group in await self.provider.list('groups')):
            raise PermissionError('The operator kernel role group cannot be a course membership group')
        for table in ('email_links', 'reviewed_account_aliases'):
            if self.provider.db.execute('SELECT 1 FROM ' + table + ' WHERE console=? AND username=?',
                                       (self.provider.console, binding['hub_user'])).fetchone():
                raise PermissionError('Neutral owner cannot be an existing person identity')
        if any(binding['hub_user'] in (member.get('person_id'), member.get('canonical_person_id'))
               for member in await self.provider.list('memberships')):
            raise PermissionError('Neutral owner cannot be an existing person identity')
        try:
            group = await self.hub.get_group(self.group)
        except GroupNotFoundError as error:
            raise RuntimeError('Configured neutral kernel group is missing; an operator must provision its role') from error
        if (not isinstance(group, dict) or group.get('name') != self.group
                or group.get('roles') != [self.role]):
            raise RuntimeError('Configured neutral group must expose exactly the minimum kernel role')
        if row is None:
            try:
                await self.hub.get_user(binding['hub_user'])
            except UserNotFoundError:
                pass
            else:
                raise PermissionError('Hub account already exists without a successful Source creation receipt; choose a fresh owner')
            try:
                with self.provider.db:
                    self.provider.db.execute('INSERT INTO workspace_accounts VALUES (?, ?, ?, ?, ?, ?)',
                        (self.provider.console, binding['hub_user'], binding['id'],
                         json.dumps(binding, sort_keys=True), 'intent', None))
            except sqlite3.IntegrityError as error:
                raise PermissionError('Concurrent Source account ownership claim; retry only a completed creation receipt') from error
            created = await self.hub.create_user(binding['hub_user'])
            receipt = self._model(created, binding)
            # Persist evidence of a successful POST before any role grant. A
            # crash before this commit leaves only intent and requires review.
            with self.provider.db:
                self.provider.db.execute('UPDATE workspace_accounts SET state=?, receipt=? WHERE console=? AND hub_user=?',
                    ('created', json.dumps(receipt, sort_keys=True), self.provider.console, binding['hub_user']))
            row = self._row(binding)
        if row['state'] not in ('created', 'ready') or not row['receipt']:
            raise PermissionError('Fresh account creation outcome is ambiguous; preserve the account and request reviewed recovery')
        try:
            user = await self.hub.get_user(binding['hub_user'])
        except UserNotFoundError as error:
            raise PermissionError('Previously created neutral account is missing; automatic recreation is forbidden') from error
        self._model(user, binding, receipt=json.loads(row['receipt']))
        if self.group not in user['groups']:
            await self.hub.add_users_to_group(self.group, [binding['hub_user']])
            user = await self.hub.get_user(binding['hub_user'])
            self._model(user, binding, receipt=json.loads(row['receipt']))
        verified_group = await self.hub.get_group(self.group)
        if (self.group not in user['groups'] or verified_group.get('name') != self.group
                or verified_group.get('roles') != [self.role]
                or binding['hub_user'] not in verified_group.get('users', [])):
            raise RuntimeError('Hub did not grant the configured minimum kernel role group')
        with self.provider.db:
            self.provider.db.execute('UPDATE workspace_accounts SET state=? WHERE console=? AND hub_user=?',
                                    ('ready', self.provider.console, binding['hub_user']))
