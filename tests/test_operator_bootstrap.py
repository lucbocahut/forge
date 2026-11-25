from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from forge.configuration_authority import ConfigurationAuthority
from forge.onboarding import Onboarding
from forge.operator_auth import AuthError, FLOW_COOKIE, SESSION_COOKIE, read_cookie
from forge.operator_bootstrap import Bootstrap, BootstrapError, verify
from forge_client.bootstrap import Host, setup, verify_https
from forge_client.cli import parser, main
from forge_client.contracts import SetupError
from tests.sqlite_fixture import selected_store

ORIGIN = 'https://forge.example.ts.net'
TARGET = {'origin': ORIGIN, 'client_id': 'identity-client', 'operators': [123]}
SECRET = 'a' * 40


class HostTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        selected_store(self.root)
        ConfigurationAuthority(self.root).replace('host-runtime.json', {}, expected_revision=0)
        Onboarding(self.root).read()
        self.generation()
        self.serve = {}
        self.commands = []
        self.verify = Mock()
        self.host = Bootstrap(self.root, command=self.command, check_https=self.verify)

    def generation(self, value='generation-one', phase='active'):
        with Onboarding(self.root).store.transaction() as setup:
            setup.update(generation=value, phase=phase)

    def command(self, argv, **kwargs):
        self.commands.append(argv)
        if argv == ['tailscale', 'status', '--json']:
            return json.dumps({'BackendState': 'Running', 'Self': {'DNSName': 'forge.example.ts.net.'}})
        if argv == ['tailscale', 'serve', 'status', '--json']:
            return json.dumps(self.serve)
        if argv[0] == 'sudo':
            self.serve = {'TCP': {'443': {'HTTPS': True}},
                          'Web': {'forge.example.ts.net:443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8787'}}}}}
        return b''

    def request(self, action='plan', *, target=None, revision=None, secret=None):
        return self.host.execute({'version': 1, 'action': action, 'target': target or TARGET,
                                  'revision': revision, 'secret': secret})

    def apply(self):
        return self.request('apply', revision=self.request()['revision'], secret=SECRET)

    def error(self, code, operation):
        with self.assertRaises(BootstrapError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_plan_then_apply_private_state_and_fixed_host_actions(self):
        plan = self.request()
        self.assertFalse(plan['configured'])
        self.assertIsNone(self.host.auth.config())
        self.assertTrue(all(command[0] == 'tailscale' for command in self.commands))
        result = self.apply()
        self.assertEqual(result['status'], 'ready')
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(self.host.auth.config()['client_secret'], SECRET)
        self.assertEqual(self.host.auth.store.path.stat().st_mode & 0o777, 0o600)
        updates = [argv for argv in self.commands if argv[0].endswith('/bin/forge')]
        self.assertEqual(updates[0][1:], ['update', '--root', str(self.root), '--no-pull', '--bind', '127.0.0.1'])
        self.assertNotIn(SECRET, json.dumps(self.commands))
        self.verify.assert_called_once_with(ORIGIN)

    def test_resume_and_apply_preserve_real_session_pending_flow_and_epoch(self):
        self.apply()
        auth = self.host.auth
        auth.identity = lambda *args: {'id': 123, 'login': 'operator'}
        url, cookie = auth.begin()
        state = parse_qs(urlsplit(url).query)['state'][0]
        session = read_cookie(auth.callback(state, 'code', read_cookie(cookie, FLOW_COOKIE)), SESSION_COOKIE)
        auth.begin()
        original = auth.store.path.read_bytes()
        for action in ('apply', 'resume'):
            result = self.request(action, revision=self.request()['revision'])
            self.assertEqual(result['status'], 'ready')
            self.assertEqual(original, auth.store.path.read_bytes())
            self.assertEqual(auth.session(session)['operator']['id'], 123)

    def test_failure_after_secret_write_resumes_without_resubmitting_secret(self):
        original = self.host.command
        def fail_update(argv, **kwargs):
            if argv[0].endswith('/bin/forge'):
                raise BootstrapError('host_unavailable', 'Updater failed')
            return original(argv, **kwargs)
        self.host.command = fail_update
        self.error('host_update_failed', self.apply)
        state = self.host.auth.store.path.read_bytes()
        self.host.command = original
        result = self.request('resume', revision=self.request()['revision'])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(state, self.host.auth.store.path.read_bytes())

    def test_conflicting_proxy_funnel_or_identity_is_never_replaced(self):
        for serve in ({'Web': {'other': {}}}, {'AllowFunnel': {'forge.example.ts.net:443': True}}):
            self.serve = serve
            self.error('https_conflict', self.apply)
            self.assertIsNone(self.host.auth.config())
        self.serve = {}
        self.apply()
        original = self.host.auth.store.path.read_bytes()
        for key, value in (('operators', [999]), ('client_id', 'another-client')):
            self.error('identity_conflict', lambda: self.request(target={**TARGET, key: value}))
        self.assertEqual(original, self.host.auth.store.path.read_bytes())

    def test_proxy_added_during_update_is_not_overwritten(self):
        original = self.host.command
        def changed(argv, **kwargs):
            if argv[0].endswith('/bin/forge'):
                self.serve = {'TCP': {'1234': {}}}
            return original(argv, **kwargs)
        self.host.command = changed
        self.error('https_conflict', self.apply)
        self.assertFalse(any(argv[0] == 'sudo' for argv in self.commands))

    def test_stale_plan_reset_or_parallel_bootstrap_is_refused(self):
        revision = self.request()['revision']
        self.generation('generation-two')
        self.error('plan_changed', lambda: self.request('apply', revision=revision, secret=SECRET))
        self.assertIsNone(self.host.auth.config())
        self.generation(phase='resetting')
        self.error('reset_in_progress', self.request)
        self.generation()
        with self.host.locked():
            self.error('bootstrap_busy', self.request)

    def test_initialize_cannot_replace_concurrent_operator_configuration(self):
        auth = self.host.auth
        auth.configure(ORIGIN, 'another-client', SECRET, [456])
        original = auth.store.path.read_bytes()
        with self.assertRaises(AuthError):
            auth.configure(ORIGIN, TARGET['client_id'], SECRET, [123], if_unconfigured=True)
        self.assertEqual(original, auth.store.path.read_bytes())

    def test_reset_during_update_stops_before_proxy_mutation(self):
        original = self.host.command
        def changed(argv, **kwargs):
            if argv[0].endswith('/bin/forge'):
                self.generation('generation-two')
            return original(argv, **kwargs)
        self.host.command = changed
        self.error('plan_changed', self.apply)
        self.assertFalse(any(argv[0] == 'sudo' for argv in self.commands))

    def test_resume_requires_prior_secret_and_wrong_host_is_rejected(self):
        self.error('identity_secret_required', lambda: self.request('resume', revision=self.request()['revision']))
        for value in ('https://other.example.ts.net', ORIGIN + ':443', 'https://public.example'):
            self.error('tailscale_origin_mismatch', lambda: self.request(target={**TARGET, 'origin': value}))

    def test_uninstalled_or_public_root_and_malformed_requests_are_refused(self):
        with tempfile.TemporaryDirectory() as uninstalled:
            uninstalled = Path(uninstalled).resolve()
            selected_store(uninstalled)
            self.error('installation_required', lambda: Bootstrap(uninstalled))
        self.root.chmod(0o755)
        self.error('invalid_root', lambda: Bootstrap(self.root))
        self.root.chmod(0o700)
        for operators in ([True], [{}], [], [2, 1], [1, 1]):
            self.error('invalid_request', lambda: self.request(target={**TARGET, 'operators': operators}))
        self.error('invalid_request', lambda: self.request(secret=SECRET))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.args = parser().parse_args(['--json', 'server', 'setup', '--ssh', 'forge', '--source', '/srv/forge',
                                        '--root', '/srv/private', '--origin', ORIGIN, '--operator', 'operator',
                                        '--client-id', TARGET['client_id']])
        self.plan = {'ok': True, 'status': 'planned', **TARGET, 'generation': 'generation-one',
                     'configured': False, 'revision': 'a' * 64}
        self.host = Mock()
        self.host.call.side_effect = lambda action, *args: deepcopy(self.plan) if action == 'plan' else {
            **self.plan, 'status': 'ready', 'configured': True}
        self.github = Mock()
        self.github.owner.return_value = {'id': 123, 'login': 'operator'}
        self.verify, self.prompt = Mock(), Mock(side_effect=AssertionError('Unexpected prompt'))
        self.tell = Mock()

    def run_setup(self, **kwargs):
        return setup(self.args, host_factory=lambda *args: self.host, github_factory=lambda: self.github,
                     verify=self.verify, prompt=self.prompt, tell=self.tell, **kwargs)

    def test_missing_app_returns_fixed_browser_fields_without_prompt_or_write(self):
        self.args.client_id = self.plan['client_id'] = None
        result = self.run_setup()
        self.assertEqual(result['status'], 'identity_app_required')
        self.assertEqual(result['next_action']['url'], 'https://github.com/settings/applications/new')
        self.assertEqual(result['next_action']['callback_url'], ORIGIN + '/auth/github/callback')
        self.assertEqual(result['next_action']['oauth_scopes'], [])
        self.assertEqual(self.host.call.call_count, 1)

    def test_plan_and_json_apply_never_prompt(self):
        self.assertEqual(self.run_setup()['status'], 'planned')
        self.args.apply = True
        self.assertEqual(self.run_setup()['status'], 'identity_secret_required')
        self.assertEqual(self.host.call.call_count, 2)
        self.verify.assert_not_called()

    def test_stdin_secret_transferred_once_without_output_and_resume_skips_input(self):
        self.args.apply = self.args.secret_stdin = True
        result = self.run_setup(stdin=io.StringIO(SECRET + '\n'))
        self.host.call.assert_called_with('apply', TARGET, self.plan['revision'], SECRET)
        self.assertNotIn(SECRET, json.dumps(result))
        self.verify.assert_called_once_with(ORIGIN)
        self.args.apply = self.args.secret_stdin = False
        self.args.resume = True
        self.plan['configured'] = True
        self.run_setup(stdin=Mock(read=Mock(side_effect=AssertionError('Unexpected secret read'))))
        self.host.call.assert_called_with('resume', TARGET, self.plan['revision'], None)

    def test_hidden_prompt_and_personal_access_token_rejection(self):
        self.args.json = False
        self.args.apply = True
        self.prompt.side_effect = None
        self.prompt.return_value = SECRET
        with patch('sys.stdin.isatty', return_value=True):
            self.run_setup()
        self.prompt.assert_called_once()
        instructions = '\n'.join(call.args[0] for call in self.tell.call_args_list)
        self.assertIn('Generate a new client secret', instructions)
        self.assertIn('Copy token', instructions)
        self.assertIn('Nothing appears while you paste', instructions)
        self.assertNotIn(SECRET, instructions)
        self.args.secret_stdin = True
        with self.assertRaises(SetupError) as caught:
            self.run_setup(stdin=io.StringIO('ghp_' + 'X' * 36))
        self.assertEqual(caught.exception.code, 'invalid_identity_secret')

    def test_ssh_uses_stdin_strict_keys_quoted_paths_and_no_github_credentials(self):
        run = Mock(return_value=json.dumps(self.plan).encode())
        source = "/srv/forge's $(touch bad)"
        host = Host('user@forge', source, '/srv/private', run=run)
        with patch.dict(os.environ, {'GH_TOKEN': 'private-token', 'GITHUB_TOKEN': 'another-token'}):
            host.call('apply', TARGET, 'a' * 64, SECRET)
        argv = run.call_args.args[0]
        options = run.call_args.kwargs
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertIn('ClearAllForwardings=yes', argv)
        self.assertIn('-a', argv)
        self.assertEqual(shlex.split(argv[-1]), ['cd', source, '&&', 'exec', 'python3', '-m',
                                                'forge.launch', 'forge.operator_bootstrap', '--root', '/srv/private'])
        self.assertNotIn('GH_TOKEN', options['environ'])
        self.assertNotIn('GITHUB_TOKEN', options['environ'])
        self.assertEqual(options['payload']['secret'], SECRET)
        self.assertNotIn(SECRET, json.dumps(argv))
        for ssh in ('-oProxyCommand=bad', 'host;bad', 'host\ncommand'):
            with self.assertRaises(SetupError):
                Host(ssh, source, '/srv/private')

    def test_remote_errors_and_mismatched_success_never_echo_remote_data(self):
        for response in ({'ok': False, 'error': 'host_unavailable', 'reason': SECRET},
                         {**self.plan, 'origin': 'https://attacker.example'},
                         {**self.plan, 'secret': SECRET}, {'ok': True}):
            host = Host('forge', '/srv/forge', '/srv/private', run=Mock(return_value=json.dumps(response)))
            with self.assertRaises(SetupError) as caught:
                host.call('plan', TARGET)
            self.assertNotIn(SECRET, str(caught.exception))
        with patch('forge_client.cli.execute', side_effect=RuntimeError(SECRET)), patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(main(['commands']), 1)
            self.assertNotIn(SECRET, out.getvalue())

    def test_generation_changed_during_apply_is_not_reported_ready(self):
        self.args.resume = True
        self.host.call.side_effect = [self.plan, {**self.plan, 'status': 'ready', 'configured': True,
                                                'generation': 'changed-generation'}]
        with self.assertRaises(SetupError):
            self.run_setup()
        self.verify.assert_not_called()


class HttpsTests(unittest.TestCase):
    def test_host_retries_temporary_transport_failure_but_stops_after_three_attempts(self):
        from urllib.error import URLError
        response = Mock(status=200)
        response.read.return_value = b'{"ok":true,"enabled":true,"operator":null}'
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        opener, sleep = Mock(), Mock()
        opener.open.side_effect = [URLError('starting'), context]
        with patch('forge.operator_bootstrap.build_opener', return_value=opener):
            verify(ORIGIN, sleep=sleep)
        self.assertEqual(opener.open.call_count, 2)
        sleep.assert_called_once_with(2)
        opener.open.reset_mock(); sleep.reset_mock()
        opener.open.side_effect = URLError(SECRET)
        with patch('forge.operator_bootstrap.build_opener', return_value=opener), self.assertRaises(BootstrapError) as caught:
            verify(ORIGIN, sleep=sleep)
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_both_verifiers_use_fixed_endpoint_without_proxies_and_reject_redirects(self):
        for module, function, exception in (('forge.operator_bootstrap', verify, BootstrapError),
                                            ('forge_client.bootstrap', verify_https, SetupError)):
            response = Mock(status=200)
            response.read.return_value = b'{"ok":true,"enabled":true,"operator":null}'
            opener = Mock()
            opener.open.return_value.__enter__ = Mock(return_value=response)
            opener.open.return_value.__exit__ = Mock(return_value=False)
            with patch(module + '.build_opener', return_value=opener) as build:
                function(ORIGIN)
            proxy, redirect = build.call_args.args
            self.assertEqual(proxy.proxies, {})
            with self.assertRaises(exception):
                redirect.redirect_request(None, None, 302, 'redirect', {}, 'https://other.example')
            self.assertEqual(opener.open.call_args.args[0].full_url, ORIGIN + '/api/auth/session')
            response.read.assert_called_once_with(4097)

    def test_both_verifiers_require_enabled_identity_and_sanitize_tls_errors(self):
        for module, function, exception in (('forge.operator_bootstrap', verify, BootstrapError),
                                            ('forge_client.bootstrap', verify_https, SetupError)):
            for body in ({'ok': True, 'enabled': False, 'operator': None},
                         {'ok': True, 'enabled': True, 'operator': {'id': 123}}, []):
                response = Mock(status=200)
                response.read.return_value = json.dumps(body).encode()
                opener = Mock()
                opener.open.return_value.__enter__ = Mock(return_value=response)
                opener.open.return_value.__exit__ = Mock(return_value=False)
                with patch(module + '.build_opener', return_value=opener), self.assertRaises(exception):
                    function(ORIGIN)
            with patch(module + '.build_opener', side_effect=RuntimeError(SECRET)), self.assertRaises(exception) as caught:
                function(ORIGIN)
            self.assertNotIn(SECRET, str(caught.exception))


if __name__ == '__main__':
    unittest.main()
