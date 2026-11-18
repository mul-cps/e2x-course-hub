"""Real Jinja renderer and Tornado content versions preserve Source URL scopes."""
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from tornado import web
from tornado.testing import AsyncHTTPTestCase
from e2x_course_hub.course_service.app import CourseServiceApp
from e2x_course_hub.course_service.handlers.base import BaseTemplateHandler

class VersionedAssets(AsyncHTTPTestCase):
    def get_app(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.static = self.root / "static"
        (self.static / "course-service-ui/assets").mkdir(parents=True)
        (self.static / "course-service-ui/main.js").write_text("initial javascript")
        (self.static / "course-service-ui/assets/main.css").write_text("initial stylesheet")
        app = CourseServiceApp()
        app.api_token = "offline-fixture-not-a-credential"
        app.api_url = "http://127.0.0.1:1/hub/api"
        app.static_path = str(self.static)
        templates = Path(__file__).resolve().parents[1] / "share/e2x_course_hub/templates/course_service"
        if not templates.exists():
            templates = Path("/usr/local/share/e2x_course_hub/templates/course_service")
        app.template_path = str(templates)
        app.database_path = str(self.root / "unused.sqlite")
        app.server_config_file = str(self.root / "unused.yml")
        app.service_prefix = self.prefix
        with patch("e2x_course_hub.course_service.app.configure_provider", return_value=SimpleNamespace()), patch("e2x_course_hub.course_service.app.ensure_initial_config"), patch("e2x_course_hub.course_service.app.API"):
            app.init_tornado_settings()
        class Page(BaseTemplateHandler):
            def get_current_user(self):return {"name":"offline-operator", "admin":True}
            def get(self):self.render_template("react_base.j2")
        return web.Application([(self.prefix+"app", Page)], **app.tornado_settings)

    def tearDown(self):
        super().tearDown()
        self.temp.cleanup()

    def asset_links(self):
        response = self.fetch(self.prefix + "app")
        self.assertEqual(response.code, 200)
        links = re.findall(r'(?:src|href)="([^"]+)"', response.body.decode())
        return [link for link in links if "main.js" in link or "main.css" in link]

    def test_actual_rendered_links_version_both_assets_and_fetch_exact_content(self):
        before = self.asset_links()
        self.assertEqual(len(before), 2)
        for link in before:
            self.assertTrue(link.startswith(self.prefix + "static/"), link)
            self.assertIn("?v=", link)
            response = self.fetch(link)
            self.assertEqual(response.code, 200)
            self.assertEqual(response.body, (self.static / link.split("static/",1)[1].split("?",1)[0]).read_bytes())
        (self.static / "course-service-ui/main.js").write_text("changed javascript")
        (self.static / "course-service-ui/assets/main.css").write_text("changed stylesheet")
        web.StaticFileHandler.reset()
        after = self.asset_links()
        self.assertEqual([link.split("?",1)[0] for link in before], [link.split("?",1)[0] for link in after])
        self.assertTrue(all(a != b for a,b in zip(before,after)))
        for link in after:
            self.assertEqual(self.fetch(link).code,200)

class CPSAssets(VersionedAssets):prefix="/services/cps-admin/"
class CITAssets(VersionedAssets):prefix="/services/cit-admin/"
# Keep only the two concrete Source cases in unittest discovery.
del VersionedAssets
