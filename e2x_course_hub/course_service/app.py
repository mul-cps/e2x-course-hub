import os
import time

from jinja2 import Environment, FileSystemLoader
from jupyterhub.services.auth import HubOAuth, HubOAuthCallbackHandler
from jupyterhub.utils import url_path_join as ujoin
from tornado import web
from traitlets import Bool, Dict, Integer, List, Unicode
from traitlets.config import Application

from ..api.api import API
from ..api.course_api import HubAPI
from ._data import DATA_FILES_PATH
from ..cps.providers import configure_provider
from ..cps.bootstrap import ensure_initial_config
from ..cps.oauth import CPSHubOAuth, SafeOAuthCallbackHandler
from ..cps.handlers import default_handlers as cps_handlers
from ..cps.platform_handlers import default_handlers as platform_handlers
from ..cps.proxy import default_handlers as proxy_handlers
from ..cps.compute import ComputePolicyClient
from ..cps.workspaces import HubWorkspaceAdapter, WorkspaceService
from .handlers import apihandlers, handlers


class CourseServiceApp(Application):
    config_file = Unicode("",help="Python application configuration file").tag(config=True)
    aliases = {"config":"CourseServiceApp.config_file"}
    legacy_rbac_roles = Dict(default_value={"instructor":"instructor"},help="Reviewed legacy role aliases interpreted without renaming Hub groups").tag(config=True)
    data_files_path = Unicode(
        DATA_FILES_PATH,
        help="Path to the data files for the application. Defaults to the package data files.",
    ).tag(config=True)

    tornado_settings = Dict(help="Tornado settings for the application")

    template_path = Unicode(
        os.path.join(DATA_FILES_PATH, "templates", "course_service"),
        help="Path to the Jinja2 templates for the application. Defaults to the package templates.",
    )

    static_path = Unicode(
        os.path.join(DATA_FILES_PATH, "static"),
        help="Path to the static files for the application. Defaults to the package static files.",
    )

    handlers = List(
        help="List of (url, handler) tuples for the application.",
    )

    service_prefix = Unicode(
        os.environ.get("JUPYTERHUB_SERVICE_PREFIX", "/"),
        help=(
            "The URL prefix for the service. Defaults to the "
            "JUPYTERHUB_SERVICE_PREFIX environment variable."
        ),
    ).tag(config=True)

    api_token = Unicode(
        os.environ.get("JUPYTERHUB_API_TOKEN"),
        help=(
            "The API token for the service to authenticate with JupyterHub. "
            "Defaults to the JUPYTERHUB_API_TOKEN environment variable."
        ),
    )

    api_url = Unicode(
        os.environ.get("JUPYTERHUB_API_URL"),
        help=(
            "The base URL of the JupyterHub API. Defaults to the "
            "JUPYTERHUB_API_URL environment variable."
        ),
    ).tag(config=True)

    add_users_to_hub = Bool(
        False,
        help=(
            "Whether to add users to JupyterHub when they are added to a course in the "
            "course service. Defaults to False."
        ),
    ).tag(config=True)

    refresh_interval = Integer(
        300, help="Interval in seconds to refresh server config from disk. Defaults to 300 seconds."
    ).tag(config=True)

    port = Integer(10101, help="The port for the service to listen on. Defaults to 10101.").tag(
        config=True
    )

    server_config_file = Unicode(
        os.environ.get("E2X_COURSE_HUB_CONFIG", "config.yml"),
        help=(
            "Path to the e2x-course-hub server configuration file. "
            "Defaults to 'config.yml' or the E2X_COURSE_HUB_CONFIG "
            "environment variable."
        ),
    ).tag(config=True)

    compute_gateway_url = Unicode("", help="HTTPS public compute gateway for visitor token bridge").tag(config=True)
    compute_policy_url = Unicode('', help='Private HTTPS shared policy API').tag(config=True)
    compute_policy_token = Unicode('', help='Private console-specific service token').tag(config=True)
    oauth_pkce = Bool(False, help="Enable S256 PKCE when qualified against the Hub OAuth endpoint").tag(config=True)
    console_owner = Unicode('cps', help='Console record owner: cps or cit').tag(config=True)
    database_path = Unicode('courses.sqlite', help='Persistent local SQLite database').tag(config=True)
    course_providers = Dict(default_value={'local': {'enabled': True}, 'moodle': {'enabled': False}}, help='Course provider configuration').tag(config=True)

    filesystem_adapter_class = Unicode("", help="Operator-qualified archive adapter dotted class path; empty fails closed").tag(config=True)

    def init_tornado_settings(self):
        CPSHubOAuth.instance().pkce_enabled = self.oauth_pkce
        CPSHubOAuth.instance().cookie_options = {"secure": True, "httponly": True, "samesite": "Lax"}
        provider = configure_provider(self.course_providers, self.database_path, self.console_owner)
        hub_api = HubAPI(api_token=self.api_token, api_url=self.api_url)
        hub_api.audit_provider = provider
        ensure_initial_config(self.server_config_file)
        api = API(
            server_config_file=self.server_config_file,
            hub_api=hub_api,
            add_users_to_hub=self.add_users_to_hub,
            logger=self.log,
        )

        compute = None
        workspace_service = None
        if self.compute_policy_url:
            compute = ComputePolicyClient(self.compute_policy_url, self.compute_policy_token, self.console_owner)
            filesystem = None
            if self.filesystem_adapter_class:
                from traitlets.utils.importstring import import_item
                filesystem = import_item(self.filesystem_adapter_class)()
            workspace_service = WorkspaceService(provider, compute, HubWorkspaceAdapter(hub_api), filesystem=filesystem)
        jinja_env = Environment(loader=FileSystemLoader(self.template_path))
        settings = {
            "api": api,
            "course_provider": provider,
            "compute_policy": compute,
            "compute_gateway_url": self.compute_gateway_url,
            "console_owner": self.console_owner,
            "legacy_rbac_roles": self.legacy_rbac_roles,
            "workspace_service": workspace_service,
            "xsrf_cookies": True,
            "cookie_options": {"secure": True, "httponly": True, "samesite": "Lax"},
            "refresh_interval": self.refresh_interval,
            "last_config_check": time.time(),
            "logger": self.log,
            "jinja_env": jinja_env,
            "cookie_secret": os.urandom(32),
            "service_prefix": self.service_prefix.rstrip("/"),
        }
        self.tornado_settings = settings

    def init_handlers(self):
        app_handlers = [
            (
                ujoin(self.service_prefix, "static", "(.*)"),
                web.StaticFileHandler,
                {"path": self.static_path},
            ),
            (
                ujoin(self.service_prefix, "oauth_callback"),
                SafeOAuthCallbackHandler,
            ),
        ]
        for pattern, handler in apihandlers.default_handlers + cps_handlers + platform_handlers + proxy_handlers + handlers.default_handlers:
            full_pattern = ujoin(self.service_prefix, pattern.lstrip("/"))
            app_handlers.append((full_pattern, handler))
        self.handlers = app_handlers

    def initialize_tornado_application(self):
        self.tornado_application = web.Application(self.handlers, **self.tornado_settings)

    def initialize(self, *args, **kwargs):
        super().initialize(*args, **kwargs)
        if self.config_file:
            self.load_config_file(self.config_file)
        self.init_tornado_settings()
        self.init_handlers()
        self.initialize_tornado_application()

    def start(self):
        self.log.warning(f"Starting Course Service on port {self.port}")
        self.tornado_application.listen(self.port)
        import asyncio

        asyncio.get_event_loop().run_forever()


if __name__ == "__main__":
    CourseServiceApp.launch_instance()
