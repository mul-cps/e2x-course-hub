"""Defense in depth for dependency-provided OAuth state/callback handling."""
from urllib.parse import urlsplit
from jupyterhub.services.auth import HubOAuthCallbackHandler
from tornado import web

class SafeOAuthCallbackHandler(HubOAuthCallbackHandler):
    def redirect(self, url, permanent=False, status=None):
        parsed = urlsplit(url)
        if parsed.scheme or parsed.netloc or not url.startswith('/') or '\\' in url:
            raise web.HTTPError(400, reason='OAuth redirect must be a local absolute path')
        return super().redirect(url, permanent=permanent, status=status)
