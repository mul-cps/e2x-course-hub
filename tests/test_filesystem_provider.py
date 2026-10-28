from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from cps_compute.internal import ControlStore, ServiceIdentity, install_internal
from cps_compute.reservations import Reservations
from cps_compute.storage import HTTPFilesystemAdapter, StorageError, binding
from e2x_course_hub.cps.providers import LocalCourseProvider
from e2x_course_hub.cps.workspaces import WorkspaceService
from e2x_course_hub.cps.filesystem import CourseServiceFilesystemAdapter
from e2x_course_hub.cps.platform_handlers import AssignmentCloseHandler
from tornado import web
from tornado.testing import AsyncHTTPTestCase


@pytest.mark.asyncio
async def test_course_filesystem_adapter_is_noarg_sdk_adapter_and_archives_groups_before_reporting(
        tmp_path, monkeypatch):
    monkeypatch.setenv('CPS_WORKSPACE_CONTROL_URL', 'https://gateway.test:8000')
    monkeypatch.setenv('CPS_WORKSPACE_CONTROL_TOKEN', 'service-token')
    monkeypatch.setenv('CPS_WORKSPACE_CONTROL_CA', '/etc/ssl/certs/ca-certificates.crt')
    store = ControlStore(tmp_path / 'central')
    reservations = Reservations(tmp_path / 'reservations')
    service = ServiceIdentity('console', 'cps')
    for name, group in [('wa', 'ga'), ('wb', 'gb')]:
        store.put_workspace({
            'workspace': name, 'group_id': group, 'owner': 'neutral-' + name,
            'server': 'rtc', 'policy_hash': 'hash',
            'storage': {'pv': 'pv-' + group, 'pvc': 'pvc-' + group,
                        'namespace': 'jupyterhub', 'path': binding('cps', group)[1],
                        'state': 'active'},
        }, service, reservations)
    calls = []

    class Backend:
        async def archive(self, source, workspaces, actor):
            group = workspaces[0]['group_id']
            calls.append((source, group, [item['workspace'] for item in workspaces], actor))
            return {'state': 'archived', 'read_only_verified': True,
                    'evidence': {'group': group}}

    async def auth(token):
        assert token == 'service-token'
        return service

    async def yes(*args):
        return True

    app = FastAPI()
    install_internal(app, store, reservations, auth, yes, SimpleNamespace(hash='hash'),
                     workspace_owner_check=yes, storage_controller=Backend())
    adapter = CourseServiceFilesystemAdapter()
    assert isinstance(adapter, HTTPFilesystemAdapter)
    assert str(adapter.client.base_url) == 'https://gateway.test:8000'
    assert adapter.client.headers['Authorization'] == 'Bearer service-token'
    original = adapter.client
    adapter.client = httpx.AsyncClient(
        base_url='https://gateway.test:8000',
        transport=httpx.ASGITransport(app=app),
        headers={'Authorization': 'Bearer service-token'},
    )
    await original.aclose()
    try:
        result = await adapter.archive([
            {'id': 'wa', 'group_id': 'ga'}, {'id': 'wb', 'group_id': 'gb'},
        ], actor='teacher', assignment='assignment-a')
    finally:
        await adapter.client.aclose()
    assert calls == [('cps', 'ga', ['wa'], 'console'), ('cps', 'gb', ['wb'], 'console')]
    assert result == {'state': 'archived', 'read_only_verified': True,
                      'evidence': {'groups': {'ga': {'group': 'ga'}, 'gb': {'group': 'gb'}}}}


@pytest.mark.asyncio
async def test_course_filesystem_adapter_rejects_partial_group_inventory_without_request():
    class NoRequestClient:
        async def post(self, *args, **kwargs):
            raise AssertionError('incomplete group inventory was sent')

    adapter = CourseServiceFilesystemAdapter.__new__(CourseServiceFilesystemAdapter)
    adapter.client = NoRequestClient()
    with pytest.raises(StorageError, match='Exact retained workspace archive targets'):
        await adapter.archive([
            {'id': 'wa', 'group_id': 'ga'}, {'id': 'wb'},
        ], actor='teacher')


@pytest.mark.asyncio
async def test_workspace_assignment_stays_pending_after_later_group_archive_failure(tmp_path, monkeypatch):
    monkeypatch.setenv('CPS_WORKSPACE_CONTROL_URL', 'https://gateway.test:8000')
    monkeypatch.setenv('CPS_WORKSPACE_CONTROL_TOKEN', 'service-token')
    monkeypatch.setenv('CPS_WORKSPACE_CONTROL_CA', '/etc/ssl/certs/ca-certificates.crt')
    store = ControlStore(tmp_path / 'central')
    reservations = Reservations(tmp_path / 'reservations')
    service_identity = ServiceIdentity('console', 'cps')
    for name, group in [('wa', 'ga'), ('wb', 'gb')]:
        store.put_workspace({
            'workspace': name, 'group_id': group, 'owner': 'neutral-' + name,
            'server': 'rtc', 'policy_hash': 'hash',
            'storage': {'pv': 'pv-' + group, 'pvc': 'pvc-' + group,
                        'namespace': 'jupyterhub', 'path': binding('cps', group)[1],
                        'state': 'active'},
        }, service_identity, reservations)
    calls = []

    class Backend:
        async def archive(self, source, workspaces, actor):
            group = workspaces[0]['group_id']
            calls.append(group)
            if group == 'gb':
                raise RuntimeError('second group archive failed')
            return {'state': 'archived', 'read_only_verified': True,
                    'evidence': {'group': group}}

    async def auth(token):
        return service_identity

    async def yes(*args):
        return True

    app = FastAPI()
    install_internal(app, store, reservations, auth, yes, SimpleNamespace(hash='hash'),
                     workspace_owner_check=yes, storage_controller=Backend())
    adapter = CourseServiceFilesystemAdapter()
    original = adapter.client
    adapter.client = httpx.AsyncClient(
        base_url='https://gateway.test:8000', transport=httpx.ASGITransport(app=app),
        headers={'Authorization': 'Bearer service-token'},
    )
    await original.aclose()

    class Compute:
        async def reservation_state(self, workspace):
            return {}

        async def release_after_shutdown(self, *args, **kwargs):
            return None

    class Hub:
        async def stop_confirmed(self, workspace):
            return None

        async def revoke_shares(self, workspace):
            return None

    provider = LocalCourseProvider(Path(tmp_path) / 'source.sqlite', 'cps')
    try:
        await provider.put('courses', {'id': 'course-a', 'resource_ceiling': {},
                                       'workspace_bindings': []}, actor='admin')
        assignment = await provider.put('assignments', {
            'id': 'assignment-a', 'course_id': 'course-a', 'mode': 'manual',
        }, actor='admin')
        for name, group in [('wa', 'ga'), ('wb', 'gb')]:
            await provider.put('groups', {'id': group, 'course_id': 'course-a',
                                          'assignment_id': 'assignment-a'}, actor='admin')
            await provider.put('workspaces', {'id': name, 'course_id': 'course-a',
                                              'group_id': group, 'hub_user': 'neutral-' + name,
                                              'hub_server': 'rtc', 'profile': 'cpu'}, actor='admin')
        workspace_service = WorkspaceService(provider, Compute(), Hub(), filesystem=adapter)
        with pytest.raises(RuntimeError, match='Filesystem archive verification failed'):
            await workspace_service.close_assignment(assignment, actor='teacher')
        saved_assignment = (await provider.list('assignments'))[0]
        saved_workspaces = await provider.list('workspaces')
        assert calls == ['ga', 'gb']
        assert store.workspace('cps', 'wa')['storage']['state'] == 'archived'
        assert saved_assignment['archive_pending'] is True
        assert saved_assignment.get('archived', False) is False
        assert all(item['archive_pending'] is True and not item.get('archived', False)
                   for item in saved_workspaces)
    finally:
        await adapter.client.aclose()
        provider.db.close()


class AssignmentCloseArchiveFailureRouteTest(AsyncHTTPTestCase):
    def get_app(self):
        outer = self
        self.assignment = {'id': 'assignment-a', 'course_id': 'course-a', 'source': 'local'}
        self.provider = SimpleNamespace(list=self.list_assignments)
        self.adapter = CourseServiceFilesystemAdapter.__new__(CourseServiceFilesystemAdapter)

        class CloseService:
            async def close_assignment(inner, assignment, *, actor):
                assignment['archive_pending'] = True
                await outer.adapter.archive([{'id': 'workspace-a', 'group_id': 'group-a'}],
                                            actor=actor, assignment=assignment)
                assignment['archive_pending'] = False
                assignment['archived'] = True
                return assignment

        class Handler(AssignmentCloseHandler):
            def get_current_user(inner):
                return {'name': 'teacher'}

            async def authorize(inner, *args, **kwargs):
                return 'teacher'

            def on_finish(inner):
                pass

        return web.Application(
            [(r'/api/assignments/([^/]+)/close', Handler)],
            course_provider=self.provider,
            workspace_service=CloseService(),
            cookie_secret='test-secret',
            last_config_check=10**12,
        )

    async def list_assignments(self, kind):
        assert kind == 'assignments'
        return [self.assignment]

    def test_http_409_transport_and_unverified_proof_return_503_pending(self):
        class Client:
            def __init__(self, failure):
                self.failure = failure

            async def post(self, *args, **kwargs):
                request = httpx.Request('POST', 'https://gateway.test/archive')
                if self.failure == 'http-409':
                    response = httpx.Response(409, request=request)
                    raise httpx.HTTPStatusError('private backend detail', request=request,
                                                response=response)
                if self.failure == 'transport':
                    raise httpx.ConnectError('private transport detail', request=request)
                if self.failure == 'unverified':
                    body = {'read_only_verified': False, 'evidence': {'hash': 'x'}}
                    return httpx.Response(200, json=body, request=request)
                if self.failure == 'malformed-proof':
                    return httpx.Response(200, json={'read_only_verified': True, 'evidence': []},
                                          request=request)
                return httpx.Response(200, content=b'not-json', request=request)

        for failure in ('http-409', 'transport', 'unverified', 'malformed-proof', 'malformed-json'):
            self.assignment.clear()
            self.assignment.update({'id': 'assignment-a', 'course_id': 'course-a', 'source': 'local'})
            self.adapter.client = Client(failure)
            response = self.fetch(f'/api/assignments/assignment-a/close', method='POST', body='{}')
            self.assertEqual(response.code, 503, failure)
            self.assertIn('Filesystem archive verification failed', response.body.decode(), failure)
            self.assertNotIn('private', response.body.decode(), failure)
            self.assertTrue(self.assignment['archive_pending'], failure)
            self.assertFalse(self.assignment.get('archived', False), failure)
