import tempfile
import unittest
from pathlib import Path
from e2x_course_hub.cps.bootstrap import ensure_initial_config
from e2x_course_hub.schema.server import Server
from e2x_course_hub.course_service.app import CourseServiceApp

class BootstrapTest(unittest.TestCase):
    def test_blank_pvc_safe_foundation_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.yml'
            self.assertTrue(ensure_initial_config(path))
            server=Server.from_config_file(str(path))
            self.assertEqual(server.profiles,{})
            self.assertEqual(server.courses,{})
            self.assertEqual(server.roles.root,{})
            original=path.read_bytes()
            self.assertFalse(ensure_initial_config(path))
            self.assertEqual(path.read_bytes(),original)

    def test_python_config_alias_initializes_blank_pvc(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=Path(tmp)/'app.py'
            config.write_text(f"c.CourseServiceApp.server_config_file = {str(Path(tmp)/'data/config.yml')!r}\nc.CourseServiceApp.database_path = {str(Path(tmp)/'courses.sqlite')!r}\nc.CourseServiceApp.console_owner = 'cit'\n")
            app=CourseServiceApp()
            app.api_token='test-service'
            app.api_url='https://hub.example/hub/api'
            app.initialize(['--config='+str(config)])
            self.assertEqual(app.tornado_settings['console_owner'],'cit')
            self.assertEqual(app.tornado_settings['api'].server.courses,{})
            app.tornado_settings['course_provider'].db.close()
