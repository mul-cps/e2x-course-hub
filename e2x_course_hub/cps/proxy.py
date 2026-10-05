"""Same-origin authenticated visitor bridge; shared kernels have separate principals."""
import re
from urllib.parse import urlsplit
from tornado import web
from tornado.httpclient import AsyncHTTPClient, HTTPRequest
from ..course_service.handlers.base import BaseHandler

ROUTES = {
    'GET': re.compile(r'(me|profiles|session|workspaces|workflows(?:/[A-Za-z0-9_.-]+(?:/(?:logs|artifacts))?)?)\Z'),
    'POST': re.compile(r'(workflows|notebook-submissions|workflows/[A-Za-z0-9_.-]+/terminate)\Z'),
}

class ComputeVisitorProxy(BaseHandler):
    async def forward(self, path):
        if '%' in self.request.path: raise web.HTTPError(404)
        if not ROUTES.get(self.request.method,re.compile(r'(?!)')).fullmatch(path):
            raise web.HTTPError(404)
        base = self.settings.get('compute_gateway_url','')
        parsed = urlsplit(base)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.query or parsed.fragment:
            raise web.HTTPError(503,reason='Compute gateway HTTPS URL not configured')
        token = self.hub_auth.get_token(self)
        if not token: raise web.HTTPError(401)
        # No arbitrary host/path, internal routes, administrative token or browser headers.
        url = base.rstrip('/') + '/v1/' + path
        if self.request.query: url += '?' + self.request.query
        headers = {'Authorization':'Bearer ' + token,
                   'X-CPS-Hub':self.settings['console_owner']}
        idempotency_key = self.request.headers.get('Idempotency-Key')
        if idempotency_key: headers['Idempotency-Key'] = idempotency_key
        content_type = self.request.headers.get('Content-Type')
        if content_type: headers['Content-Type'] = content_type
        response = await AsyncHTTPClient().fetch(HTTPRequest(
            url,method=self.request.method,headers=headers,
            body=self.request.body if self.request.method == 'POST' else None,
            request_timeout=60,follow_redirects=False),raise_error=False)
        self.set_status(response.code)
        if response.headers.get('Content-Type'): self.set_header('Content-Type',response.headers['Content-Type'])
        self.finish(response.body)

    @web.authenticated
    async def get(self,path): await self.forward(path)
    @web.authenticated
    async def post(self,path): await self.forward(path)
    @web.authenticated
    async def delete(self,path): await self.forward(path)

class ComputeCSRFHandler(BaseHandler):
    @web.authenticated
    async def get(self):
        self.set_header('Cache-Control','no-store')
        self.write({'xsrf_token':self.xsrf_token.decode('ascii')})

default_handlers = [(r'/api/compute/v1/(.*)',ComputeVisitorProxy),
                    (r'/api/compute/xsrf',ComputeCSRFHandler)]
