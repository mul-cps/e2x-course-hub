import asyncio
import contextlib
import functools
import io
import json
import socket
import ssl
import tempfile
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

from tornado import web
from tornado.testing import AsyncHTTPSTestCase, gen_test

from e2x_course_hub.cps import account_migration


class AdoptionCLIXSRFTest(AsyncHTTPSTestCase):
    """Exercise the CLI over verified TLS with real Tornado XSRF checks."""

    def get_app(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bootstrap = "cookie"
        self.actions = []
        self.gets = []
        outer = self

        class Handler(web.RequestHandler):
            def get_current_user(self):
                # Only the external Hub identity lookup is represented here.
                if self.request.headers.get("Authorization") == "Bearer fixture-private-token":
                    return {"name": "operator", "kind": "user"}
                return None

            @web.authenticated
            def get(self):
                outer.gets.append(self.request.path)
                if outer.bootstrap == "redirect":
                    self.redirect("https://foreign.invalid/app", status=303)
                    return
                if outer.bootstrap == "cookie":
                    self.xsrf_token
                self.write("Authenticated Source application")

            @web.authenticated
            def post(self):
                action = self.request.path.rsplit("/", 1)[1]
                outer.actions.append(action)
                body = json.loads(self.request.body)
                if action == "plan":
                    if body != {"reason": "review", "provenance": "ticket"}:
                        raise web.HTTPError(400)
                    self.write({"binding": {"id": "w"}, "actor": "operator"})
                else:
                    if body != {"review": {"binding": {"id": "w"}, "actor": "operator"}}:
                        raise web.HTTPError(400)
                    self.write({"state": "ready", "provenance": "reviewed_legacy_adoption"})

        return web.Application(
            [(r"/services/source/.*", Handler)],
            xsrf_cookies=True,
            xsrf_cookie_kwargs={"secure": True, "path": "/services/source/"},
            cookie_secret="fictional-test-secret",
            login_url="/login",
        )

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def transport(self):
        # Trust the fixture CA and its real hostname; production TLS is unchanged.
        context = ssl.create_default_context(cafile=self.get_ssl_options()["certfile"])
        build_opener = urllib.request.build_opener
        getaddrinfo = socket.getaddrinfo

        def opener(*handlers):
            return build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=context),
                *handlers,
            )

        def resolve(host, *args, **kwargs):
            return getaddrinfo("127.0.0.1" if host == "foo.example.com" else host, *args, **kwargs)

        stack = contextlib.ExitStack()
        stack.enter_context(unittest.mock.patch.object(urllib.request, "build_opener", opener))
        stack.enter_context(unittest.mock.patch.object(socket, "getaddrinfo", resolve))
        return stack

    @property
    def source_url(self):
        return f"https://foo.example.com:{self.get_http_port()}/services/source"

    async def run_client(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(
            None, functools.partial(function, *args)
        )

    @gen_test
    async def test_private_cli_plan_and_execute_bootstrap_real_xsrf(self):
        token = Path(self.tmp.name) / "token"
        token.write_text("fixture-private-token")
        token.chmod(0o600)
        packet = Path(self.tmp.name) / "review.json"
        common = [
            "--source-url", self.source_url, "--token-file", str(token), "--packet", str(packet)
        ]
        output = io.StringIO()
        with self.transport() as stack:
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(output))
            result = await self.run_client(
                account_migration.main,
                common + ["--workspace", "w", "--reason", "review", "--provenance", "ticket"],
            )
            self.assertEqual(result, 0)
            self.assertEqual(packet.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                json.loads(packet.read_text()), {"binding": {"id": "w"}, "actor": "operator"}
            )
            self.assertEqual(
                await self.run_client(account_migration.main, common + ["--execute"]), 0
            )
        self.assertEqual(self.gets, ["/services/source/app", "/services/source/app"])
        self.assertEqual(self.actions, ["plan", "execute"])
        self.assertNotIn("fixture-private-token", output.getvalue())
        self.assertNotIn("operator", output.getvalue())

    @gen_test
    async def test_bootstrap_redirect_is_not_followed_or_posted(self):
        self.bootstrap = "redirect"
        with self.transport(), self.assertRaises(urllib.error.HTTPError) as raised:
            await self.run_client(
                account_migration.request, self.source_url, "fixture-private-token",
                "workspace-account-adoptions/w/plan", {"reason": "review", "provenance": "ticket"},
            )
        self.assertEqual(raised.exception.code, 303)
        self.assertEqual(self.actions, [])

    @gen_test
    async def test_missing_or_inapplicable_cookie_refuses_before_post(self):
        for mode in ("missing", "wrong_path"):
            self.bootstrap = "missing" if mode == "missing" else "cookie"
            self._app.settings["xsrf_cookie_kwargs"]["path"] = (
                "/unrelated/" if mode == "wrong_path" else "/services/source/"
            )
            with self.subTest(mode=mode), self.transport(), self.assertRaises(ValueError):
                await self.run_client(
                    account_migration.request, self.source_url, "fixture-private-token",
                    "workspace-account-adoptions/w/plan",
                    {"reason": "review", "provenance": "ticket"},
                )
        self.assertEqual(self.actions, [])
