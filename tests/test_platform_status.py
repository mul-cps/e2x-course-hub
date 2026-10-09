from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch

from tornado import web
from tornado.testing import AsyncHTTPTestCase
from e2x_course_hub.cps.platform_status import read_status, PlatformStatusHandler
from e2x_course_hub.schema.user import User
from e2x_course_hub.cps.providers import LocalCourseProvider

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def status(**changes):
    return {'version': 1, 'state': 'qualification', 'title': 'GPU group sharing: qualification in progress',
        'detail': 'Startup is blocked. Standard GPU profiles remain disabled.',
        'updatedAt': NOW.isoformat().replace('+00:00', 'Z'),
        'checks': [{'id': 'apps', 'label': 'Applications', 'state': 'passed'},
                   {'id': 'startup', 'label': 'Workspace startup', 'state': 'blocked'},
                   {'id': 'cleanup', 'label': 'Cleanup', 'state': 'pending'}], **changes}


class PlatformStatusFileTest(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_current_operator_record_is_preserved_and_mounted_symlink_can_update(self):
        target = self.path / 'version1'
        target.write_text(json.dumps(status()))
        mounted = self.path / 'status.json'
        mounted.symlink_to(target)
        self.assertEqual(read_status(mounted, now=NOW), status())
        target2 = self.path / 'version2'
        target2.write_text(json.dumps(status(state='limited-pilot')))
        mounted.unlink()
        mounted.symlink_to(target2)
        self.assertEqual(read_status(mounted, now=NOW)['state'], 'limited-pilot')

    def test_enabled_requires_explicit_operator_record_and_passed_checks(self):
        filename = self.path / 'status.json'
        record = status(state='enabled', checks=[{'id': 'qualification', 'label': 'Qualification', 'state': 'passed'}])
        filename.write_text(json.dumps(record))
        self.assertEqual(read_status(filename, now=NOW), record)

    def test_invalid_or_stale_records_never_report_ready(self):
        changes = [{'enabled': True}, {'version': True}, {'state': 'ready'},
            {'updatedAt': '2026-10-09T12:00:00'}, {'updatedAt': '2026-10-09T11:44:59Z'},
            {'updatedAt': '2026-10-09T12:01:00Z'},
            {'checks': [{'id': 'startup', 'label': 'Startup', 'state': 'ready'}]},
            {'state': 'enabled'}, {'state': 'enabled', 'checks': []},
            {'checks': [{'id': 'same', 'label': 'A', 'state': 'passed'}, {'id': 'same', 'label': 'B', 'state': 'passed'}]}]
        filename = self.path / 'status.json'
        for change in changes:
            with self.subTest(change=change):
                filename.write_text(json.dumps(status(**change)))
                result = read_status(filename, now=NOW)
                self.assertEqual(result['state'], 'unavailable')
                self.assertEqual(result['checks'], [])

    def test_malformed_duplicate_non_json_or_oversized_files_fail_unavailable(self):
        filename = self.path / 'status.json'
        for raw in [b'{"version":1,"version":1}', b'not JSON', b'\xff', b' ' * 16385,
                    b'{"state":NaN}', b'[' * 3000 + b']' * 3000]:
            with self.subTest(raw=raw[:40]):
                filename.write_bytes(raw)
                self.assertEqual(read_status(filename, now=NOW)['state'], 'unavailable')

    def test_absent_and_nonregular_paths_fail_unavailable(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(read_status(now=NOW)['state'], 'unavailable')
        self.assertEqual(read_status(self.path / 'missing', now=NOW)['state'], 'unavailable')
        fifo = self.path / 'fifo'
        os.mkfifo(fifo)
        self.assertEqual(read_status(fifo, now=NOW)['state'], 'unavailable')


class PlatformStatusAuthTest(AsyncHTTPTestCase):
    def get_app(self):
        self.authenticated = True
        self.admin = True
        outer = self
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = LocalCourseProvider(Path(self.tmp.name) / 'db', 'cps')
        class Handler(PlatformStatusHandler):
            def get_current_user(self): return {'name': 'visitor'} if outer.authenticated else None
            async def get_user(self): return User(username='visitor', admin=outer.admin, groups=[])
        class Hub:
            async def get_user(self, name): return {'name': name, 'admin': outer.admin, 'groups': []}
        return web.Application([(r'/api/platform-status', Handler)], login_url='/login',
            last_config_check=time.time(), api=NS(course_api=NS(hub_api=Hub())),
            course_provider=self.provider, cookie_secret='test')

    def tearDown(self):
        self.provider.db.close()
        self.tmp.cleanup()
        super().tearDown()

    def test_only_authenticated_current_administrator_can_read_status(self):
        with patch('e2x_course_hub.cps.platform_status.read_status', return_value=status()) as reader:
            response = self.fetch('/api/platform-status')
            self.assertEqual(response.code, 200)
            self.assertEqual(json.loads(response.body), status())
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.authenticated = False
            self.assertEqual(self.fetch('/api/platform-status', follow_redirects=False).code, 302)
            self.authenticated = True
            self.admin = False
            self.assertEqual(self.fetch('/api/platform-status').code, 403)
            self.assertEqual(reader.call_count, 1)

    def test_query_cannot_select_path_or_enable_status_and_post_is_rejected(self):
        with patch('e2x_course_hub.cps.platform_status.read_status', return_value=status()) as reader:
            response = self.fetch('/api/platform-status?path=/etc/shadow&enabled=true')
            self.assertEqual(response.code, 200)
            self.assertEqual(json.loads(response.body)['state'], 'qualification')
            reader.assert_called_once_with()
            self.assertIn(self.fetch('/api/platform-status', method='POST', body='{}').code, (403, 405))
