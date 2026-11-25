#!/usr/bin/env python3
"""Loopback JSON API behind the operator UI (ui/), for the durable factory.

Only the UI may call it (UPSTREAM_SECRET). Operational reads and browser
mutations require a signed-in operator. Browser mutations also require a
same-origin JSON request and the session's CSRF token. The server queues intent;
only the locked supervisor acts on it.
"""
import hashlib
import json, re, subprocess, os, secrets, hmac, threading, ipaddress
from contextlib import nullcontext
from pathlib import Path
from time import perf_counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = 8787
WD = os.environ.get("FORGE_ROOT", str(Path.home() / ".local/share/forge"))
from forge.view_cache import ViewCache
from forge.mutation_audit import RequestReplay
VIEWS = ViewCache()
# Proof that a request came through the operator UI, not from another local
# process: a 0600 file only this account (and the UI container, read-only) can
# read. Loaded by __main__; None only when imported by tests.
UPSTREAM_SECRET = None


def upstream_secret(path):
    """Read the UI↔API secret, creating it (0600) on first start."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    info = path.lstat()
    if not path.is_file() or path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SystemExit(f'{path} must be a private regular file owned by this account')
    value = path.read_text().strip()
    if len(value) < 32:
        raise SystemExit(f'{path} does not hold a usable secret')
    return value




def runtime_control(name):
    from forge.store.runtime import control_enabled
    return control_enabled(WD,name)


def setup_phase():
    from forge.configuration_authority import ConfigurationAuthority
    return ConfigurationAuthority(WD).phase()


def settings_service():
    from forge.settings import Settings
    return Settings(WD, scope='active')


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, body, ctype="application/json", headers=None, status=200, etag=None, timings=None):
        audit = getattr(self, '_mutation_audit', None)
        if audit is not None:
            try:
                audit.result(json.loads(body), status)
            except (ValueError, TypeError):
                audit.result(None, status)
            audit.finish()
        data = body if isinstance(body, bytes) else body.encode()
        # Validators are private to each client; no response is stored by a
        # browser/proxy cache. Include the action token in the digest so a
        # service restart always sends the new capability with a fresh body.
        etag = etag or ('W/"' + hashlib.sha256(data).hexdigest() + '"' if status == 200 and self.command == 'GET' and ctype == 'application/json' else None)
        conditional_started = perf_counter() if timings is not None else None
        unchanged = bool(etag and self.headers.get('If-None-Match') == etag)
        timing_header = None
        if timings is not None:
            values = dict(timings)
            if unchanged:
                values['conditional'] = (perf_counter() - conditional_started) * 1000
            # Ends at response preparation: socket transfer and the UI proxy are
            # outside this process and cannot be measured in this response header.
            values['request'] = (perf_counter() - self._request_started) * 1000
            timing_header = ', '.join(f'{name};dur={duration:.3f}' for name, duration in values.items())
        if unchanged:
            self.send_response(304)
            if timing_header is not None: self.send_header('Server-Timing', timing_header)
            self.send_header('ETag', etag)
            self.send_header('Cache-Control', 'private, no-store')
            self.end_headers()
            return
        self.send_response(status)
        if timing_header is not None: self.send_header('Server-Timing', timing_header)
        if audit is not None: self.send_header('X-Forge-Request-ID', audit.caller_id)
        if etag: self.send_header('ETag', etag)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for key,value in (headers or {}).items():
            self.send_header(key,value)
        self.end_headers()
        self.wfile.write(data)

    def send_error(self, code, message=None, explain=None):
        audit = getattr(self, '_mutation_audit', None)
        if audit is not None:
            audit.result(None, code)
            audit.finish()
        return super().send_error(code, message, explain)

    def _cached(self, key, build):
        projection_ms = None
        def project():
            nonlocal projection_ms
            started = perf_counter()
            try:
                return build()
            finally:
                projection_ms = (perf_counter() - started) * 1000
        started = perf_counter()
        etag, body = VIEWS.response(WD, key, project, self._action_token, self.headers.get('If-None-Match'))
        cache_ms = (perf_counter() - started) * 1000
        # Validator short-circuiting need not consult a cached body: it also
        # works after eviction. Do not call that a serialized-response cache hit.
        outcome = 'cache_miss' if projection_ms is not None else 'cache_validated' if body is None else 'cache_hit'
        timings = {outcome: cache_ms}
        if projection_ms is not None: timings['projection'] = projection_ms
        self._send(body or b'', etag=etag, timings=timings)

    def _browser(self):
        from http.cookies import SimpleCookie, CookieError
        try:
            cookie=SimpleCookie(self.headers.get('Cookie',''))
            value=cookie['forge_setup'].value if 'forge_setup' in cookie else ''
            return value if re.fullmatch(r'[A-Za-z0-9_-]{32,128}',value) else ''
        except CookieError:
            return ''

    def _redirect(self, target, headers=()):
        self.send_response(303)
        self.send_header('Location',target)
        self.send_header('Cache-Control','no-store')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Length','0')
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()

    def _upstream(self):
        # Every request must come through the operator UI (see UPSTREAM_SECRET).
        return UPSTREAM_SECRET is None or hmac.compare_digest(self.headers.get('X-Forge-Upstream',''),UPSTREAM_SECRET)

    def _public_host(self):
        # Behind the operator UI, Host is the loopback upstream; the UI forwards
        # the name the browser used after validating it against its own binds.
        # Only the UI can send it: requests without the upstream secret never get here.
        forwarded=self.headers.get('X-Forwarded-Host','')
        if re.fullmatch(r'[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?|\[[0-9A-Fa-f:.]{2,45}\](?::[0-9]{1,5})?',forwarded):
            return forwarded
        return self.headers.get('Host','')

    def _trusted_host(self):
        # Reject DNS rebinding even when Origin and Host agree with each other.
        # Only the address actually bound by this tailnet server can read its
        # controls token or submit an operator action.
        address,port=self.server.server_address[:2]
        expected=f'[{address}]:{port}' if ':' in address else f'{address}:{port}'
        return self.headers.get('Host','')==expected

    def _tailnet_login(self):
        # Only the operator UI sets this, and only for a request Tailscale Serve
        # delivered to it; reaching this API at all required the upstream secret.
        from forge.tailnet_identity import UPSTREAM_HEADER, login
        values = (self.headers.get_all(UPSTREAM_HEADER) if hasattr(self.headers, 'get_all')
                  else [self.headers[UPSTREAM_HEADER]] if UPSTREAM_HEADER in self.headers else None) or []
        return login(values[0]) if len(values) == 1 else None

    def _operator(self):
        from forge.operator_auth import OperatorAuth, read_cookie, SESSION_COOKIE
        self._auth = OperatorAuth(WD)
        self._auth_config = self._auth.config()
        self._session_token = read_cookie(self.headers.get('Cookie', ''), SESSION_COOKIE)
        self._session = self._auth.session(self._session_token) if self._auth_config else None
        # Tailscale identity first when enabled; a GitHub cookie is the fallback.
        tailnet = self._tailnet_login() if self._auth_config else None
        found = self._auth.tailnet_session(tailnet) if tailnet else None
        if found:
            self._session_token, self._session = found
        # Control belongs to a signed-in operator only; there is no anonymous token.
        self._action_token = self._session['csrf'] if self._session else None
        if getattr(self, '_mutation_audit', None):
            from forge.operator_auth import digest
            self._mutation_audit.bind(session=self._session, session_id=(self._session.get('id') or digest(self._session_token))
                                      if self._session else None)
        if self._auth_config and self._public_host() != urlparse(self._auth_config['origin']).netloc:
            self.send_error(403)
            return False
        return True

    def _operator_guard(self, *, read_snapshot=False):
        try:
            if not read_snapshot:
                return self._operator()
            from forge.store.runtime import required
            # A Tailscale session is recorded once per window; do that write
            # before the read session, which then only observes it.
            login = self._tailnet_login()
            if login:
                from forge.operator_auth import OperatorAuth
                auth = OperatorAuth(WD)
                if auth.config():
                    auth.tailnet_session(login)
            # Authentication and pause state describe the same committed state.
            # Close before cache fingerprinting, projection, callbacks or waits.
            with required(WD).read_session():
                if not self._operator():
                    return False
                self._factory_paused = runtime_control('factory_paused')
                return True
        except (ValueError, OSError, RuntimeError, KeyError, TypeError):
            self.send_error(503)
            return False

    def _signed_in(self):
        if not self._session:
            self.send_error(401)
            return False
        return True

    def _auth_get(self, url):
        from forge.operator_auth import read_cookie, FLOW_COOKIE, cookie, failure_reason
        if url.path == '/api/auth/session':
            from forge.factory_identity import display_name
            mode = self._auth.identity_settings()['mode'] if self._auth_config else None
            self._send(json.dumps({'ok': True, 'enabled': bool(self._auth_config),
                                  'operator': self._session['operator'] if self._session else None,
                                  'factory_name': display_name(WD),
                                  **({'mode': mode} if self._auth_config else {}),
                                  **({'controls_token': self._action_token, 'via': self._session.get('via', 'github')} if self._session else {})}))
            return True
        if url.path == '/api/auth/identity':
            if not self._session:
                self.send_error(401)
                return True
            self._send(json.dumps({'ok': True, **self._auth.identity_settings(), 'controls_token': self._action_token}))
            return True
        if url.path not in ('/auth/github/start', '/auth/github/callback'):
            return False
        try:
            if not self._auth_config:
                self.send_error(404)
                return True
            if url.path == '/auth/github/start':
                query = parse_qs(url.query)
                target, value = self._auth.begin(query.get('return_to', ['/'])[0])
                self._redirect(target, [('Set-Cookie', value)])
            else:
                query = parse_qs(url.query, keep_blank_values=True)
                if set(query) not in ({'state', 'code'}, {'state', 'code', 'iss'}) or any(len(v) != 1 for v in query.values()):
                    raise ValueError('Invalid callback')
                if 'iss' in query and query['iss'] != ['https://github.com/login/oauth']:
                    raise ValueError('Invalid callback issuer')
                value, target = self._auth.callback(query['state'][0], query['code'][0],
                                            read_cookie(self.headers.get('Cookie', ''), FLOW_COOKIE), self._session_token,
                                            with_target=True, tailnet_login=self._tailnet_login())
                self._redirect(target, [('Set-Cookie', value), ('Set-Cookie', cookie(FLOW_COOKIE))])
        except (ValueError, OSError, RuntimeError, KeyError, TypeError) as error:
            print(json.dumps({'event': 'operator_sign_in_failed', 'reason': failure_reason(error)}), flush=True)
            self._redirect('/login?error=sign_in', [('Set-Cookie', cookie(FLOW_COOKIE))])
        return True

    def _client_post(self):
        from forge.client_connections import ClientConnections, ConnectionError, fields, need
        from forge.operator_auth import OperatorAuth
        paths = {'/api/client/device', '/api/client/token', '/api/client/refresh',
                 '/api/client/status', '/api/client/disconnect', '/api/client/requirements',
                 '/api/client/authorize', '/api/client/read', '/api/client/operation', '/api/client/policy', '/api/client/planning', '/api/client/delivery'}
        if self.path not in paths:
            return False
        try:
            # These routes use client credentials only. Browser cookies, UI action
            # tokens and cross-origin fetches never authorize a client request.
            need(not self.headers.get('Origin') and not self.headers.get('Cookie'), status=403)
            need(self.headers.get('Content-Type', '').split(';')[0].strip() == 'application/json'
                 and not self.headers.get('Transfer-Encoding'), status=415)
            length = int(self.headers.get('Content-Length', '0'))
            need(0 < length <= 16384, status=413)
            value = json.loads(self.rfile.read(length))
            if getattr(self, '_mutation_audit', None):
                self._mutation_audit.describe(value)
            auth = OperatorAuth(WD)
            config = auth.config()
            need(config and self._public_host() == urlparse(config['origin']).netloc,
                 'sign_in_required', 'Configure HTTPS operator sign-in before connecting an agent.', 403)
            clients = ClientConnections(WD, auth=auth)
            authorization = self.headers.get('Authorization', '')
            bearer = authorization[7:] if authorization.startswith('Bearer ') else ''
            if self.path == '/api/client/device':
                result = clients.begin(value)
            elif self.path == '/api/client/token':
                result = clients.poll(value)
            elif self.path == '/api/client/refresh':
                fields(value, ('refresh_token',))
                result = clients.refresh(value['refresh_token'])
            elif self.path == '/api/client/status':
                fields(value, ())
                result = clients.status(bearer)
            elif self.path == '/api/client/disconnect':
                fields(value, ())
                clients.disconnect(bearer)
                result = {'disconnected': True}
            elif self.path == '/api/client/authorize':
                fields(value, ('repository', 'instance', 'generation', 'capability'))
                clients.authorize(bearer, value['repository'], value['capability'], value['instance'], value['generation'])
                result = {'authorized': True}
            elif self.path == '/api/client/delivery':
                from forge.delivery_actions import ClientDelivery
                result = ClientDelivery(WD, clients).request(bearer, value)
            elif self.path == '/api/client/policy':
                from forge.repository_merge_policy import ClientMergePolicy
                result = ClientMergePolicy(settings_service(), clients).request(bearer, value)
            elif self.path == '/api/client/planning':
                from forge.factory.planning_service import client_request
                result = client_request(WD, clients, bearer, value)
            elif self.path in ('/api/client/read', '/api/client/operation'):
                from forge.client_repositories import ClientRepositories, OPERATIONS
                # Fence before opening repository state, including malformed or
                # out-of-scope requests. Service methods repeat the check.
                capability = OPERATIONS.get(value.get('action'), 'repositories:read') if isinstance(value, dict) else 'repositories:read'
                clients.authorize(bearer, value['repository'], capability, value['instance'], value['generation'])
                service = ClientRepositories(settings_service(), clients)
                result = service.read(bearer, value) if self.path.endswith('/read') else service.operation(bearer, value)
            else:
                fields(value, ('repository', 'instance', 'generation'))
                clients.authorize(bearer, value['repository'], 'repositories:read', value['instance'], value['generation'])
                from forge.repository_requirements import describe
                result = describe(settings_service(), value['repository'])
                # Recheck after collecting data, including any factory reset.
                clients.authorize(bearer, value['repository'], 'repositories:read', value['instance'], value['generation'])
            self._send(json.dumps({'ok': True, **result}))
        except ConnectionError as exc:
            if getattr(self, '_mutation_audit', None):
                self._mutation_audit.failed(exc.code)
            self._send(json.dumps({'ok': False, 'error': exc.code, 'reason': str(exc)}), status=exc.status)
        except (ValueError, OSError, RuntimeError, KeyError, TypeError):
            if getattr(self, '_mutation_audit', None):
                self._mutation_audit.failed('operation_interrupted', uncertain=True)
            self._send(json.dumps({'ok': False, 'error': 'temporarily_unavailable',
                                   'reason': 'Client connection could not be verified.'}), status=503)
        return True

    def _operator_actor(self):
        return 'github:'+str(self._session['operator']['id']) if self._session else 'local:control-capability'

    def do_POST(self):
        from forge.store.runtime import required
        from forge.mutation_audit import MutationAudit, request_context
        self._mutation_audit = MutationAudit(required(WD), self.path, request_id=self.headers.get('X-Forge-Request-ID'))
        with request_context(self._mutation_audit):
            try:
                return self._dispatch_POST()
            except RequestReplay as exc:
                self._send(json.dumps({'ok': False, 'error': 'request_already_recorded',
                    'reason': str(exc), 'receipt': exc.receipt}), status=409)
            finally:
                if self._mutation_audit is not None:
                    self._mutation_audit.finish()
                self._mutation_audit = None

    def _dispatch_POST(self):
        if not self._trusted_host() or not self._upstream():
            self.send_error(403);return
        if self._client_post():
            return
        if not self._operator_guard():
            return
        if not self._session:
            # Without configured sign-in and a session, the API offers no control.
            self.send_error(401);return
        if self.path not in {'/api/connections/preview', '/api/connections/decide', '/api/connections/revoke', '/api/auth/logout', '/api/auth/identity', '/api/settings/sync', '/api/factory/action', '/api/runtime', '/api/onboarding', '/api/installation', '/api/accounts', '/api/repositories/check', '/api/repositories/mirror', '/api/repositories/review-scope', '/api/repositories/merge-policy', '/api/repositories/github-protection', '/api/repositories/steward', '/api/repositories/enable', '/api/repositories/disable', '/api/repositories/deployment', '/api/repositories/environment', '/api/issues/action', '/api/issues/steward', '/api/issues/delivery', '/api/issues/verify', '/api/issues/environment', '/api/issues/refresh', '/api/issues/discussion', '/api/repositories/planning'}:
            self.send_error(404);return
        # An attacker may cause a browser to send a form, but cannot read the
        # dashboard token or set this header cross-origin without CORS approval.
        token=self.headers.get('X-Forge-Action','')
        origin=urlparse(self.headers.get('Origin',''))
        host=self.headers.get('Host','')
        if (not hmac.compare_digest(token,self._action_token) or origin.scheme!='http'
                or origin.netloc!=host or origin.path or origin.query or origin.fragment
                or self.headers.get('Sec-Fetch-Site','same-origin')!='same-origin'):
            self.send_error(403);return
        if (self.headers.get('Content-Type','').split(';')[0].strip()!='application/json'
                or self.headers.get('Transfer-Encoding')):
            self.send_error(415);return
        try:
            length=int(self.headers.get('Content-Length','0'))
            maximum = 262144 if self.path == '/api/runtime' else 8192
            if not 0<length<=maximum:raise ValueError('invalid_request_size')
            value=json.loads(self.rfile.read(length))
            self._mutation_audit.describe(value)
            try:
                self._mutation_audit.start()
            except RequestReplay:
                # Steward selection is a single idempotent SQL command. A lost
                # reply may safely replay that exact command, even if the HTTP
                # audit claim was recorded before the process stopped.
                if self.path != '/api/issues/steward':raise
            if self.path.startswith('/api/connections/'):
                from forge.client_connections import ClientConnections, fields
                clients = ClientConnections(WD, auth=self._auth)
                if self.path == '/api/connections/preview':
                    fields(value, ('code',))
                    result = clients.preview(self._session_token, value['code'])
                elif self.path == '/api/connections/decide':
                    result = clients.decide(self._session_token, value)
                else:
                    fields(value, ('id',))
                    clients.revoke(self._session_token, value['id'])
                    result = {'revoked': True}
                self._send(json.dumps({'ok': True, **result}))
                return
            if self.path == '/api/auth/identity':
                action = value.get('action') if isinstance(value, dict) else None
                if action == 'mode' and set(value) == {'action', 'mode'}:
                    result = self._auth.set_identity_mode(value['mode'])
                elif action == 'unlink' and set(value) == {'action', 'login'} and isinstance(value['login'], str):
                    result = self._auth.unlink_tailnet(value['login'])
                else:
                    raise ValueError('Expected an identity mode or a tailnet login to unlink')
                self._send(json.dumps({'ok': True, **result}))
                return
            if self.path == '/api/auth/logout':
                from forge.operator_auth import cookie, SESSION_COOKIE
                if value != {} or not self._session:
                    raise ValueError('Expected an authenticated logout request')
                self._auth.logout(self._session_token)
                self._send(json.dumps({'ok': True}), headers={'Set-Cookie': cookie(SESSION_COOKIE)})
                return
            if self.path == '/api/accounts':
                from forge.account_web import AccountWeb
                result = AccountWeb(WD).request(value, actor_id=self._operator_actor())
                self._send(json.dumps({'ok': True, **result}));return
            if self.path == '/api/installation':
                from forge.installation_web import InstallationWeb
                result = InstallationWeb(WD).request(value, actor_id=self._operator_actor())
                self._send(json.dumps({'ok': True, **result}));return
            if self.path == '/api/onboarding':
                from forge.onboarding import Onboarding
                setup=Onboarding(WD)
                if not isinstance(value,dict):raise ValueError('Expected a setup action')
                action=value.pop('action',None)
                fields={'github':{'role','owner','account_type'},'refresh':set(),'repository':{'repo_id'},
                        'check':{'revision'},'start':{'revision'},'reset':{'generation'}}
                if action not in fields or set(value)!=fields[action]:raise ValueError('Invalid setup action')
                result={}
                if action=='github':result=setup.begin(**value,origin=self._auth_config['origin'],browser=self._browser())
                elif action=='refresh':result=setup.refresh()
                elif action=='repository':setup.select(value['repo_id'])
                else:result={'request':setup.queue(action,**value)}
                self._send(json.dumps({'ok':True,**result}));return
            if runtime_control('factory_paused'):
                raise ValueError('Factory reset is in progress')
            phase=setup_phase() if self.path=='/api/runtime' else None
            if self.path == '/api/runtime' and phase is not None and phase!='active':
                raise ValueError('Use guided setup until the factory has started')
            if self.path == '/api/repositories/planning':
                from forge.factory.planning_service import PlanningService
                if not isinstance(value,dict) or set(value)!={'repository_id','operation'}:raise ValueError('Expected a repository planning operation')
                result=PlanningService(WD,actor_id=self._operator_actor()).action(value['repository_id'],value['operation'])
                self._send(json.dumps({'ok':True,**result}));return
            if self.path == '/api/issues/discussion':
                from forge.factory.discussions import Discussions
                service = Discussions(WD)
                if not isinstance(value, dict): raise ValueError('Expected a conversation action')
                action = value.get('action')
                fields = {'send': {'key', 'action', 'message', 'request_id'}, 'cancel': {'key', 'action'}, 'apply': {'key', 'action', 'message_id'}, 'undo': {'key', 'action', 'message_id'}}
                if action not in fields or not fields[action] <= set(value) or set(value) - fields[action] - ({'reason','choice_id'} if action=='apply' else set()): raise ValueError('Invalid conversation action')
                result = None
                if action == 'send': result = service.send(value['key'], value['message'], value['request_id'], actor_id=self._operator_actor())
                elif action == 'cancel': service.cancel(value['key'], actor_id=self._operator_actor())
                elif action == 'undo': result = service.undo(value['key'], value['message_id'], actor_id=self._operator_actor())
                else: result = service.apply(value['key'], value['message_id'], actor_id=self._operator_actor(), **{key:value[key] for key in ('reason','choice_id') if key in value})
                self._send(json.dumps({'ok': True, 'request': result}));return
            if self.path == '/api/issues/steward':
                from forge.factory.steward_service import StewardService
                try:
                    if not isinstance(value,dict) or set(value)!={'key','decision_id','choice_id','digest','request_id'}:
                        raise ValueError('Expected issue, stored choice, decision digest, and request identity')
                    result=StewardService(WD,actor_id=self._operator_actor()).choose(**value)
                except (ValueError,KeyError) as exc:
                    self._send(json.dumps({'ok':False,'reason':str(exc)}),status=409);return
                self._send(json.dumps({'ok':True,**result,'controls_token':self._action_token}));return
            if self.path == '/api/issues/verify':
                from forge.factory.store_operator import selected_service
                if not isinstance(value,dict) or set(value)!={'key','revision','gate_id','statement','request_id'}:
                    raise ValueError('Expected issue, current revision, verification request, statement, and request identity')
                service=selected_service(WD,actor_id=self._operator_actor())
                receipt=service.verify(value['key'],value['revision'],value['gate_id'],value['statement'],request_id=value['request_id'])
                self._send(json.dumps({'ok':True,'receipt':receipt,'controls_token':self._action_token}));return
            if self.path == '/api/issues/delivery':
                from forge.delivery_actions import request as delivery_request
                from forge.factory.store_operator import selected_service
                service=selected_service(WD,actor_id=self._operator_actor())
                result=delivery_request(service,value)
                self._send(json.dumps({'ok':True,**result}));return
            if self.path == '/api/issues/action':
                from forge.factory.store_operator import IMMEDIATE, selected_service
                from forge.factory.issues import IssueInventory
                if not isinstance(value,dict) or set(value) not in ({'key','action','revision','reason'}, {'key','action','revision','reason','request_id'}):
                    raise ValueError('Expected issue, action, current revision, and decision')
                identifier=selected_service(WD,actor_id=self._operator_actor()).request(
                    value['key'],value['action'],value['revision'],value['reason'],request_id=value.get('request_id'))
                result=IssueInventory(WD).view(key=value['key']) if value['action'] in IMMEDIATE else {}
                self._send(json.dumps({'ok':True,'request':identifier,**result,'controls_token':self._action_token}));return
            if self.path == '/api/issues/environment':
                from forge.factory.store_operator import selected_service
                from forge.factory.environments import override
                if not isinstance(value,dict) or set(value)!={'key','revision','environment'}:
                    raise ValueError('Expected the issue, current revision, and environment selection.')
                selected=override(value['environment'])
                reason=('Supervisor chooses the environment for this issue' if selected['mode']=='automatic' else
                        'Use '+(', '.join(selected['services']) if selected['mode']=='full' else 'the standard environment')+' for this issue')
                identifier=selected_service(WD,actor_id=self._operator_actor()).request(
                    value['key'],'environment',value['revision'],reason,environment=selected)
                self._send(json.dumps({'ok':True,'request':identifier}));return
            if self.path == '/api/issues/refresh':
                from forge.factory.issues import IssueInventory
                if value!={} and (not isinstance(value,dict) or set(value)!={'key'}):raise ValueError('Expected an empty refresh request or an issue key')
                if value:
                    from forge.factory.control import request_check
                    request_check(WD, value['key'],actor_id=self._operator_actor())
                IssueInventory(WD).start_refresh(force=True)
                self._send(json.dumps({'ok':True}));return
            if self.path == '/api/factory/action':
                from forge.factory.control import queue
                if not isinstance(value, dict) or set(value) != {'action', 'target', 'reason'}:
                    raise ValueError('Expected action, target, and reason')
                identifier=queue(WD, **value,actor_id=self._operator_actor())
                self._send(json.dumps({'ok':True,'request':identifier}));return
            if self.path == '/api/runtime':
                from forge.factory.control import save_runtime
                if not isinstance(value, dict) or set(value) != {'config', 'revision'}:
                    raise ValueError('Expected config and revision')
                result=save_runtime(WD, **value)
                self._send(json.dumps({**result,'ok':True,'controls_token':self._action_token}));return
            if self.path == '/api/repositories/check':
                from forge.repository_setup import RepositorySetup
                if not isinstance(value,dict) or set(value)!={'repo_id','revision'} or (value['repo_id'] is not None and type(value['repo_id']) is not int):
                    raise ValueError('Expected repository identity and current revision')
                RepositorySetup(settings_service()).start_check(value['repo_id'],value['revision'])
                self._send(json.dumps({'ok':True}));return
            if self.path == '/api/repositories/steward':
                from forge.repository_steward_policy import StewardPolicy
                try:
                    if not isinstance(value,dict) or set(value)!={'repo_id','revision','policy'}:
                        raise ValueError('Expected repository, policy revision and autonomy')
                    result=StewardPolicy(settings_service(),actor_id=self._operator_actor()).set(**value)
                except ValueError as exc:
                    self._send(json.dumps({'ok':False,'reason':str(exc)}),status=409);return
                self._send(json.dumps({'ok':True,**result,'controls_token':self._action_token}));return
            if self.path == '/api/repositories/merge-policy':
                from forge.repository_setup import RepositorySetup
                if not isinstance(value, dict) or set(value) != {'repo_id', 'revision', 'mode'}:
                    raise ValueError('Expected repository identity, runtime revision and merge policy')
                result = RepositorySetup(settings_service()).merge_policy(**value)
                self._send(json.dumps({**result, 'ok': True, 'controls_token': self._action_token}));return
            if self.path == '/api/repositories/review-scope':
                from forge.repository_setup import RepositorySetup
                if not isinstance(value, dict) or set(value) != {'repo_id', 'revision', 'scope'}:
                    raise ValueError('Expected repository identity, runtime revision and review scope')
                result = RepositorySetup(settings_service()).review_scope(**value)
                self._send(json.dumps({**result, 'ok': True, 'controls_token': self._action_token}));return
            if self.path == '/api/repositories/mirror':
                from forge.repository_setup import RepositorySetup
                if not isinstance(value, dict) or set(value) != {'repo_id', 'revision', 'denied_repositories'}:
                    raise ValueError('Expected repository identity, current revision and denied repositories')
                result = RepositorySetup(settings_service()).mirror_policy(**value)
                self._send(json.dumps({**result, 'ok': True, 'controls_token': self._action_token}));return
            if self.path in ('/api/repositories/enable', '/api/repositories/disable'):
                from forge.repository_setup import RepositorySetup
                if (not isinstance(value,dict) or set(value)!={'repo_id','revision'}
                        or type(value['repo_id']) is not int or value['repo_id'] <= 0
                        or not isinstance(value['revision'],str) or not value['revision']):
                    raise ValueError('Expected repository identity and current revision')
                setup=RepositorySetup(settings_service())
                action=setup.enable if self.path.endswith('/enable') else setup.disable
                result=action(value['repo_id'],value['revision'])
                self._send(json.dumps({**result,'ok':True,'controls_token':self._action_token}));return
            if self.path == '/api/repositories/deployment':
                from forge.repository_setup import RepositorySetup
                fields={'confirm':{'domain'},'reject':{'reason'},'reread':set()}
                action=value.get('action') if isinstance(value,dict) else None
                if (action not in fields or set(value)!={'repo_id','revision','action'}|fields[action]
                        or type(value['repo_id']) is not int):
                    raise ValueError('Expected repository identity, current revision, and a deployment decision')
                result=RepositorySetup(settings_service()).deployment(**value)
                self._send(json.dumps({**result,'ok':True,'controls_token':self._action_token}));return
            if self.path == '/api/repositories/environment':
                from forge.repository_environments import RepositoryEnvironments
                from forge.repository_setup import RepositorySetup
                if (not isinstance(value,dict) or set(value)!={'repo_id','revision','environment_revision','action'}
                        or type(value['repo_id']) is not int or value['action'] not in ('scan','confirm','automatic')
                        or not isinstance(value['revision'],str) or not isinstance(value['environment_revision'],str)):
                    raise ValueError('Expected repository, current requirements, and scan or confirm.')
                settings=settings_service()
                RepositoryEnvironments(settings).act(**value)
                result=RepositorySetup(settings).view(value['repo_id'])
                self._send(json.dumps({**result,'ok':True,'controls_token':self._action_token}));return
            if self.path == '/api/settings/sync':
                if value != {}:raise ValueError('Expected an empty sync request')
                settings_service().start_refresh()
                self._send(json.dumps({'ok':True,'message':'Access refresh requested'}));return
            raise ValueError('invalid_action_request')
        except RequestReplay:
            raise
        except BlockingIOError:
            if self._mutation_audit is not None:
                self._mutation_audit.failed('operation_busy')
            self._send(json.dumps({'ok':False,'reason':'A repository operation is in progress. Try saving again shortly.'}), status=409)
        except (ValueError,TypeError) as exc:
            if self._mutation_audit is not None:
                self._mutation_audit.failed('invalid_request')
            self._send(json.dumps({'ok':False,'reason':str(exc)}))
        except (OSError,ImportError,RuntimeError,subprocess.SubprocessError):
            if self._mutation_audit is not None:
                self._mutation_audit.failed('operation_interrupted', uncertain=True)
            self._send(json.dumps({'ok':False,'reason':'Operation unavailable; check server configuration'}))

    def do_GET(self):
        self._request_started = perf_counter()
        if not self._trusted_host() or not self._upstream():
            self.send_error(403);return
        u = urlparse(self.path)
        store = None
        if u.path in {'/api/factory', '/api/issues', '/api/delivery', '/api/pull-requests'}:
            from forge.store.runtime import required
            try:
                store = required(WD)
            except (ValueError, OSError, RuntimeError, KeyError, TypeError):
                self.send_error(503);return
        # Keep authorization and projection transactions separate, while the
        # cached GET owns the physical read connection until its response ends.
        # Long polls and mutation routes never acquire this lease.
        with store.read_connection_lease(getattr(getattr(self, 'server', None), 'read_handles', None)) if store is not None else nullcontext():
            self._get(u)

    def _get(self, u):
        if not self._operator_guard(read_snapshot=True):
            return
        if self._auth_get(u):
            return
        if u.path == '/onboarding/cli/github/start' and self._auth_config and not self._session:
            from forge.operator_auth import sign_in_destination
            from urllib.parse import quote
            if not sign_in_destination(self.path):
                self.send_error(400);return
            self._redirect('/auth/github/start?return_to=' + quote(self.path, safe=''))
            return
        if not self._signed_in():
            return
        if u.path == '/api/connections':
            try:
                from forge.client_connections import ClientConnections
                result = ClientConnections(WD, auth=self._auth).view(self._session_token)
                self._send(json.dumps({'ok': True, **result, 'controls_token': self._action_token}))
            except (ValueError, OSError, RuntimeError, KeyError, TypeError):
                self._send(json.dumps({'ok': False, 'reason': 'Connections are unavailable until setup or reset has completed.'}))
            return
        if u.path.startswith('/api/') and u.path!='/api/onboarding' and self._factory_paused:
            self._send(json.dumps({'ok':False,'reason':'Factory reset is in progress'}));return
        if u.path in {'/onboarding/cli/github/start', '/onboarding/cli/github/callback'}:
            from forge.installation_apps import RoleAppHandoff
            try:
                query = parse_qs(u.query)
                expected = {'state'} if u.path.endswith('/start') else {'state', 'code'}
                if set(query) != expected or any(len(v) != 1 for v in query.values()) or not self._session:
                    raise ValueError('Invalid CLI registration handoff')
                actor_id = self._session['operator']['id']
                handoff = RoleAppHandoff(WD)
                if u.path.endswith('/start'):
                    self._send(handoff.page(query['state'][0], actor_id), 'text/html; charset=utf-8',
                               headers={'Content-Security-Policy': "default-src 'none'; form-action https://github.com; frame-ancestors 'none'; base-uri 'none'"})
                else:
                    self._redirect(handoff.callback(query['state'][0], query['code'][0], actor_id))
            except (ValueError, OSError, RuntimeError, KeyError, TypeError):
                self._send('Registration did not complete. Return to the CLI and resume setup.', 'text/plain', status=400)
            return
        if u.path == '/onboarding/github/callback':
            from forge.onboarding import Onboarding
            try:
                query=parse_qs(u.query)
                if set(query)!={'state','code'} or any(len(v)!=1 for v in query.values()):
                    raise ValueError('Invalid GitHub callback')
                target=Onboarding(WD).callback(query['state'][0],query['code'][0],self._browser())
                self._redirect(target)
            except (ValueError,OSError,RuntimeError):
                self._redirect('/onboarding?connection_error=1')
            return
        if u.path == '/api/accounts':
            from forge.account_web import AccountWeb
            self._send(json.dumps({'ok': True, **AccountWeb(WD).view(), 'controls_token': self._action_token}))
        elif u.path == '/api/installation':
            from forge.installation_web import InstallationWeb
            try:
                result = InstallationWeb(WD).view()
                self._send(json.dumps({'ok': True, **result, 'controls_token': self._action_token}))
            except (ValueError, OSError, RuntimeError):
                self._send(json.dumps({'ok': False, 'reason': 'Installation progress is unavailable. Refresh to try again.'}))
        elif u.path == '/api/updates':
            from forge.updates import view
            self._send(json.dumps(view(refresh=parse_qs(u.query).get('refresh') == ['1'])))
        elif u.path == '/api/onboarding':
            from forge.onboarding import Onboarding
            try:
                self._send(json.dumps({'ok':True,**Onboarding(WD).view(),'controls_token':self._action_token}))
            except (ValueError,OSError,RuntimeError):
                self._send(json.dumps({'ok':False,'reason':'Setup state is unavailable; inspect the supervisor service'}))
        elif u.path == '/api/repositories/planning':
            from forge.factory.planning_service import PlanningService
            try:
                query=parse_qs(u.query)
                if set(query)-{'repository_id','proposal_id','row_id','after'} or 'repository_id' not in query or any(len(v)!=1 for v in query.values()):raise ValueError('Invalid planning view')
                args={k:v[0] for k,v in query.items()};args['repository_id']=int(args['repository_id'])
                result=PlanningService(WD,actor_id=self._operator_actor()).read(**args)
                self._send(json.dumps({'ok':True,**result,'controls_token':self._action_token}))
            except (ValueError,KeyError,OSError,RuntimeError):
                self._send(json.dumps({'ok':False,'reason':'Repository planning is unavailable. Refresh to try again.'}))
        elif u.path == '/api/issues/discussion':
            from forge.factory.discussions import Discussions
            try:
                query = parse_qs(u.query)
                if set(query) - {'issue', 'before'} or 'issue' not in query or any(len(v) != 1 for v in query.values()):
                    raise ValueError('Invalid conversation view')
                result = Discussions(WD).view(query['issue'][0], int(query['before'][0]) if 'before' in query else None)
                self._send(json.dumps({'ok': True, **result, 'controls_token': self._action_token}))
            except (ValueError, KeyError, OSError, RuntimeError):
                self._send(json.dumps({'ok': False, 'reason': 'Conversation unavailable. Refresh the issue and try again.'}))
        elif u.path == '/api/repositories/steward':
            from forge.repository_steward_policy import StewardPolicy
            try:
                query=parse_qs(u.query)
                if set(query)!={'repo_id'} or len(query['repo_id'])!=1:raise ValueError('Expected one repository')
                result=StewardPolicy(settings_service()).view(int(query['repo_id'][0]))
                self._send(json.dumps({'ok':True,**result,'controls_token':self._action_token}))
            except (ValueError,KeyError,OSError,RuntimeError):
                self._send(json.dumps({'ok':False,'reason':'Steward policy is unavailable. Refresh to try again.'}),status=409)
        elif u.path == '/api/issues/steward':
            from forge.factory.steward_service import StewardService
            try:
                query=parse_qs(u.query)
                if set(query)!={'key'} or len(query['key'])!=1:raise ValueError('Expected one issue key')
                result=StewardService(WD).view(query['key'][0])
                self._send(json.dumps({'ok':True,**result,'controls_token':self._action_token}))
            except (ValueError,KeyError,OSError,RuntimeError):
                self._send(json.dumps({'ok':False,'reason':'Steward choices are unavailable. Refresh to try again.'}),status=409)
        elif u.path == '/api/issues/delivery':
            try:
                from forge.factory.store_operator import selected_service
                query=parse_qs(u.query)
                if set(query)!={'key'} or len(query['key'])!=1:raise ValueError('Expected one issue key')
                service=selected_service(WD,actor_id=self._operator_actor())
                self._send(json.dumps({'ok':True,**service.membership(query['key'][0])}))
            except (ValueError,KeyError,RuntimeError) as error:
                self._send(json.dumps({'ok':False,'reason':str(error)}),status=409)
        elif u.path == '/api/issues':
            from forge.factory.issues import IssueInventory
            try:
                query=parse_qs(u.query)
                if set(query)-{'repo_id','state','q','page','issue'} or any(len(v)!=1 for v in query.values()):
                    raise ValueError('Invalid issue view')
                service=IssueInventory(WD)
                service.start_refresh()
                args=dict(repo_id=int(query['repo_id'][0]) if 'repo_id' in query else None,
                          state=query.get('state',['open'])[0],query=query.get('q',[''])[0],
                          page=int(query.get('page',['1'])[0]),key=query.get('issue',[None])[0])
                self._cached(('issues',)+tuple(sorted(args.items())), lambda: service.view(**args))
            except (OSError,ValueError,KeyError,RuntimeError):
                self._send(json.dumps({'ok':False,'reason':'Issue inventory is unavailable. Refresh to try again.'}))
        elif u.path == '/api/pull-requests':
            try:
                from forge.factory.control import public_views
                query = parse_qs(u.query, keep_blank_values=True)
                if (set(query)-{'repo_id','number','before','job'}
                        or any(len(value)!=1 or not value[0] for value in query.values())):
                    raise ValueError('Invalid pull request view')
                before = query.get('before', [''])[0]
                if set(query) <= {'before'}:
                    self._cached(('pull-requests',before),lambda: public_views(WD).pull_requests(before=before))
                else:
                    if not {'repo_id','number'} <= set(query):
                        raise ValueError('Expected repository and pull request number')
                    repo_id, number = int(query['repo_id'][0]), int(query['number'][0])
                    job = query.get('job', [None])[0]
                    self._cached(('pull-request',repo_id,number,before,job),
                        lambda: public_views(WD).pull_request(repo_id,number,before=before,job=job))
            except (ValueError,KeyError,OSError,RuntimeError):
                self._send(json.dumps({'ok':False,'reason':'Pull request work is unavailable. Refresh to try again.'}),status=404)
        elif u.path == '/api/delivery':
            from forge.store.models import DeliveryPlanChanged
            try:
                from forge.factory.control import public_views
                query = parse_qs(u.query, keep_blank_values=True)
                if set(query)-{'issue','after','task_after'} or 'issue' not in query or any(len(v)!=1 for v in query.values()):
                    raise ValueError('Expected an issue and optional delivery history cursor')
                service = public_views(WD)
                self._cached(('delivery',query['issue'][0],query.get('after',[''])[0],query.get('task_after',[''])[0]),
                             lambda: service.delivery(query['issue'][0],after=query.get('after',[''])[0],task_after=query.get('task_after',[''])[0]))
            except DeliveryPlanChanged:
                self._send(json.dumps({'ok':False,'reason':'The delivery plan changed. Reload its tasks.'}),status=409)
            except (OSError,ValueError,KeyError,RuntimeError):
                self._send(json.dumps({'ok':False,'reason':'Delivery history is unavailable. Refresh to try again.'}))
        elif u.path == '/api/factory/changes':
            try:
                from forge.factory.control import public_views
                from forge.factory.changes import Changes, ChangeFeedCancelled, socket_eof
                query = parse_qs(u.query, keep_blank_values=True)
                if set(query) - {'since', 'wait'} or any(len(v) != 1 or not v[0] for v in query.values()):
                    raise ValueError('Invalid factory change query')
                service = public_views(WD)
                result = Changes(service).read(since=query.get('since', [None])[0], wait=float(query.get('wait', ['25'])[0]),
                                               cancelled=lambda: socket_eof(self.connection))
                # A session revoked while this request waited cannot receive its
                # response. The same signed-in checks run before every request.
                if not self._operator_guard(read_snapshot=True) or not self._signed_in():
                    return
                if self._factory_paused:
                    self._send(json.dumps({'ok':False,'reason':'Factory reset is in progress'}))
                    return
                result['board'].update(ok=True, controls_token=self._action_token)
                self._send(json.dumps({'ok':True, **result, 'controls_token':self._action_token}))
            except ChangeFeedCancelled:
                return
            except ValueError:
                self._send(json.dumps({'ok':False,'reason':'Invalid factory change query'}), status=400)
            except (OSError, RuntimeError):
                self._send(json.dumps({'ok':False,'reason':'Factory changes are unavailable'}), status=503)
        elif u.path == '/api/factory':
            try:
                from forge.factory.control import factory_view
                query=parse_qs(u.query, keep_blank_values=True)
                if set(query)-{'section','issue','limit','before'} or any(len(v)!=1 or not v[0] for v in query.values()):
                    raise ValueError('Invalid factory view')
                self._cached(('factory',)+tuple(sorted((k, v[0]) for k, v in query.items())),
                             lambda: factory_view(WD, section=query.get('section',[None])[0], issue=query.get('issue',[None])[0],
                                                 limit=query.get('limit',[None])[0], before=query.get('before',[None])[0]))
            except (ValueError,OSError,RuntimeError):
                self._send(json.dumps({'ok':False,'reason':'Factory state is unavailable; check the supervisor service and retry.'}))
        elif u.path == '/api/factory/output':
            try:
                from forge.factory.contracts import identifier
                query=parse_qs(u.query)
                if set(query) != {'id'} or len(query['id']) != 1:raise ValueError('Invalid execution')
                from forge.factory.control import public_views
                result=public_views(WD).output(identifier(query['id'][0]))
                self._send(json.dumps({'ok':True,**result}))
            except (ValueError,KeyError,OSError,RuntimeError):
                self._send(json.dumps({'ok':False,'reason':'Execution output unavailable'}))
        elif u.path == '/api/repositories':
            from forge.repository_setup import RepositorySetup
            try:
                query=parse_qs(u.query)
                allowed={'repo_id'}
                if set(query)-allowed or any(len(v)!=1 for v in query.values()):raise ValueError('Invalid repository request')
                repo_id=int(query['repo_id'][0]) if 'repo_id' in query else None
                service=RepositorySetup(settings_service())
                self._send(json.dumps({'ok':True,**service.view(repo_id),'controls_token':self._action_token}))
            except (OSError,ValueError,TypeError,KeyError):
                self._send(json.dumps({'ok':False,'reason':'Repository setup changed or is unavailable. Check again.'}))
        elif u.path == '/api/settings':
            try:
                query=parse_qs(u.query)
                if query and query != {'summary':['1']}: raise ValueError('Invalid settings view')
                result=settings_service().view()
                if query:
                    fields=('id','name','enabled','configured','access','access_ready','protection_ready','protection_status','protection_previous_status','dispatch_allowed','setup_url','merge_policy')
                    result['repositories']=[{key:row[key] for key in fields if key in row} for row in result['repositories']]
                result.update(ok=True,controls_token=self._action_token)
                self._send(json.dumps(result))
            except (OSError,ValueError,TypeError,KeyError):
                self._send(json.dumps({'ok':False,'reason':'Settings unavailable. Validate factory.json on the server.'}))
        else:
            self.send_error(404)


class StatusServer(ThreadingHTTPServer):
    def __init__(self, *args, **kwargs):
        from forge.store.read_handles import IdleReadHandle
        self.read_handles = IdleReadHandle()
        super().__init__(*args, **kwargs)

    def server_close(self):
        self.read_handles.close()
        super().server_close()


if __name__ == "__main__":
    # The operator UI is the network front door; this API listens on loopback.
    ip = os.environ.get('FORGE_BIND_ADDRESS', '')
    try:
        address=ipaddress.ip_address(ip)
        port=int(os.environ.get('FORGE_PORT',str(PORT)))
        if address.version != 4 or address.is_unspecified or not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        raise SystemExit('Set FORGE_BIND_ADDRESS=127.0.0.1 and a valid FORGE_PORT; bin/forge setup writes both')
    if not address.is_loopback:
        raise SystemExit('The status API binds loopback only; the operator UI serves the network.')
    UPSTREAM_SECRET = upstream_secret(os.environ.get('FORGE_UPSTREAM_SECRET_FILE') or Path(WD) / 'state/ui-upstream.secret')
    print(f"forge-status on http://{ip}:{port}")
    threading.Thread(target=settings_service().watch,daemon=True).start()
    with StatusServer((ip, port), H) as server:
        server.serve_forever()
