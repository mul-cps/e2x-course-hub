"""Hub 5.5.2 handler/network fixtures, not live Hub/browser qualification."""

import asyncio
import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from http.cookies import SimpleCookie
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import jupyterhub
from tornado import web
from tornado.httpclient import AsyncHTTPClient, HTTPClientError, HTTPRequest, HTTPResponse
from tornado.httputil import HTTPHeaders
from tornado.iostream import StreamClosedError
from tornado.tcpclient import TCPClient
from tornado.testing import AsyncHTTPTestCase, gen_test

from e2x_course_hub.cps.argo_service import (
    ArgoCallbackHandler, ArgoProxyHandler, ArgoServiceConfig, create_argo_service,
)
from e2x_course_hub.cps.oauth import CPSHubOAuth, challenge


def config(ca_file):
    return ArgoServiceConfig(
        source="cps", oauth_client_id="service-cps-argo-ui",
        oauth_client_secret="independent-argo-client-secret", cookie_secret="c" * 48,
        public_origin="https://hub.example", hub_api_url="https://hub-api.example/hub/api",
        hub_authorization_url="https://hub.example/hub/api/oauth2/authorize",
        compute_url="https://compute.example", native_argo_url="https://native-argo.example/argo",
        hub_ca_file=ca_file, compute_ca_file=ca_file, native_argo_ca_file=ca_file,
    )


class ControlledTransport:
    """Hold real handler requests while controlling remote I/O completion."""

    def __init__(self):
        self.requests = asyncio.Queue()

    def fetch(self, request, **kwargs):
        future = asyncio.get_running_loop().create_future()
        self.requests.put_nowait((request, future))
        return future

    @staticmethod
    def response(request, *, body=b"{}"):
        return HTTPResponse(request, 200, headers=HTTPHeaders({"Content-Type": "application/json"}),
                            buffer=io.BytesIO(body))


class ConfigTest(unittest.TestCase):
    def test_configuration_is_explicit_and_closed(self):
        with tempfile.NamedTemporaryFile() as ca:
            candidate = config(ca.name)
            for change in (
                {"source": "unknown"}, {"oauth_client_id": "service-admin"},
                {"oauth_client_secret": ""}, {"cookie_secret": "short"},
                {"compute_url": "http://compute.example"},
                {"compute_url": "https://user:secret@compute.example"},
                {"compute_url": "https://compute.example/another"},
                {"native_argo_url": "https://native-argo.example/foreign"},
                {"public_origin": "https://hub.example/?target=evil"},
                {"compute_ca_file": "/missing/ca"},
            ):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    replace(candidate, **change)
            with patch.dict(os.environ, {"JUPYTERHUB_CLIENT_ID": "service-admin",
                                         "JUPYTERHUB_API_TOKEN": "admin-secret"}, clear=True):
                with self.assertRaisesRegex(ValueError, "ARGO_USER_SOURCE"):
                    ArgoServiceConfig.from_env()
            with patch.dict(os.environ, {
                "ARGO_USER_" + name.upper(): str(getattr(candidate, name))
                for name in candidate.__dataclass_fields__
            }, clear=True):
                self.assertEqual(ArgoServiceConfig.from_env(), candidate)

    def test_native_backend_accepts_only_root_or_argo_prefix(self):
        with tempfile.NamedTemporaryFile() as ca:
            candidate = config(ca.name)
            for suffix, expected in (("", ""), ("/", ""), ("/argo", "/argo"), ("/argo/", "/argo")):
                with self.subTest(suffix=suffix):
                    changed = replace(candidate, native_argo_url="https://native-argo.example" + suffix)
                    self.assertEqual(changed.native_argo_url, "https://native-argo.example" + expected)
            for suffix in ("/foreign", "/argo/api", "//", "/argo//", "/argo/../", "/%61rgo", "/?target=evil"):
                with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                    replace(candidate, native_argo_url="https://native-argo.example" + suffix)


class ArgoServiceTest(AsyncHTTPTestCase):
    def get_app(self):
        self.ca = tempfile.NamedTemporaryFile()
        self.outbound = []
        self.hub_calls = []
        self.token_calls = []
        self.stream_delay = 0.03
        self.stream_type = "text/event-stream"
        self.truncate_artifact = False
        self.artifact_body = b'{"cells":[]}\n'
        self.identities = {
            "visitor-alice": {"name": "alice", "admin": False, "kind": "user", "groups": [],
                              "scopes": ["access:services!service=cps-argo-ui"]},
            "visitor-bob": {"name": "bob", "admin": False, "kind": "user", "groups": [],
                            "scopes": ["access:services!service=cps-argo-ui"]},
            "visitor-no-scope": {"name": "outsider", "admin": False, "kind": "user", "groups": [],
                                 "scopes": []},
            "service-token": {"name": "robot", "kind": "service", "scopes": [
                "access:services!service=cps-argo-ui"]},
        }
        outer = self

        class Transport:
            def fetch(self, request, **kwargs):
                outer.outbound.append(request)
                path = urlsplit(request.url).path
                host = urlsplit(request.url).hostname
                local = "/fixture/native" if host == "native-argo.example" else "/fixture/compute" + path
                if urlsplit(request.url).query:
                    local += "?" + urlsplit(request.url).query
                return AsyncHTTPClient().fetch(HTTPRequest(
                    outer.get_url(local), method=request.method, headers=request.headers,
                    header_callback=request.header_callback, streaming_callback=request.streaming_callback,
                    follow_redirects=request.follow_redirects, request_timeout=request.request_timeout,
                ), **kwargs)

        class HubToken(web.RequestHandler):
            def check_xsrf_cookie(self):
                pass  # This fixture models a server-to-server OAuth token endpoint.

            def post(self):
                data = {key: value[0] for key, value in parse_qs(self.request.body.decode()).items()}
                outer.token_calls.append(data)
                if (data.get("client_id") != "service-cps-argo-ui"
                        or data.get("client_secret") != "independent-argo-client-secret"
                        or data.get("redirect_uri") != "https://hub.example/argo/oauth_callback"
                        or challenge(data.get("code_verifier", "")) != outer.expected_challenge):
                    raise web.HTTPError(403)
                self.write({"access_token": "visitor-alice"})

        class HubUser(web.RequestHandler):
            def get(self):
                token = self.request.headers.get("Authorization", "")[len("token "):]
                self.write(outer.identities.get(token) or {"missing": True})

        class Compute(web.RequestHandler):
            async def get(self, path):
                # Stand-in for the independently tested downstream ownership adapter.
                # It authenticates the forwarded visitor, never the service secret.
                token = self.request.headers.get("Authorization", "")[len("Bearer "):]
                if token not in ("visitor-alice", "visitor-bob"):
                    raise web.HTTPError(401)
                if path.startswith("artifact-files/"):
                    self.set_header("Content-Type", "application/octet-stream")
                    self.set_header("Content-Disposition", 'attachment; filename="executed.ipynb"')
                    self.set_header("Content-Length", len(outer.artifact_body))
                    if outer.truncate_artifact:
                        self.write(outer.artifact_body[:-1])
                        await self.flush()
                        self._finished = True
                        self.request.connection.close()
                        return
                    for start in range(0, len(outer.artifact_body), 64 * 1024):
                        self.write(outer.artifact_body[start:start + 64 * 1024])
                        await self.flush()
                        await asyncio.sleep(0)
                elif path.endswith("/log") or "workflow-events/" in path:
                    self.set_header("Content-Type", outer.stream_type)
                    self.set_header("Set-Cookie", "upstream-cookie=forbidden")
                    self.write('data: {"result":{"type":"ADDED","owner":"' + token + '"}}\n\n')
                    await self.flush()
                    try:
                        await asyncio.sleep(outer.stream_delay)
                    except asyncio.CancelledError:
                        return  # Fixture shutdown while a finite stream is pending.
                    self.write('data: {"result":{"type":"MODIFIED"}}\n\n')
                elif self.get_argument("fixture", "") == "redirect":
                    self.redirect("https://evil.example")
                else:
                    self.set_header("Set-Cookie", "upstream-cookie=forbidden")
                    self.set_header("X-Secret", "never-forward")
                    self.write({"visitor": token, "query": self.request.query})

            def head(self, path):
                self.set_header("Content-Type", "application/x-ipynb+json")

        class Native(web.RequestHandler):
            def get(self):
                self.set_header("Content-Type", "text/html")
                self.set_header("Set-Cookie", "native-secret=forbidden")
                self.write('<html><base href="/argo/"><body>Argo fixture</body></html>')

        class RedirectProbe(ArgoCallbackHandler):
            def get(self):
                self.redirect(self.get_argument("next"))

        app = create_argo_service(config(self.ca.name), http_client=Transport())
        self.auth = app.settings["argo_hub_auth"]

        async def hub_api(method, url, **kwargs):
            self.hub_calls.append((method, url, kwargs))
            if method == "GET":
                # HubAuthenticated runs identity lookups on its dedicated sync
                # loop. Keep Hub's real cookie and scope path, substituting only
                # the remote Hub API response rather than the handler identity.
                token = kwargs["headers"]["Authorization"][len("token "):]
                return self.identities.get(token)
            local = "/fixture/hub-token" if method == "POST" else "/fixture/hub-user"
            response = await AsyncHTTPClient().fetch(HTTPRequest(
                self.get_url(local), method=method, body=kwargs.get("body"),
                headers=kwargs.get("headers", {})))
            model = json.loads(response.body)
            return None if model.get("missing") else model

        self.auth._api_request = hub_api
        app.add_handlers(r".*", [
            (r"/fixture/hub-token", HubToken), (r"/fixture/hub-user", HubUser),
            (r"/fixture/compute/argo/(.*)", Compute), (r"/fixture/native", Native),
            (r"/fixture/redirect", RedirectProbe),
        ])
        return app

    def tearDown(self):
        super().tearDown()
        thread_pool = getattr(self.auth, "_thread_pool", None)
        if thread_pool is not None:
            thread_pool.shutdown(wait=True)
        thread_loop = getattr(self.auth, "_thread_loop", None)
        if thread_loop is not None and not thread_loop.is_closed():
            thread_loop.close()
        self.ca.close()

    def cookie(self, token):
        value = web.create_signed_value(self._app.settings["cookie_secret"], self.auth.cookie_name, token)
        return self.auth.cookie_name + "=" + value.decode()

    def headers(self, token="visitor-alice", **extra):
        return {"Cookie": self.cookie(token), "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors", "Origin": "https://hub.example", **extra}

    def test_root_backend_keeps_public_prefix_and_asset_allowlist(self):
        self._app.settings["argo_config"] = replace(config(self.ca.name), native_argo_url="https://native-argo.example/")
        headers = self.headers(**{"Sec-Fetch-Mode": "no-cors"})
        page = self.fetch("/argo/workflows/cps-workflows", headers=headers, follow_redirects=False)
        self.assertEqual(page.code, 200)
        self.assertEqual(self.outbound[-1].url, "https://native-argo.example/")
        asset = self.fetch("/argo/main.js", headers=headers, follow_redirects=False)
        self.assertEqual(asset.code, 200)
        self.assertEqual(self.outbound[-1].url, "https://native-argo.example/main.js")
        self.assertNotIn("Authorization", self.outbound[-1].headers)
        self.assertNotIn("Cookie", self.outbound[-1].headers)
        calls = len(self.outbound)
        denied = self.fetch("/argo/private.txt", headers=headers, follow_redirects=False)
        self.assertEqual(denied.code, 404)
        self.assertEqual(len(self.outbound), calls)

    def test_actual_hub_oauth_pkce_callback_cookie_and_replay(self):
        self.assertEqual(jupyterhub.__version__, "5.5.2")
        self.assertIsNot(self.auth, CPSHubOAuth.instance())
        start = self.fetch("/argo/workflows/cps-workflows?limit=50", follow_redirects=False)
        self.assertEqual(start.code, 302)
        authorization = parse_qs(urlsplit(start.headers["Location"]).query)
        self.assertEqual(authorization["client_id"], ["service-cps-argo-ui"])
        self.assertEqual(authorization["code_challenge_method"], ["S256"])
        self.expected_challenge = authorization["code_challenge"][0]
        jar = SimpleCookie()
        for value in start.headers.get_list("Set-Cookie"):
            jar.load(value)
        self.assertEqual(len(jar), 2)
        for item in jar.values():
            self.assertEqual(item["path"], "/argo/")
            self.assertTrue(item["secure"])
            self.assertTrue(item["httponly"])
            self.assertEqual(item["samesite"], "Lax")
        cookies = "; ".join(name + "=" + item.value for name, item in jar.items())
        callback = "/argo/oauth_callback?" + urlencode({"state": authorization["state"][0], "code": "code"})
        result = self.fetch(callback, headers={"Cookie": cookies}, follow_redirects=False)
        self.assertEqual(result.code, 302)
        self.assertEqual(result.headers["Location"], "/argo/workflows/cps-workflows?limit=50")
        self.assertEqual(len(self.token_calls), 1)
        login_cookies = SimpleCookie()
        for value in result.headers.get_list("Set-Cookie"):
            login_cookies.load(value)
        token_cookie = login_cookies[self.auth.cookie_name]
        self.assertTrue(token_cookie["httponly"])
        self.assertTrue(token_cookie["secure"])
        self.assertEqual(token_cookie["samesite"], "Lax")
        self.assertEqual(token_cookie["path"], "/argo/")
        self.assertEqual(login_cookies["_xsrf"]["path"], "/argo/")
        self.assertTrue(login_cookies["_xsrf"]["secure"])
        self.assertEqual(self.fetch(callback, headers={"Cookie": cookies}, follow_redirects=False).code, 403)
        self.assertEqual(len(self.token_calls), 1)

    def test_missing_mismatched_and_expired_state_never_exchanges_code(self):
        for query in ("code=x", "code=x&state=unknown", "state=unknown"):
            self.assertEqual(self.fetch("/argo/oauth_callback?" + query).code, 403)
        start = self.fetch("/argo/", follow_redirects=False)
        state = parse_qs(urlsplit(start.headers["Location"]).query)["state"][0]
        self.auth.clear_oauth_state(state)
        self.assertEqual(self.fetch("/argo/oauth_callback?" + urlencode({"state": state, "code": "x"})).code, 403)
        # An existing state ID paired with another signed state cookie is denied.
        start = self.fetch("/argo/", follow_redirects=False)
        state = parse_qs(urlsplit(start.headers["Location"]).query)["state"][0]
        jar = SimpleCookie()
        for value in start.headers.get_list("Set-Cookie"):
            jar.load(value)
        jar[self.auth.state_cookie_name] = web.create_signed_value(
            self._app.settings["cookie_secret"], self.auth.state_cookie_name, "other-state").decode()
        cookies = "; ".join(name + "=" + item.value for name, item in jar.items())
        self.assertEqual(self.fetch("/argo/oauth_callback?" + urlencode({"state": state, "code": "x"}),
                                    headers={"Cookie": cookies}).code, 403)
        self.assertFalse(self.token_calls)

    def test_missing_pkce_and_callback_redirects_are_rejected(self):
        start = self.fetch("/argo/", follow_redirects=False)
        state = parse_qs(urlsplit(start.headers["Location"]).query)["state"][0]
        jar = SimpleCookie()
        for value in start.headers.get_list("Set-Cookie"):
            jar.load(value)
        # Keep the real state cookie but withhold its independent PKCE verifier.
        cookies = "; ".join(name + "=" + item.value for name, item in jar.items()
                            if not name.startswith("cps-pkce-"))
        self.assertEqual(self.fetch("/argo/oauth_callback?" + urlencode({"state": state, "code": "x"}),
                                    headers={"Cookie": cookies}).code, 403)
        self.assertFalse(self.token_calls)
        for target in ("https://evil.example", "//evil.example", "/services/admin/app",
                       "/argo/\\evil.example", "/argo/%255cevil.example", "/%252f%252fevil.example",
                       "/argo/%0d%0aLocation:evil"):
            with self.subTest(target=target):
                response = self.fetch("/fixture/redirect?" + urlencode({"next": target}),
                                      follow_redirects=False)
                self.assertEqual(response.code, 400)
        response = self.fetch("/fixture/redirect?" + urlencode({"next": "/argo/workflows/cps-workflows?limit=50"}),
                              follow_redirects=False)
        self.assertEqual(response.headers["Location"], "/argo/workflows/cps-workflows?limit=50")

    def test_normal_users_are_distinct_and_browser_headers_cannot_replace_identity(self):
        for token in ("visitor-alice", "visitor-bob"):
            response = self.fetch("/argo/api/v1/workflows/cps-workflows?limit=50", headers=self.headers(
                token, Authorization="Bearer browser-admin", **{"X-CPS-Hub": "cit",
                    "X-Forwarded-Host": "evil.example", "X-Kubernetes-Token": "secret"}))
            self.assertEqual(response.code, 200)
            self.assertEqual(json.loads(response.body)["visitor"], token)
            request = self.outbound[-1]
            self.assertEqual(request.url, "https://compute.example/argo/api/v1/workflows/cps-workflows?limit=50")
            self.assertEqual(dict(request.headers), {"Authorization": "Bearer " + token, "X-CPS-Hub": "cps"})
            self.assertTrue(request.validate_cert)
            self.assertEqual(request.ca_certs, self.ca.name)
            self.assertFalse(request.follow_redirects)
            self.assertNotIn("upstream-cookie", str(response.headers))
            self.assertNotIn("X-Secret", response.headers)
        no_cookie = self.fetch("/argo/api/v1/info?token=visitor-alice", headers={
            "Authorization": "Bearer visitor-alice"}, follow_redirects=False)
        self.assertEqual(no_cookie.code, 302)

    def test_service_scope_and_user_kind_are_required_without_admin_roles(self):
        for token in ("visitor-no-scope", "service-token"):
            self.assertEqual(self.fetch("/argo/api/v1/info", headers=self.headers(token)).code, 403)
        self.assertFalse(self.outbound)
        self.assertEqual(self.auth.access_scopes, {"access:services!service=cps-argo-ui"})
        self.assertEqual(self.fetch("/argo/", headers=self.headers(**{"Sec-Fetch-Mode": "navigate"}),
                                    follow_redirects=False).headers[
            "Location"], "/argo/workflows/cps-workflows?limit=50")

    def test_valid_visitor_xsrf_cannot_enable_writes_or_authenticate_another_visitor(self):
        visitor = "visitor-alice"
        session = "browser-session"
        xsrf_id = (session + ":" + hashlib.sha256(visitor.encode()).hexdigest()).encode()
        signed = web.create_signed_value(self._app.settings["cookie_secret"], "_xsrf", xsrf_id)
        xsrf = base64.urlsafe_b64encode(signed).rstrip(b"=").decode()
        headers = self.headers(visitor)
        headers["Cookie"] += "; jupyterhub-session-id=" + session + "; _xsrf=" + xsrf
        headers["X-XSRFToken"] = xsrf
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = self.fetch("/argo/api/v1/workflows/cps-workflows/wf", method=method,
                                  body=b"{}" if method in ("POST", "PUT", "PATCH") else None,
                                  headers=headers, allow_nonstandard_methods=True)
            self.assertEqual(response.code, 405)
        # The same signed token fails Hub's visitor binding with Bob's OAuth cookie.
        headers["Cookie"] = self.cookie("visitor-bob") + "; jupyterhub-session-id=" + session + "; _xsrf=" + xsrf
        response = self.fetch("/argo/api/v1/workflows/cps-workflows/wf", method="POST",
                              body=b"{}", headers=headers)
        self.assertEqual(response.code, 403)
        self.assertFalse(self.outbound)

    def test_routes_namespace_actions_and_cross_origin_are_closed(self):
        for path in (
            "/argo/api/v1/workflows/default", "/argo/api/v1/workflows/cps-workflows/wf/retry",
            "/argo/api/v1/cron-workflows/cps-workflows", "/argo/api/v1/templates/cps-workflows",
            "/argo/api/v1/workflows/cps-workflows/%2e%2e", "/argo/api/v1/internal",
            "/argo/artifact-files/cps-workflows/workflows/wf/node/outputs/snapshot",
            "/argo/artifact-files/cps-workflows/workflows/wf/node/outputs/arbitrary",
            "/argo/arbitrary/index.html", "/argo/api/v1/pods/cps-workflows",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.fetch(path, headers=self.headers()).code, 404)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = self.fetch("/argo/api/v1/workflows/cps-workflows/wf", method=method,
                                  body=b"{}" if method in ("POST", "PUT", "PATCH") else None,
                                  headers=self.headers(), allow_nonstandard_methods=True)
            self.assertEqual(response.code, 403)  # Hub XSRF validation precedes prepare().
        for extra in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.fetch("/argo/api/v1/info", headers=self.headers(**extra)).code, 403)
        self.assertFalse(self.outbound)

    def test_ui_assets_only_go_to_native_argo_without_any_credentials(self):
        for path in ("/argo/workflows", "/argo/workflows/cps-workflows?limit=50", "/argo/main.123.js",
                     "/argo/assets/logo.svg"):
            response = self.fetch(path, headers=self.headers(**{"Sec-Fetch-Mode": "no-cors"}))
            self.assertEqual(response.code, 200)
            self.assertEqual(dict(self.outbound[-1].headers), {})
            self.assertTrue(self.outbound[-1].url.startswith("https://native-argo.example/argo/"))
            self.assertEqual(self.outbound[-1].ca_certs, self.ca.name)
            self.assertNotIn("native-secret", str(response.headers))
        response = self.fetch("/argo/api/v1/info?fixture=redirect", headers=self.headers())
        self.assertEqual(response.code, 502)
        self.assertNotIn("Location", response.headers)

    def test_native_login_reauthorizes_with_canonical_return_target(self):
        response = self.fetch("/argo/login?redirect=https://evil.example", follow_redirects=False)
        self.assertEqual(response.code, 302)
        args = parse_qs(urlsplit(response.headers["Location"]).query)
        self.assertEqual(args["client_id"], ["service-cps-argo-ui"])
        self.assertEqual(self.auth.get_next_url(args["state"][0]),
                         "/argo/workflows/cps-workflows?limit=50")

    @gen_test
    async def test_native_eventsource_cookie_stream_flushes_before_completion(self):
        chunks = []
        request = HTTPRequest(self.get_url("/argo/api/v1/workflow-events/cps-workflows"),
                              headers=self.headers(), streaming_callback=chunks.append)
        response = await self.http_client.fetch(request)
        self.assertEqual(response.code, 200)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertIn(b'"owner":"visitor-alice"', chunks[0])
        self.assertIn(b'"MODIFIED"', b"".join(chunks))
        self.assertEqual(response.headers["Content-Type"], "text/event-stream")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Accel-Buffering"], "no")
        self.assertNotIn("upstream-cookie", str(response.headers))
        self.assertEqual(self.outbound[-1].request_timeout, 30)
        self.assertEqual(dict(self.outbound[-1].headers), {
            "Authorization": "Bearer visitor-alice", "X-CPS-Hub": "cps"})

    def test_fixed_owned_artifact_stream_and_head(self):
        path = "/argo/artifact-files/cps-workflows/workflows/wf/node/outputs/executed-notebook"
        response = self.fetch(path, headers=self.headers())
        self.assertEqual(response.code, 200)
        self.assertEqual(response.body, b'{"cells":[]}\n')
        self.assertEqual(response.headers["Content-Disposition"], 'attachment; filename="executed.ipynb"')
        self.assertEqual(response.headers["Content-Type"], "application/octet-stream")
        self.assertEqual(response.headers["Content-Length"], "13")
        self.assertEqual(self.outbound[-1].request_timeout, 120)
        response = self.fetch(path, method="HEAD", headers=self.headers())
        self.assertEqual(response.code, 200)
        self.assertEqual(response.body, b"")

    @gen_test
    async def test_truncated_artifact_aborts_browser_transfer(self):
        self.truncate_artifact = True
        chunks = []
        request = HTTPRequest(self.get_url(
            "/argo/artifact-files/cps-workflows/workflows/wf/node/outputs/executed-notebook"),
            headers=self.headers(), streaming_callback=chunks.append)
        with self.assertRaises(HTTPClientError) as caught:
            await self.http_client.fetch(request)
        self.assertEqual(caught.exception.code, 599)
        self.assertEqual(b"".join(chunks), b'{"cells":[]}')

    def test_stream_deadline_finishes_finite_response_and_unexpected_type_is_hidden(self):
        self.stream_delay = 0.2
        with patch("e2x_course_hub.cps.argo_service.STREAM_SECONDS", 0.03):
            response = self.fetch("/argo/api/v1/workflow-events/cps-workflows", headers=self.headers())
        self.assertEqual(response.code, 200)
        self.assertIn(b'"ADDED"', response.body)
        self.assertNotIn(b'"MODIFIED"', response.body)
        self.assertEqual(self.outbound[-1].request_timeout, 0.03)
        self.stream_type = "text/html"
        self.stream_delay = 0
        response = self.fetch("/argo/api/v1/workflows/cps-workflows/wf/log", headers=self.headers())
        self.assertEqual(response.code, 502)
        self.assertNotIn(b'"owner"', response.body)

    def test_metadata_and_asset_response_limits(self):
        with patch("e2x_course_hub.cps.argo_service.MAX_RESPONSE_BYTES", 10):
            response = self.fetch("/argo/api/v1/info", headers=self.headers())
            self.assertEqual(response.code, 502)
            response = self.fetch("/argo/main.js", headers=self.headers(**{"Sec-Fetch-Mode": "no-cors"}))
            self.assertEqual(response.code, 502)

    @gen_test
    async def test_large_legitimate_artifact_succeeds_above_pending_buffer_cap(self):
        self.artifact_body = b"n" * (8 * 1024 * 1024)
        counters = []
        original_queue = ArgoProxyHandler._queue_write

        def observe_queue(handler, data):
            pending = original_queue(handler, data)
            counters.append(handler.pending_flush_bytes)
            return pending

        with patch.object(ArgoProxyHandler, "_queue_write", observe_queue):
            response = await self.http_client.fetch(HTTPRequest(self.get_url(
                "/argo/artifact-files/cps-workflows/workflows/wf/node/outputs/executed-notebook"),
                headers=self.headers()))
        self.assertEqual(response.body, self.artifact_body)
        self.assertEqual(response.headers["Content-Length"], str(len(self.artifact_body)))
        self.assertLessEqual(max(counters), 1024 * 1024)
        self.assertEqual(self._app.settings["argo_request_limits"].active_requests, 0)

    @gen_test
    async def test_global_capacity_includes_assets_and_releases_after_upstream_error(self):
        transport = ControlledTransport()
        self._app.settings["argo_http_client"] = transport
        responses = {index: self.http_client.fetch(HTTPRequest(self.get_url(
                     "/argo/api/v1/info?fixture=" + str(index)),
                     headers=self.headers()), raise_error=False) for index in range(4)}
        held = [await transport.requests.get() for _ in range(4)]
        limits = self._app.settings["argo_request_limits"]
        self.assertEqual(limits.active_requests, 4)
        for path, headers in (("/argo/api/v1/version", self.headers()),
                              ("/argo/main.js", self.headers(**{"Sec-Fetch-Mode": "no-cors"}))):
            rejected = await self.http_client.fetch(HTTPRequest(self.get_url(path), headers=headers),
                                                    raise_error=False)
            self.assertEqual(rejected.code, 503)
        self.assertTrue(transport.requests.empty())
        request, upstream = held.pop(0)
        upstream.set_exception(HTTPClientError(599, "Fixture remote error"))
        failed_index = int(parse_qs(urlsplit(request.url).query)["fixture"][0])
        self.assertEqual((await responses.pop(failed_index)).code, 502)
        self.assertEqual(limits.active_requests, 3)
        accepted = self.http_client.fetch(HTTPRequest(self.get_url("/argo/api/v1/version"),
                     headers=self.headers()), raise_error=False)
        request, upstream = await transport.requests.get()
        upstream.set_result(transport.response(request))
        self.assertEqual((await accepted).code, 200)
        for request, upstream in held:
            upstream.set_result(transport.response(request))
        self.assertTrue(all(response.code == 200 for response in await asyncio.gather(*responses.values())))
        self.assertEqual(limits.active_requests, 0)

    @gen_test
    async def test_slow_flush_cap_aborts_before_extra_write_and_releases_after_drain(self):
        transport = ControlledTransport()
        self._app.settings["argo_http_client"] = transport
        handlers, pending_flushes, writes = [], [], []
        original_initialize = ArgoProxyHandler.initialize
        original_write = ArgoProxyHandler.write

        def initialize(handler):
            original_initialize(handler)
            handlers.append(handler)

        def slow_flush(handler, *args, **kwargs):
            pending = asyncio.get_running_loop().create_future()
            pending_flushes.append(pending)
            return pending

        def observe_write(handler, data):
            writes.append(data)
            return original_write(handler, data)

        with patch.object(ArgoProxyHandler, "initialize", initialize), \
                patch.object(ArgoProxyHandler, "flush", slow_flush), \
                patch.object(ArgoProxyHandler, "write", observe_write):
            browser = self.http_client.fetch(HTTPRequest(self.get_url(
                "/argo/artifact-files/cps-workflows/workflows/wf/node/outputs/executed-notebook"),
                headers=self.headers()))
            request, upstream = await transport.requests.get()
            request.header_callback("HTTP/1.1 200 OK\r\n")
            request.header_callback("Content-Type: application/octet-stream\r\n")
            request.header_callback("Content-Length: 3145728\r\n")
            for _ in range(4):
                request.streaming_callback(b"x" * (256 * 1024))
            handler = handlers[0]
            self.assertEqual(handler.pending_flush_bytes, 1024 * 1024)
            self.assertEqual(len(writes), 4)
            pending_flushes[0].set_result(None)
            await asyncio.sleep(0)
            self.assertEqual(handler.pending_flush_bytes, 768 * 1024)
            request.streaming_callback(b"y" * (256 * 1024))
            self.assertEqual(handler.pending_flush_bytes, 1024 * 1024)
            self.assertEqual(len(writes), 5)
            request.streaming_callback(b"over-limit")
            request.streaming_callback(b"must-never-be-written-after-abort")
            with self.assertRaises(HTTPClientError):
                await browser
            self.assertEqual(len(writes), 5)
            self.assertEqual(handler.pending_flush_bytes, 1024 * 1024)
            self.assertEqual(self._app.settings["argo_request_limits"].active_requests, 1)
            # A canceled local await does not pretend the remote transfer ended.
            upstream.set_result(transport.response(request))
            await asyncio.sleep(0)
            self.assertEqual(self._app.settings["argo_request_limits"].active_requests, 1)
            for pending in pending_flushes:
                if not pending.done():
                    pending.set_exception(StreamClosedError())
            await asyncio.sleep(0)
            self.assertEqual(handler.pending_flush_bytes, 0)
            self.assertEqual(self._app.settings["argo_request_limits"].active_requests, 0)

    @gen_test
    async def test_flush_error_aborts_sse_and_no_later_callback_writes(self):
        transport = ControlledTransport()
        self._app.settings["argo_http_client"] = transport
        handlers, pending_flushes = [], []
        original_initialize = ArgoProxyHandler.initialize

        def initialize(handler):
            original_initialize(handler)
            handlers.append(handler)

        def delayed_flush(handler, *args, **kwargs):
            pending = asyncio.get_running_loop().create_future()
            pending_flushes.append(pending)
            return pending

        with patch.object(ArgoProxyHandler, "initialize", initialize), \
                patch.object(ArgoProxyHandler, "flush", delayed_flush):
            browser = self.http_client.fetch(HTTPRequest(self.get_url(
                "/argo/api/v1/workflow-events/cps-workflows"), headers=self.headers()))
            request, upstream = await transport.requests.get()
            request.header_callback("HTTP/1.1 200 OK\r\n")
            request.header_callback("Content-Type: text/event-stream\r\n")
            request.streaming_callback(b"data: first\n\n")
            self.assertEqual(handlers[0].pending_flush_bytes, 13)
            pending_flushes[0].set_exception(StreamClosedError())
            await asyncio.sleep(0)
            request.streaming_callback(b"data: forbidden\n\n")
            with self.assertRaises(HTTPClientError):
                await browser
            self.assertEqual(len(pending_flushes), 1)
            self.assertEqual(handlers[0].pending_flush_bytes, 0)
            upstream.set_exception(HTTPClientError(599, "Fixture independent timeout"))
            await asyncio.sleep(0)
            self.assertEqual(self._app.settings["argo_request_limits"].active_requests, 0)

    @gen_test
    async def test_disconnect_keeps_slot_until_remote_deadline_and_releases(self):
        transport = ControlledTransport()
        self._app.settings["argo_http_client"] = transport
        headers = self.headers()
        client = TCPClient()
        stream = await client.connect("127.0.0.1", self.get_http_port())
        wire = "GET /argo/api/v1/workflow-events/cps-workflows HTTP/1.1\r\nHost: localhost\r\n"
        wire += "\r\n".join(name + ": " + value for name, value in headers.items()) + "\r\n\r\n"
        await stream.write(wire.encode())
        request, upstream = await transport.requests.get()
        request.header_callback("HTTP/1.1 200 OK\r\n")
        request.header_callback("Content-Type: text/event-stream\r\n")
        request.streaming_callback(b"data: first\n\n")
        await stream.read_until(b"\r\n\r\n")
        stream.close()
        await asyncio.sleep(0.01)
        limits = self._app.settings["argo_request_limits"]
        self.assertEqual(limits.active_requests, 1)
        request.streaming_callback(b"data: discarded-after-disconnect\n\n")
        upstream.set_exception(HTTPClientError(599, "Fixture independent timeout"))
        await asyncio.sleep(0)
        self.assertEqual(limits.active_requests, 0)
        client.close()


if __name__ == "__main__":
    unittest.main()
