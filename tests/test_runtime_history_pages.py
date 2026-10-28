"""Read-only retained-reference paging through the actual private handler."""
import json
import uuid
from urllib.parse import urlencode

import test_runtime_observation as original


class TestRuntimeHistoryPages(original.RuntimeObservationTests):
    def seed(self):
        rows = [(str(uuid.UUID(int=i)), 'sha256:' + format(i, '064x')) for i in range(65, 0, -1)]
        with self.provider.db:
            self.provider.db.executemany('INSERT INTO pool_source_receipt VALUES(?,?,?)',
                [(ident, digest, '{"secret":"never-expose"}') for ident, digest in rows])
        return sorted(rows)

    def request(self, query=None, source='cps'):
        path = '/internal/v1/runtime-observation'
        if query is not None: path += '?' + urlencode(query)
        return self.fetch(path, headers={'Authorization': 'Bearer service-only-secret', 'X-CPS-Source': source})

    def query(self, retained, offset=0):
        return dict(kind='source_receipts', offset=offset,
            expected_count=retained['source_receipt_count'],
            expected_set_sha256=retained['source_receipt_set_sha256'])

    def test_all_pages_are_ordered_complete_read_only_and_do_not_expose_payloads(self):
        rows = self.seed()
        original = json.loads(self.request().body)
        writes = self.provider.db.total_changes
        records, process = [], None
        for offset, length, next_offset in ((0, 32, 32), (32, 32, 64), (64, 1, None)):
            response = self.request(self.query(original['retained'], offset))
            assert response.code == 200
            page = json.loads(response.body)
            assert page['source'] == original['source']
            assert page['writer_guard'] == original['writer_guard']
            assert page['page']['offset'] == offset
            assert len(page['page']['records']) == length
            assert page['page']['next_offset'] == next_offset
            if process is None: process = page['process_id']
            assert page['process_id'] == process and str(uuid.UUID(process)) == process
            records += page['page']['records']
            assert b'never-expose' not in response.body
        assert [(r['transition_id'], r['input_sha256']) for r in records] == rows
        assert self.provider.db.total_changes == writes and not self.provider.db.in_transaction
        assert json.loads(self.request().body) == original  # original no-query API unchanged

    def test_same_count_mutation_between_pages_refuses_original_commitment(self):
        rows = self.seed()
        query = self.query(json.loads(self.request().body)['retained'])
        assert self.request(query).code == 200
        with self.provider.db:
            self.provider.db.execute('UPDATE pool_source_receipt SET input_sha256=? WHERE transition_id=?',
                ('sha256:' + 'f' * 64, rows[0][0]))
        query['offset'] = 32
        assert self.request(query).code == 409
        assert not self.provider.db.in_transaction

    def test_foreign_source_and_noncanonical_page_requests_are_refused(self):
        self.seed()
        query = self.query(json.loads(self.request().body)['retained'])
        assert self.request(query, source='cit').code == 403
        for change in ({'offset': '00'}, {'offset': '-1'}, {'offset': '1'},
                       {'offset': '96'}, {'kind': 'records'}, {'limit': 33},
                       {'expected_count': '065'}):
            assert self.request(dict(query, **change)).code == 400
        duplicate = list(query.items()) + [('offset', '0')]
        assert self.request(duplicate).code == 400

    def test_real_hub_journal_pages_validate_retained_codec_without_exposing_records(self):
        import hashlib
        from cps_compute.native_groups import canonical_sha256
        from e2x_course_hub.cps.hub_journal import SourceHubJournal, SOURCE_JOURNAL_SCHEMA
        pins = dict(console='cps', source_path=str(self.tmp_path / 'source.sqlite'),
            hub_api='https://hub.example/hub/api', coordinator_url='https://coordinator.example',
            coordinator_database_id=self.database_id, installation_id=str(uuid.uuid4()),
            writer_actor='operator', writer_instance=str(uuid.uuid4()))
        with self.provider.db:
            for sql in SOURCE_JOURNAL_SCHEMA.values(): self.provider.db.execute(sql)
            self.provider.db.execute('PRAGMA user_version=6')
            self.provider.db.execute('INSERT INTO hub_journal_authority VALUES(?,?,?,?)',
                (1, 1, json.dumps(pins, sort_keys=True, separators=(',', ':')),
                 'sha256:' + hashlib.sha256(b'').hexdigest()))
        journal = SourceHubJournal(self.provider, **pins)
        self.provider.hub_journal = journal
        preimage = journal.capture_source()
        intent = dict(console='cps', actor='course-admin', hub_api=pins['hub_api'],
            request=dict(action='group-create', name='private-group-name'), before=None,
            source_revision=canonical_sha256(preimage))
        digest = canonical_sha256(dict(operation='admin.hub', payload=intent))
        ids = [str(uuid.UUID(int=i)) for i in range(33, 0, -1)]
        for ident in ids:
            ticket = dict(transition_id=ident, predecessor=None, operation='admin.hub',
                input_sha256=digest, actor='operator', instance=pins['writer_instance'],
                target=None, mutation_result=None, receipt=None, effect_sha256=None,
                database_id=self.database_id)
            journal.prepare(operation_id=ident, input_sha256=digest, intent=intent,
                source_preimage=preimage, hub_preimage=None, actor='course-admin', ticket=ticket)
        original = json.loads(self.request().body)
        retained = original['retained']
        assert retained['hub_operation_count'] == 33
        query = dict(kind='hub_operations', offset=0, expected_count=33,
            expected_set_sha256=retained['hub_operation_set_sha256'])
        writes, records = self.provider.db.total_changes, []
        for offset in (0, 32):
            response = self.request(dict(query, offset=offset))
            assert response.code == 200
            records.extend(json.loads(response.body)['page']['records'])
            assert b'private-group-name' not in response.body and b'source_preimage' not in response.body
        assert records == [dict(operation_id=ident, input_sha256=digest, state='prepared') for ident in sorted(ids)]
        assert self.provider.db.total_changes == writes and not self.provider.db.in_transaction
