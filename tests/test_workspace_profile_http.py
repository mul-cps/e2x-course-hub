import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from tornado import web
from tornado.httpclient import HTTPClientError
from tornado.testing import AsyncHTTPTestCase

from e2x_course_hub.cps.platform_handlers import WorkspaceHandler, default_handlers
from e2x_course_hub.cps.providers import LocalCourseProvider


class WorkspaceProfileHTTPTest(AsyncHTTPTestCase):
    def get_app(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = LocalCourseProvider(Path(self.tmp.name) / 'db', 'cps')
        self.groups = ['lms.course.c.term.t.instructor']
        self.calls = []
        self.failure = None
        self.workspace = {'id': 'w', 'course_id': 'c', 'term_id': 't',
                          'profile': 'cpu-small', 'state': 'stopped'}
        outer = self

        class Hub:
            async def get_user(self, name):
                return {'name': name, 'admin': False, 'groups': outer.groups}

        class AuthenticatedWorkspace(WorkspaceHandler):
            def get_current_user(self):
                name = self.request.headers.get('X-Test-User')
                return {'name': name} if name else None

        class Service:
            async def get(self, identifier):
                if identifier != 'w':
                    raise web.HTTPError(404)
                return dict(outer.workspace)

            async def change_profile(self, identifier, profile, *, actor):
                if outer.failure:
                    raise outer.failure
                if profile not in ('cpu-small', 'cpu-large'):
                    raise ValueError('Unknown profile')
                outer.calls.append(('profile', identifier, profile, actor))
                outer.workspace['profile'] = profile

            async def start(self, identifier, *, actor):
                outer.calls.append(('start', identifier, actor))

            async def close(self, identifier, *, actor):
                outer.calls.append(('stop', identifier, actor))

            async def remove_member(self, identifier, membership_id, *, actor):
                outer.calls.append(('remove-member', identifier, membership_id, actor))

        # Use production route patterns rather than adding a test-only profile route.
        routes = [(pattern, AuthenticatedWorkspace)
                  for pattern, handler in default_handlers if handler is WorkspaceHandler]
        return web.Application(routes, workspace_service=Service(),
            last_config_check=time.time(), course_provider=self.provider,
            api=SimpleNamespace(course_api=SimpleNamespace(hub_api=Hub())),
            login_url='/login', cookie_secret='test')

    def tearDown(self):
        self.provider.db.close()
        self.tmp.cleanup()
        super().tearDown()

    def post(self, body, action='profile', user='alice'):
        return self.fetch('/api/workspaces/w/' + action, method='POST',
                          body=json.dumps(body),
                          headers={'X-Test-User': user} if user else {})

    def test_intentional_start_cancellation_returns_exact_json_detail(self):
        from e2x_course_hub.cps.workspaces import _WorkspaceStartCancelled
        async def cancelled(identifier, *, actor):
            self.assertEqual((identifier, actor), ('w', 'alice'))
            raise _WorkspaceStartCancelled('Workspace start cancelled; observed shutdown required')
        self._app.settings['workspace_service'].start = cancelled
        response = self.post({}, action='start')
        self.assertEqual(response.code, 409)
        self.assertTrue(response.headers['Content-Type'].startswith('application/json'))
        self.assertEqual(json.loads(response.body), {
            'detail': 'Workspace start cancelled; observed shutdown required',
        })

    def failure_response(self, error, *, action='start'):
        async def fail(*args, **kwargs):
            raise error
        method = {'start': 'start', 'stop': 'close', 'profile': 'change_profile',
                  'remove-member': 'remove_member'}[action]
        setattr(self._app.settings['workspace_service'], method, fail)
        body = {'profile': 'cpu-large'} if action == 'profile' else (
            {'membership_id': 'm'} if action == 'remove-member' else {})
        return self.post(body, action=action)

    def test_upstream_conflicts_do_not_claim_entitlement_denial(self):
        for action in ('start', 'stop', 'profile', 'remove-member'):
            with self.subTest(action=action):
                response = self.failure_response(HTTPClientError(409,
                    'private claim token=secret'), action=action)
                self.assertEqual(response.code, 409)
                detail = json.loads(response.body)['detail']
                self.assertIn('could not complete', detail)
                self.assertNotIn('policy', detail.lower())
                self.assertNotIn('secret', detail)
                self.assertNotIn('private', detail)

    def test_upstream_authentication_and_authorization_statuses_are_preserved(self):
        from e2x_course_hub.api.errors import HubAPIError
        for status in (401, 403):
            for wrapped in (False, True):
                with self.subTest(status=status, wrapped=wrapped):
                    error = HTTPClientError(status, 'private token=secret')
                    if wrapped:
                        original = error
                        error = HubAPIError('private token=secret')
                        error.__cause__ = original
                    response = self.failure_response(error)
                    self.assertEqual(response.code, status)
                    detail = json.loads(response.body)['detail']
                    self.assertNotIn('secret', detail)
                    self.assertNotIn('private', detail)

    def test_wrapped_hub_failure_and_timeout_are_sanitized_json(self):
        from e2x_course_hub.api.errors import HubAPIError
        wrapped = HubAPIError('private https://hub.invalid?token=secret')
        wrapped.__cause__ = HTTPClientError(500, 'private token=secret')
        for error, status in ((wrapped, 500),
                              (HubAPIError('private secret'), 502),
                              (TimeoutError('private token=secret'), 409)):
            with self.subTest(error=type(error).__name__):
                response = self.failure_response(error)
                self.assertEqual(response.code, status)
                detail = json.loads(response.body)['detail']
                self.assertIn('workspace', detail.lower())
                self.assertNotIn('secret', detail)
                self.assertNotIn('private', detail)

    def test_native_cleanup_pending_requires_wait_not_start_retry(self):
        from e2x_course_hub.cps.compute import NativeCleanupPending
        response = self.failure_response(NativeCleanupPending('private-workspace', 'secret-attempt'))
        self.assertEqual(response.code, 409)
        detail = json.loads(response.body)['detail']
        self.assertIn('cleanup is pending', detail)
        self.assertIn('Wait', detail)
        self.assertNotIn('retry', detail.lower())
        self.assertNotIn('private', detail)
        self.assertNotIn('secret', detail)

    def test_owned_course_changes_profile_with_authenticated_human_actor(self):
        response = self.post({'profile': 'cpu-large'})
        self.assertEqual(response.code, 200)
        self.assertEqual(json.loads(response.body)['workspace']['profile'], 'cpu-large')
        self.assertEqual(self.calls, [('profile', 'w', 'cpu-large', 'alice')])

    def test_profile_request_requires_exact_string_field(self):
        for body in ({}, {'profile': None}, {'profile': 3}, {'profile': []},
                     {'profile': {}}, [], {'profile': 'cpu-large', 'actor': 'admin'},
                     {'profile': 'cpu-large', 'course_id': 'other'},
                     {'profile': 'cpu-large', 'resources': {'cpu': 100}}):
            with self.subTest(body=body):
                self.assertEqual(self.post(body).code, 400)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.workspace['profile'], 'cpu-small')

    def test_empty_and_malformed_json_are_rejected(self):
        for body in ('', '{invalid'):
            with self.subTest(body=body):
                response = self.fetch('/api/workspaces/w/profile', method='POST',
                    body=body, headers={'X-Test-User': 'alice'})
                self.assertEqual(response.code, 400)
        self.assertEqual(self.calls, [])

    def test_unknown_profile_is_rejected_without_workspace_change(self):
        self.assertEqual(self.post({'profile': 'unknown'}).code, 400)
        self.assertEqual(self.workspace['profile'], 'cpu-small')

    def test_denied_course_and_cross_course_or_term_cannot_change_profile(self):
        for groups in ([], ['lms.course.other.term.t.instructor'],
                       ['lms.course.c.term.other.instructor'],
                       ['lms.course.c.term.t.student']):
            with self.subTest(groups=groups):
                self.groups = groups
                self.assertEqual(self.post({'profile': 'cpu-large'}).code, 403)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.workspace['profile'], 'cpu-small')

    def test_unauthenticated_request_is_denied(self):
        self.assertEqual(self.post({'profile': 'cpu-large'}, user=None).code, 403)
        self.assertEqual(self.calls, [])

    def test_manager_errors_keep_existing_lifecycle_status_mapping(self):
        for error, status in ((PermissionError('read only'), 403),
                              (TimeoutError('pending'), 409),
                              (RuntimeError('unavailable'), 503),
                              (HTTPClientError(403), 403),
                              (httpx.HTTPStatusError('private upstream detail',
                                  request=httpx.Request('POST', 'https://private.invalid'),
                                  response=httpx.Response(403)), 403),
                              (httpx.HTTPStatusError('private upstream detail',
                                  request=httpx.Request('POST', 'https://private.invalid'),
                                  response=httpx.Response(503)), 502)):
            with self.subTest(error=error):
                self.failure = error
                self.assertEqual(self.post({'profile': 'cpu-large'}).code, status)
        self.assertEqual(self.calls, [])

    def assert_failed_start(self, error, status, *, during_registration=False, during_spawn=False,
                            cleanup_error=None):
        from e2x_course_hub.cps.workspaces import WorkspaceService

        async def prepare():
            await self.provider.put('courses', {'id': 'c', 'resource_ceiling': {},
                'workspace_bindings': [{'group_id': 'g', 'hub_user': 'neutral',
                    'hub_server': 'rtc', 'namespace': 'trusted', 'pod': 'preserved'}]}, actor='admin')
            await self.provider.put('groups', {'id': 'g', 'course_id': 'c'}, actor='admin')
            person = '00000000-0000-4000-8000-000000000001'
            await self.provider.link_identities([{'hub': 'cps', 'username': 'student',
                'email': 'student@example.edu', 'person_id': person,
                'administrator_reviewed': True, 'verified': True}], actor='admin')
            await self.provider.put('memberships', {'id': 'm', 'course_id': 'c',
                'group_id': 'g', 'person_id': 'student', 'canonical_person_id': person}, actor='admin')
            await self.provider.put('workspaces', {'id': 'w', 'course_id': 'c',
                'group_id': 'g', 'profile': 'shared-5', 'hub_user': 'neutral',
                'hub_server': 'rtc', 'state': 'stopped'}, actor='admin')

        self.io_loop.run_sync(prepare)
        compute = SimpleNamespace(validate_workspace=AsyncMock(return_value={'policy_hash': 'current'}),
            register_workspace=AsyncMock(return_value={}), release_after_shutdown=AsyncMock(),
            reservation_state=AsyncMock(return_value={'active': True, 'attempt': 'secret-attempt'}))
        hub = SimpleNamespace(assert_stopped=AsyncMock(), start=AsyncMock(),
            sync_group=AsyncMock(), replace_shares=AsyncMock(), stop_confirmed=AsyncMock())
        filesystem = SimpleNamespace(provision=AsyncMock())
        if during_registration:
            compute.register_workspace.side_effect = error
        elif during_spawn:
            hub.start.side_effect = error
            compute.release_after_shutdown.side_effect = cleanup_error
        else:
            filesystem.provision.side_effect = error
        service = WorkspaceService(self.provider, compute, hub, filesystem=filesystem)
        self._app.settings['workspace_service'] = service
        response = self.post({}, action='start')
        self.assertEqual(response.code, status)
        self.assertNotIn(b'private', response.body)
        self.assertNotIn(b'secret', response.body)
        workspace = self.io_loop.run_sync(lambda: service.get('w'))
        if during_spawn:
            self.assertEqual(workspace['state'], 'reconciling' if cleanup_error else 'stopped')
            hub.start.assert_awaited_once()
            hub.stop_confirmed.assert_awaited_once()
            compute.release_after_shutdown.assert_awaited_once()
            hub.replace_shares.assert_not_awaited()
            return response
        self.assertEqual(workspace['state'], 'stopped')
        if during_registration:
            compute.register_workspace.assert_awaited_once()
            filesystem.provision.assert_not_awaited()
        else:
            filesystem.provision.assert_awaited_once()
        hub.start.assert_not_awaited()
        hub.sync_group.assert_not_awaited()
        compute.release_after_shutdown.assert_not_awaited()

    def test_real_failed_start_cleanup_conflict_does_not_become_policy_denial(self):
        response = self.assert_failed_start(TimeoutError('private placement secret'), 409,
            during_spawn=True, cleanup_error=HTTPClientError(409, 'private cleanup secret'))
        self.assertIn('could not complete', json.loads(response.body)['detail'])
        self.assertNotIn('policy', response.body.decode().lower())

    def test_real_failed_start_scoped_cleanup_pending_retains_reconciling(self):
        from e2x_course_hub.cps.compute import NativeCleanupPending
        response = self.assert_failed_start(TimeoutError('private placement secret'), 409,
            during_spawn=True, cleanup_error=NativeCleanupPending('w', 'secret-attempt'))
        self.assertIn('cleanup is pending', json.loads(response.body)['detail'])

    def test_storage_http_conflict_preserves_stopped_lifecycle_and_safe_response(self):
        request = httpx.Request('POST', 'https://private-control.invalid/workspaces?token=secret')
        upstream = httpx.Response(409, request=request, text='private internal diagnostic')
        self.assert_failed_start(httpx.HTTPStatusError('secret private upstream URL',
            request=request, response=upstream), 409)

    def test_registration_connection_refused_preserves_stopped_lifecycle(self):
        self.assert_failed_start(ConnectionRefusedError('private upstream secret'),
                                 503, during_registration=True)

    def test_registration_httpx_connect_error_preserves_stopped_lifecycle(self):
        request = httpx.Request('POST', 'https://private-control.invalid/?token=secret')
        self.assert_failed_start(httpx.ConnectError('private secret', request=request),
                                 503, during_registration=True)

    def test_registration_httpx_read_timeout_preserves_stopped_lifecycle(self):
        request = httpx.Request('POST', 'https://private-control.invalid/?token=secret')
        self.assert_failed_start(httpx.ReadTimeout('private secret', request=request),
                                 503, during_registration=True)

    def test_existing_actions_preserve_payload_and_actor_contract(self):
        self.assertEqual(self.post({}, action='start').code, 200)
        self.assertEqual(self.post({}, action='stop').code, 200)
        self.assertEqual(self.post({'membership_id': 'm'}, action='remove-member').code, 200)
        self.assertEqual(self.calls, [('start', 'w', 'alice'), ('stop', 'w', 'alice'),
                                     ('remove-member', 'w', 'm', 'alice')])
        self.assertEqual(self.post({'profile': 'cpu-large'}, action='start').code, 400)
        self.assertEqual(self.post({}, action='remove-member').code, 400)
