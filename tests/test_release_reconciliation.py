"""Source Stop reconciles only the exact completed concurrent-finalizer outcome."""
import asyncio
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tornado.httpclient import HTTPClientError, HTTPRequest, HTTPResponse
from e2x_course_hub.cps.compute import ComputePolicyClient, is_shutdown_conflict
from e2x_course_hub.cps.providers import LocalCourseProvider
from e2x_course_hub.cps.workspaces import HubWorkspaceAdapter, WorkspaceService


def conflict(detail='Shutdown unconfirmed or invalid release', status=409, raw=None):
    response = HTTPResponse(HTTPRequest('https://gateway.invalid/internal/v1/reservations/release'),
        status, buffer=io.BytesIO(json.dumps({'detail': detail}).encode() if raw is None else raw))
    return HTTPClientError(status, response=response)


class ReleaseRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = LocalCourseProvider(Path(self.tmp.name) / 'source.sqlite', 'cps')
        await self.provider.put('courses', {'id': 'course'}, actor='operator')
        await self.provider.put('groups', {'id': 'group', 'course_id': 'course'}, actor='operator')
        await self.provider.put('workspaces', {'id': 'w', 'course_id': 'course', 'group_id': 'group',
            'hub_user': 'owner', 'hub_server': 'rtc', 'profile': 'interactive-shared-5',
            'state': 'running', 'namespace': 'jupyterhub', 'pod': 'exact-pod'}, actor='operator')
        self.user = {'name': 'owner', 'created': '2026-11-16T12:00:00Z',
                     'servers': {'rtc': {'name': 'rtc', 'ready': True, 'pending': None}}}
        self.reservation = {'workspace': 'w', 'active': True, 'attempt': 'attempt-1'}
        self.release_entered = asyncio.Event()
        self.finalizer_finished = asyncio.Event()
        self.calls = []
        self.release_error = conflict()
        self.finalizer_change = None
        self.reservation_error = None
        self.user_error = None
        self.after_reservation = None
        self.on_revoke = None
        outer = self

        class HubAPI:
            api_url = 'https://hub.invalid/hub/api'
            async def request(self, url, method):
                outer.calls.append(('hub', method, url))
                if method == 'DELETE' and '/users/' in url:
                    outer.user['servers'] = {}
                if '/shares/' in url and outer.on_revoke:
                    await outer.on_revoke()
            async def get_user(self, name):
                outer.calls.append(('hub_user', name))
                if outer.user_error is not None:
                    error, outer.user_error = outer.user_error, None
                    raise error
                return copy.deepcopy(outer.user)

        class GatewayHTTP:
            async def fetch(self, request):
                if request.method == 'GET':
                    outer.calls.append(('reservation', copy.deepcopy(outer.reservation)))
                    if outer.reservation_error is not None:
                        error, outer.reservation_error = outer.reservation_error, None
                        raise error
                    body = json.dumps(outer.reservation).encode()
                    if outer.after_reservation:
                        await outer.after_reservation()
                    return SimpleNamespace(body=body)
                outer.calls.append(('release', json.loads(request.body)))
                outer.release_entered.set()
                await outer.finalizer_finished.wait()
                if outer.release_error is not None:
                    raise outer.release_error
                return SimpleNamespace(body=b'{"released":true}')

        self.transport = patch('e2x_course_hub.cps.compute.AsyncHTTPClient', return_value=GatewayHTTP())
        self.transport.start()
        self.hub = HubWorkspaceAdapter(HubAPI())
        self.service = WorkspaceService(self.provider,
            ComputePolicyClient('https://gateway.invalid', 'test-token', 'cps'), self.hub)

    async def asyncTearDown(self):
        self.transport.stop()
        self.provider.db.close()
        self.tmp.cleanup()

    async def finish_from_hub(self):
        await self.release_entered.wait()
        self.reservation = {'workspace': 'w', 'active': False, 'attempt': None}
        if self.finalizer_change is not None:
            await self.finalizer_change()
        self.finalizer_finished.set()

    async def test_concurrent_hub_finalizer_release_completes_normal_stop_without_retry(self):
        finalizer = asyncio.create_task(self.finish_from_hub())
        try:
            with patch('e2x_course_hub.cps.workspaces.asyncio.sleep', side_effect=AssertionError('No generic409 polling')):
                await self.service.close('w', actor='operator')
        finally:
            await finalizer
        self.assertEqual((await self.service.get('w'))['state'], 'stopped')
        self.assertEqual([x[1] for x in self.calls if x[0] == 'release'],
                         [{'workspace': 'w', 'attempt': 'attempt-1'}])
        self.assertIn(('hub', 'DELETE', 'https://hub.invalid/hub/api/shares/owner/rtc'), self.calls)

    async def stop_with_finalizer(self):
        self.release_entered.clear()
        self.finalizer_finished.clear()
        task = asyncio.create_task(self.finish_from_hub())
        try:
            await self.service.close('w', actor='operator')
        finally:
            await task

    def assert_not_closed_or_revoked(self):
        self.assertFalse(any(x[0] == 'hub' and '/shares/' in x[2] for x in self.calls))
        self.assertEqual(sum(x[0] == 'release' for x in self.calls), 1)

    async def test_active_original_attempt_is_not_reconciled(self):
        async def active():
            self.reservation = {'workspace': 'w', 'active': True, 'attempt': 'attempt-1'}
        self.finalizer_change = active
        with self.assertRaises(HTTPClientError): await self.stop_with_finalizer()
        self.assert_not_closed_or_revoked()
        self.assertEqual((await self.service.get('w'))['state'], 'running')

    async def test_new_attempt_is_not_reconciled(self):
        async def replacement():
            self.reservation = {'workspace': 'w', 'active': True, 'attempt': 'new-attempt'}
        self.finalizer_change = replacement
        with self.assertRaises(HTTPClientError): await self.stop_with_finalizer()
        self.assert_not_closed_or_revoked()

    async def test_malformed_inactive_state_is_not_reconciled(self):
        for value in (None, {}, {'workspace': 'foreign', 'active': False, 'attempt': None},
                      {'workspace': 'w', 'active': 0, 'attempt': None},
                      {'workspace': 'w', 'active': False, 'attempt': 'attempt-1'},
                      {'workspace': 'w', 'active': False, 'attempt': None, 'extra': 1}):
            with self.subTest(value=value):
                self.calls.clear()
                self.reservation = {'workspace': 'w', 'active': True, 'attempt': 'attempt-1'}
                async def malformed(): self.reservation = value
                self.finalizer_change = malformed
                with self.assertRaises(HTTPClientError): await self.stop_with_finalizer()
                self.assert_not_closed_or_revoked()

    async def test_workspace_replacement_during_release_is_not_closed(self):
        async def replacement():
            row = await self.service.get('w')
            await self.provider.put('workspaces', {**row, 'hub_user': 'new-owner'}, actor='other')
        self.finalizer_change = replacement
        with self.assertRaises(ValueError): await self.stop_with_finalizer()
        self.assert_not_closed_or_revoked()
        self.assertEqual((await self.service.get('w'))['hub_user'], 'new-owner')

    async def test_same_username_new_incarnation_is_not_reconciled(self):
        async def replacement(): self.user['created'] = '2026-11-18T00:00:00Z'
        self.finalizer_change = replacement
        with self.assertRaises(ValueError): await self.stop_with_finalizer()
        self.assert_not_closed_or_revoked()

    async def test_pending_or_malformed_fresh_hub_state_is_not_absence(self):
        for state in ({'rtc': {'pending': 'spawn'}}, {'rtc': None}, None, []):
            with self.subTest(state=state):
                self.calls.clear()
                self.user['servers'] = {}
                self.reservation = {'workspace': 'w', 'active': True, 'attempt': 'attempt-1'}
                async def replacement(): self.user['servers'] = state
                self.finalizer_change = replacement
                with self.assertRaises(ValueError): await self.stop_with_finalizer()
                self.assert_not_closed_or_revoked()

    async def test_new_attempt_between_readbacks_is_not_reconciled(self):
        async def change():
            if not self.reservation['active']:
                self.reservation = {'workspace': 'w', 'active': True, 'attempt': 'new-attempt'}
        self.after_reservation = change
        with self.assertRaises(HTTPClientError): await self.stop_with_finalizer()
        self.assert_not_closed_or_revoked()

    async def test_workspace_change_after_final_observation_is_not_revoked(self):
        count = 0
        async def change():
            nonlocal count
            if not self.reservation['active']:
                count += 1
                if count == 2:
                    row = await self.service.get('w')
                    await self.provider.put('workspaces', {**row, 'profile': 'new-profile'}, actor='other')
        self.after_reservation = change
        with self.assertRaises(ValueError): await self.stop_with_finalizer()
        self.assert_not_closed_or_revoked()
        self.assertEqual((await self.service.get('w'))['profile'], 'new-profile')

    async def test_local_commit_cas_refuses_new_course_lifecycle(self):
        async def change():
            await self.provider.put('courses', {'id': 'course', 'new': True}, actor='other')
        self.on_revoke = change
        with self.assertRaises(ValueError): await self.stop_with_finalizer()
        self.assertEqual((await self.service.get('w'))['state'], 'running')

    async def test_changed_workspace_before_local_close_is_not_overwritten(self):
        async def change():
            row = await self.service.get('w')
            await self.provider.put('workspaces', {**row, 'pod': 'new-pod'}, actor='other')
        self.on_revoke = change
        with self.assertRaises(ValueError): await self.stop_with_finalizer()
        self.assertEqual((await self.service.get('w'))['pod'], 'new-pod')
        self.assertEqual((await self.service.get('w'))['state'], 'running')

    async def test_optional_owner_observation_failure_preserves_successful_stop(self):
        self.user_error = HTTPClientError(503)
        self.release_error = None
        await self.stop_with_finalizer()
        self.assertEqual((await self.service.get('w'))['state'], 'stopped')

    async def test_existing_mandatory_reservation_failure_aborts_before_hub_stop(self):
        original = HTTPClientError(599)
        self.reservation_error = original
        self.release_error = None
        with self.assertRaises(HTTPClientError) as raised:
            await self.service.close('w', actor='operator')
        self.assertIs(raised.exception, original)
        self.assertEqual([x[0] for x in self.calls], ['reservation'])
        self.assertEqual((await self.service.get('w'))['state'], 'running')

    async def test_existing_malformed_reservation_does_not_submit_release(self):
        self.reservation = None
        self.release_error = None
        with self.assertRaises(AttributeError):
            await self.service.close('w', actor='operator')
        self.assertEqual(sum(x[0] == 'release' for x in self.calls), 0)
        self.assertEqual((await self.service.get('w'))['state'], 'running')

    async def test_failed_optional_owner_read_cannot_reconcile(self):
        self.user_error = HTTPClientError(503)
        with self.assertRaises(HTTPClientError) as raised: await self.stop_with_finalizer()
        self.assertIs(raised.exception, self.release_error)
        self.assert_not_closed_or_revoked()

    async def test_untrusted_owner_cannot_reconcile(self):
        self.user['created'] = 'not-a-timestamp'
        with self.assertRaises(HTTPClientError): await self.stop_with_finalizer()
        self.assert_not_closed_or_revoked()

    async def test_unknown_conflict_auth_and_transport_cannot_reconcile(self):
        for error in (conflict('another conflict'), conflict(status=403), HTTPClientError(599)):
            with self.subTest(code=error.code):
                self.calls.clear()
                self.reservation = {'workspace': 'w', 'active': True, 'attempt': 'attempt-1'}
                self.release_error = error
                with self.assertRaises(HTTPClientError) as raised: await self.stop_with_finalizer()
                self.assertIs(raised.exception, error)
                self.assert_not_closed_or_revoked()

    async def test_malformed_pre_stop_context_does_not_enable_reconciliation(self):
        for value in ({}, {'workspace': 'foreign', 'active': True, 'attempt': 'attempt-1'},
                      {'workspace': 'w', 'active': 1, 'attempt': 'attempt-1'},
                      {'workspace': 'w', 'active': True, 'attempt': ''}):
            with self.subTest(value=value):
                self.calls.clear()
                self.reservation = value
                with self.assertRaises(HTTPClientError): await self.stop_with_finalizer()
                self.assert_not_closed_or_revoked()

    async def test_initial_inactive_cpu_stop_needs_no_owner_observation(self):
        self.reservation = {'workspace': 'w', 'active': False, 'attempt': None}
        self.release_error = None
        await self.stop_with_finalizer()
        self.assertEqual(sum(x[0] == 'hub_user' for x in self.calls), 1)
        self.assertEqual((await self.service.get('w'))['state'], 'stopped')

    async def test_fresh_owner_auth_or_transport_failure_refuses_cleanup(self):
        for error in (HTTPClientError(403), HTTPClientError(599)):
            with self.subTest(code=error.code):
                self.calls.clear()
                self.reservation = {'workspace': 'w', 'active': True, 'attempt': 'attempt-1'}
                async def unavailable(): self.user_error = error
                self.finalizer_change = unavailable
                with self.assertRaises(HTTPClientError) as raised: await self.stop_with_finalizer()
                self.assertIs(raised.exception, error)
                self.assert_not_closed_or_revoked()

    async def test_fresh_reservation_auth_or_transport_failure_refuses_cleanup(self):
        for error in (HTTPClientError(403), HTTPClientError(599)):
            with self.subTest(code=error.code):
                self.calls.clear()
                self.reservation = {'workspace': 'w', 'active': True, 'attempt': 'attempt-1'}
                async def unavailable(): self.reservation_error = error
                self.finalizer_change = unavailable
                with self.assertRaises(HTTPClientError) as raised: await self.stop_with_finalizer()
                self.assertIs(raised.exception, error)
                self.assert_not_closed_or_revoked()

    async def test_wrong_owner_name_refuses_cleanup(self):
        async def wrong(): self.user['name'] = 'different-owner'
        self.finalizer_change = wrong
        with self.assertRaises(ValueError): await self.stop_with_finalizer()
        self.assert_not_closed_or_revoked()

    async def test_context_deepcopy_does_not_follow_shared_mutable_workspace(self):
        shared = await self.service.get('w')
        original_get = self.service.get
        async def shared_get(identifier): return shared
        self.service.get = shared_get
        async def changed(): shared['hub_user'] = 'new-owner'
        self.finalizer_change = changed
        try:
            with self.assertRaises(ValueError): await self.stop_with_finalizer()
        finally:
            self.service.get = original_get
        self.assert_not_closed_or_revoked()


class ExactConflictTests(unittest.TestCase):
    def test_only_exact_generic_release_conflict_is_classified(self):
        self.assertTrue(is_shutdown_conflict(conflict()))
        for error in (conflict('different'), conflict(status=400), conflict(status=403),
                      conflict(raw=b'{"detail":"Shutdown unconfirmed or invalid release","extra":1}'),
                      conflict(raw=b'{"detail":null,"detail":"Shutdown unconfirmed or invalid release"}'),
                      conflict(raw=b'{"detail":NaN}'), conflict(raw=b'\xff'),
                      conflict(raw=b' '*8193 + b'{"detail":"Shutdown unconfirmed or invalid release"}'),
                      conflict(raw=b'{"detail":"Shutdown unconfirmed or invalid release"'),
                      HTTPClientError(599)):
            with self.subTest(error=error): self.assertFalse(is_shutdown_conflict(error))
