import unittest
from unittest.mock import Mock, patch
import tempfile

from forge import status_api as dashboard
from tests.sqlite_fixture import selected_store

OPERATOR_CSRF = 'synthetic-operator-csrf'
OPERATOR_SESSION = 's' * 43


def signed_in(test, origin='http://127.0.0.1:8787'):
    """Configure operator sign-in and sign every request in, for one test.

    The status API offers control only to a signed-in operator; tests of the
    routes behind it start from that state."""
    from forge.operator_auth import SESSION_COOKIE, digest
    from forge.store.runtime import required
    # Mutation receipts reference the signed-in operator and session rows.
    with required(dashboard.WD).write() as db:
        db.execute("INSERT INTO operator_identities(id,github_id,login) VALUES('7',7,'operator')")
        db.execute('INSERT INTO operator_sessions(id,operator_id,token_hash,authorization_epoch,created_at,expires_at) '
                   "VALUES(?,'7',?,1,1,4102444800)", (digest(OPERATOR_SESSION), digest(OPERATOR_SESSION)))
    auth = Mock()
    auth.config.return_value = {'origin': origin}
    auth.session.return_value = {'operator': {'id': 7, 'login': 'operator'}, 'csrf': OPERATOR_CSRF, 'expires': 4102444800}
    patcher = patch('forge.operator_auth.OperatorAuth', return_value=auth)
    patcher.start(); test.addCleanup(patcher.stop)
    return {'Cookie': SESSION_COOKIE + '=' + OPERATOR_SESSION}


class NoSignInTests(unittest.TestCase):
    """Operational API access requires configured operator sign-in."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        selected_store(temporary.name)
        patcher = patch.object(dashboard, 'WD', temporary.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def handler(self, path, body=b''):
        import io
        handler = object.__new__(dashboard.H)
        handler.server = Mock(server_address=('127.0.0.1', 8787), read_handles=None)
        handler.path = path
        handler.headers = {'Host': '127.0.0.1:8787', 'Origin': 'http://127.0.0.1:8787', 'X-Forge-Action': '',
                           'Content-Type': 'application/json', 'Content-Length': str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._send = Mock(); handler.send_error = Mock()
        return handler

    def test_mutations_are_refused_without_configured_sign_in(self):
        for path in ('/api/settings/sync', '/api/factory/action', '/api/onboarding', '/api/issues/action'):
            with self.subTest(path=path), patch.object(dashboard, 'settings_service') as service:
                handler = self.handler(path, b'{}')
                handler.do_POST()
                handler.send_error.assert_called_once_with(401)
                handler._send.assert_not_called()
                service.assert_not_called()

    def test_session_metadata_is_available_without_configured_sign_in(self):
        import json
        handler = self.handler('/api/auth/session')
        handler.do_GET()
        session = json.loads(handler._send.call_args.args[0])
        self.assertFalse(session['enabled'])
        self.assertIsNone(session['operator'])
        self.assertNotIn('controls_token', session)

    def test_operational_reads_require_configured_sign_in(self):
        for path in ('/api/settings', '/api/factory', '/api/onboarding'):
            with self.subTest(path=path):
                handler = self.handler(path)
                handler.do_GET()
                handler.send_error.assert_called_once_with(401)
                handler._send.assert_not_called()


class HostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        selected_store(temporary.name)
        patcher = patch.object(dashboard, 'WD', temporary.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rebound_hostname_cannot_read_token_or_post_even_with_token(self):
        import io
        handler = object.__new__(dashboard.H)
        handler.server = Mock(server_address=('127.0.0.1', 8787), read_handles=None)
        handler.path = '/api/settings'
        handler.headers = {'Host': 'attacker.example:8787', 'Origin': 'http://attacker.example:8787',
                           'X-Forge-Action': OPERATOR_CSRF, 'Content-Type': 'application/json', 'Content-Length': '2'}
        handler.rfile = io.BytesIO(b'{}')
        handler._send = Mock(); handler.send_error = Mock()
        handler.do_POST()
        handler.send_error.assert_called_once_with(403)
        handler = object.__new__(dashboard.H)
        handler.server = Mock(server_address=('127.0.0.1', 8787), read_handles=None)
        handler.headers = {'Host': 'attacker.example:8787'}; handler.path = '/api/factory'
        handler.send_error = Mock(); handler._send = Mock()
        handler.do_GET()
        handler.send_error.assert_called_once_with(403); handler._send.assert_not_called()

    def test_retired_legacy_endpoints_are_gone(self):
        headers = signed_in(self)
        for path in ('/api/status', '/api/workflow', '/api/history', '/api/repository', '/api/pane'):
            handler = object.__new__(dashboard.H)
            handler.server = Mock(server_address=('127.0.0.1', 8787), read_handles=None)
            handler.headers = {'Host': '127.0.0.1:8787', **headers}; handler.path = path
            handler.send_error = Mock(); handler._send = Mock()
            handler.do_GET()
            handler.send_error.assert_called_once_with(404)


if __name__ == '__main__':
    unittest.main()
