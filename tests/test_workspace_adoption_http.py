import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from tornado import web
from tornado.testing import AsyncHTTPTestCase

from e2x_course_hub.cps.platform_handlers import default_handlers
from e2x_course_hub.cps.providers import LocalCourseProvider


class AdoptionHTTPTest(AsyncHTTPTestCase):
    def get_app(self):
        from e2x_course_hub.cps.platform_handlers import WorkspaceAccountAdoptionHandler

        self.tmp = tempfile.TemporaryDirectory()
        self.provider = LocalCourseProvider(Path(self.tmp.name) / "db", "cps")
        self.admin, self.calls, self.failure = True, [], None
        self.auth_kind = "user"
        outer = self

        class Hub:
            async def get_user(self, name):
                return {"name": name, "admin": outer.admin, "groups": []}

        class Accounts:
            async def plan_adoption(self, identifier, **kwargs):
                outer.calls.append(("plan", identifier, kwargs))
                return {"binding": {"id": identifier}, "actor": kwargs["actor"]}

            async def adopt_reviewed(self, packet, **kwargs):
                if outer.failure:
                    raise outer.failure
                outer.calls.append(("execute", copy.deepcopy(packet), kwargs))
                return {"state": "ready", "provenance": "reviewed_legacy_adoption"}

        class Handler(WorkspaceAccountAdoptionHandler):
            def get_current_user(self):
                name = self.request.headers.get("X-Test-User")
                if not name:
                    return None
                return {"name": name, "kind": outer.auth_kind}

        routes = [
            (pattern, Handler)
            for pattern, handler in default_handlers
            if handler is WorkspaceAccountAdoptionHandler
        ]
        return web.Application(
            routes,
            workspace_service=SimpleNamespace(accounts=Accounts()),
            last_config_check=time.time(),
            course_provider=self.provider,
            api=SimpleNamespace(course_api=SimpleNamespace(hub_api=Hub())),
            login_url="/login",
            cookie_secret="test",
        )

    def tearDown(self):
        self.provider.db.close()
        self.tmp.cleanup()
        super().tearDown()

    def request(self, body, action="plan", user="operator"):
        return self.fetch(
            "/api/workspace-account-adoptions/w/" + action,
            method="POST",
            body=json.dumps(body),
            headers={"X-Test-User": user} if user else {},
        )

    def test_plan_and_execute_use_authenticated_actor_and_exact_route(self):
        self.assertEqual(self.request({"reason": "review", "provenance": "ticket"}).code, 200)
        packet = {"binding": {"id": "w"}, "actor": "operator"}
        self.assertEqual(self.request({"review": packet}, action="execute").code, 200)
        self.assertEqual(
            self.calls,
            [
                ("plan", "w", {"actor": "operator", "reason": "review", "provenance": "ticket"}),
                ("execute", packet, {"actor": "operator"}),
            ],
        )

    def test_instructor_and_unauthenticated_execute_are_denied(self):
        self.admin = False
        self.assertEqual(self.request({"reason": "review", "provenance": "ticket"}).code, 403)
        self.assertEqual(self.request({"review": {}}, action="execute", user=None).code, 403)
        self.assertEqual(self.calls, [])

    def test_service_and_missing_kind_cannot_impersonate_same_named_admin(self):
        for kind in ("service", None):
            self.auth_kind = kind
            for body, action in [
                ({"reason": "review", "provenance": "ticket"}, "plan"),
                ({"review": {"binding": {"id": "w"}, "actor": "operator"}}, "execute"),
            ]:
                with self.subTest(kind=kind, action=action):
                    self.assertEqual(self.request(body, action, user="operator").code, 403)
        self.assertEqual(self.calls, [])

    def test_route_and_body_injections_are_rejected(self):
        for body, action in [
            ({"reason": "review", "provenance": "ticket", "actor": "foreign"}, "plan"),
            ({"review": {"binding": {"id": "other"}}}, "execute"),
            ({"review": {"binding": {"id": "w"}}, "fingerprint": {}}, "execute"),
        ]:
            with self.subTest(body=body):
                self.assertEqual(self.request(body, action).code, 400)
        self.assertEqual(self.calls, [])

    def test_conflicts_remain_errors(self):
        self.failure = PermissionError("Reviewed source changed")
        self.assertEqual(self.request({"review": {"binding": {"id": "w"}}}, "execute").code, 403)


class AdoptionCLITest(unittest.TestCase):
    def test_protected_packet_and_https_no_redirect_and_default_plan(self):
        from e2x_course_hub.cps.account_migration import parser, read_private

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.json"
            path.write_text("{}")
            path.chmod(0o600)
            self.assertEqual(read_private(path), b"{}")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                read_private(path)
            args = parser().parse_args(
                [
                    "--source-url",
                    "https://source.invalid",
                    "--token-file",
                    str(path),
                    "--workspace",
                    "w",
                    "--reason",
                    "review",
                    "--provenance",
                    "ticket",
                    "--packet",
                    str(path),
                ]
            )
            self.assertFalse(args.execute)
