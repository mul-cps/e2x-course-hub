import unittest
from tornado import web
from tornado.testing import AsyncHTTPTestCase
from e2x_course_hub.cps.oauth import SafeOAuthCallbackHandler

class RedirectProbe(SafeOAuthCallbackHandler):
    async def get(self):
        self.redirect(self.get_argument('next'))

class OAuthTest(AsyncHTTPTestCase):
    def get_app(self):
        return web.Application([(r'/callback', SafeOAuthCallbackHandler), (r'/redirect', RedirectProbe)], cookie_secret='test')

    def test_callback_requires_code(self):
        self.assertEqual(self.fetch('/callback').code, 400)

    def test_callback_requires_state(self):
        self.assertEqual(self.fetch('/callback?code=attack').code, 400)

    def test_redirect_rejects_external_and_accepts_local(self):
        from urllib.parse import urlencode
        for url in ['https://evil.example/', '//evil.example/', '/\\evil.example/']:
            response = self.fetch('/redirect?' + urlencode({'next':url}), follow_redirects=False)
            self.assertEqual(response.code, 400)
        response = self.fetch('/redirect?next=/services/admin/app', follow_redirects=False)
        self.assertEqual(response.code,302)
        self.assertEqual(response.headers['Location'], '/services/admin/app')

from e2x_course_hub.cps.oauth import CPSHubOAuth, PKCELoginMixin, challenge
from jupyterhub.services.auth import HubOAuthenticated
from tornado.httpclient import AsyncHTTPClient, HTTPRequest
from urllib.parse import parse_qs, urlencode, urlsplit
import json

class PKCEFlowTest(AsyncHTTPTestCase):
    def get_app(self):
        outer = self
        self.expected = {}
        self.token_calls = []
        class Auth(CPSHubOAuth):
            async def _api_request(self, method, url, **kwargs):
                response = await AsyncHTTPClient().fetch(HTTPRequest(outer.get_url('/token'), method=method, **kwargs))
                return json.loads(response.body)
            async def user_for_token(self, token, **kwargs):
                return {'name':'alice'}
        self.auth = Auth(pkce_enabled=True, api_token='private-client-secret', oauth_client_id='console',
                         base_url='/', oauth_redirect_uri='/callback', login_url='/authorize')
        class Login(PKCELoginMixin, HubOAuthenticated, web.RequestHandler):
            _hub_auth = outer.auth
            async def get(self): self.redirect(self.get_login_url())
        class Callback(SafeOAuthCallbackHandler):
            _hub_auth = outer.auth
        class Authorize(web.RequestHandler):
            def get(self):
                outer.expected['challenge'] = self.get_argument('code_challenge')
                outer.expected['method'] = self.get_argument('code_challenge_method')
                self.redirect('/callback?' + urlencode({'state':self.get_argument('state'), 'code':'fake-code'}))
        class Token(web.RequestHandler):
            def post(self):
                data = {k:v[0] for k,v in parse_qs(self.request.body.decode()).items()}
                outer.token_calls.append(data)
                if challenge(data.get('code_verifier','')) != outer.expected['challenge']:
                    raise web.HTTPError(403)
                if data.get('client_secret') != 'private-client-secret': raise web.HTTPError(403)
                self.write({'access_token':'fake-access-token'})
        return web.Application([(r'/login',Login),(r'/authorize',Authorize),(r'/callback',Callback),(r'/token',Token)],cookie_secret='test')

    def test_s256_complete_and_replay_rejected(self):
        start = self.fetch('/login',follow_redirects=False)
        from http.cookies import SimpleCookie
        jar = SimpleCookie()
        for value in start.headers.get_list('Set-Cookie'): jar.load(value)
        cookies = '; '.join(f'{name}={m.value}' for name,m in jar.items())
        authorized = self.fetch(start.headers['Location'],follow_redirects=False)
        callback_url = authorized.headers['Location']
        result = self.fetch(callback_url,headers={'Cookie':cookies},follow_redirects=False)
        self.assertEqual(result.code,302)
        self.assertEqual(self.expected['method'],'S256')
        self.assertEqual(len(self.token_calls),1)
        self.assertIn('code_verifier',self.token_calls[0])
        replay = self.fetch(callback_url,headers={'Cookie':cookies},follow_redirects=False)
        self.assertEqual(replay.code,403)
        self.assertEqual(len(self.token_calls),1)
