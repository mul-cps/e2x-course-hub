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
