from contextlib import ExitStack, redirect_stdout
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge import host_update, host_setup
from tests.sqlite_fixture import selected_store


class FakeHost:
    """Docker, systemd and sudo stand-ins that record what an update did."""

    def __init__(self, directory):
        self.base = Path(directory)
        self.root = self.base / 'root'; self.root.mkdir(mode=0o700)
        selected_store(self.root)
        self.home = self.base / 'home'; self.home.mkdir()
        self.apparmor = self.base / 'apparmor/forge-runner-private-proc'; self.apparmor.parent.mkdir()
        self.images, self.calls, self.revision, self.builds = {}, [], 'a' * 40, 0
        self.qualify = Mock()
        self.active, self.activations = set(), {}
        self.health_checks = 0

    def run(self, argv, check=False, **kwargs):
        self.calls.append(argv)
        result = Mock(returncode=0, stdout='')
        if argv[:2] == ['systemctl', '--user'] and argv[2] in ('start', 'restart'):
            for unit in argv[3:]:
                if argv[2] == 'restart' or unit not in self.active:
                    self.activations[unit] = self.activations.get(unit, 0) + 1
                self.active.add(unit)
        elif argv[:2] == ['docker', 'version']:
            result.stdout = '29.5.3\n'
        elif argv[:2] == ['git', '-C'] and argv[3:] == ['rev-parse', 'HEAD']:
            result.stdout = self.revision + '\n'
        elif argv[:3] == ['docker', 'image', 'inspect']:
            if argv[3] in self.images:
                result.stdout = self.images[argv[3]] + '\n'
            else:
                result.returncode = 1
        elif argv[:2] == ['docker', 'build']:
            tag = argv[argv.index('-t') + 1]
            self.builds += 1
            self.images[tag] = 'sha256:' + str(self.builds) * 64
        elif argv[:2] == ['docker', 'run'] and 'npm run build' in argv[-1]:
            build = Path(argv[argv.index('--mount') + 1].split('source=')[1].split(',')[0])
            (build / '.output/server').mkdir(parents=True)
            (build / '.output/server/index.mjs').write_text('ui')
        elif argv[:2] == ['sudo', 'install']:
            Path(argv[-1]).write_bytes(Path(argv[-2]).read_bytes())
        if check and result.returncode:
            raise subprocess.CalledProcessError(result.returncode, argv)
        return result

    def patches(self):
        policy = {'defaultAction': 'SCMP_ACT_ERRNO'}
        return [patch.dict('os.environ', {'FORGE_BUILD_FROM_SOURCE': '1'}),
                patch.object(host_update, 'command', side_effect=lambda argv, **kw: self.run(argv, check=True, **kw)),
                patch.object(host_update.subprocess, 'run', side_effect=self.run),
                patch.object(host_update.Path, 'home', return_value=self.home),
                patch.object(host_update, 'APPARMOR_TARGET', self.apparmor),
                patch.object(host_update.shutil, 'which', return_value='/usr/bin/docker'),
                patch.object(host_update.urllib.request, 'urlopen', side_effect=lambda *a, **k: io.BytesIO(b'{}')),
                patch.object(host_update.runpy, 'run_path', return_value={'generate': lambda *a: policy}),
                patch.object(host_update, 'qualify_runner', self.qualify),
                patch.object(host_update, 'wait_healthy', side_effect=lambda bind, **kw: self.healthy(bind)),
                patch.object(host_update, 'Onboarding', return_value=Mock(runtime=Mock(return_value={'slots': {}})))]

    def healthy(self, bind):
        self.health_checks += 1
        if self.active != {name + '.service' for name in host_update.SERVICES}:
            raise RuntimeError('Managed service is stopped')

    def reconcile(self, **kwargs):
        with ExitStack() as stack:
            for item in self.patches():
                stack.enter_context(item)
            self.calls.clear()
            with patch('sys.stdout', io.StringIO()) as out:
                changed = host_update.reconcile(self.root, **kwargs)
            return changed, out.getvalue()

    def document(self, name):
        return host_update.runtime_document(self.root, name)

    def seed(self, name, value):
        snapshot = host_update.runtime_snapshot(self.root, name)
        host_update.save_runtime_document(self.root, name, value, expected_revision=snapshot['revision'] if snapshot else 0)

    def restarted(self):
        return [argv[3:] for argv in self.calls if argv[:3] == ['systemctl', '--user', 'restart']]

    def unit(self, name):
        return (self.home / '.config/systemd/user' / (name + '.service')).read_text()


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.host = FakeHost(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_fresh_install_runs_the_operator_ui_in_front_of_a_loopback_api(self):
        self.host.reconcile()
        status, ui = self.host.unit('forge-status'), self.host.unit('forge-ui')
        self.assertIn('FORGE_BIND_ADDRESS=127.0.0.1', status)
        self.assertIn('FORGE_PORT=8788', status)
        self.assertIn('"HOST=127.0.0.1"', ui)
        self.assertIn('NUXT_PUBLIC_FORGE_SETUP_PENDING=true', ui)
        self.assertIn('"NITRO_FORGE_API=http://127.0.0.1:8788"', ui)
        self.assertIn(host_update.NODE_IMAGE, ui)
        self.assertIn('service --root', self.host.unit('forge-supervisor'))
        secret = self.host.root / 'state/ui-upstream.secret'
        self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
        self.assertIn('FORGE_UPSTREAM_SECRET_FILE=' + str(secret), status)
        self.assertIn('source=' + str(secret) + ',target=/run/forge/upstream.secret,readonly', ui)
        self.assertIn('NITRO_FORGE_UPSTREAM_SECRET_FILE=/run/forge/upstream.secret', ui)
        self.assertTrue((self.host.root / 'ui/current/server/index.mjs').exists())
        for guide in ('onboarding.md', 'issue-state-machine.md', 'factory-runtime.md'):
            self.assertTrue((self.host.root / 'ui/current/docs' / guide).exists())
        self.assertTrue((self.host.root / 'ui/current/docs/assets/setup-hero.svg').exists())
        self.assertIn('"FORGE_ROOT=/app"', ui)
        self.assertEqual(self.host.restarted(), [['forge-mirror.service'], ['forge-registry.service'], ['forge-status.service', 'forge-ui.service', 'forge-supervisor.service']])
        self.host.qualify.assert_called_once()
        runtime = self.host.document('host-runtime.json')
        self.assertEqual(runtime['image'], 'sha256:' + '1' * 64)
        self.assertEqual(runtime['apparmor_profile'], 'forge-runner-private-proc')
        self.assertEqual(self.host.document('host.json')['source'], 'a' * 40)
        self.assertFalse((self.host.root / 'host-runtime.json').exists())

    def test_configured_identity_replaces_setup_screen_without_weakening_host_checks(self):
        with patch('forge.operator_auth.OperatorAuth.config', return_value={
                'origin': 'https://factory.example.ts.net'}):
            ui = host_update.desired_units(self.host.root, '127.0.0.1')['forge-ui']
        self.assertIn('NUXT_PUBLIC_FORGE_SETUP_PENDING=false', ui)
        self.assertIn('NITRO_ALLOWED_HOSTS=factory.example.ts.net', ui)
        self.assertIn('NITRO_FORGE_TAILSCALE_IDENTITY=serve', ui)

    def test_an_update_after_a_maintenance_stop_starts_unchanged_services_too(self):
        # The schema upgrade stops every Forge unit, then runs this update.
        # A source-only change restarts the Python services; the unchanged UI
        # must still come back or the health wait fails the whole update.
        self.host.reconcile()
        active = set()
        run = self.host.run
        def systemd(argv, check=False, **kwargs):
            if argv[:3] in (['systemctl', '--user', 'restart'], ['systemctl', '--user', 'start']):
                active.update(argv[3:])
            return run(argv, check=check, **kwargs)
        self.host.run = systemd
        self.host.revision = 'b' * 40
        def healthy(bind):
            self.assertEqual(active, {name + '.service' for name in host_update.SERVICES})
        self.host.healthy = healthy
        self.host.reconcile()
        self.assertEqual(self.host.restarted(), [['forge-mirror.service'], ['forge-registry.service'], ['forge-status.service', 'forge-supervisor.service']])
        self.assertIn(['systemctl', '--user', 'start', 'forge-ui.service'], self.host.calls)
        self.assertEqual(self.host.document('host.json')['source'], 'b' * 40)
        _, output = self.host.reconcile()
        self.assertIn('Forge host is up to date', output)

    def test_stopping_or_replacing_an_already_removed_ui_container_succeeds(self):
        import os
        import shlex
        self.host.reconcile()
        unit = self.host.unit('forge-ui')
        name = host_update.ui_container(self.host.root)
        # `docker run --rm` already removed the container: Docker refuses both.
        docker = self.host.base / 'docker'
        docker.write_text('#!/bin/sh\nfor last; do :; done\ncase "$1" in stop|rm)\n'
                          '  echo "Error response from daemon: No such container: $last" >&2; exit 1;;\nesac\n')
        os.chmod(docker, 0o755)
        def execute(directive):
            lines = [line.split('=', 1)[1] for line in unit.splitlines() if line.startswith(directive + '=')]
            self.assertEqual(len(lines), 1, directive)
            ignore_failure = lines[0].startswith('-')  # systemd's "-" prefix
            argv = shlex.split(lines[0].lstrip('-').replace('/usr/bin/docker', str(docker)))
            result = subprocess.run(argv, capture_output=True, text=True)
            self.assertIn('No such container: ' + name, result.stderr)
            return 'done' if ignore_failure or not result.returncode else 'failed'
        # Stop (including a restart's stop), then the replacement's start.
        self.assertEqual([execute(d) for d in ('ExecStop', 'ExecStopPost', 'ExecStartPre')], ['done'] * 3)

    def test_mirror_unit_runs_the_qualified_launcher_before_supervisor(self):
        self.host.reconcile()
        mirror = self.host.unit('forge-mirror')
        self.assertIn('forge.launch forge.mirror_daemon --root', mirror)
        supervisor = self.host.unit('forge-supervisor')
        self.assertIn('Requires=forge-mirror.service', supervisor)
        self.assertIn('After=network-online.target forge-mirror.service', supervisor)
        health = next(i for i, call in enumerate(self.host.calls) if 'forge.mirror_daemon' in call and '--check' in call)
        start = next(i for i, call in enumerate(self.host.calls) if call[:3] == ['systemctl', '--user', 'restart'] and 'forge-supervisor.service' in call)
        self.assertLess(health, start)
        registry = self.host.unit('forge-registry')
        self.assertIn('forge.launch forge.registry_cache --root', registry)
        self.assertIn('Requires=forge-mirror.service forge-registry.service', supervisor)
        registry_health = next(i for i, call in enumerate(self.host.calls)
                              if 'forge.registry_cache' in call and '--check' in call)
        self.assertLess(registry_health, start)

    def test_unsupported_engine_refuses_before_build_or_host_changes(self):
        original = self.host.run
        def old_engine(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[:2] == ['docker', 'version']: result.stdout = '23.0.5'
            return result
        self.host.run = old_engine
        with self.assertRaisesRegex(RuntimeError, 'Docker Engine 28'):
            self.host.reconcile()
        self.assertFalse(self.host.builds)
        self.assertFalse((self.host.home / '.config/systemd/user').exists())

    def test_failed_fresh_canary_installs_no_services_or_success_marker(self):
        self.host.qualify.side_effect = RuntimeError('Browser unavailable')
        with self.assertRaisesRegex(RuntimeError, 'Browser unavailable'):
            self.host.reconcile()
        self.assertFalse((self.host.home / '.config/systemd/user/forge-ui.service').exists())
        self.assertIsNone(self.host.document('host-runtime.json'))
        self.assertEqual(self.host.restarted(), [])

    def test_repeating_an_unchanged_update_touches_nothing(self):
        self.host.reconcile()
        changed, output = self.host.reconcile()
        self.assertFalse(changed)
        self.assertIn('Forge host is up to date at aaaaaaaaaaaa', output)
        self.assertEqual(self.host.restarted(), [])
        self.assertFalse(any(argv[:2] in (['docker', 'build'], ['docker', 'run'], ['docker', 'compose']) for argv in self.host.calls))
        self.host.qualify.assert_called_once()  # Only by the install.

    def test_merge_only_update_starts_unchanged_ui_after_migration_stops_units(self):
        self.host.reconcile()
        self.host.active.clear()  # Upgrade's migration stops every managed unit.
        self.host.revision = 'b' * 40
        changed, _ = self.host.reconcile()
        self.assertFalse(changed)
        self.assertEqual(self.host.restarted(), [['forge-mirror.service'], ['forge-registry.service'],
                                                ['forge-status.service', 'forge-supervisor.service']])
        self.assertIn(['systemctl', '--user', 'start', 'forge-ui.service'], self.host.calls)
        self.assertEqual(self.host.health_checks, 2)
        self.assertEqual(self.host.document('host.json')['source'], 'b' * 40)
        self.assertEqual(self.host.builds, 1)
        self.host.qualify.assert_called_once()

    def test_up_to_date_update_restores_stopped_units_without_restarting_healthy_units(self):
        self.host.reconcile()
        self.host.active.difference_update({'forge-mirror.service', 'forge-ui.service'})
        changed, _ = self.host.reconcile()
        self.assertFalse(changed)
        self.assertEqual(self.host.restarted(), [])
        self.assertEqual(self.host.activations, {'forge-mirror.service': 2, 'forge-registry.service': 1, 'forge-status.service': 1,
                                                 'forge-ui.service': 2, 'forge-supervisor.service': 1})
        health = next(i for i, call in enumerate(self.host.calls)
                      if 'forge.mirror_daemon' in call and '--check' in call)
        start = next(i for i, call in enumerate(self.host.calls)
                     if call[:3] == ['systemctl', '--user', 'start'] and 'forge-supervisor.service' in call)
        self.assertLess(health, start)
        self.assertEqual(self.host.health_checks, 2)
        self.assertFalse(any(argv[:2] in (['docker', 'build'], ['docker', 'run'], ['docker', 'compose'])
                             for argv in self.host.calls))

    def failing(self, prefix):
        run = self.host.run
        def fail(argv, check=False, **kwargs):
            if argv[:len(prefix)] == prefix:
                self.host.calls.append(argv)
                raise subprocess.CalledProcessError(1, argv)
            return run(argv, check=check, **kwargs)
        self.host.run = fail

    def test_without_sudo_setup_prints_the_administrator_apparmor_commands(self):
        self.failing(['sudo'])
        with self.assertRaises(ValueError) as caught:
            self.host.reconcile()
        message = str(caught.exception)
        self.assertIn('sudo install -m 0644 ' + str(host_update.SOURCE / 'deploy/runner/forge-runner-private-proc.apparmor')
                      + ' ' + str(self.host.apparmor), message)
        self.assertIn('sudo apparmor_parser -r ' + str(self.host.apparmor), message)
        self.assertFalse((self.host.home / '.config/systemd/user/forge-ui.service').exists())
        self.host.qualify.assert_not_called()

    def test_a_profile_an_administrator_installed_needs_no_sudo_and_later_updates_normally(self):
        wanted = (host_update.SOURCE / 'deploy/runner/forge-runner-private-proc.apparmor').read_bytes()
        self.host.apparmor.write_bytes(wanted)
        self.host.reconcile()
        self.assertFalse(any(argv[0] == 'sudo' for argv in self.host.calls))
        self.assertTrue(host_update.Record(self.host.root).owns(self.host.apparmor, wanted))

    def test_the_egress_proxy_runs_as_the_service_account_and_must_become_healthy(self):
        seen = []
        run = self.host.run
        def record(argv, check=False, **kwargs):
            if argv[:2] == ['docker', 'compose']:
                seen.append((argv, kwargs.get('env', {})))
            return run(argv, check=check, **kwargs)
        self.host.run = record
        self.host.reconcile()
        argv, env = seen[0]
        self.assertEqual(argv[argv.index('up'):], ['up', '-d', '--wait', '--wait-timeout', '60', 'egress'])
        self.assertEqual(env['FORGE_EGRESS_USER'], f'{host_update.os.getuid()}:{host_update.os.getgid()}')
        compose = (host_update.SOURCE / 'deploy/runner/compose.yaml').read_text()
        self.assertIn('user: "${FORGE_EGRESS_USER:-1000:1000}"', compose)
        self.assertIn('healthcheck:', compose)
        self.assertIn('name: forge-uplink', compose)

    def test_an_unhealthy_egress_proxy_fails_setup_before_services_are_installed(self):
        self.failing(['docker', 'compose'])
        with self.assertRaisesRegex(ValueError, 'egress proxy did not become healthy'):
            self.host.reconcile()
        self.assertFalse((self.host.home / '.config/systemd/user/forge-ui.service').exists())
        self.assertIsNone(self.host.document('host-runtime.json'))

    def test_a_source_change_restarts_the_python_services_only(self):
        self.host.reconcile()
        self.host.revision = 'b' * 40
        changed, _ = self.host.reconcile()
        self.assertFalse(changed)
        self.assertEqual(self.host.restarted(), [['forge-mirror.service'], ['forge-registry.service'], ['forge-status.service', 'forge-supervisor.service']])
        self.assertEqual(self.host.builds, 1)

    def test_a_new_runner_is_qualified_before_every_runtime_switches_to_it(self):
        self.host.reconcile()
        for name in ('setup/runtime.json', 'runtime.json'):
            self.host.seed(name, {'image': 'sha256:' + '1' * 64, 'slots': {'build': 3}})
        with patch.object(host_update, 'RUNNER_INPUTS', host_update.RUNNER_INPUTS + ('README.md',)):
            self.host.qualify.side_effect = RuntimeError('Browser unavailable')
            with self.assertRaisesRegex(RuntimeError, 'Browser unavailable'):
                self.host.reconcile()
            for name in ('host-runtime.json', 'setup/runtime.json', 'runtime.json'):
                self.assertEqual(self.host.document(name)['image'], 'sha256:' + '1' * 64)
            self.host.qualify.side_effect = None
            changed, output = self.host.reconcile()
        self.assertTrue(changed)
        self.assertIn('runner image', output)
        self.assertEqual(self.host.builds, 2)  # The failed attempt's image is reused, not rebuilt.
        for name in ('host-runtime.json', 'setup/runtime.json', 'runtime.json'):
            self.assertEqual(self.host.document(name)['image'], 'sha256:' + '2' * 64)
        self.assertEqual(self.host.document('runtime.json')['slots'], {'build': 3})

    def test_an_update_interrupted_after_switching_still_requalifies_when_rerun(self):
        self.host.reconcile()
        with patch.object(host_update, 'RUNNER_INPUTS', host_update.RUNNER_INPUTS + ('README.md',)):
            with patch.object(host_update, 'switch_ui', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    self.host.reconcile()
            self.assertEqual(self.host.document('host-runtime.json')['image'], 'sha256:' + '2' * 64)
            changed, _ = self.host.reconcile()
        self.assertTrue(changed)

    def test_full_environment_is_opt_in_and_only_switches_after_service_qualification(self):
        self.host.reconcile()
        with ExitStack() as stack:
            for patcher in self.host.patches():
                stack.enter_context(patcher)
            self.host.qualify.side_effect = RuntimeError('MongoDB round trip failed')
            with self.assertRaisesRegex(RuntimeError, 'MongoDB'):
                host_update.install_full_environment(self.host.root)
            runtime = self.host.document('host-runtime.json')
            self.assertEqual(runtime.get('environment_tiers', {}), {})
            self.host.qualify.side_effect = None
            host_update.install_full_environment(self.host.root)
        full = self.host.document('host-runtime.json')['environment_tiers']['full']
        self.assertEqual(full['image'], 'sha256:' + '2' * 64)
        self.assertEqual(self.host.qualify.call_args.kwargs, {'full': True})
        self.assertEqual(self.host.document('host.json')['full_image'], full['image'])

    def test_update_rebuilds_and_qualifies_the_opted_in_full_tier(self):
        self.host.reconcile()
        with ExitStack() as stack:
            for patcher in self.host.patches():
                stack.enter_context(patcher)
            host_update.install_full_environment(self.host.root)
        self.host.qualify.reset_mock()
        with patch.object(host_update, 'RUNNER_INPUTS', host_update.RUNNER_INPUTS + ('README.md',)):
            changed, _ = self.host.reconcile()
        self.assertTrue(changed)
        self.assertEqual(self.host.qualify.call_count, 2)
        self.assertTrue(self.host.qualify.call_args.kwargs['full'])
        runtime = self.host.document('host-runtime.json')
        self.assertEqual(runtime['environment_tiers']['full']['image'], 'sha256:' + '4' * 64)

    def test_a_source_update_points_to_a_forward_update_for_recovery(self):
        self.host.reconcile()
        self.host.revision = 'b' * 40
        _, output = self.host.reconcile()
        self.assertIn('Updated to bbbbbbbbbbbb. SQLite history is retained; use a compatible forward update for recovery.', output)
        self.assertNotIn('git checkout', output)

    def test_rolling_back_the_source_reuses_the_earlier_image_and_ui(self):
        self.host.reconcile()
        first = self.host.document('host.json')
        with patch.object(host_update, 'RUNNER_INPUTS', host_update.RUNNER_INPUTS + ('README.md',)):
            self.host.reconcile()
        self.assertTrue(self.host.reconcile()[0])
        record = self.host.document('host.json')
        self.assertEqual(record['image'], first['image'])
        self.assertEqual(self.host.builds, 2)

    def test_automated_confirmation_does_not_emit_opaque_unit_credentials(self):
        from forge.store.migration.upgrade import output_tail
        secret = 'opaque-private-value-without-token-prefix'
        override = self.host.base / 'credential-override.conf'
        override.write_text('[Service]\nExecStart=/worker --credential ' + secret + '\n')
        changes = [(self.host.base / 'foreign.service',
                    '[Service]\nEnvironment=CLIENT_SECRET=' + secret + '\n', '[Service]\n')]
        for yes in (False, True):
            output = io.StringIO()
            with self.subTest(assume_yes=yes), redirect_stdout(output), patch.object(host_update.sys.stdin, 'isatty', return_value=False):
                if yes:
                    host_update.confirm(changes, True, [('forge-ui', override, True)])
                else:
                    with self.assertRaisesRegex(ValueError, 'rerun with --yes') as error:
                        host_update.confirm(changes, False, [('forge-ui', override, True)])
                    output.write(str(error.exception))
            self.assertNotIn(secret, output.getvalue())
            self.assertNotIn(secret, output_tail(output.getvalue()))

    def test_files_edited_outside_forge_are_only_replaced_after_confirmation(self):
        self.host.reconcile()
        path = self.host.home / '.config/systemd/user/forge-ui.service'
        path.write_text(path.read_text().replace('512m', '1g'))
        self.host.revision = 'b' * 40
        with patch.object(host_update.sys.stdin, 'isatty', return_value=False):
            with self.assertRaisesRegex(ValueError, 'rerun with --yes'):
                self.host.reconcile()
        self.assertIn('1g', path.read_text())
        self.assertEqual(self.host.restarted(), [])
        self.host.reconcile(assume_yes=True)
        self.assertIn('512m', path.read_text())

    def test_drop_ins_that_override_the_service_are_moved_aside_after_confirmation(self):
        self.host.reconcile()
        units = self.host.home / '.config/systemd/user'
        override = units / 'forge-supervisor.service.d/99-onboarding-service.conf'
        override.parent.mkdir()
        override.write_text('[Service]\nExecStart=\nExecStart=/old/copy/bin/forge service --root /r\n')
        extra = units / 'forge-supervisor.service.d/capacity.conf'
        extra.write_text('[Service]\nEnvironment=MAX_SESSIONS=6\n')
        allowed = units / 'forge-ui.service.d/override.conf'
        allowed.parent.mkdir()
        allowed.write_text('[Service]\nEnvironment=NITRO_ALLOWED_HOSTS=forge.example.ts.net:8787\n')
        with patch.object(host_update.sys.stdin, 'isatty', return_value=False):
            with self.assertRaisesRegex(ValueError, 'rerun with --yes'):
                self.host.reconcile()
        self.assertTrue(override.exists())
        _, output = self.host.reconcile(assume_yes=True)
        self.assertFalse(override.exists())
        self.assertTrue(extra.exists())
        self.assertTrue(allowed.exists())
        self.assertIn('remove overriding drop-in 99-onboarding-service.conf', output)
        moved = list((self.host.root / 'backups').glob('unit-dropins-*/forge-supervisor.service.d/99-onboarding-service.conf'))
        self.assertEqual(len(moved), 1)
        self.assertEqual(self.host.restarted(), [['forge-supervisor.service']])

    def test_update_removes_only_the_named_stale_units_and_is_idempotent(self):
        self.host.reconcile()
        units = self.host.home / '.config/systemd/user'
        stale = [units / name for name in ('forge-notify.service', 'forge-notify.timer', 'forge-activity.service',
                                            'forge-observability.service', 'forge-babysitter.timer', 'forge-triage.service')]
        for path in stale:
            path.write_text('[Unit]\n')
        kept = [units / 'forge-notify-extra.service', units / 'other.timer', units / 'forge-notify.service.d']
        kept[0].write_text('[Unit]\n'); kept[1].write_text('[Unit]\n'); kept[2].mkdir()
        _, output = self.host.reconcile()
        self.assertIn('remove stale unit forge-notify.timer', output)
        self.assertFalse(any(path.exists() for path in stale))
        self.assertTrue(all(path.exists() for path in kept))
        disabled = [argv[-1] for argv in self.host.calls if argv[:4] == ['systemctl', '--user', 'disable', '--now']]
        self.assertEqual(sorted(disabled), sorted(path.name for path in stale))
        self.assertEqual(self.host.restarted(), [])
        _, output = self.host.reconcile()
        self.assertIn('Forge host is up to date', output)
        self.assertTrue(self.host.unit('forge-status'))

    def test_environment_overrides_count_as_overriding(self):
        for line, overriding in [('Environment=PYTHONPATH=/old', True), ('Environment="FORGE_ROOT=/x" "A=b"', True),
                                 ('Environment=HOST=10.0.0.1', True), ('Environment=NITRO_ALLOWED_HOSTS=a:1', False),
                                 ('Environment=MAX_SESSIONS=6', False), ('MemoryMax=2G', False)]:
            self.assertEqual(bool(host_update.OVERRIDING.search('[Service]\n' + line + '\n')), overriding, line)

    def test_the_operator_ui_only_binds_loopback_or_tailnet(self):
        with self.assertRaisesRegex(ValueError, 'loopback or a private tailnet'):
            self.host.reconcile(bind='0.0.0.0')


class BindTests(unittest.TestCase):
    def test_an_install_without_a_recorded_bind_keeps_the_old_status_address(self):
        with tempfile.TemporaryDirectory() as directory:
            root, units = Path(directory) / 'root', Path(directory) / 'units'
            root.mkdir(mode=0o700); units.mkdir()
            selected_store(root)
            (units / 'forge-status.service').write_text('[Service]\nEnvironment="FORGE_BIND_ADDRESS=100.64.0.7"\n')
            self.assertEqual(host_update.resolve_bind(root, None, units), '100.64.0.7')
            self.assertEqual(host_update.resolve_bind(root, '127.0.0.1', units), '127.0.0.1')
            (units / 'forge-status.service').unlink()
            self.assertEqual(host_update.resolve_bind(root, None, units), '127.0.0.1')


class PullTests(unittest.TestCase):
    def git(self, cwd, *args):
        return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.upstream, self.clone = base / 'upstream', base / 'clone'
        self.upstream.mkdir()
        self.git(self.upstream, 'init', '-q', '-b', 'main')
        self.git(self.upstream, 'config', 'user.email', 'test@example.com')
        self.git(self.upstream, 'config', 'user.name', 'Test')
        (self.upstream / 'file').write_text('one')
        self.git(self.upstream, 'add', 'file'); self.git(self.upstream, 'commit', '-qm', 'one')
        subprocess.run(['git', 'clone', '-q', str(self.upstream), str(self.clone)], check=True)
        self.git(self.clone, 'config', 'user.email', 'test@example.com')
        self.git(self.clone, 'config', 'user.name', 'Test')
        self.source = patch.object(host_update, 'SOURCE', self.clone); self.source.start()

    def tearDown(self):
        self.source.stop()
        self.directory.cleanup()

    def advance(self, where, content):
        (where / 'file').write_text(content)
        self.git(where, 'commit', '-qam', content)

    def test_fast_forwards_to_upstream(self):
        self.assertFalse(host_update.pull())
        self.advance(self.upstream, 'two')
        self.assertTrue(host_update.pull())
        self.assertEqual((self.clone / 'file').read_text(), 'two')

    def test_refuses_local_changes_and_diverged_history(self):
        (self.clone / 'file').write_text('edited')
        with self.assertRaisesRegex(ValueError, 'local changes'):
            host_update.pull()
        self.git(self.clone, 'checkout', '-q', 'file')
        self.advance(self.clone, 'local')
        self.advance(self.upstream, 'remote')
        with self.assertRaisesRegex(ValueError, 'diverged'):
            host_update.pull()
        self.assertEqual((self.clone / 'file').read_text(), 'local')


class SetupCommandTests(unittest.TestCase):
    def test_update_reruns_itself_from_the_pulled_source(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(host_setup, 'private_root', return_value=Path(directory)), \
                patch.object(host_update, 'pull', return_value=True), \
                patch.object(host_setup.os, 'execv', side_effect=SystemExit(0)) as execv, \
                patch('sys.argv', ['forge', 'update', '--bind', '100.64.0.7', '--yes']):
            with self.assertRaises(SystemExit):
                host_setup.main()
        argv = execv.call_args.args[1]
        self.assertEqual(argv[1:5], ['-m', 'forge.launch', 'forge.host_setup', 'update'])
        self.assertIn('--no-pull', argv)
        self.assertEqual(argv[argv.index('--bind') + 1], '100.64.0.7')
        self.assertIn('--yes', argv)

    def test_setup_finds_the_lingering_user_manager_after_sudo_i(self):
        seen = []
        with tempfile.TemporaryDirectory() as directory, patch.dict(host_setup.os.environ, {}, clear=True), \
                patch.object(host_setup, 'Path', side_effect=lambda *parts: Path(directory) if parts == ('/run/user',) else Path(*parts)), \
                patch.object(host_setup, 'command', side_effect=lambda argv, **kw: seen.append(host_setup.os.environ.get('XDG_RUNTIME_DIR'))):
            (Path(directory) / str(host_setup.os.getuid())).mkdir()
            host_setup.user_manager()
            self.assertEqual(seen, [str(Path(directory) / str(host_setup.os.getuid()))])

    def test_setup_explains_a_missing_user_manager(self):
        failure = subprocess.CalledProcessError(1, ['systemctl'])
        with patch.dict(host_setup.os.environ, {'XDG_RUNTIME_DIR': '/nonexistent'}), \
                patch.object(host_setup, 'command', side_effect=failure), \
                patch.object(host_setup.getpass, 'getuser', return_value='forge'):
            with self.assertRaisesRegex(ValueError, r'sudo loginctl enable-linger forge'):
                host_setup.user_manager()

    def test_update_requires_an_installed_host(self):
        with tempfile.TemporaryDirectory() as directory:
            selected_store(Path(directory))
            with self.assertRaisesRegex(ValueError, 'run bin/forge setup first'):
                host_setup.update(Path(directory))

    def test_update_verifies_connected_accounts_when_the_runner_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); selected_store(root)
            host_update.save_runtime_document(root, 'host-runtime.json', {}, expected_revision=0)
            with patch.object(host_setup, 'preflight'), \
                    patch.object(host_update, 'reconcile', return_value=True), \
                    patch.object(host_setup, 'Onboarding') as onboarding, \
                    patch.object(host_setup, 'check') as check, patch('sys.stdout', io.StringIO()):
                onboarding.return_value.view.return_value = {'provider_connected': True, 'image_verified': True}
                host_setup.update(root)
                check.assert_called_once_with(root)
                check.reset_mock()
                host_update.reconcile.return_value = False
                host_setup.update(root)
                check.assert_not_called()
                # A prior updater installed the image, then failed before its
                # account check. Unchanged artifacts do not prove qualification.
                onboarding.return_value.view.return_value['image_verified'] = False
                check.side_effect = ValueError('account check interrupted')
                with self.assertRaisesRegex(ValueError, 'account check interrupted'):
                    host_setup.update(root)
                check.assert_called_once_with(root)
                check.reset_mock(side_effect=True)
                host_setup.update(root)
                check.assert_called_once_with(root)
                check.reset_mock()
                onboarding.return_value.view.return_value['provider_connected'] = False
                host_setup.update(root)
                check.assert_not_called()


class ServiceReadinessTests(unittest.TestCase):
    def test_slow_probe_is_retried_within_the_overall_deadline(self):
        # A large store makes one probe outlast a short per-call timeout.
        calls = [subprocess.TimeoutExpired('probe', 5), subprocess.CompletedProcess('probe', 0)]
        with patch.object(host_update.subprocess, 'run', side_effect=calls) as run, \
                patch.object(host_update.time, 'sleep'):
            host_update.wait_mirror('/root')
        self.assertEqual(run.call_count, 2)
        self.assertGreater(run.call_args.kwargs['timeout'], 5)


if __name__ == '__main__':
    unittest.main()
