"""Independent, read-only Hub OAuth shell for the owner-filtered native Argo UI.

This factory is deliberately not wired into the administrative course service.
Compute owns all workflow authorization; the native Argo server supplies assets only.
"""

import asyncio
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from jupyterhub.services.auth import HubOAuthenticated
from tornado import web
from tornado.httpclient import AsyncHTTPClient, HTTPClientError, HTTPRequest

from .oauth import CPSHubOAuth, PKCELoginMixin, SafeOAuthCallbackHandler

PREFIX = "/argo/"
WORKFLOW_PAGE = "/argo/workflows/cps-workflows?limit=50"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
MAX_PENDING_FLUSH_BYTES = 1024 * 1024
MAX_PROXY_REQUESTS = 4
STREAM_SECONDS = 30
_NAME = r"[a-z0-9](?:[a-z0-9-]{0,251}[a-z0-9])?"
_NODE = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}"
_API = re.compile(
    r"api/v1/(?:info|version|userinfo|workflows/cps-workflows(?:/"
    + _NAME + r"(?:/log)?)?|workflow-events/cps-workflows)"
)
_ARTIFACT = re.compile(
    r"artifact-files/cps-workflows/workflows/" + _NAME + "/" + _NODE
    + r"/(?:inputs/snapshot|outputs/executed-notebook)"
)
_ASSET = re.compile(
    r"(?:assets/)?[A-Za-z0-9_-]+(?:[./][A-Za-z0-9_-]+)*"
    r"\.(?:js|css|png|svg|ico|woff2?|ttf)"
)
_PAGE = re.compile(r"workflows(?:/cps-workflows(?:/" + _NAME + r")?)?")


def _https_url(value, label, *, root=False):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in value)
            or "\\" in value or "%" in value
            or (root and parsed.path not in ("", "/"))):
        raise ValueError(label + " must be a fixed HTTPS URL without credentials or query")
    # Validate the port now, rather than failing after accepting browser traffic.
    parsed.port
    if any(part in (".", "..") for part in parsed.path.split("/")):
        raise ValueError(label + " contains a non-canonical path")
    return value.rstrip("/")


@dataclass(frozen=True)
class ArgoServiceConfig:
    source: str
    oauth_client_id: str
    oauth_client_secret: str = field(repr=False)
    cookie_secret: str = field(repr=False)
    public_origin: str
    hub_api_url: str
    hub_authorization_url: str
    compute_url: str
    native_argo_url: str
    hub_ca_file: str
    compute_ca_file: str
    native_argo_ca_file: str

    def __post_init__(self):
        if self.source not in ("cps", "cit"):
            raise ValueError("source must be cps or cit")
        if self.oauth_client_id != "service-" + self.service_name:
            raise ValueError("a dedicated service-{source}-argo-ui OAuth client is required")
        if not self.oauth_client_secret or len(self.cookie_secret) < 32:
            raise ValueError("a dedicated client secret and >=32-character cookie secret are required")
        for name in ("public_origin", "hub_api_url", "hub_authorization_url", "compute_url",
                     "native_argo_url"):
            normalized = _https_url(getattr(self, name), name,
                                    root=name in ("public_origin", "compute_url"))
            object.__setattr__(self, name, normalized)
        if urlsplit(self.hub_api_url).path.rstrip("/").split("/")[-1] != "api":
            raise ValueError("hub_api_url must identify the existing Hub API")
        if not self.hub_authorization_url.endswith("/api/oauth2/authorize"):
            raise ValueError("hub_authorization_url must identify the existing Hub OAuth endpoint")
        if urlsplit(self.native_argo_url).path != "/argo":
            raise ValueError("native_argo_url must use the reviewed /argo base path")
        for name in ("hub_ca_file", "compute_ca_file", "native_argo_ca_file"):
            if not Path(getattr(self, name)).is_file():
                raise ValueError(name + " must identify an installed trusted CA bundle")

    @property
    def service_name(self):
        return self.source + "-argo-ui"

    @classmethod
    def from_env(cls):
        """No fallback to the administrative service's JUPYTERHUB_* credentials."""
        fields = cls.__dataclass_fields__
        values = {}
        for name in fields:
            key = "ARGO_USER_" + name.upper()
            value = os.environ.get(key)
            if not value:
                raise ValueError(key + " is required")
            values[name] = value
        return cls(**values)


class ArgoHubOAuth(CPSHubOAuth):
    """Browser identity comes only from this service's signed OAuth cookie."""

    def get_token(self, handler, in_cookie=True):
        # HubAuth probes headers with in_cookie=False before checking cookie auth.
        # Returning None there preserves Hub's cookie/XSRF checks and prevents a
        # caller-supplied Authorization or URL token from changing the visitor.
        return self._get_token_cookie(handler) if in_cookie else None

    def set_state_cookie(self, handler, next_url=None):
        # Native Argo redirects expired API sessions to /argo/login?redirect=...
        # Reauthorization always returns to the fixed owner-filtered landing page.
        if handler.request.path == PREFIX + "login":
            next_url = WORKFLOW_PAGE
        return super().set_state_cookie(handler, next_url=next_url)


class ArgoRequestLimits:
    """App-global capacity, including requests draining after browser aborts."""

    def __init__(self):
        self.active_requests = 0

    def acquire(self):
        if self.active_requests >= MAX_PROXY_REQUESTS:
            return False
        self.active_requests += 1
        return True

    def release(self):
        self.active_requests -= 1


class ArgoHandler(PKCELoginMixin, HubOAuthenticated, web.RequestHandler):
    def initialize(self):
        self.hub_auth = self.settings["argo_hub_auth"]
        self.config = self.settings["argo_config"]
        self.pending_flush_bytes = 0
        self._flush_futures = {}
        self._slot_acquired = False
        self._proxy_complete = False
        self._stream_aborted = False
        self._client_disconnected = False
        self._upstream_request = None
        self._upstream_future = None
        self._deadline_handle = None
        self._transfer_timed_out = False

    def _take_request_slot(self):
        if not self.settings["argo_request_limits"].acquire():
            raise web.HTTPError(503, reason="Argo service capacity is exhausted")
        self._slot_acquired = True

    def _maybe_release_request_slot(self):
        if (not self._slot_acquired or self.pending_flush_bytes
                or (self._upstream_request is not None and not self._upstream_request.done())
                or not (self._proxy_complete or self._client_disconnected or self._stream_aborted)):
            return
        self._slot_acquired = False
        self.settings["argo_request_limits"].release()
        if self._deadline_handle is not None:
            self._deadline_handle.cancel()

    def _fetch_upstream(self, request):
        self._deadline_handle = asyncio.get_running_loop().call_later(
            request.request_timeout, self._expire_transfer)
        self._upstream_request = self.settings["argo_http_client"].fetch(request, raise_error=False)
        self._upstream_request.add_done_callback(self._upstream_done)
        self._upstream_future = asyncio.shield(self._upstream_request)
        return self._upstream_future

    def _upstream_done(self, future):
        # The browser/shield may already have gone away. Retrieve any late
        # network failure without changing what a live awaiter will receive.
        if not future.cancelled():
            future.exception()
        self._maybe_release_request_slot()

    def _expire_transfer(self):
        self._transfer_timed_out = True
        if self.pending_flush_bytes:
            self._abort_forward()

    def _abort_forward(self):
        if self._stream_aborted:
            return
        self._stream_aborted = True
        self._finished = True
        if self._upstream_future is not None:
            self._upstream_future.cancel()  # shield only; raw HTTP request remains independently bounded
        self.request.connection.close()
        self._maybe_release_request_slot()

    def _flush_done(self, future):
        size = self._flush_futures.pop(future, None)
        if size is None:
            return
        self.pending_flush_bytes -= size
        try:
            future.result()
        except (Exception, asyncio.CancelledError):
            self._abort_forward()
        self._maybe_release_request_slot()

    def _queue_write(self, data):
        # A completed flush may have a callback queued behind this upstream
        # read callback. Account for it immediately before checking capacity.
        for pending in tuple(self._flush_futures):
            if pending.done():
                self._flush_done(pending)
        if self._stream_aborted or self._client_disconnected or self._finished:
            return None
        if self._transfer_timed_out or self.pending_flush_bytes + len(data) > MAX_PENDING_FLUSH_BYTES:
            self._abort_forward()
            return None
        self.pending_flush_bytes += len(data)
        try:
            self.write(data)
            pending = self.flush()
        except Exception:
            self.pending_flush_bytes -= len(data)
            self._abort_forward()
            return None
        self._flush_futures[pending] = len(data)
        if pending.done():
            self._flush_done(pending)
        else:
            pending.add_done_callback(self._flush_done)
        return pending

    async def _finish_buffered(self, body):
        # This async path can await real browser backpressure. The upstream
        # streaming callback cannot; it uses the same bounded queue and aborts.
        for start in range(0, len(body or b""), 64 * 1024):
            pending = self._queue_write(body[start:start + 64 * 1024])
            if pending is None:
                return
            try:
                await asyncio.shield(pending)
            except (Exception, asyncio.CancelledError):
                self._abort_forward()
                return
        if not self._finished:
            self.finish()

    def on_connection_close(self):
        self._client_disconnected = True
        self._stream_aborted = True
        if self._upstream_future is not None:
            self._upstream_future.cancel()
        self._maybe_release_request_slot()
        super().on_connection_close()

    def set_default_headers(self):
        self.set_header("Cache-Control", "no-store")
        self.set_header("X-Content-Type-Options", "nosniff")
        self.set_header("Referrer-Policy", "same-origin")
        self.set_header("Content-Security-Policy",
                        "frame-ancestors 'self'; base-uri 'self'; connect-src 'self'; object-src 'none'")

    def prepare(self):
        if "%" in self.request.path or "\\" in self.request.path:
            raise web.HTTPError(404)
        if self.request.method not in ("GET", "HEAD"):
            raise web.HTTPError(405)
        origin = self.request.headers.get("Origin")
        site = self.request.headers.get("Sec-Fetch-Site")
        if (origin and origin != self.config.public_origin) or site == "cross-site":
            raise web.HTTPError(403, reason="Same-origin requests are required")

    def check_hub_user(self, model):
        if model.get("kind", "user") != "user":
            raise web.HTTPError(403, reason="A Hub user identity is required")
        return super().check_hub_user(model)

    def check_xsrf_cookie(self):
        # Native EventSource cannot set X-XSRFToken. No write route is installed;
        # read-only browser requests with same-origin metadata may use cookies.
        route = self.request.path[len(PREFIX):] if self.request.path.startswith(PREFIX) else ""
        if (self.request.method in ("GET", "HEAD")
                and (_API.fullmatch(route) or _ARTIFACT.fullmatch(route))
                and self.request.headers.get("Sec-Fetch-Site") == "same-origin"
                and self.request.headers.get("Origin", self.config.public_origin)
                == self.config.public_origin):
            return
        return super().check_xsrf_cookie()


class ArgoCallbackHandler(SafeOAuthCallbackHandler):
    def initialize(self):
        self.hub_auth = self.settings["argo_hub_auth"]

    def redirect(self, url, permanent=False, status=None):
        decoded = url
        for _ in range(5):
            decoded = unquote(decoded)
        if "%" in decoded or not urlsplit(decoded).path.startswith(PREFIX):
            raise web.HTTPError(400, reason="OAuth redirect must remain under /argo/")
        return super().redirect(url, permanent=permanent, status=status)


class ArgoRootHandler(ArgoHandler):
    @web.authenticated
    def get(self):
        self.redirect(WORKFLOW_PAGE)


class ArgoLoginHandler(ArgoHandler):
    def get(self):
        self.redirect(self.get_login_url())


class ArgoProxyHandler(ArgoHandler):
    def prepare(self):
        super().prepare()
        path = self.request.path[len(PREFIX):]
        if not _API.fullmatch(path) and not _ARTIFACT.fullmatch(path):
            raise web.HTTPError(404)
        if self.request.method == "HEAD" and not _ARTIFACT.fullmatch(path):
            raise web.HTTPError(405)

    async def _forward(self, path):
        is_api = bool(_API.fullmatch(path))
        is_artifact = bool(_ARTIFACT.fullmatch(path))
        if self.request.method == "HEAD" and not is_artifact:
            raise web.HTTPError(405)
        if not is_api and not is_artifact:
            raise web.HTTPError(404)
        token = self.hub_auth.get_token(self)
        if not token:
            raise web.HTTPError(401)
        # The downstream adapter validates names, queries, ownership and outputs.
        # Neither namespace nor upstream authority is a browser choice.
        url = self.config.compute_url + PREFIX + path
        if self.request.query:
            url += "?" + self.request.query
        headers = {"Authorization": "Bearer " + token, "X-CPS-Hub": self.config.source}
        is_stream = path.endswith("/log") or path.startswith("api/v1/workflow-events/")
        request = HTTPRequest(url, method=self.request.method, headers=headers,
                              follow_redirects=False, validate_cert=True,
                              ca_certs=self.config.compute_ca_file,
                              request_timeout=STREAM_SECONDS if is_stream else (120 if is_artifact else 60))
        if is_stream or (is_artifact and self.request.method == "GET"):
            await self._stream(request, artifact=is_artifact)
            return
        try:
            response = await self._fetch_upstream(request)
        except asyncio.CancelledError:
            return
        except HTTPClientError as error:
            raise web.HTTPError(502, reason="Compute service unavailable") from error
        if 300 <= response.code < 400:
            raise web.HTTPError(502, reason="Upstream redirects are disabled")
        if len(response.body or b"") > MAX_RESPONSE_BYTES:
            raise web.HTTPError(502, reason="Compute response exceeds the service limit")
        self.set_status(response.code)
        for name in ("Content-Type", "Content-Disposition"):
            if response.headers.get(name):
                self.set_header(name, response.headers[name])
        await self._finish_buffered(response.body)

    async def _stream(self, request, *, artifact=False):
        # Header inspection precedes chunk forwarding; error bodies, cookies,
        # redirect locations and authority-related headers never reach browsers.
        status = {"code": None, "type": None, "disposition": None, "length": None,
                  "bytes": 0, "started": False}
        allowed_types = {"application/octet-stream"} if artifact else {"text/event-stream"}
        limit = MAX_ARTIFACT_BYTES if artifact else MAX_RESPONSE_BYTES
        seconds = request.request_timeout

        def header(line):
            text = line.decode("latin1") if isinstance(line, bytes) else line
            if text.startswith("HTTP/"):
                status["code"] = int(text.split()[1])
            elif text.lower().startswith("content-type:"):
                status["type"] = text.split(":", 1)[1].strip().split(";", 1)[0].lower()
            elif artifact and text.lower().startswith("content-disposition:"):
                status["disposition"] = text.split(":", 1)[1].strip()
            elif artifact and text.lower().startswith("content-length:"):
                value = text.split(":", 1)[1].strip()
                if value.isascii() and value.isdecimal() and len(value) <= 12:
                    length = int(value)
                    if length <= limit:
                        status["length"] = length

        def chunk(data):
            if (self._finished or self._stream_aborted or self._client_disconnected
                    or status["code"] != 200 or status["type"] not in allowed_types
                    or (artifact and status["length"] is None)):
                return
            status["bytes"] += len(data)
            if status["bytes"] > limit:
                self._abort_forward()
                return
            if not status["started"]:
                self.set_header("Content-Type", status["type"])
                if artifact:
                    # The owner adapter spools/validates the object first. Keep
                    # its bounded length so a truncated transfer cannot look
                    # like a successful completed chunked download.
                    self.set_header("Content-Length", status["length"])
                if status["disposition"]:
                    self.set_header("Content-Disposition", status["disposition"])
                self.set_header("X-Accel-Buffering", "no")
                status["started"] = True
            # Tornado's simple HTTP client invokes streaming_callback
            # synchronously; returning a coroutine silently drops every chunk.
            # The queue accounts for pending bytes until flush completion and
            # aborts before exceeding its hard limit for slow consumers.
            self._queue_write(data)

        request.header_callback = header
        request.streaming_callback = chunk
        # Canceling Tornado's raw fetch future doesn't cancel its connection and
        # makes a later timeout log an unhandled cancellation. Cancel the shield
        # while the independently bounded HTTP request completes underneath it.
        self._fetch_upstream(request)
        try:
            response = await asyncio.wait_for(self._upstream_future, seconds)
        except asyncio.CancelledError:
            return
        except (asyncio.TimeoutError, HTTPClientError):
            if not status["started"]:
                raise web.HTTPError(502, reason="Compute stream unavailable")
            if artifact:
                self._abort_forward()
                return
        else:
            if response.code != 200 or status["type"] not in allowed_types:
                raise web.HTTPError(response.code if response.code in (401, 403, 404, 429) else 502,
                                    reason="Compute stream rejected")
            if artifact and (status["length"] is None or status["bytes"] != status["length"]):
                if status["started"]:
                    self._abort_forward()
                    return
                raise web.HTTPError(502, reason="Compute artifact length is invalid")
            if not status["started"]:
                self.set_header("Content-Type", status["type"])
                if artifact:
                    self.set_header("Content-Length", status["length"])
                if status["disposition"]:
                    self.set_header("Content-Disposition", status["disposition"])
                self.set_header("X-Accel-Buffering", "no")
        finally:
            self._upstream_future = None
        if not self._finished:
            self.finish()

    @web.authenticated
    async def get(self, path):
        self._take_request_slot()
        try:
            await self._forward(path)
        finally:
            self._proxy_complete = True
            self._maybe_release_request_slot()

    @web.authenticated
    async def head(self, path):
        self._take_request_slot()
        try:
            await self._forward(path)
        finally:
            self._proxy_complete = True
            self._maybe_release_request_slot()


class ArgoAssetsHandler(ArgoHandler):
    def prepare(self):
        super().prepare()
        path = self.request.path[len(PREFIX):]
        if not _PAGE.fullmatch(path) and not (_ASSET.fullmatch(path) and ".." not in path):
            raise web.HTTPError(404)

    @web.authenticated
    async def get(self, path):
        self._take_request_slot()
        try:
            await self._forward_asset(path)
        finally:
            self._proxy_complete = True
            self._maybe_release_request_slot()

    async def _forward_asset(self, path):
        if _PAGE.fullmatch(path):
            upstream_path = ""  # SPA index from the fixed native Argo base URL.
        elif _ASSET.fullmatch(path) and ".." not in path:
            upstream_path = path
        else:
            raise web.HTTPError(404)
        try:
            response = await self._fetch_upstream(HTTPRequest(
                self.config.native_argo_url + "/" + upstream_path,
                # No Hub, Kubernetes, Argo or S3 credential is sent for UI assets.
                headers={}, follow_redirects=False, validate_cert=True,
                ca_certs=self.config.native_argo_ca_file, request_timeout=30))
        except asyncio.CancelledError:
            return
        except HTTPClientError as error:
            raise web.HTTPError(502, reason="Native Argo asset unavailable") from error
        if response.code != 200:
            raise web.HTTPError(502, reason="Native Argo asset unavailable")
        if len(response.body or b"") > MAX_RESPONSE_BYTES:
            raise web.HTTPError(502, reason="Native Argo asset exceeds the service limit")
        self.set_header("Content-Type", response.headers.get("Content-Type", "application/octet-stream"))
        await self._finish_buffered(response.body)


def create_argo_service(config, *, http_client=None):
    """Build a standalone Tornado app; never changes the admin app or live Hub."""
    if not isinstance(config, ArgoServiceConfig):
        raise TypeError("ArgoServiceConfig is required")
    auth = ArgoHubOAuth(
        oauth_client_id=config.oauth_client_id, api_token=config.oauth_client_secret,
        api_url=config.hub_api_url, client_ca=config.hub_ca_file,
        oauth_redirect_uri=config.public_origin + PREFIX + "oauth_callback",
        oauth_authorization_url=config.hub_authorization_url,
        oauth_token_url=config.hub_api_url + "/oauth2/token",
        base_url=PREFIX, access_scopes={"access:services!service=" + config.service_name},
        cookie_options={"secure": True, "httponly": True, "samesite": "Lax"},
        pkce_enabled=True, allow_token_in_url=False, allow_websocket_cookie_auth=False,
        cache_max_age=0, cookie_host_prefix_enabled=False,
    )
    return web.Application([
        (r"/argo/oauth_callback", ArgoCallbackHandler),
        (r"/argo/login", ArgoLoginHandler),
        (r"/argo/?", ArgoRootHandler),
        (r"/argo/((?:api|artifact-files)/.*)", ArgoProxyHandler),
        (r"/argo/(.*)", ArgoAssetsHandler),
    ], cookie_secret=config.cookie_secret, xsrf_cookies=True,
        xsrf_cookie_kwargs={"secure": True, "samesite": "Lax", "path": PREFIX},
        argo_hub_auth=auth, argo_config=config, argo_request_limits=ArgoRequestLimits(),
        argo_http_client=http_client or AsyncHTTPClient(force_instance=True,
                                                       max_clients=MAX_PROXY_REQUESTS,
                                                       max_body_size=MAX_ARTIFACT_BYTES))
