import json
import time
from logging import Logger

from jupyterhub.services.auth import HubOAuthenticated
from jupyterhub.utils import url_path_join as ujoin
from tornado.web import RequestHandler

from ...api.course_api import CourseAPI
from ...api.errors import APIError, UnauthorizedError
from ...api.profile_api import ProfileAPI
from ...schema.user import User
from ...cps.oauth import CPSHubOAuth, PKCELoginMixin
from ...cps.audit import AuditMixin, actor_context


class BaseHandler(PKCELoginMixin, HubOAuthenticated, RequestHandler):
    hub_auth_class = CPSHubOAuth

    def write_error(self, status_code: int, **kwargs):
        """Override to handle APIError exceptions with RFC 9457 format."""
        exc_info = kwargs.get("exc_info")
        if exc_info:
            _, error, _ = exc_info
            if isinstance(error, APIError):
                # Set status code from the error
                self.set_status(error.status_code)
                self.set_header("Content-Type", "application/problem+json")

                # Build RFC 9457 problem details
                problem = {
                    "type": error.type_uri,
                    "title": error.title,
                    "status": error.status_code,
                    "detail": error.detail,
                }

                # Add any extra fields from the error
                problem.update(error.extra)

                self.finish(json.dumps(problem))
                return

        # Fall back to default error handling
        super().write_error(status_code, **kwargs)


class BaseAPIHandler(AuditMixin, BaseHandler):
    @property
    def course_api(self) -> CourseAPI:
        return self.settings["api"].course_api

    @property
    def profile_api(self) -> ProfileAPI:
        return self.settings["api"].profile_api

    @property
    def log(self) -> Logger:
        return self.settings["logger"]

    def prepare(self):
        """Called before every request to check and reload config if needed."""
        last_check = self.settings.get("last_config_check", 0)
        refresh_interval = self.settings.get("refresh_interval", 300)
        current_time = time.time()
        if current_time - last_check < refresh_interval:
            return  # No need to check yet
        api = self.settings["api"]
        if api.server.config_changed_on_disk():
            self.log.info("Server configuration changed on disk, reloading...")
            api.reload_server_config()
            # Update the API instances in course_api and profile_api
            api.course_api.server = api.server
            api.profile_api.server = api.server
            self.settings["last_config_check"] = current_time
            self.log.info("Server configuration reloaded successfully")

    async def get_user(self) -> User:
        """Get the current authenticated user as a User object."""
        hub_user = self.get_current_user()

        if not hub_user:
            raise UnauthorizedError("No authenticated user found.")
        # This user is only set when the user logs in
        # The group memberships might not be updated if they change in JupyterHub
        # after the user has logged in
        updated_user = await self.course_api.hub_api.get_user(hub_user["name"])
        groups=updated_user.get("groups",[])
        if self.settings.get("course_provider"):
            from ...cps.expiry import effective_groups
            groups=effective_groups(self.settings["course_provider"],hub_user["name"],groups)
        self._audit_actor = hub_user["name"]
        actor_context.set(hub_user["name"])
        return User(
            username=hub_user["name"],
            admin=updated_user.get("admin", False),
            groups=groups,
        )


class BaseTemplateHandler(BaseHandler):
    def render_template(self, template_name, **kwargs):
        jinja_env = self.settings["jinja_env"]
        template = jinja_env.get_template(template_name)
        user_model = self.get_current_user()
        if user_model:
            kwargs["user"] = user_model.get("name")
        else:
            kwargs["user"] = None
        # Add any common template variables here
        service_prefix = self.settings.get("service_prefix", "/").rstrip("/")
        kwargs.update(
            {
                "base_url": self.settings.get("base_url", "/"),
                "service_prefix": service_prefix,
                "api_url": ujoin(service_prefix, "api"),
                "static_url": self.settings.get("static_url"),
                "user": {
                    "name": user_model.get("name") if user_model else None,
                    "admin": user_model.get("admin", False) if user_model else False,
                },
            }
        )
        self.write(template.render(**kwargs))
