from jupyterhub.utils import url_path_join as ujoin
from tornado import web

from .base import BaseHandler, BaseTemplateHandler


class RootHandler(BaseHandler):
    @web.authenticated
    async def get(self):
        # Redirect to the home page
        service_prefix = self.settings.get("service_prefix", "/")
        self.redirect(ujoin(service_prefix, "app/"))


class HomeHandler(BaseTemplateHandler):
    @web.authenticated
    async def get(self):
        self.xsrf_token  # Set the readable CSRF token before rendering the browser client.
        self.render_template("react_base.j2")


default_handlers = [
    (r"/?", RootHandler),
    (r"/app/?.*", HomeHandler),
]
