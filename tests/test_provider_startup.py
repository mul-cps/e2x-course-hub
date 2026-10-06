"""Application boundary checks for the deliberately disabled Moodle scaffold."""
import builtins
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from e2x_course_hub.course_service.app import CourseServiceApp
from e2x_course_hub.cps.providers import LocalCourseProvider


class ProviderStartup(unittest.TestCase):
    def app(self, directory, owner, enabled):
        app = CourseServiceApp()
        app.api_token = 'synthetic-startup-token'
        app.api_url = 'https://hub.invalid/hub/api'
        app.console_owner = owner
        app.database_path = str(directory / 'courses.sqlite')
        app.server_config_file = str(directory / 'server.yaml')
        app.course_providers = {
            'local': {'enabled': True},
            'moodle': {'enabled': enabled,
                       'baseUrl': 'https://moodle-must-not-connect.invalid',
                       'authSecretRef': 'moodle-secret-must-not-be-read',
                       'syncInterval': '1s', 'readOnly': True},
        }
        return app

    def test_disabled_provider_does_no_external_work_at_app_startup(self):
        for owner in ('cps', 'cit'):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as temporary:
                app = self.app(Path(temporary), owner, False)
                with patch('socket.socket.connect', side_effect=AssertionError('network connection during startup')), \
                     patch('tornado.ioloop.PeriodicCallback.start', side_effect=AssertionError('periodic work during startup')), \
                     patch('e2x_course_hub.cps.providers.MoodleCourseProvider', side_effect=AssertionError('disabled Moodle initialized')) as moodle, \
                     patch('builtins.open', wraps=builtins.open) as opened:
                    app.initialize([])
                    moodle.assert_not_called()
                    self.assertFalse(any('moodle-secret-must-not-be-read' in str(call.args[0])
                                         for call in opened.call_args_list if call.args))
                provider = app.tornado_settings['course_provider']
                try:
                    self.assertIsInstance(provider, LocalCourseProvider)
                    self.assertEqual(provider.console, owner)
                    self.assertTrue(Path(app.database_path).is_file())
                finally:
                    provider.db.close()

    def test_enabling_unfinished_provider_fails_before_persistent_initialization(self):
        for owner in ('cps', 'cit'):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                app = self.app(directory, owner, True)
                with patch('socket.socket.connect', side_effect=AssertionError('network connection before rejection')), \
                     patch('tornado.ioloop.PeriodicCallback.start', side_effect=AssertionError('scheduled work before rejection')):
                    with self.assertRaisesRegex(ValueError, 'planned / not deployed'):
                        app.initialize([])
                self.assertEqual(list(directory.iterdir()), [])
