import json
import hashlib
import uuid
from pathlib import Path

import pytest
from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application


def managed_provider(tmp_path):
    from e2x_course_hub.cps.compute import ComputePolicyClient
    from e2x_course_hub.cps.pool_heads import PoolMutationRPCGuard, SOURCE_SCHEMA
    from e2x_course_hub.cps.providers import LocalCourseProvider

    path = tmp_path / 'source.sqlite'
    legacy = LocalCourseProvider(path, 'cps')
    database_id = str(uuid.uuid4())
    origin = 'https://coordinator.example'
    with legacy.db:
        for sql in SOURCE_SCHEMA.values():
            legacy.db.execute(sql)
        legacy.db.execute('INSERT INTO pool_source_authority VALUES(?,?,?,?)',
            ('cps', origin, database_id, str(path.resolve())))
    legacy.db.close()
    client = ComputePolicyClient(origin, 'service-only-secret', 'cps')
    guard = PoolMutationRPCGuard(client, database_id=database_id)
    provider = LocalCourseProvider(path, 'cps', v2_managed=True, pool_guard=guard)

    def no_network(*args, **kwargs):
        raise AssertionError('runtime observation must not call the coordinator')

    client._request = no_network
    return provider, database_id


class RuntimeObservationTests(AsyncHTTPTestCase):
    def get_app(self):
        from e2x_course_hub.cps.runtime_observation import RuntimeObservationHandler
        self.provider, self.database_id = managed_provider(self.tmp_path)
        return Application([
            (r'/internal/v1/runtime-observation', RuntimeObservationHandler),
        ], course_provider=self.provider, console_owner='cps',
            runtime_observation_token='service-only-secret')

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        super().setUp()

    def tearDown(self):
        self.provider.db.close()
        super().tearDown()
        self._tmp.cleanup()

    def test_service_observation_is_bound_to_actual_source_and_redacts_data(self):
        transition = str(uuid.uuid4())
        input_hash = 'sha256:' + 'b' * 64
        with self.provider.db:
            self.provider.db.execute('INSERT INTO pool_source_receipt VALUES(?,?,?)',
                (transition, input_hash, '{"sensitive":"must-not-leak"}'))
        before = self.provider.db.in_transaction
        writes_before = self.provider.db.total_changes
        response = self.fetch('/internal/v1/runtime-observation', headers={
            'Authorization': 'Bearer service-only-secret', 'X-CPS-Source': 'cps'})
        assert response.code == 200
        result = json.loads(response.body)
        assert result['protocol'] == 'e2x-cps-source-runtime-observation/v1'
        assert result['source']['console'] == 'cps'
        assert result['source']['database_id'] == self.database_id
        assert result['writer_guard']['installed'] is True
        assert result['writer_guard']['coordinator']['database_id'] == self.database_id
        assert result['retained']['source_receipt_count'] == 1
        assert result['retained']['hub_operation_count'] == 0
        assert result['retained']['hub_operation_set_sha256'] == 'sha256:' + hashlib.sha256(
            b'e2x-cps-hub-operation-set/v1\0').hexdigest()
        receipt = result['retained']['source_receipts'][0]
        assert receipt['transition_id'] == transition and receipt['input_sha256'] == input_hash
        assert b'must-not-leak' not in response.body
        assert not before and not self.provider.db.in_transaction
        assert self.provider.db.total_changes == writes_before

    def test_browser_admin_or_wrong_console_cannot_use_service_route(self):
        for headers in (
            {'Authorization': 'Bearer browser-admin', 'X-CPS-Source': 'cps'},
            {'Authorization': 'Bearer service-only-secret', 'X-CPS-Source': 'cit'},
        ):
            response = self.fetch('/internal/v1/runtime-observation', headers=headers)
            assert response.code == 403

    def test_unconfigured_route_fails_closed(self):
        self._app.settings['runtime_observation_token'] = ''
        response = self.fetch('/internal/v1/runtime-observation', headers={
            'Authorization': 'Bearer service-only-secret', 'X-CPS-Source': 'cps'},
            method='GET')
        assert response.code == 503

    def test_changed_local_guard_binding_fails_closed(self):
        with self.provider.db:
            self.provider.db.execute('UPDATE pool_source_authority SET database_id=?',
                (str(uuid.uuid4()),))
        response = self.fetch('/internal/v1/runtime-observation', headers={
            'Authorization': 'Bearer service-only-secret', 'X-CPS-Source': 'cps'})
        assert response.code == 503
        assert b'source.sqlite' not in response.body

    def test_complete_receipt_digest_covers_rows_outside_bounded_window(self):
        rows = [(str(uuid.UUID(int=i + 1)), 'sha256:' + format(i + 1, '064x'))
            for i in range(40)]
        with self.provider.db:
            self.provider.db.executemany('INSERT INTO pool_source_receipt VALUES(?,?,?)',
                [(ident, digest, '{}') for ident, digest in rows])
        headers = {'Authorization': 'Bearer service-only-secret', 'X-CPS-Source': 'cps'}
        first = self.fetch('/internal/v1/runtime-observation', headers=headers)
        assert first.code == 200
        before = json.loads(first.body)['retained']
        assert len(before['source_receipts']) == 32
        assert before['truncated'] is True
        assert before['source_receipt_count'] == 40
        encoded = [ident for ident, _ in sorted(rows)]
        expected = hashlib.sha256(b'e2x-cps-source-receipt-set/v1\0')
        for ident in encoded:
            digest = dict(rows)[ident]
            record = json.dumps([ident, digest], ensure_ascii=False,
                separators=(',', ':')).encode('utf-8')
            expected.update(len(record).to_bytes(8, 'big') + record)
        assert before['source_receipt_set_sha256'] == 'sha256:' + expected.hexdigest()
        with self.provider.db:
            self.provider.db.execute('UPDATE pool_source_receipt SET input_sha256=? WHERE transition_id=?',
                ('sha256:' + 'f' * 64, rows[0][0]))
        second = self.fetch('/internal/v1/runtime-observation', headers=headers)
        assert second.code == 200
        after = json.loads(second.body)['retained']
        assert after['source_receipts'] == before['source_receipts']
        assert after['source_receipt_count'] == 40
        assert after['source_receipt_set_sha256'] != before['source_receipt_set_sha256']

    def test_malformed_receipt_outside_window_denies_observation_without_writes(self):
        rows = [(str(uuid.UUID(int=i + 101)), 'sha256:' + format(i + 101, '064x'))
            for i in range(33)]
        with self.provider.db:
            self.provider.db.executemany('INSERT INTO pool_source_receipt VALUES(?,?,?)',
                [(ident, digest, '{}') for ident, digest in rows])
            self.provider.db.execute('UPDATE pool_source_receipt SET input_sha256=? WHERE transition_id=?',
                ('malformed', rows[0][0]))
        writes_before = self.provider.db.total_changes
        response = self.fetch('/internal/v1/runtime-observation', headers={
            'Authorization': 'Bearer service-only-secret', 'X-CPS-Source': 'cps'})
        assert response.code == 503
        assert self.provider.db.total_changes == writes_before
        assert not self.provider.db.in_transaction

    def test_present_journal_without_its_real_owner_fails_closed(self):
        from e2x_course_hub.cps.hub_journal import SOURCE_JOURNAL_SCHEMA
        with self.provider.db:
            for sql in SOURCE_JOURNAL_SCHEMA.values():
                self.provider.db.execute(sql)
        response = self.fetch('/internal/v1/runtime-observation', headers={
            'Authorization': 'Bearer service-only-secret', 'X-CPS-Source': 'cps'})
        assert response.code == 503
        assert not self.provider.db.in_transaction


def test_course_service_registers_the_private_runtime_observation_route():
    from e2x_course_hub.course_service.app import CourseServiceApp
    from e2x_course_hub.cps.runtime_observation import RuntimeObservationHandler

    app = CourseServiceApp()
    app.service_prefix = '/source/'
    app.static_path = '.'
    app.init_handlers()
    matches = [(path, handler) for path, handler, *_ in app.handlers
        if path.endswith('/internal/v1/runtime-observation')]
    assert matches == [('/source/internal/v1/runtime-observation', RuntimeObservationHandler)]
