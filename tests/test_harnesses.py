import hashlib
import io
import json
import subprocess
import threading
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.credentials import Credentials
from forge.harnesses.registry import manifests, adapter
from forge.harnesses.claude.canary import check_repository_access
from forge.host_setup import login, pending_login, main as setup_main
from forge.factory.control import controller_lock, ControllerBusy
from forge.onboarding import Onboarding
from forge.store.files import StateError
from forge.store.executions import provider_available
from tests import test_onboarding
from tests.sqlite_fixture import selected_store


def pool():
    return [{'id': 'codex', 'provider': 'codex', 'model': None, 'provider_environment': ['OPENAI_API_KEY'], 'provider_auth_file_env': None},
            {'id': 'claude', 'provider': 'claude', 'model': None, 'provider_environment': ['ANTHROPIC_API_KEY'], 'provider_auth_file_env': None}]


class Facts:
    """The GitHub surface a supervisor cycle touches while setup verification runs."""
    deadline = 0
    def issue(self, item): return {'state': 'open', 'labels': [], 'title': 'Implement behavior'}, item['requirements']
    def issues(self, item): return []
    def pulls(self, item): return []
    def publisher(self, role): return role + '[bot]'
    def own_draft(self, item, number, publisher): return None
    def target(self, item): return 'main'
    def token(self, *args, **kwargs): return 'synthetic-worker-token'
    def ci_evidence(self, job): return None
    def protections(self, *args): return True
    def publication(self, *args): return None
    def pr(self, item, number): return None
    def check(self, *args): return 1
    def labels(self, item): pass


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = selected_store(self.root)

    def test_installed_modules_implement_contract_and_cannot_load_repository_code(self):
        for name in manifests():
            self.assertTrue(callable(adapter(name)['run']))
            self.assertTrue(callable(adapter(name)['probe']))
        with self.assertRaises(ValueError): adapter('../../untrusted')

    def test_probe_requires_real_canary_tool_result_not_model_claim(self):
        completed = adapter('claude')['canary_completed']
        self.assertFalse(completed([{'type':'result','structured_output':{'ready':True}}], 'canary', 'nonce'))
        events = [{'type':'assistant', 'message':{'content':[{'type':'tool_use', 'id':'tool', 'name':'Bash', 'input':{'command':'canary'}}]}},
                  {'type':'user', 'message':{'content':[{'type':'tool_result','tool_use_id':'tool','content':'forge-canary:nonce'}]}}]
        self.assertTrue(completed(events, 'canary', 'nonce'))
        events[1]['message']['content'][0]['is_error'] = True
        self.assertFalse(completed(events, 'canary', 'nonce'))

    def test_claude_canary_failure_records_the_failed_step_bounded(self):
        report = adapter('claude')['canary_report']
        use = {'type':'assistant', 'message':{'content':[{'type':'tool_use', 'id':'tool', 'name':'Bash', 'input':{'command':'canary'}}]}}
        output = 'x' * 5000 + '\nforge-canary-failed:isolation:SystemExit: Private execution envelope was readable\x1b[0m'
        result = {'type':'user', 'message':{'content':[{'type':'tool_result','tool_use_id':'tool','is_error':True,
                                                        'content':[{'type':'text','text':output}]}]}}
        value = report([use, result], 'canary')
        self.assertEqual((value['type'], value['step'], value['is_error']), ('claude.canary', 'isolation', True))
        self.assertLessEqual(len(value['output'].encode()), 1024)
        self.assertNotIn('\x1b', value['output'])
        self.assertIn('Private execution envelope was readable', value['output'])
        forged = {'type':'user', 'message':{'content':[{'type':'tool_result','tool_use_id':'tool','content':'forge-canary-failed:../x:E'}]}}
        self.assertIsNone(report([use, forged], 'canary')['step'])
        self.assertEqual(report([], 'canary'), {'type':'claude.canary', 'tool_called':False, 'tool_result':False,
                                                'is_error':None, 'step':None, 'output':''})

    def test_claude_canary_names_its_failed_step(self):
        import contextlib, runpy
        canary = runpy.run_path('forge/harnesses/claude/canary.py')
        stdout = io.StringIO()
        with patch('sys.argv', ['canary', 'nonce']), contextlib.redirect_stdout(stdout), \
                patch.dict('os.environ', {'GH_TOKEN': 'synthetic'}), self.assertRaises(SystemExit) as raised:
            canary['main']()
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(stdout.getvalue(), 'forge-canary-failed:repository_access:SystemExit: Unexpected GitHub credential in worker environment\n')

    def test_claude_canary_accepts_a_writable_mask_but_not_a_readable_envelope(self):
        import runpy
        check = runpy.run_path('forge/harnesses/claude/canary.py')['check_isolation']
        real = Path.read_bytes
        def read(path):
            if str(path) == '/work/home/forge-exit.json':
                return b'synthetic private envelope'
            raise FileNotFoundError(path)
        writes = patch('pathlib.Path.write_text', side_effect=OSError(30, 'Read-only file system'))
        with patch.dict('os.environ', {}, clear=True), writes, patch('pathlib.Path.rename', side_effect=OSError(18, 'Invalid cross-device link')):
            with patch('pathlib.Path.read_bytes', side_effect=FileNotFoundError):
                check()
            with patch('pathlib.Path.read_bytes', new=read), self.assertRaisesRegex(SystemExit, 'envelope was readable'):
                check()
        self.assertIs(Path.read_bytes, real)

    def test_claude_probe_verifies_the_real_envelope_outside_the_sandbox(self):
        intact = adapter('claude')['envelope_intact']
        envelope, repository = self.root / 'forge-exit.json', self.root / 'repository'
        repository.mkdir()
        self.assertFalse(intact(envelope, repository))
        envelope.write_text('synthetic private envelope')
        self.assertTrue(intact(envelope, repository))
        (repository / 'stolen-envelope').write_text('forged')
        self.assertTrue(intact(envelope, repository))
        (repository / 'stolen-envelope').write_text('synthetic private envelope')
        self.assertFalse(intact(envelope, repository))
        (repository / 'stolen-envelope').unlink(); envelope.write_text('forged')
        self.assertFalse(intact(envelope, repository))

    def test_claude_large_transcript_retains_terminal_result_with_bounded_memory(self):
        import sys, os
        module = adapter('claude')
        temporary_file = tempfile.TemporaryFile
        terminal = {'type': 'result', 'structured_output': {'ready': True}}
        program = ('import sys,json; '
                   'sys.stdout.write(("x"*4096+"\\n")*800); '
                   'print(json.dumps(' + repr(terminal) + '))')
        with patch('tempfile.TemporaryFile', side_effect=lambda **kw: temporary_file(mode=kw['mode'], dir=self.root)):
            code, raw = module['collect']([sys.executable, '-c', program], 'prompt', self.root, dict(os.environ))
        self.assertEqual(code, 0)
        self.assertLessEqual(len(raw.encode()), 2*1024*1024)
        self.assertEqual(json.loads(raw.splitlines()[-1]), terminal)

    def test_claude_parent_never_launches_from_repository(self):
        module = adapter('claude')
        collect = Mock(return_value=(0, json.dumps({'type': 'result', 'structured_output': {'ready': True}})))
        with patch.dict(module['invoke'].__globals__, {'collect': collect,
                'Path': lambda value: self.root / Path(value).name}):
            module['invoke']({}, self.root, {}, {}, 'Test')
        args, prompt, directory, env = collect.call_args.args
        self.assertIn(str(self.root), args)
        self.assertIn('explicit cd', prompt)
        self.assertEqual(env['CLAUDE_CODE_SUBPROCESS_ENV_SCRUB'], '1')
        self.assertNotEqual(directory, self.root)

    def test_claude_probe_reports_fixed_failure_reasons(self):
        reason = adapter('claude')['probe_failure_reason']
        self.assertEqual(reason(None, 'auth', None, 'n'), 'provider_login_expired')
        self.assertEqual(reason(None, 'rate_limit', None, 'n'), 'provider_usage_limit')
        self.assertEqual(reason(None, 'unknown', None, 'n'), 'provider_connection_failed')
        self.assertEqual(reason(None, 'canary', None, 'n'), 'native_sandbox_failed')
        self.assertEqual(reason({'ready': True}, None, 'other', 'n'), 'native_sandbox_failed')
        self.assertEqual(reason({'ready': True}, None, None, 'n'), 'native_sandbox_failed')
        self.assertIsNone(reason({'ready': True}, None, 'n', 'n'))

    def test_claude_harness_environment_lets_oauth_refresh_reach_the_proxy(self):
        env = adapter('claude')['harness_environment']({'HTTPS_PROXY': 'http://forge-egress:3128', 'CLAUDE_CODE_PROXY_RESOLVES_HOSTS': '1', 'GH_TOKEN': 'job'})
        self.assertNotIn('CLAUDE_CODE_PROXY_RESOLVES_HOSTS', env)
        self.assertEqual(env['HTTPS_PROXY'], 'http://forge-egress:3128')
        self.assertEqual(env['CLAUDE_CODE_SUBPROCESS_ENV_SCRUB'], '1')
        self.assertEqual(env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'], '1')
        self.assertTrue(env['PATH'].startswith('/opt/forge/claude-tools:'))

    def test_grok_probe_reports_fixed_failure_reasons(self):
        module = adapter('grok')
        reason = module['probe_failure_reason']
        self.assertEqual(reason(None, 'auth', None, 'n'), 'provider_login_expired')
        self.assertEqual(reason(None, 'unknown', None, 'n'), 'provider_connection_failed')
        self.assertEqual(reason(None, 'canary', None, 'n'), 'native_sandbox_failed')
        self.assertEqual(reason({'ready': True}, None, 'other', 'n'), 'native_sandbox_failed')
        self.assertIsNone(reason({'ready': True}, None, 'n', 'n'))
        env = module['harness_environment']({'GH_TOKEN': 'job', 'XAI_API_KEY': 'synthetic'})
        self.assertEqual(env['GROK_HOME'], '/work/home/.grok')
        self.assertEqual(env['GROK_DISABLE_AUTOUPDATER'], '1')
        self.assertNotIn('/work/repository', env['PATH'])

    def test_binary_harness_manifest_is_pinned_and_verifiable(self):
        item = manifests()['grok']
        self.assertEqual(item['distribution'], 'binary')
        self.assertRegex(item['url'], r'^https://x\.ai/cli/grok-' + item['version'].replace('.', r'\.') + r'-linux-x86_64$')
        self.assertRegex(item['sha256'], r'^[a-f0-9]{64}$')
        self.assertEqual(item['auth_file'], '.grok/auth.json')
        self.assertEqual(item['login'], ['login', '--device-auth'])
        self.assertEqual(set(item['managed_files'].values()), {'/etc/grok/requirements.toml', '/etc/grok/managed_config.toml'})
        for name in item['managed_files']:
            self.assertTrue((Path('forge/harnesses/grok') / name).is_file(), name)

    def test_setup_probe_failure_messages_name_the_cause(self):
        from forge.setup_service import probe_failure_message
        self.assertIn('setup login claude', probe_failure_message({'result': {'reason': 'provider_login_expired'}}, 'claude'))
        self.assertIn('usage limit', probe_failure_message({'result': {'reason': 'provider_usage_limit'}}, 'claude'))
        self.assertIn('egress proxy', probe_failure_message({'result': {'reason': 'provider_connection_failed'}}, 'codex'))
        self.assertIn('native tool sandbox', probe_failure_message({'result': {'reason': 'native_sandbox_failed'}}, 'claude'))
        canary = json.dumps({'type': 'claude.canary', 'step': 'dependency_install', 'output': 'npm ERR'})
        self.assertIn('(failed canary step: dependency_install)', probe_failure_message({'result': {'reason': 'native_sandbox_failed'}, 'logs': canary}, 'claude'))
        odd = json.dumps({'type': 'claude.canary', 'step': 'x) run this'})
        self.assertNotIn('failed canary step', probe_failure_message({'result': {'reason': 'native_sandbox_failed'}, 'logs': odd}, 'claude'))
        self.assertIn('deadline', probe_failure_message({'result': None, 'logs': ''}, 'claude', timed_out=True))
        self.assertIn('deadline', probe_failure_message({'result': None, 'logs': '', 'execution': {'exit_code': 124}}, 'codex'))
        logs = '{"runner": "alive"}\n' + json.dumps({'type': 'error', 'message': 'Provider authentication_error; reconnect the account'}) + '\nTraceback\n'
        self.assertIn('the harness reported: Provider authentication_error', probe_failure_message({'result': None, 'logs': logs}, 'claude'))
        self.assertIn('(reason: odd_label)', probe_failure_message({'result': {'reason': 'odd_label'}}, 'claude'))
        self.assertNotIn('\x1b', probe_failure_message({'result': None, 'logs': json.dumps({'type': 'error', 'message': 'x\x1b[31m y'})}, 'claude'))
        self.assertTrue(probe_failure_message({'result': None, 'logs': 'garbage'}, 'claude').startswith('Provider or native sandbox check failed'))

    def test_credentials_refresh_cannot_overwrite_manual_reconnect(self):
        store = Credentials(self.root)
        ref = 'FORGE_SETUP_CODEX_AUTH'
        original = json.dumps({'tokens': {'synthetic': 'first'}})
        newer = json.dumps({'tokens': {'synthetic': 'new-login'}})
        store.put(ref, original, file=True)
        digest = hashlib.sha256(original.encode()).hexdigest()
        self.assertTrue(store.refresh(ref, digest, newer))
        self.assertFalse(store.refresh(ref, digest, original))
        self.assertEqual(Path(store.environment()[ref]).read_text(), newer)
        self.assertFalse(store.refresh('EXTERNAL_AUTH', digest, original))

    def test_repository_access_probe_requires_mirror_and_no_github_credentials(self):
        with patch.dict('os.environ', {}, clear=True), patch('pathlib.Path.read_text', return_value='http://forge-git/github/'):
            check_repository_access()
        with patch.dict('os.environ', {'GH_TOKEN': 'synthetic'}):
            with self.assertRaisesRegex(SystemExit, 'Unexpected GitHub credential'):
                check_repository_access()
        with patch.dict('os.environ', {}, clear=True), patch('pathlib.Path.read_text', return_value=''):
            with self.assertRaisesRegex(SystemExit, 'mirror routing'):
                check_repository_access()

    def test_terminal_import_preserves_source_and_connects_both_harnesses(self):
        source = self.root / 'source.json'; source.write_text('{"synthetic":"login"}')
        with patch('sys.stdout', new=io.StringIO()):
            for name in manifests(): login(self.root, name, import_login=source)
        setup = Onboarding(self.root)
        self.assertEqual({e['provider'] for e in setup.runtime()['provider_pool']}, set(manifests()))
        self.assertEqual(source.read_text(), '{"synthetic":"login"}')
        self.assertTrue(all(h['connected'] for h in setup.view()['harnesses']))
        self.assertNotIn('synthetic', json.dumps(setup.view()))

    def test_login_waits_for_supervisor_and_resumes_saved_sign_in_after_timeout(self):
        source = self.root / 'source.json'; source.write_text('{"synthetic":"login"}')
        with patch('forge.host_setup.controller_lock', side_effect=ControllerBusy('busy')) as lock:
            with patch('sys.stdout', new=io.StringIO()), self.assertRaisesRegex(ValueError, 'Sign-in is saved'):
                login(self.root, 'claude', import_login=source, models=['sonnet'])
            lock.assert_called_once_with(self.root, timeout=120)
        setup = Onboarding(self.root)
        pending = self.root / 'credentials/pending-login-claude.json'
        self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
        self.assertFalse(setup.runtime()['provider_pool'])
        self.assertNotIn('synthetic', json.dumps(setup.view()))
        # A second invocation uses the private saved login without Docker/OAuth.
        with patch('forge.host_setup.command') as command, patch('sys.stdout', new=io.StringIO()):
            login(self.root, 'claude')
            command.assert_not_called()
        self.assertFalse(pending.exists())
        self.assertEqual(setup.runtime()['provider_pool'][0]['model'], 'sonnet')
        self.assertEqual(Path(setup.credentials.environment()['FORGE_SETUP_CLAUDE_AUTH']).read_text(), source.read_text())

    def test_interactive_login_is_saved_before_container_cleanup_failure(self):
        setup = Onboarding(self.root)
        config = setup.runtime(); config['image'] = 'sha256:' + 'a' * 64
        setup.save_runtime(config, active=False)
        def command(args, **kwargs):
            if args[1] == 'create':
                mount = args[args.index('--mount') + 1]
                home = Path(mount.split('source=', 1)[1].split(',', 1)[0])
                (home / '.claude').mkdir()
                (home / '.claude/.credentials.json').write_text('{"synthetic":"interactive"}')
                return Mock(stdout='container-id')
            if args[1] == 'rm':
                raise subprocess.CalledProcessError(1, args)
            return Mock(returncode=0)
        from forge.harnesses.accounts import owner
        name = 'forge-login-' + owner(self.root) + '-claude'
        with patch('forge.host_setup.subprocess.run', side_effect=[Mock(returncode=1), Mock(returncode=0, stdout=name)]), patch('forge.host_setup.command', side_effect=command):
            with self.assertRaises(subprocess.CalledProcessError): login(self.root, 'claude')
        record = pending_login(setup, 'claude', setup.read()['generation'])
        self.assertEqual(record['value'], '{"synthetic":"interactive"}')
        with patch('forge.host_setup.subprocess.run', return_value=Mock(returncode=0, stdout=name)), patch('forge.host_setup.command') as command, patch('sys.stdout', new=io.StringIO()):
            login(self.root, 'claude')
            self.assertEqual(command.call_args.args[0], ['docker', 'rm', '--force', name])
        self.assertFalse((self.root / 'credentials/pending-login-claude.json').exists())

    def test_saved_login_from_before_reset_is_never_imported(self):
        setup = Onboarding(self.root); generation = setup.read()['generation']
        pending_login(setup, 'claude', generation, {'secret': None, 'value': '{"old":"login"}', 'models': None})
        with setup.store.transaction() as data: data['generation'] = 'new-generation'
        self.assertIsNone(pending_login(setup, 'claude', 'new-generation'))
        self.assertFalse((self.root / 'credentials/pending-login-claude.json').exists())
        with self.assertRaisesRegex(ValueError, 'reset during login'):
            pending_login(setup, 'claude', generation, {'secret': None, 'value': '{}', 'models': None})

    def test_controller_lock_remains_exclusive_and_can_wait_for_release(self):
        with controller_lock(self.root):
            with self.assertRaises(ControllerBusy):
                with controller_lock(self.root): pass
            with self.assertRaises(ControllerBusy):
                with controller_lock(self.root, timeout=0.01): pass
            entered = threading.Event()
            def contender():
                with controller_lock(self.root, timeout=2): entered.set()
            thread = threading.Thread(target=contender); thread.start()
            self.assertFalse(entered.wait(0.05))
        thread.join(timeout=3)
        self.assertTrue(entered.is_set())

    def test_terminal_explains_state_errors_without_generic_host_failure(self):
        stderr = io.StringIO()
        with patch('sys.argv', ['forge setup', 'login', 'claude', '--root', str(self.root)]), patch('forge.host_setup.login', side_effect=StateError('Supervisor is busy')), patch('sys.stderr', new=stderr):
            self.assertEqual(setup_main(), 1)
        self.assertIn('Supervisor is busy', stderr.getvalue())
        self.assertNotIn('Host command failed', stderr.getvalue())

    def test_invalid_models_do_not_persist_credentials_or_partial_configuration(self):
        setup = Onboarding(self.root)
        with self.assertRaises(ValueError):
            setup.connect('claude', api_key='synthetic-secret-value', models=['--unsafe'])
        self.assertFalse((self.root / 'credentials').exists())
        self.assertFalse(setup.runtime()['provider_pool'])


class TokenLoginTests(unittest.TestCase):
    """A long-lived token is an environment credential: shared, never refreshed, parallel."""
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = selected_store(self.root)
        self.setup = Onboarding(self.root)

    def test_token_connects_claude_as_a_parallel_environment_credential(self):
        token = 'synthetic-claude-token'
        self.setup.connect('claude', token=token)
        entry = self.setup.runtime()['provider_pool'][0]
        self.assertEqual(entry['provider_environment'], ['CLAUDE_CODE_OAUTH_TOKEN'])
        self.assertIsNone(entry['provider_auth_file_env'])
        self.assertEqual(self.setup.credentials.environment()['CLAUDE_CODE_OAUTH_TOKEN'], token)
        view = next(h for h in self.setup.view()['harnesses'] if h['id'] == 'claude')
        self.assertEqual(view['models'][0]['auth'], 'token')
        self.assertIn('--token', view['token_login_command'])
        self.assertNotIn('synthetic', json.dumps(self.setup.view()))
        # No login file to pin: a second job may share the token.
        with self.database.read_snapshot() as db:
            self.assertTrue(provider_available(db, 'claude', entry['id'], entry, 0))

    def test_browser_login_stays_single_writer(self):
        self.setup.connect('claude', auth_value='{"synthetic":"login"}')
        entry = self.setup.runtime()['provider_pool'][0]
        self.assertEqual(next(h for h in self.setup.view()['harnesses'] if h['id'] == 'claude')['models'][0]['auth'], 'login')
        # The refreshable login file is what admission pins to one writer.
        self.assertEqual(entry['provider_auth_file_env'], 'FORGE_SETUP_CLAUDE_AUTH')

    def test_token_login_is_refused_where_the_harness_offers_none(self):
        with self.assertRaisesRegex(ValueError, 'no long-lived token login'):
            self.setup.connect('codex', token='synthetic-token-value-1234')
        with self.assertRaisesRegex(ValueError, 'valid long-lived token'):
            self.setup.connect('claude', token='short')

    def test_terminal_token_login_prompts_privately_and_resumes_after_busy_supervisor(self):
        with patch('forge.host_setup.getpass.getpass', return_value='synthetic-claude-token') as prompt, \
                patch('forge.host_setup.controller_lock', side_effect=ControllerBusy('busy')), \
                patch('sys.stdout', new=io.StringIO()), self.assertRaisesRegex(ValueError, 'Sign-in is saved'):
            login(self.root, 'claude', token=True)
        self.assertIn('setup-token', prompt.call_args.args[0])
        self.assertFalse(self.setup.runtime()['provider_pool'])
        with patch('forge.host_setup.command') as command, patch('sys.stdout', new=io.StringIO()):
            login(self.root, 'claude')
            command.assert_not_called()
        entry = self.setup.runtime()['provider_pool'][0]
        self.assertEqual(entry['provider_environment'], ['CLAUDE_CODE_OAUTH_TOKEN'])
        self.assertFalse((self.root / 'credentials/pending-login-claude.json').exists())

    def test_terminal_flag_is_wired(self):
        with patch('sys.argv', ['forge setup', 'login', 'claude', '--token', '--root', str(self.root)]), \
                patch('forge.host_setup.login') as call, patch('sys.stdout', new=io.StringIO()):
            setup_main()
        self.assertTrue(call.call_args.kwargs['token'])


PIPELINE_CYCLE = 'forge.factory.sqlite_runtime.cycle'


class PoolSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = selected_store(self.root)
        self.api = Mock()
        self.api.jwt.return_value = 'synthetic-app-jwt'
        self.setup = Onboarding(self.root, client=self.api)
        self.setup.read()

    service = test_onboarding.SetupTests.service

    def test_unavailable_harness_does_not_prevent_verifying_other_account(self):
        self.setup.connect('codex', api_key='synthetic-codex-secret')
        self.setup.connect('claude', api_key='synthetic-claude-secret')
        service, engine, resets = self.service()
        original = service.runner_class.capture
        def capture(runner, job):
            if runner.config['provider'] == 'codex':
                return {'result': {'ready':False, 'reason':'provider_login_expired'}}
            return original(runner, job)
        service.runner_class.capture = capture
        self.setup.queue('harness_check', self.setup.view()['revision'])
        for _ in range(30): service.tick()
        self.assertIsNone(self.setup.read()['error'])
        self.assertEqual(self.setup.runtime()['provider_ready'], ['claude'])
        self.assertEqual(self.setup.read()['provider_checks'], {'codex':'failed','claude':'passed'})
        self.assertFalse(engine)

    def test_terminal_checks_all_harnesses_before_github_setup_and_cleans_every_probe(self):
        self.setup.connect('codex', api_key='synthetic-codex-secret')
        self.setup.connect('claude', api_key='synthetic-claude-secret', models=['sonnet', 'opus'])
        service, engine, resets = self.service()
        self.setup.queue('harness_check', self.setup.view()['revision'])
        for _ in range(30): service.tick()
        self.assertEqual(self.setup.read()['provider_checks'], {'codex':'passed','claude-1':'passed','claude-2':'passed'})
        self.assertFalse(self.setup.view()['can_start'])
        self.assertFalse(engine)
        self.assertEqual(len(set(resets)), 5)
        service.github_class.assert_not_called()

    # --- Scoped account verification (bin/forge setup check <harness>) -------

    def active(self, *, ready=None, enabled=False, keys=False, image='sha256:' + 'a' * 64):
        """Two connected harnesses on an active factory, with a usable GitHub fake.

        API-key accounts have no single-writer login, so their workers run in
        parallel; refreshable logins are the case a check has to wait for.
        """
        for name in ('codex', 'claude'):
            if keys:
                self.setup.connect(name, api_key='synthetic-' + name + '-api-key')
            else:
                self.setup.connect(name, auth_value='{"synthetic":"' + name + '"}')
        config = self.setup.runtime()
        config.update(enabled=enabled, merge_enabled=enabled, image=image, provider_ready=ready)
        self.setup.save_runtime(config, active=True)
        with self.setup.store.transaction() as data:
            data['phase'] = 'active'
        service, engine, resets = self.service()
        service.next_sync = time.monotonic() + 3600  # No background access refresh in tests.
        service.github_class = lambda settings, config: Facts()
        return service, engine, resets

    def worker(self, provider_id, number=7):
        """A pipeline worker holding a lease with one harness login."""
        from forge.factory.config import load
        entry = next(e for e in load(self.root)['provider_pool'] if e['id'] == provider_id)
        unit_id, issue, revision = 'unit-%d' % number, '10:%d' % number, 'revision-%d' % number
        job = 'job-%d' % number
        with self.database.write() as db:
            db.execute("INSERT OR IGNORE INTO repositories(id,full_name) VALUES(10,'example/project')")
            db.execute("INSERT INTO units(id,repository_id,base_branch,state,created_at,updated_at) VALUES(?,10,'main','building',90,90)", (unit_id,))
            db.execute("INSERT INTO issues(id,repository_id,github_number,github_state,current_revision_id,observed_at) VALUES(?,10,?,'open',?,90)",
                       (issue, number, revision))
            db.execute("INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) "
                       "VALUES(?,?,'Issue','Body','body','requirements','{}',90)", (revision, issue))
            db.execute("INSERT INTO unit_issues(id,unit_id,issue_id,repository_id,base_branch,requirements_revision_id,state,joined_at) "
                       "VALUES(?,?,?,10,'main',?,'pending',90)", ('member-' + unit_id, unit_id, issue, revision))
            db.execute("INSERT INTO attempts(id,unit_id,membership_generation,assignment_generation,input_manifest_json,budget_generation,created_at) "
                       "VALUES(?,?,1,1,'{}',1,100)", ('attempt-' + unit_id, unit_id))
            db.execute("INSERT INTO jobs(id,unit_id,attempt_id,role,phase,created_at) VALUES(?,?,?,'build','reserved',100)",
                       (job, unit_id, 'attempt-' + unit_id))
            db.execute('INSERT INTO job_leases VALUES(?,?,?,100)', ('build-%d' % number, job, 'fence-' + job))
            db.execute("INSERT INTO job_launches(job_id,slot,execution_fence,branch,publisher,provider,provider_id,runner_config_json,policy_json,context_json,deadline) "
                       "VALUES(?,?,?,'forge/work','build[bot]',?,?,?,'{}','{}',2800)",
                       (job, 'build-%d' % number, 'fence-' + job, entry['provider'], provider_id, json.dumps(entry)))
        return job

    def leased(self, job):
        with self.database.read_snapshot() as db:
            return db.execute('SELECT 1 FROM job_leases WHERE job_id=?', (job,)).fetchone() is not None

    def release(self, job):
        """The worker exits; it is fenced and cleaned before its lease is released."""
        with self.database.write() as db:
            db.execute("UPDATE jobs SET fenced=1,phase='cleaned',cleanup=1 WHERE id=?", (job,))
            db.execute('DELETE FROM job_leases WHERE job_id=?', (job,))

    def test_scoped_check_verifies_one_harness_without_stopping_other_workers(self):
        service, engine, resets = self.active(ready=['codex'])
        with self.setup.store.transaction() as data:
            data['provider_checks'] = {'codex': 'passed'}
            data['provider_errors'] = {'codex': None}
        job = self.worker('codex')
        self.setup.queue('harness_check', self.setup.view()['revision'], harness='claude')
        with patch(PIPELINE_CYCLE, return_value={'at': 0}):
            for _ in range(30): service.tick()
        data = self.setup.read()
        self.assertIsNone(data['operation'])
        self.assertIsNone(data['error'])
        self.assertEqual(data['provider_checks'], {'codex': 'passed', 'claude': 'passed'})
        self.assertTrue(self.leased(job))
        self.assertNotIn(job, resets)
        self.assertEqual(self.setup.active_runtime()['provider_ready'], ['codex', 'claude'])
        self.assertEqual([j['provider_id'] for j in data['probe_jobs'] if j['kind'] == 'provider'], ['claude'])

    def test_scoped_check_waits_for_workers_holding_the_same_login(self):
        service, engine, resets = self.active(ready=['codex', 'claude'])
        job = self.worker('codex')
        self.setup.queue('harness_check', self.setup.view()['revision'], harness='codex')
        with patch(PIPELINE_CYCLE, return_value={'at': 0}):
            for _ in range(5): service.tick()
            data = self.setup.read()
            self.assertEqual(data['operation']['step'], 'prepare')
            self.assertFalse(data['probe_jobs'])
            self.assertEqual(self.setup.activity(data)['label'],
                             'Waiting for workers using the Codex login to finish; other work continues')
            self.release(job)
            for _ in range(30): service.tick()
        data = self.setup.read()
        self.assertIsNone(data['operation'])
        self.assertEqual([j['provider_id'] for j in data['probe_jobs'] if j['kind'] == 'provider'], ['codex'])

    def test_scoped_check_reruns_runner_probes_only_for_a_new_image(self):
        service, engine, resets = self.active()
        self.setup.queue('harness_check', self.setup.view()['revision'], harness='claude')
        with patch(PIPELINE_CYCLE, return_value={'at': 0}):
            for _ in range(30): service.tick()
            data = self.setup.read()
            self.assertEqual([j['kind'] for j in data['probe_jobs']], ['provider', 'termination', 'reuse'])
            self.assertEqual(data['qualification']['image'], 'sha256:' + 'a' * 64)
            self.setup.queue('harness_check', self.setup.view()['revision'], harness='claude')
            for _ in range(30): service.tick()
            data = self.setup.read()
            self.assertEqual([j['kind'] for j in data['probe_jobs']], ['provider'])
            self.assertEqual(self.setup.read()['provider_checks'], {'claude': 'passed'})
            config = self.setup.active_runtime()
            config['image'] = 'sha256:' + 'b' * 64
            self.setup.save_runtime(config, active=True)
            self.setup.queue('harness_check', self.setup.view()['revision'], harness='claude')
            for _ in range(30): service.tick()
        self.assertEqual([j['kind'] for j in self.setup.read()['probe_jobs']], ['provider', 'termination', 'reuse'])

    def test_scoped_check_keeps_dispatching_unscoped_harnesses(self):
        service, engine, resets = self.active(ready=['codex', 'claude'], enabled=True, keys=True)
        self.setup.queue('harness_check', self.setup.view()['revision'], harness='claude')
        with patch(PIPELINE_CYCLE, return_value={'at': 0}) as cycle:
            service.tick(); service.tick()
        # The pipeline keeps dispatching, but never with the account being checked.
        dispatching = [c for c in cycle.call_args_list if c.kwargs.get('dispatch', True)]
        self.assertTrue(dispatching)
        self.assertTrue(all(c.kwargs['config']['provider_ready'] == ['codex'] for c in dispatching))
        self.assertEqual(self.setup.read()['operation']['step'], 'probe')

    def test_full_check_still_pauses_all_dispatch(self):
        service, engine, resets = self.active(ready=['codex', 'claude'], enabled=True, keys=True)
        self.setup.queue('harness_check', self.setup.view()['revision'])
        with patch(PIPELINE_CYCLE, return_value={'at': 0}) as cycle:
            service.tick(); service.tick()
        self.assertTrue(cycle.called)
        self.assertFalse([c for c in cycle.call_args_list if c.kwargs.get('dispatch', True)])
        self.assertEqual(self.setup.read()['operation']['step'], 'probe')

    def test_scope_names_a_connected_harness(self):
        service, engine, resets = self.active()
        with self.assertRaises(ValueError):
            self.setup.queue('harness_check', self.setup.view()['revision'], harness='../../elsewhere')
        with self.assertRaisesRegex(ValueError, 'Connect Grok'):
            self.setup.queue('harness_check', self.setup.view()['revision'], harness='grok')
        self.assertIsNone(self.setup.read()['operation'])


if __name__ == '__main__': unittest.main()
