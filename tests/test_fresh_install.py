"""Fresh-host tests never use a live VM, GitHub mutation, or system installer."""
from copy import deepcopy
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge_client import _install_host as remote
from forge_client.cli import main, parser
from forge_client.contracts import SetupError
from forge_client.install import Installation, InstallerHost, ROOT, SOURCE, STEPS, wizard
from forge_client.workflow import Journal
from forge_client.ssh_host import discover, pinned_options

COMMIT = 'a' * 40
INTENT = {'id': '1' * 32, 'name': 'Anvil', 'revision': COMMIT}
FACTS = {'os': 'ubuntu', 'version': '24.04', 'cpus': 4, 'memory_mib': 8192, 'free_disk_mib': 40000}
PACK = {'sha256': hashlib.sha256(b'PACKfixture').hexdigest(), 'data': base64.b64encode(b'PACKfixture').decode()}


def response(intent=None, status='not_installed', step=None, completed=None):
    return {'ok': True, 'intent': intent, 'host': FACTS, 'status': status, 'step': step,
            'completed': completed or [], 'error': None, 'source': SOURCE, 'root': ROOT, 'account': 'forge',
            'source_received': intent is not None}


class LocalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.store = Journal('test', 'target', root=self.directory)
        self.host = Mock()
        self.now = 100
        self.workflow = Installation('root@anvil.example', host=self.host, storage=self.store, clock=lambda: self.now)
        self.host.call.return_value = response()

    def plan(self):
        return self.workflow.execute('plan', name='Anvil', commit=COMMIT)

    def test_plan_is_read_only_remote_and_pins_intent_before_contact(self):
        def call(action, intent):
            self.assertEqual(action, 'plan')
            self.assertEqual(self.store.read()['intent'], intent)
            return response()
        self.host.call.side_effect = call
        result = self.plan()
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(result['revision'], COMMIT)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result['next_action']['command'], ['forge', 'setup', 'apply', '--ssh', 'root@anvil.example'])

    def test_apply_needs_saved_plan_and_conflicting_intent_never_contacts_host(self):
        with self.assertRaises(SetupError): self.workflow.execute('apply')
        self.assertFalse(self.host.call.called)
        self.plan(); self.host.call.reset_mock()
        for overrides in ({'name': 'Other'}, {'commit': 'b' * 40}):
            with self.assertRaises(SetupError): self.workflow.execute('plan', **overrides)
        self.assertFalse(self.host.call.called)

    def test_retry_keeps_identity_revision_and_timer_after_lost_response(self):
        self.plan()
        original = deepcopy(self.store.read())
        self.host.call.side_effect = SetupError('ssh_unavailable', 'Interrupted')
        with self.assertRaises(SetupError): self.workflow.execute('apply')
        self.now = 160
        self.host.call.side_effect = None
        self.host.call.return_value = response(original['intent'], 'installing', 'source', STEPS[:2])
        fresh = Installation('root@anvil.example', host=self.host, storage=self.store, clock=lambda: self.now)
        with patch('forge_client.install.revision', side_effect=AssertionError('must not resolve main again')):
            result = fresh.execute('resume')
        self.assertEqual(result['setup_elapsed_seconds'], 60)
        self.assertEqual(self.store.read()['intent'], original['intent'])
        self.host.call.assert_called_with('resume', original['intent'])

    def test_installed_is_not_a_factory_ready_or_first_merge_claim(self):
        self.plan()
        self.host.call.return_value = response(self.store.read()['intent'], 'installed', 'complete', STEPS)
        self.now = 200
        result = self.workflow.execute('status')
        self.assertEqual(result['status'], 'installed')
        self.assertEqual(result['milestones'], {'host_installed': 200})
        self.assertIn('still required', result['next_action']['instruction'])
        self.now = 300
        self.assertEqual(self.workflow.execute('status')['milestones'], {'host_installed': 200})

    def test_invalid_inputs_do_not_create_remote_effects(self):
        for value in ('bad\nname', 'x' * 65, '', ';touch /tmp/wrong'):
            with self.assertRaises(SetupError): self.workflow.execute('plan', name=value, commit=COMMIT)
        for value in (-1, 601, True):
            with self.assertRaises(SetupError): self.workflow.execute('plan', name='Anvil', commit=COMMIT, wait=value)
        self.assertFalse(self.host.call.called)

    def test_rebuilt_vm_new_run_archives_timer_and_checks_before_replacing(self):
        self.plan()
        previous = deepcopy(self.store.read())
        self.now = 500
        self.host.call.side_effect = SetupError('installation_conflict', 'Existing host')
        with self.assertRaises(SetupError):
            self.workflow.execute('plan', name='Anvil', commit=COMMIT, new_run=True)
        self.assertEqual(self.store.read(), previous)
        self.host.call.side_effect = None
        result = self.workflow.execute('plan', name='Anvil', commit=COMMIT, new_run=True)
        self.assertNotEqual(result['setup_id'], previous['intent']['id'])
        self.assertEqual(result['setup_elapsed_seconds'], 0)
        archive = Journal('archived-setup', previous['intent']['id'], root=self.directory)
        self.assertEqual(archive.read(), previous)

    def test_resumption_cannot_silently_accept_changed_host_key(self):
        first, second = 'SHA256:' + 'a' * 43, 'SHA256:' + 'b' * 43
        self.workflow.execute('plan', name='Anvil', commit=COMMIT, fingerprint=first)
        with self.assertRaises(SetupError):
            self.workflow.execute('plan', name='Anvil', fingerprint=second)
        self.workflow.execute('status')
        self.assertEqual(self.host.fingerprint, first)

    def test_wizard_decline_does_not_apply(self):
        answers = iter(['root@anvil.example', 'Anvil', 'yes', 'no'])
        args = SimpleNamespace(ssh=None, name=None, revision=COMMIT)
        result = wizard(args, ask=lambda _: next(answers), tell=lambda _: None, installation=lambda _: self.workflow)
        self.assertEqual(result['status'], 'planned')
        self.assertEqual([c.args[0] for c in self.host.call.call_args_list], ['plan'])

    def test_wizard_uses_same_operations_as_noninteractive_client(self):
        def call(action, intent):
            return response() if action == 'plan' else response(intent, 'installed', 'complete', STEPS)
        self.host.call.side_effect = call
        args = SimpleNamespace(ssh='root@anvil.example', name='Anvil', revision=COMMIT)
        result = wizard(args, ask=lambda _: 'yes', tell=lambda _: None, installation=lambda _: self.workflow,
                        network_wizard=lambda *a, **kw: {'status': 'installed'})
        self.assertEqual(result['status'], 'installed')
        self.assertEqual([c.args[0] for c in self.host.call.call_args_list], ['plan', 'apply'])

    def test_json_never_prompts_even_on_terminal(self):
        with patch('sys.stdin.isatty', return_value=True), patch('forge_client.install.execute', return_value={'status': 'planned'}) as execute, \
                patch('forge_client.install.wizard', side_effect=AssertionError('no wizard')), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(['setup', 'new', '--json', '--ssh', 'root@anvil.example', '--name', 'Anvil']), 0)
            self.assertEqual(json.loads(output.getvalue())['status'], 'planned')
        self.assertTrue(execute.called)
        self.assertTrue(parser().parse_args(['--json', 'setup', 'new']).json)


class TransportTests(unittest.TestCase):
    def test_only_fixed_payload_and_no_operator_credentials_cross_ssh(self):
        secret = 'synthetic-github-admin-secret'
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            self.assertNotIn(secret, json.dumps([argv, kwargs]))
            self.assertIn('StrictHostKeyChecking=yes', argv)
            self.assertIn('BatchMode=yes', argv)
            self.assertEqual(kwargs['payload'], {'version': 1, 'action': 'plan', 'intent': INTENT,
                                                'source': None, 'upgrade_installer': False})
            # The transmitted Python payload is syntactically valid and bundled.
            command = argv[-1].split('then exec ', 1)[1].split('; else exec ', 1)[0]
            code = shlex.split(command)[2]
            compile(code, '<remote>', 'exec')
            return json.dumps(response()).encode()
        with patch.dict(os.environ, GH_TOKEN=secret, GITHUB_TOKEN=secret, OPENAI_API_KEY=secret):
            result = InstallerHost('root@anvil.example', run=run).call('plan', INTENT)
        self.assertEqual(result['status'], 'not_installed')
        self.assertEqual(len(calls), 1)

    def test_untrusted_response_and_diagnostics_cannot_echo_secret(self):
        secret = 'synthetic-secret'
        for raw in ({'ok': False, 'error': secret, 'reason': secret},
                    {'ok': False, 'error': {'secret': secret}},
                    {**response(), 'host': {**FACTS, 'password': secret}},
                    {**response(), 'completed': [secret]},
                    response(INTENT, 'installed', 'complete', ['packages'])):
            with self.assertRaises(SetupError) as caught:
                InstallerHost('root@anvil.example', run=lambda *a, **k: json.dumps(raw).encode()).call('plan', INTENT)
            self.assertNotIn(secret, str(caught.exception))

    def test_old_server_error_points_to_the_host_update(self):
        host = InstallerHost('root@anvil.example', run=lambda *a, **k:
            json.dumps({'ok': False, 'error': 'server_maintenance_required'}).encode())
        with self.assertRaises(SetupError) as caught:
            host.rpc('payload', {})
        self.assertEqual(caught.exception.code, 'server_maintenance_required')
        self.assertIn('bin/forge update', str(caught.exception))

    def test_account_failures_preserve_sanitized_runtime_codes(self):
        for code in ('account_runtime_failed', 'runner_image_required', 'account_owner_changed', 'account_login_running'):
            with self.subTest(code=code):
                host = InstallerHost('root@anvil.example', run=lambda *a, **k:
                    json.dumps({'ok': False, 'error': code, 'reason': 'private-provider-output'}).encode())
                with self.assertRaises(SetupError) as caught:
                    host.rpc('payload', {})
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn('private-provider-output', str(caught.exception))

    def test_destination_cannot_inject_shell_options(self):
        for value in ('-oProxyCommand=bad', 'root@host;touch /tmp/x', 'root@host\nother'):
            with self.assertRaises(SetupError): InstallerHost(value)


class HostKeyTests(unittest.TestCase):
    def setUp(self):
        wire = b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + b'x' * 32
        self.key = base64.b64encode(wire).decode()

    def test_discovery_is_read_only_and_scans_the_resolved_port(self):
        run = Mock(side_effect=[b'hostname anvil.example\nport 2222\nuser root\n',
                                ('[anvil.example]:2222 ssh-ed25519 ' + self.key + '\n').encode()])
        observed = discover('anvil', run=run)
        self.assertEqual(observed['port'], 2222)
        self.assertRegex(observed['fingerprint'], r'^SHA256:[A-Za-z0-9+/]{43}$')
        self.assertEqual(run.call_args_list[1].args[0][-3:], ['-t', 'ed25519', 'anvil.example'])

    def test_verified_key_uses_private_ephemeral_known_hosts(self):
        value = {'fingerprint': 'SHA256:' + 'a' * 43, 'key': self.key}
        with pinned_options('anvil', value['fingerprint'], discover_key=lambda _: value) as options:
            file = Path(next(item.split('=', 1)[1] for item in options if item.startswith('UserKnownHostsFile=')))
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            self.assertIn(self.key, file.read_text())
            self.assertIn('HostKeyAlgorithms=ssh-ed25519', options)
        self.assertFalse(file.exists())

    def test_changed_key_or_invalid_key_cannot_reach_ssh(self):
        with self.assertRaises(SetupError) as caught:
            with pinned_options('anvil', 'SHA256:' + 'b' * 43,
                                discover_key=lambda _: {'fingerprint': 'SHA256:' + 'a' * 43}):
                self.fail('Must not connect')
        self.assertEqual(caught.exception.code, 'host_key_changed')
        for raw in (b'host ssh-ed25519 invalid', b'host ssh-rsa aaaa', b''):
            run = Mock(side_effect=[b'hostname host\nport 22\n', raw])
            with self.assertRaises(SetupError): discover('host', run=run)

    def test_jump_host_scan_does_not_mistake_public_address_for_target(self):
        run = Mock(return_value=b'hostname target\nport 22\nproxyjump gateway\n')
        with self.assertRaises(SetupError): discover('target', run=run)
        self.assertEqual(run.call_count, 1)


class HostArchitectureTests(unittest.TestCase):
    def setUp(self):
        for target, options in (
            ('sys.platform', {'new': 'linux'}),
            ('os.geteuid', {'return_value': 0}),
            ('os.cpu_count', {'return_value': 4}),
            ('os.statvfs', {'return_value': SimpleNamespace(f_bavail=40000, f_frsize=1048576)}),
            ('Path.exists', {'return_value': True}),
            ('Path.is_dir', {'return_value': True}),
            ('Path.read_text', {'autospec': True, 'side_effect': lambda path: {
                '/etc/os-release': 'ID=ubuntu\nVERSION_ID="24.04"\n',
                '/sys/module/apparmor/parameters/enabled': 'Y\n',
                '/proc/meminfo': 'MemTotal: 8388608 kB\n',
            }[str(path)]}),
        ):
            context = patch('forge_client._install_host.' + target, **options)
            context.start(); self.addCleanup(context.stop)

    def test_x86_64_and_arm64_ubuntu_facts_match_supported_contract(self):
        for architecture in ('x86_64', 'aarch64', 'arm64'):
            with self.subTest(architecture=architecture), patch.object(remote.platform, 'machine', return_value=architecture):
                self.assertEqual(remote.facts(), FACTS)

    def test_other_architectures_are_refused_before_planning_or_host_mutations(self):
        for architecture in ('i686', 'armv7l', 'riscv64'):
            for action in ('plan', 'apply', 'resume'):
                with self.subTest(architecture=architecture, action=action), \
                     patch.object(remote.platform, 'machine', return_value=architecture), \
                     patch.object(remote, 'read') as read, \
                     patch.object(remote, 'private_directory') as directory, \
                     patch.object(remote, 'write') as write, patch.object(remote, 'run') as run:
                    with self.assertRaisesRegex(remote.InstallError, '^unsupported_architecture$'):
                        remote.dispatch({'version': 1, 'action': action, 'intent': INTENT,
                                         'source': PACK if action != 'plan' else None,
                                         'upgrade_installer': False}, 'payload')
                    read.assert_not_called(); directory.assert_not_called()
                    write.assert_not_called(); run.assert_not_called()

    def test_cli_preserves_architecture_error_and_explains_supported_host(self):
        host = InstallerHost('anvil', run=Mock(return_value=b'{"ok":false,"error":"unsupported_architecture"}'))
        with self.assertRaises(SetupError) as caught:
            host.rpc('payload', {})
        self.assertEqual(caught.exception.code, 'unsupported_architecture')
        self.assertIn('x86-64 (amd64) or ARM64 (aarch64) Ubuntu 24.04', str(caught.exception))


class RemoteInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / 'state'
        for name, value in (('STATE', self.state), ('facts', lambda: FACTS),
                            ('check_new_host', Mock()), ('running', lambda data: False)):
            context = patch.object(remote, name, value)
            context.start(); self.addCleanup(context.stop)
        # Tests have a private directory owned by the local unprivileged user.
        def private(path, create=False):
            if create: path.mkdir(mode=0o700, exist_ok=True)
            remote.need(path.resolve() == path and path.is_dir() and path.stat().st_mode & 0o077 == 0, 'unsafe_install_state')
        context = patch.object(remote, 'private_directory', private)
        context.start(); self.addCleanup(context.stop)
        self.commands = []
        context = patch.object(remote, 'run', lambda argv, **kwargs: self.commands.append(argv))
        context.start(); self.addCleanup(context.stop)

    def call(self, action='plan', intent=None, payload='payload', source=PACK, upgrade=False):
        return remote.dispatch({'version': 1, 'action': action, 'intent': intent or INTENT,
                                'source': source if action in ('apply', 'resume') else None,
                                'upgrade_installer': upgrade}, payload)

    def test_plan_has_no_host_writes_and_apply_launches_durable_work(self):
        self.assertEqual(self.call()['status'], 'not_installed')
        self.assertFalse(self.state.exists())
        self.call('apply')
        self.assertEqual(remote.read()['intent'], INTENT)
        command = self.commands[-1]
        self.assertEqual(command[0], 'systemd-run')
        self.assertIn('--property=UMask=0077', command)
        self.assertNotIn('--wait', command)
        self.assertEqual((self.state / 'worker.py').stat().st_mode & 0o777, 0o600)

    def test_dropped_response_does_not_launch_duplicate_running_job(self):
        self.call('apply')
        with patch.object(remote, 'running', return_value=True):
            result = self.call('resume')
        self.assertEqual(result['status'], 'installing')
        self.assertEqual(len(self.commands), 1)

    def test_conflicts_and_changed_installer_preserve_state(self):
        self.call('apply')
        original = (self.state / 'installation.json').read_bytes()
        for kwargs in ({'intent': {**INTENT, 'revision': 'b' * 40}}, {'payload': 'different'}):
            with self.assertRaises(remote.InstallError): self.call('resume', **kwargs)
        self.assertEqual((self.state / 'installation.json').read_bytes(), original)
        self.assertEqual(len(self.commands), 1)

    def test_explicit_installer_upgrade_keeps_identity_source_and_completed_steps(self):
        self.call('apply')
        data = remote.read()
        data.update(step='source', completed=['packages', 'account'], package_revision=remote.PACKAGE_REVISION)
        remote.write(data)
        self.call('resume', payload='repaired-installer', upgrade=True)
        result = remote.read()
        for key in ('intent', 'started_at', 'completed', 'source_sha256'):
            self.assertEqual(result[key], data[key])
        self.assertEqual(result['payload'], hashlib.sha256(b'repaired-installer').hexdigest())

    def test_repaired_package_plan_rechecks_only_changed_prerequisites(self):
        self.call('apply')
        data = remote.read()
        data.update(step='host', completed=list(remote.STEPS[:-1]), package_revision='older-package-plan')
        remote.write(data)
        self.call('resume', payload='new-installer', upgrade=True)
        self.assertEqual(remote.read()['completed'], ['account', 'source', 'isolation'])
        done = []
        with patch.object(remote, 'install_step', side_effect=lambda step, data: done.append(step)):
            remote.work()
        self.assertEqual(done, ['packages', 'host'])
        self.assertEqual(remote.read()['completed'], list(remote.STEPS))

    def test_source_pack_digest_is_checked_before_job_launch(self):
        with self.assertRaises(remote.InstallError):
            self.call('apply', source={**PACK, 'sha256': '0' * 64})
        self.assertFalse(self.commands)
        self.assertFalse((self.state / 'source.pack').exists())

    def test_interrupted_step_replays_without_repeating_completed_steps(self):
        self.call('apply')
        done = []
        def install(step, data):
            done.append(step)
            if step == 'source': raise RuntimeError('synthetic-secret-in-child-error')
        with patch.object(remote, 'install_step', side_effect=install):
            self.assertEqual(remote.work(), 1)
        data = remote.read()
        self.assertEqual(data['completed'], ['packages', 'account'])
        self.assertEqual(data['step'], 'source')
        self.assertEqual(data['error'], 'host_command_failed')
        self.assertNotIn('synthetic-secret', json.dumps(data))
        done.clear()
        with patch.object(remote, 'install_step', side_effect=lambda step, data: done.append(step)):
            self.assertEqual(remote.work(), 0)
        self.assertEqual(done, ['source', 'isolation', 'host'])
        self.assertEqual(self.call('status')['status'], 'installed')
        count = len(self.commands)
        self.call('apply')
        self.assertEqual(len(self.commands), count)

    def test_status_after_restart_does_not_resume_by_itself(self):
        self.call('apply')
        result = self.call('status')
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(len(self.commands), 1)

    def test_symlink_worker_and_state_are_refused(self):
        self.state.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(remote.InstallError): self.call('apply')
        self.state.unlink()
        self.call('apply')
        worker = self.state / 'worker.py'
        worker.unlink(); worker.symlink_to(self.root / 'foreign')
        with self.assertRaises(remote.InstallError): self.call('resume')
        self.assertFalse((self.root / 'foreign').exists())

    def test_unknown_fields_rejected_before_inspection(self):
        with patch.object(remote, 'facts', side_effect=AssertionError('must validate first')):
            with self.assertRaises(remote.InstallError):
                remote.dispatch({'version': 1, 'action': 'apply', 'intent': INTENT, 'token': 'secret'}, 'payload')


if __name__ == '__main__':
    unittest.main()
