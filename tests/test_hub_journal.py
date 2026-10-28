"""Persistent internal journal: custody, retries and atomic local completion."""
import copy
import asyncio
import hashlib
import importlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cps_compute.native_groups import canonical_sha256
from cps_compute.native_pool_hub_effects import validate_effect_record
from e2x_course_hub.cps.pool_heads import SOURCE_SCHEMA
from e2x_course_hub.cps.providers import LocalCourseProvider

DB = '11111111-1111-4111-8111-111111111111'
INSTALLATION = '22222222-2222-4222-8222-222222222222'
INSTANCE = '33333333-3333-4333-8333-333333333333'
OP = '44444444-4444-4444-8444-444444444444'
HUB = 'https://hub.example/hub/api'
ORIGIN = 'https://coordinator.example'


def module():
    try:
        return importlib.import_module('e2x_course_hub.cps.hub_journal')
    except ImportError:
        raise AssertionError('Durable Source journal building block missing') from None


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def empty_manifest():
    return 'sha256:' + hashlib.sha256(b'').hexdigest()


class HubJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'source.sqlite'
        self.provider = LocalCourseProvider(self.path, 'cps')
        self.connections = [self.provider.db]
        self.addCleanup(lambda: [db.close() for db in self.connections])
        with self.provider.db:
            self.provider.db.execute('INSERT INTO records VALUES(?,?,?,?)',
                ('cps', 'courses', 'course', '{"id":"course","name":"Original"}'))
            self.provider.db.execute('INSERT INTO email_links VALUES(?,?,?,?,?)',
                ('cps', 'student', 'student@example.edu', DB, 1))
            self.provider.db.execute('INSERT INTO audit VALUES(?,?,?,?,?,?,?,?)',
                ('seed', 'cps', 'courses', 'course', None, '{}', 'seed-time', 'success'))
            for sql in SOURCE_SCHEMA.values():
                self.provider.db.execute(sql)
            self.provider.db.execute('INSERT INTO pool_source_authority VALUES(?,?,?,?)',
                ('cps', ORIGIN, DB, str(self.path.resolve())))
            self.provider.db.execute('INSERT INTO pool_source_receipt VALUES(?,?,?)',
                ('prior', 'prior-hash', '{"prior":true}'))
        self.pins = dict(console='cps', source_path=str(self.path.resolve()), hub_api=HUB,
            coordinator_url=ORIGIN, coordinator_database_id=DB, installation_id=INSTALLATION,
            writer_actor='operator', writer_instance=INSTANCE)

    def provision(self):
        m = module()
        # Test-only explicit creation; production has no initializer or migration.
        with self.provider.db:
            for sql in m.SOURCE_JOURNAL_SCHEMA.values():
                self.provider.db.execute(sql)
            self.provider.db.execute('PRAGMA user_version=6')
            binding = {key: value for key, value in self.pins.items()}
            self.provider.db.execute('INSERT INTO hub_journal_authority VALUES(?,?,?,?)',
                (1, 1, canonical(binding), empty_manifest()))
        self.journal = m.SourceHubJournal(self.provider, **self.pins)
        self.preimage = self.journal.capture_source()
        before = dict(name='team', users=['student'], roles=[], properties_sha256=canonical_sha256({}))
        self.intent = dict(console='cps', actor='course-admin', hub_api=HUB,
            request=dict(action='group-users-add', name='team', users=['teacher']),
            before=before, source_revision=canonical_sha256(self.preimage))
        self.input = canonical_sha256(dict(operation='admin.hub', payload=self.intent))
        self.ticket = dict(transition_id=OP, predecessor=None, operation='admin.hub',
            input_sha256=self.input, actor='operator', instance=INSTANCE,
            target=None, mutation_result=None, receipt=None, effect_sha256=None, database_id=DB)
        self.args = dict(operation_id=OP, input_sha256=self.input, intent=self.intent,
            source_preimage=self.preimage, hub_preimage=before, actor='course-admin', ticket=self.ticket)
        self.effect = dict(protocol='cps-native-hub-effect/v1', version=1, transition_id=OP,
            actor='operator', instance=INSTANCE, input_sha256=self.input, intent=self.intent)
        self.effect = validate_effect_record(self.effect, transition_id=OP, actor='operator',
            instance=INSTANCE, input_sha256=self.input)
        self.effect_hash = canonical_sha256(self.effect)
        after = dict(before, users=['student', 'teacher'])
        self.receipt = dict(protocol='cps-hub-permission-receipt/v1', version=1,
            transition_id=OP, input_sha256=self.input, effect_sha256=self.effect_hash,
            console='cps', service='cps-admin', actor='course-admin',
            result=dict(action='group-users-add', before=before, after=after))
        return self.journal

    def arm(self):
        return self.journal.arm(OP, self.input, effect=self.effect, effect_sha256=self.effect_hash)

    def complete(self):
        return self.journal.complete(OP, self.input, receipt=self.receipt)

    def source_tables(self):
        return {name: [tuple(r) for r in self.provider.db.execute('SELECT * FROM '+name)]
            for name in ('records', 'email_links', 'audit', 'pool_source_receipt')}

    def reopen(self):
        # provider v5 deliberately cannot open v6 yet; reuse its exact provider
        # object with a new real connection, never bypass constructor in runtime.
        db = sqlite3.connect(str(self.path))
        db.row_factory = sqlite3.Row
        self.connections.append(db)
        self.provider.db = db
        return module().SourceHubJournal(self.provider, **self.pins)

    def test_missing_schema_never_initializes_or_adopts(self):
        before = self.source_tables()
        with self.assertRaises(ValueError):
            module().SourceHubJournal(self.provider, **self.pins)
        self.assertEqual(self.source_tables(), before)
        self.assertEqual(self.provider.db.execute('PRAGMA user_version').fetchone()[0], 5)

    def test_transitions_detached_exact_retries_and_atomic_local_evidence(self):
        journal = self.provision()
        original = self.source_tables()
        prepared = journal.prepare(**self.args)
        self.assertEqual(prepared['state'], 'prepared')
        prepared['intent']['request']['users'].append('intruder')
        self.assertEqual(journal.prepare(**self.args)['intent'], self.intent)
        armed = self.arm()
        self.assertEqual(armed['state'], 'armed')
        self.assertEqual(self.arm(), armed)
        completed = self.complete()
        self.assertEqual(completed['state'], 'completed')
        self.assertEqual(completed['receipt'], self.receipt)
        completed['receipt']['result']['after']['users'].clear()
        self.assertEqual(self.complete()['receipt'], self.receipt)
        now = self.source_tables()
        self.assertEqual(now['records'], original['records'])
        self.assertEqual(now['email_links'], original['email_links'])
        self.assertEqual(now['audit'][:-1], original['audit'])
        self.assertEqual(now['audit'][-1][0:4], ('course-admin', 'cps', 'admin.hub', 'team'))
        self.assertEqual(now['pool_source_receipt'][:-1], original['pool_source_receipt'])
        self.assertEqual(json.loads(now['pool_source_receipt'][-1][2]), self.receipt)
        self.assertEqual(len(now['audit']), len(original['audit'])+1)

    def test_reopen_lost_response_and_old_completion_preserve_later_source_edit(self):
        journal = self.provision()
        for state, action in [('prepared', lambda: journal.prepare(**self.args)),
                              ('armed', self.arm), ('completed', self.complete)]:
            action()
            self.journal = journal = self.reopen()
            self.assertEqual(journal.read(OP, self.input)['state'], state)
        with self.provider.db:
            self.provider.db.execute("UPDATE records SET payload=? WHERE id='course'", ('{"name":"Later"}',))
        before = self.source_tables()
        self.assertEqual(journal.prepare(**self.args)['state'], 'completed')
        self.assertEqual(self.complete()['receipt'], self.receipt)
        self.assertEqual(self.source_tables(), before)
        with self.assertRaises(ValueError):
            self.arm()

    def test_complete_before_arm_and_changed_arguments_are_denied(self):
        journal = self.provision()
        journal.prepare(**self.args)
        with self.assertRaises(ValueError): self.complete()
        changes = [('input_sha256', 'sha256:'+'f'*64), ('actor', 'another'),
            ('source_preimage', {}), ('hub_preimage', None)]
        for key, value in changes:
            with self.subTest(key=key), self.assertRaises(ValueError):
                journal.prepare(**dict(self.args, **{key:value}))
        for key, value in [('operation', 'admin.record.put'), ('transition_id', DB),
                           ('database_id', INSTALLATION), ('input_sha256', 'sha256:'+'0'*64),
                           ('actor', 'foreign'), ('instance', INSTALLATION), ('receipt', {})]:
            ticket = dict(self.ticket, **{key:value})
            with self.subTest(ticket=key), self.assertRaises(ValueError):
                journal.prepare(**dict(self.args, ticket=ticket))
        changed = copy.deepcopy(self.effect)
        changed['intent']['actor'] = 'another'
        with self.assertRaises(ValueError):
            journal.arm(OP, self.input, effect=changed, effect_sha256=canonical_sha256(changed))
        with self.assertRaises(ValueError):
            journal.arm(OP, self.input, effect=self.effect, effect_sha256='sha256:'+'0'*64)
        self.arm()
        for key in self.receipt:
            changed = dict(self.receipt, **{key:None})
            with self.subTest(receipt=key), self.assertRaises(ValueError):
                journal.complete(OP, self.input, receipt=changed)
        self.assertEqual(journal.read(OP, self.input)['state'], 'armed')

    def test_dirty_connection_is_untouched_for_all_calls(self):
        journal = self.provision()
        journal.prepare(**self.args)
        self.provider.db.execute("UPDATE records SET payload='pending'")
        before = self.provider.db.total_changes
        for call in (lambda: journal.read(OP, self.input), journal.capture_source,
                     lambda: journal.prepare(**self.args), self.arm, self.complete,
                     lambda: module().SourceHubJournal(self.provider, **self.pins)):
            with self.assertRaises(ValueError): call()
            self.assertTrue(self.provider.db.in_transaction)
            self.assertEqual(self.provider.db.total_changes, before)
            self.assertEqual(self.provider.db.execute('SELECT payload FROM records').fetchone()[0], 'pending')
        self.provider.db.rollback()

    def test_completion_failure_rolls_back_journal_receipt_and_audit_only(self):
        journal = self.provision()
        journal.prepare(**self.args)
        self.arm()
        before = self.source_tables()
        self.provider.db.execute("CREATE TRIGGER fail_completion BEFORE UPDATE ON hub_journal_authority BEGIN SELECT RAISE(ABORT,'private-token'); END")
        with self.assertRaises(ValueError) as raised: self.complete()
        self.assertNotIn('private-token', str(raised.exception))
        self.assertFalse(self.provider.db.in_transaction)
        self.assertEqual(self.source_tables(), before)
        self.assertEqual(journal.read(OP, self.input)['state'], 'armed')
        self.provider.db.execute('DROP TRIGGER fail_completion')
        self.assertEqual(self.complete()['state'], 'completed')

    def test_concurrent_exact_once_on_separate_persistent_connections(self):
        journal = self.provision()
        args, pins, path = copy.deepcopy(self.args), self.pins.copy(), str(self.path)
        receipt, input_hash = self.receipt, self.input
        # Prepare and arm concurrency independently; backward arm after a peer
        # completes is intentionally forbidden, so race completion only here.
        journal.prepare(**args)
        self.arm()
        def finish(_):
            db = sqlite3.connect(path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                provider = LocalCourseProvider.__new__(LocalCourseProvider)
                provider.db, provider.console = db, 'cps'
                j = module().SourceHubJournal(provider, **pins)
                return j.complete(OP, input_hash, receipt=receipt)
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(finish, range(8)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.provider.db.execute('SELECT count(*) FROM audit').fetchone()[0], 2)
        self.assertEqual(self.provider.db.execute('SELECT count(*) FROM pool_source_receipt').fetchone()[0], 2)

    def test_capture_returns_detached_column_metadata(self):
        journal = self.provision()
        snapshot = journal.capture_source()
        snapshot['records']['columns'].clear()
        snapshot['records']['rows'][0][-1] = 'changed-by-caller'
        self.assertEqual(journal.capture_source(), self.preimage)

    def test_actual_source_changes_block_each_new_transition_without_writes(self):
        journal = self.provision()
        db = self.provider.db
        for stage in ('prepare', 'arm', 'complete'):
            with self.subTest(stage=stage):
                original = db.execute('SELECT payload FROM records').fetchone()[0]
                with db:
                    db.execute('UPDATE records SET payload=?', ('{"id":"course","name":"Concurrent"}',))
                before = self.source_tables()
                action = {'prepare':lambda: journal.prepare(**self.args), 'arm':self.arm, 'complete':self.complete}[stage]
                with self.assertRaises(ValueError): action()
                self.assertEqual(self.source_tables(), before)
                with db:
                    db.execute('UPDATE records SET payload=?', (original,))
                action()

    def test_foreign_context_pins_and_connection_path_cannot_adopt_custody(self):
        self.provision()
        changes = dict(console='cit', hub_api='https://other.example/hub/api',
            coordinator_url='https://other.example', coordinator_database_id=INSTALLATION,
            installation_id=DB, writer_actor='foreign', writer_instance=DB,
            source_path=str(Path(self.tmp.name) / 'absent.sqlite'))
        for key, value in changes.items():
            with self.subTest(pin=key), self.assertRaises(ValueError):
                module().SourceHubJournal(self.provider, **dict(self.pins, **{key:value}))
        self.provider.db.close()
        copied = Path(self.tmp.name) / 'copied.sqlite'
        copied.write_bytes(self.path.read_bytes())
        self.provider.db = sqlite3.connect(str(copied))
        self.connections.append(self.provider.db)
        with self.assertRaises(ValueError):
            module().SourceHubJournal(self.provider, **dict(self.pins, source_path=str(copied.resolve())))

    def test_untrusted_urls_actions_actor_source_and_receipt_results_are_rejected(self):
        journal = self.provision()
        before = self.source_tables()
        for action in ('user-create', 'users-create', 'shares-grant', 'roles-add', 'server-start'):
            plan = copy.deepcopy(self.intent)
            plan['request'] = dict(action=action, name='team')
            plan['before'] = None
            args = dict(self.args, intent=plan, hub_preimage=None,
                input_sha256=canonical_sha256(dict(operation='admin.hub', payload=plan)))
            with self.subTest(action=action), self.assertRaises(ValueError): journal.prepare(**args)
        for key, value in [('actor', 'impostor'), ('hub_api', 'https://other.example/hub/api'),
                           ('console', 'cit'), ('source_revision', 'sha256:'+'f'*64)]:
            plan = dict(self.intent, **{key:value})
            with self.subTest(intent=key), self.assertRaises(ValueError):
                journal.prepare(**dict(self.args, intent=plan))
        for url in ('http://coordinator.example', 'https://user:private-token@coordinator.example',
                    'https://coordinator.example/?token=private-token', 'https://coordinator.example/../x',
                    'https://coordinator.example:99999', 'https://coordinator.example/%2f'):
            with self.subTest(url=url), self.assertRaises(ValueError) as raised:
                module().SourceHubJournal(self.provider, **dict(self.pins, coordinator_url=url))
            self.assertNotIn('private-token', str(raised.exception))
        journal.prepare(**self.args)
        self.arm()
        changed = copy.deepcopy(self.receipt)
        changed['result']['after']['users'].append('unrequested')
        with self.assertRaises(ValueError): journal.complete(OP, self.input, receipt=changed)
        self.assertEqual(self.source_tables(), before)

    def test_missing_partial_or_damaged_history_is_never_repaired(self):
        journal = self.provision()
        journal.prepare(**self.args)
        self.arm()
        self.complete()
        baseline = sqlite3.connect(':memory:')
        self.addCleanup(baseline.close)
        self.provider.db.backup(baseline)
        faults = {
            'one-table': 'DROP TABLE hub_journal_operation',
            'all-tables': 'DROP TABLE hub_journal_operation; DROP TABLE hub_journal_authority',
            'authority-row': 'DELETE FROM hub_journal_authority',
            'authority-binding': "UPDATE hub_journal_authority SET binding='{}'",
            'authority-schema': 'ALTER TABLE hub_journal_authority ADD COLUMN extra TEXT',
            'marker': 'PRAGMA user_version=0',
            'old-marker': 'PRAGMA user_version=5',
            'foreign-marker': 'PRAGMA user_version=8',
            'source-authority-table': 'DROP TABLE pool_source_authority',
            'source-authority-row': 'DELETE FROM pool_source_authority',
            'source-authority-console': "UPDATE pool_source_authority SET console='cit'",
            'source-authority-path': "UPDATE pool_source_authority SET source_path='/foreign'",
            'source-authority-origin': "UPDATE pool_source_authority SET origin='https://foreign.example'",
            'source-authority-database': "UPDATE pool_source_authority SET database_id='foreign'",
            'owner': 'DELETE FROM console_owner',
            'journal-rows': 'DELETE FROM hub_journal_operation',
            'journal-record': "UPDATE hub_journal_operation SET record='{}'",
            'state-regression': "UPDATE hub_journal_operation SET state='prepared'",
            'manifest': "UPDATE hub_journal_authority SET manifest='sha256:"+'0'*64+"'",
            'receipt-row': "DELETE FROM pool_source_receipt WHERE transition_id='"+OP+"'",
            'receipt-result': "UPDATE pool_source_receipt SET result='{}' WHERE transition_id='"+OP+"'",
            'audit-row': "DELETE FROM audit WHERE actor='course-admin'",
            'audit-change': "UPDATE audit SET actor='other' WHERE actor='course-admin'",
            'record-schema': 'ALTER TABLE records ADD COLUMN extra TEXT',
        }
        for fault, sql in faults.items():
            with self.subTest(fault=fault):
                baseline.backup(self.provider.db)
                self.provider.db.executescript(sql)
                before = list(self.provider.db.iterdump())
                for call in (lambda: journal.read(OP, self.input),
                             lambda: journal.prepare(**self.args), self.arm, self.complete,
                             lambda: module().SourceHubJournal(self.provider, **self.pins)):
                    with self.assertRaises(ValueError): call()
                    self.assertEqual(list(self.provider.db.iterdump()), before)
                    self.assertFalse(self.provider.db.in_transaction)

    def test_four_group_actions_use_actual_codec_and_task2_result(self):
        journal = self.provision()
        for index, action in enumerate(('group-create', 'group-users-add', 'group-users-remove', 'group-delete')):
            with self.subTest(action=action):
                operation_id = '55555555-5555-4555-8555-%012d' % index
                plan = copy.deepcopy(self.intent)
                plan['request'] = dict(action=action, name='team')
                if action in ('group-users-add', 'group-users-remove'):
                    plan['request']['users'] = ['student']
                if action == 'group-create': plan['before'] = None
                input_hash = canonical_sha256(dict(operation='admin.hub', payload=plan))
                ticket = dict(self.ticket, transition_id=operation_id, input_sha256=input_hash)
                journal.prepare(**dict(self.args, operation_id=operation_id, input_sha256=input_hash,
                    intent=plan, ticket=ticket, hub_preimage=plan['before']))
                effect = dict(self.effect, transition_id=operation_id, input_sha256=input_hash, intent=plan)
                effect_hash = canonical_sha256(effect)
                journal.arm(operation_id, input_hash, effect=effect, effect_sha256=effect_hash)
                after = None if action == 'group-delete' else dict(name='team', users=[] if action in (
                    'group-create', 'group-users-remove') else ['student'], roles=[], properties_sha256=canonical_sha256({}))
                receipt = dict(self.receipt, transition_id=operation_id, input_sha256=input_hash,
                    effect_sha256=effect_hash, result=dict(action=action, before=plan['before'], after=after))
                self.assertEqual(journal.complete(operation_id, input_hash, receipt=receipt)['receipt'], receipt)

    def test_shared_removal_journal_fixture_uses_strict_codec_and_real_sql(self):
        journal = self.provision()
        db = self.provider.db
        db.execute('PRAGMA user_version=7')
        from cps_compute.native_groups import canonical_sha256
        from cps_compute.native_pool_neutral_effects import descriptor, control_scopes, runtime_scopes
        from cps_compute.native_gpu_runtime import native_spec_sha256

        workspace, course, term, group = 'shared-workspace', 'course', 'term', 'shared-group'
        descriptor_body = dict(workspace=workspace, course=course, term=term, group=group,
            server='rtc', namespace='jupyterhub', pod='jupyter-workspace')
        owner = descriptor('cps', descriptor_body)
        pod_uid = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        generation = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
        group_incarnation = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
        control_incarnation = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
        runtime_incarnation = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'
        people = [DB, INSTALLATION]
        roster_before = [
            dict(membership_id='member-a', username='student-a', canonical_person_id=people[0]),
            dict(membership_id='member-b', username='student-b', canonical_person_id=people[1]),
        ]
        roster_after = [roster_before[1]]
        registration_obj = dict(workspace=workspace, owner=owner, server='rtc', group_id=group, members=people)
        registration = canonical(registration_obj)
        pod = dict(apiVersion='v1', kind='Pod', metadata=dict(namespace='jupyterhub', name='jupyter-workspace', uid=pod_uid),
            spec=dict(containers=[dict(name='server', image='example.invalid/server')]))
        spec_sha = native_spec_sha256(pod)
        writer = dict(kind='cpu', workspace=workspace, descriptor=descriptor_body, owner=owner, pod_uid=pod_uid,
            registration=registration, spawn=dict(version=1, generation=generation, mode='none', workspace=workspace,
                owner=owner, server='rtc', pod=dict(namespace='jupyterhub', name='jupyter-workspace', uid=pod_uid),
                spec_sha256=spec_sha, unbound=False), cpu_spawn=dict(version=1, generation=generation,
                workspace=workspace, registration_sha256=canonical_sha256(registration_obj), owner=owner, server='rtc',
                pod=dict(namespace='jupyterhub', name='jupyter-workspace', uid=pod_uid), spec_sha256=spec_sha, unbound=False), pod=pod)

        hub_group = dict(id=41, name=group, users=['student-a', 'student-b'], roles=[],
            properties={'cps_workspace_incarnation': group_incarnation}, custody=dict(name=group, source='cps',
            course=course, term=term, hub_group_id=41, incarnation=group_incarnation))
        authority = dict(service=dict(id=1, name='cps-admin'), kernel_group=dict(id=2, name='cps-workspace-kernel-accounts'),
            kernel_role=dict(id=3, name='cps-workspace-kernel'))
        receipt = dict(version=1, kind='hub_neutral_creation', source='cps', workspace=workspace, owner=owner,
            descriptor_hash=canonical_sha256(dict(source='cps', **descriptor_body)).removeprefix('sha256:'),
            group='cps-workspace-kernel-accounts', role='cps-workspace-kernel',
            identity=dict(id=51, name=owner, created='2026-01-01T00:00:00Z'))
        controls = dict(owner=owner, hub_service_id=1, hub_role_id=61, incarnation=control_incarnation,
            hub_runtime_role_id=62, runtime_incarnation=runtime_incarnation)
        control_role = dict(id=61, name='cps-workspace-control-'+owner, scopes=control_scopes(owner, descriptor_body),
            managed_by_auth=False, groups=[], description='Permanent Source custody '+control_incarnation, users=[], services=[1])
        runtime_role = dict(id=62, name='cps-workspace-runtime-'+owner, scopes=runtime_scopes(owner, descriptor_body),
            managed_by_auth=False, groups=[], description='Permanent kernel runtime '+runtime_incarnation, users=[51], services=[])
        owner_after = dict(id=51, name=owner, admin=False, roles=[3, 62], groups=[2])
        account = dict(receipt=receipt, authority=authority, course_group=hub_group, controls=controls,
            control_role=control_role, runtime_role=runtime_role, owner=owner_after)
        before = dict(group=hub_group, accounts=[account], shares=[dict(id=71, owner_id=51, spawner_id=81,
            group_id=41, user_id=None, scopes=['access:servers!server='+owner+'/rtc'], expires_at=None)],
            spawners=[dict(id=81, owner_id=51, name='rtc')])

        membership_a = dict(id='member-a', person_id='student-a', canonical_person_id=people[0], group_id=group,
            course_id=course, term_id=term, role='student', source='local', external_id=None)
        membership_b = dict(id='member-b', person_id='student-b', canonical_person_id=people[1], group_id=group,
            course_id=course, term_id=term, role='student', source='local', external_id=None)
        workspace_row = dict(id=workspace, course_id=course, term_id=term, group_id=group, hub_user=owner,
            hub_server='rtc', source='local', state='running')
        stopped = dict(workspace_row, state='stopped', notice='Membership changed; kernel interrupted. Files retained. Profile must be revalidated before restart.')
        group_row = dict(id=group, course_id=course, term_id=term, role='student', members=['student-a', 'student-b'])
        group_after = dict(group_row, members=['student-b'])
        records = [
            ('memberships', 'member-a', canonical(membership_a)), ('memberships', 'member-b', canonical(membership_b)),
            ('groups', group, canonical(group_row)), ('workspaces', workspace, canonical(workspace_row)),
        ]
        with db:
            for kind, identifier, payload in records:
                db.execute('INSERT INTO records VALUES(?,?,?,?)', ('cps', kind, identifier, payload))
        source = journal.capture_source()
        request = dict(action='shared-student-remove', workspace=workspace, membership_id='member-a', course=course,
            term=term, group=group, roster_before=roster_before, roster_after=roster_after, writers=[writer],
            source_changes=[
                dict(kind='groups', id=group, before=canonical(group_row), after=canonical(group_after)),
                dict(kind='memberships', id='member-a', before=canonical(membership_a), after=None),
                dict(kind='workspaces', id=workspace, before=canonical(workspace_row), after=canonical(stopped)),
            ])
        intent = dict(console='cps', actor='course-admin', hub_api=HUB, request=request, before=before,
            source_revision=canonical_sha256(source))
        input_hash = canonical_sha256(dict(operation='admin.hub', payload=intent))
        ticket = dict(self.ticket, input_sha256=input_hash)
        record = journal.prepare(operation_id=OP, input_sha256=input_hash, intent=intent,
            source_preimage=source, hub_preimage=before, actor='course-admin', ticket=ticket)
        self.assertEqual(record['state'], 'prepared')
        self.assertEqual(journal.read(OP, input_hash)['intent']['request']['action'], 'shared-student-remove')
        from e2x_course_hub.cps.compute import ComputePolicyClient
        from e2x_course_hub.cps.hub_permissions import HubPermissionController, SourceJournalBinding
        from e2x_course_hub.cps.pool_heads import PoolMutationRPCGuard
        from e2x_course_hub.schema.user import User
        from e2x_course_hub.api.hub_api import HubAPI
        from unittest.mock import patch
        client = ComputePolicyClient(ORIGIN, 'test-token', 'cps')
        guard = PoolMutationRPCGuard(client, database_id=DB)
        self.provider.pool_guard = guard
        binding = SourceJournalBinding(**self.pins)
        hub = HubAPI(api_url=HUB, api_token='test-token')
        controller = HubPermissionController(self.provider, guard, binding, hub)
        before_sql = list(db.iterdump())
        before_changes = db.total_changes

        async def fresh_hub(path, **kwargs):
            return dict(name='course-admin', admin=False, groups=[])

        with patch.object(controller, '_hub', side_effect=fresh_hub):
            operations = asyncio.run(controller.pending_workspace_removals(
                User(username='course-admin', admin=False, groups=[]), workspace))
        self.assertEqual(operations, [dict(operation_id=OP, input_sha256=input_hash, state='prepared')])
        async def unrelated_user(path, **kwargs):
            return dict(name='another-instructor', admin=False, groups=[])
        with patch.object(controller, '_hub', side_effect=unrelated_user):
            self.assertEqual(asyncio.run(controller.pending_workspace_removals(
                User(username='another-instructor', admin=False, groups=[]), workspace)), [])
        async def fresh_admin(path, **kwargs):
            return dict(name='current-admin', admin=True, groups=[])
        with patch.object(controller, '_hub', side_effect=fresh_admin):
            self.assertEqual(asyncio.run(controller.pending_workspace_removals(
                User(username='current-admin', admin=False, groups=[]), workspace)), operations)
        self.assertEqual(list(db.iterdump()), before_sql)
        self.assertEqual(db.total_changes, before_changes)

    def test_large_total_history_is_streamed_not_capped_as_one_record(self):
        journal = self.provision()
        # Two individually bounded preimages exceed the per-effect total bound.
        payload = canonical(dict(id='course', metadata='x' * (2200*1024)))
        with self.provider.db:
            self.provider.db.execute('UPDATE records SET payload=?', (payload,))
        for operation_id in (OP, DB):
            preimage = journal.capture_source()
            intent = dict(self.intent, source_revision=canonical_sha256(preimage))
            input_hash = canonical_sha256(dict(operation='admin.hub', payload=intent))
            ticket = dict(self.ticket, transition_id=operation_id, input_sha256=input_hash)
            journal.prepare(**dict(self.args, operation_id=operation_id, input_sha256=input_hash,
                intent=intent, ticket=ticket, source_preimage=preimage))
        self.assertEqual(journal.read(OP, input_hash)['state'], 'prepared')

    def test_individual_record_bounds_and_secret_metadata_fail_without_retention(self):
        journal = self.provision()
        db = self.provider.db
        for payload in (canonical(dict(id='course', token='private-token')),
                        canonical(dict(id='course', metadata='x'*(4*1024*1024)))):
            with db:
                db.execute('UPDATE records SET payload=?', (payload,))
            before = self.source_tables()
            with self.assertRaises(ValueError) as raised:
                journal.capture_source()
            self.assertNotIn('private-token', str(raised.exception))
            self.assertEqual(self.source_tables(), before)
            self.assertIsNone(journal.read(OP, self.input))
        with db:
            db.execute('UPDATE records SET payload=?', (self.preimage['records']['rows'][0][-1],))
        journal.prepare(**self.args)
        effect = dict(self.effect, unexpected='x'*(4*1024*1024))
        with self.assertRaises(ValueError):
            journal.arm(OP, self.input, effect=effect, effect_sha256=canonical_sha256(effect))
        self.arm()
        receipt = dict(self.receipt, result='x'*(8*1024*1024))
        with self.assertRaises(ValueError): journal.complete(OP, self.input, receipt=receipt)
        self.assertEqual(journal.read(OP, self.input)['state'], 'armed')

    def test_identity_preimage_change_blocks_arm(self):
        journal = self.provision()
        journal.prepare(**self.args)
        with self.provider.db:
            self.provider.db.execute("UPDATE email_links SET email='new@example.edu'")
        with self.assertRaises(ValueError): self.arm()
        self.assertEqual(journal.read(OP, self.input)['state'], 'prepared')

    def test_exact_concurrent_prepare_and_arm_serialize_without_duplicate_rows(self):
        self.provision()
        def concurrent(action):
            def run(_):
                db = sqlite3.connect(str(self.path), timeout=10)
                db.row_factory = sqlite3.Row
                try:
                    provider = LocalCourseProvider.__new__(LocalCourseProvider)
                    provider.db, provider.console = db, 'cps'
                    journal = module().SourceHubJournal(provider, **self.pins)
                    if action == 'prepare':
                        return journal.prepare(**self.args)
                    return journal.arm(OP, self.input, effect=self.effect, effect_sha256=self.effect_hash)
                finally:
                    db.close()
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(run, range(8)))
            self.assertTrue(all(result == results[0] for result in results))
            self.assertEqual(results[0]['state'], 'prepared' if action == 'prepare' else 'armed')
        concurrent('prepare')
        concurrent('arm')
        self.assertEqual(self.provider.db.execute('SELECT count(*) FROM hub_journal_operation').fetchone()[0], 1)
        self.assertEqual(self.provider.db.execute('SELECT count(*) FROM audit').fetchone()[0], 1)

    def test_preimages_must_be_identical_on_retry_even_when_input_digest_is_preserved(self):
        journal = self.provision()
        journal.prepare(**self.args)
        changed = copy.deepcopy(self.preimage)
        changed['records']['rows'][0][-1] = '{"name":"forged"}'
        with self.assertRaises(ValueError):
            journal.prepare(**dict(self.args, source_preimage=changed))
        with self.assertRaises(ValueError):
            journal.prepare(**dict(self.args, ticket=dict(self.ticket, predecessor='sha256:'+'a'*64)))
        self.arm()
        self.complete()
        with self.assertRaises(ValueError):
            journal.complete(OP, self.input, receipt=dict(self.receipt, actor='changed'))

    def test_existing_provider_rejects_journal_generation_without_repair(self):
        self.provision()
        before = list(self.provider.db.iterdump())
        with self.assertRaises(ValueError): LocalCourseProvider(self.path, 'cps')
        self.assertEqual(list(self.provider.db.iterdump()), before)

    def test_no_memory_connection_or_legacy_source_adoption(self):
        self.provision()
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        self.provider.db.backup(db)
        provider = LocalCourseProvider.__new__(LocalCourseProvider)
        provider.console, provider.db = 'cps', db
        with self.assertRaises(ValueError): module().SourceHubJournal(provider, **self.pins)
        with self.provider.db:
            self.provider.db.execute('DELETE FROM pool_source_authority')
        before = list(self.provider.db.iterdump())
        with self.assertRaises(ValueError): module().SourceHubJournal(self.provider, **self.pins)
        self.assertEqual(list(self.provider.db.iterdump()), before)
