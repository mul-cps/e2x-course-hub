"""Local redirects and optional S256 PKCE around pinned Hub service OAuth."""
import base64
import contextvars
import hashlib
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit, unquote
from jupyterhub.services.auth import HubOAuth, HubOAuthCallbackHandler
from tornado import web
from tornado.httputil import url_concat
from traitlets import Bool

_verifier = contextvars.ContextVar('cps_pkce_verifier', default=None)

def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')

def verifier_cookie(state):
    return 'cps-pkce-' + hashlib.sha256(state.encode()).hexdigest()[:32]

class CPSHubOAuth(HubOAuth):
    pkce_enabled = Bool(False).tag(config=True)

    async def _token_for_code(self, code):
        if not self.pkce_enabled:
            return await super()._token_for_code(code)
        verifier = _verifier.get()
        if not verifier:
            raise web.HTTPError(403, reason='PKCE verifier missing or expired')
        # Preserve the confidential client's authentication while proving code possession.
        params = {'client_id': self.oauth_client_id, 'client_secret': self.api_token,
                  'grant_type': 'authorization_code', 'code': code,
                  'redirect_uri': self.oauth_redirect_uri, 'code_verifier': verifier}
        reply = await self._api_request('POST', self.oauth_token_url,
            body=urlencode(params).encode(), headers={'Content-Type': 'application/x-www-form-urlencoded'})
        return reply['access_token']

class PKCELoginMixin:
    def get_login_url(self):
        if self._hub_login_url is not None:
            return self._hub_login_url
        url = super().get_login_url()
        if self.hub_auth.pkce_enabled:
            state = parse_qs(urlsplit(url).query)['state'][0]
            verifier = secrets.token_urlsafe(48)
            self.set_secure_cookie(verifier_cookie(state), verifier, max_age=600,
                path=self.hub_auth.cookie_path, secure=True, httponly=True, samesite='Lax')
            url = url_concat(url, {'code_challenge': challenge(verifier), 'code_challenge_method':'S256'})
            self._hub_login_url = url
        return url

class SafeOAuthCallbackHandler(HubOAuthCallbackHandler):
    hub_auth_class = CPSHubOAuth

    async def get(self):
        reset = None
        if self.hub_auth.pkce_enabled:
            state = self.get_argument('state', '')
            if not self.hub_auth._decode_state(state):
                raise web.HTTPError(403, reason='OAuth state expired or consumed')
            cookie = verifier_cookie(state)
            verifier = self.get_secure_cookie(cookie, max_age_days=10/1440)
            self.clear_cookie(cookie, path=self.hub_auth.cookie_path)
            if not verifier:
                raise web.HTTPError(403, reason='PKCE verifier missing or expired')
            reset = _verifier.set(verifier.decode('ascii'))
        try:
            await super().get()
        finally:
            if reset is not None: _verifier.reset(reset)

    def redirect(self, url, permanent=False, status=None):
        decoded = url
        for _ in range(3):
            decoded = unquote(decoded)
        parsed = urlsplit(decoded)
        if (parsed.scheme or parsed.netloc or not decoded.startswith('/') or decoded.startswith('//') or '\\' in decoded
                or any(ord(char) < 32 or ord(char) == 127 for char in decoded)):
            raise web.HTTPError(400, reason='OAuth redirect must be a local absolute path')
        return super().redirect(url, permanent=permanent, status=status)
