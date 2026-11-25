"""Setup state and failure boundaries on a selected store, with explicit external-system fixtures."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from forge.app_access import ROLE_PERMISSIONS
from forge.configuration_authority import ConfigurationAuthority
from forge.credentials import Credentials
from forge.factory.config import load as runtime_load
from forge.factory.runner import RunnerError
from forge.onboarding import Onboarding, ROLES
from forge.settings import Settings, digest, read_json
from forge.setup_service import Service
from forge.store.configuration import Configuration
from forge.store.files import StateError
from tests.test_sqlite_identity import selected_store


# Manifest conversion checks for this marker in GitHub's key response.
FAKE_APP_KEY = 'synthetic fixture containing PRIVATE KEY-----'


def control(db, name, enabled, now, details=None):
    db.execute('INSERT INTO runtime_controls(name,enabled,updated_at,details_json) VALUES(?,?,?,?) '
               'ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,generation=generation+1,'
               'updated_at=excluded.updated_at,details_json=excluded.details_json',
               (name, int(enabled), now, json.dumps(details or {})))


class SetupTests(unittest.TestCase):
    def test_relative_installation_root_supplies_absolute_app_key_path(self):
        from forge.app_access import AppClient
        store=Credentials(os.path.relpath(self.root, Path.cwd()))
        store.put('FORGE_SETUP_CONTROLLER_KEY', 'synthetic-private-key', file=True)
        env=store.environment()
        self.assertTrue(Path(env['FORGE_SETUP_CONTROLLER_KEY']).is_absolute())
        self.assertEqual(Path(env['FORGE_SETUP_CONTROLLER_KEY']).read_text(),'synthetic-private-key')
        signer=Mock(return_value=Mock(returncode=0, stdout=b'synthetic-signature'))
        token=AppClient(environ=env,run=signer).jwt({'app_id':1,'private_key_env':'FORGE_SETUP_CONTROLLER_KEY'})
        self.assertEqual(len(token.split('.')),3)
        signer.assert_called_once()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = selected_store(self.root)
        self.api = Mock()
        self.api.jwt.return_value = 'synthetic-app-jwt'
        self.setup = Onboarding(self.root, client=self.api)
        self.setup.read()
        self.browser = 'browser-session-for-tests'
        self.installations = [{'id': 101, 'app_id': 1, 'account': {'login': 'example'}}]
        def request(method, path, token, payload=None):
            if path.startswith('/app-manifests/'):
                self.assertIsNone(token)
                return {'id': 1, 'slug': 'forge-build-test', 'pem': FAKE_APP_KEY}
            if path == '/app':
                return {'id': 1, 'slug': 'forge-build-test', 'owner': {'login': 'example'}, 'permissions': ROLE_PERMISSIONS['build']}
            if path.startswith('/app/installations?'):
                return self.installations
            raise AssertionError(path)
        self.api.request.side_effect = request
        def discover(connection):
            role = ROLES[connection['app_id'] - 1]
            return {'repositories': [{'id': 10, 'name': 'example/project'}], 'permissions': ROLE_PERMISSIONS[role]}
        self.api.discover.side_effect = discover

    def begin(self):
        value = self.setup.begin('build', 'example', 'organization', 'http://127.0.0.1:8787', self.browser)
        return parse_qs(urlparse(value['action']).query)['state'][0]

    def connected(self):
        settings = self.setup.settings()
        config = settings.load()
        config['connections'] = {r: {'app_id': i, 'installation_id': 100+i, 'private_key_env': 'FORGE_SETUP_'+r.upper()+'_KEY'} for i,r in enumerate(ROLES, 1)}
        settings.save(config, digest(settings.load()))
        settings.refresh()
        with self.setup.store.transaction() as data:
            data['apps'] = {r: {'connection': config['connections'][r], 'install_url': 'https://github.com/apps/fixture/installations/new'} for r in ROLES}
            data['owner'] = 'example'
            data['account_type'] = 'organization'
        self.setup.select(10)
        self.setup.connect('codex', api_key='test-provider-value-no-real-credential')

    def service(self):
        engine = {}
        resets = []
        class Runner:
            def __init__(self, config, **kw): self.config = config
            def ready(self): return True
            def capacity(self, job=None): return {'allowed':True, 'reason':'capacity_available'}
            def observe(self, job, current=None): return {'logs':'running output', 'at':0}
            def inspect(self, job): return engine.get(job['id'])
            def create(self, job):
                engine[job['id']] = {'id':job['id'], 'status':'created', 'running':False, 'exit_code':0}
                return job['id']
            def prepare(self, job): pass
            def start(self, job): engine[job['id']].update(status='exited' if job.get('kind')=='provider' else 'running', running=job.get('kind')!='provider')
            def capture(self, job): return {'result':{'ready':True,'attempt':job['id']}, 'logs':'', 'missing':[]}
            def reset(self, job):
                assert job['fenced']
                resets.append(job['id']); engine.pop(job['id'], None)
        github = Mock()
        github.return_value.call.return_value = {'id':10,'default_branch':'main'}
        github.return_value.protection_report.return_value = {'branch':'main','available':True,
            'requirements':[{'id':'review','label':'Required review','passed':True}], 'bypass':{'status':'ready'}}
        service = Service(self.root, runner=Runner, github=github)
        service.setup = self.setup
        return service, engine, resets

    def test_draft_does_not_admit_live_dispatch(self):
        self.connected()
        self.assertTrue(self.setup.settings().load()['repositories'][0]['enabled'])
        self.assertFalse(Settings(self.root).configured())
        self.assertFalse(ConfigurationAuthority(self.root).active())
        self.assertEqual(Settings(self.root).admission('example/project'),
                         {'allowed': False, 'reason': 'setup_not_started'})
        self.assertFalse(self.setup.view()['can_start'])

    def test_selected_configuration_and_setup_are_normalized(self):
        self.connected()
        self.assertEqual(self.setup.database.path, self.database.path)
        self.assertEqual(Onboarding(self.root).read()['selected']['id'], 10)
        self.assertTrue(Configuration(self.database).get('setup/runtime.json'))
        self.assertTrue(Configuration(self.database).get('setup/factory.json'))
        with self.database.read_snapshot() as db:
            row = db.execute("SELECT settings_json FROM setup_instances WHERE scope='live'").fetchone()
        self.assertNotIn('test-provider-value', row[0])
        self.assertFalse((self.root / 'setup/runtime.json').exists())
        self.assertFalse((self.root / 'setup/factory.json').exists())

    def test_selecting_smoke_repository_preserves_other_policies_but_disables_their_work(self):
        self.connected()
        discover = self.api.discover.side_effect
        self.api.discover.side_effect = lambda connection: {
            **discover(connection), 'repositories': [
                {'id': 10, 'name': 'example/project'}, {'id': 11, 'name': 'example/smoke'}]}
        settings = self.setup.settings()
        settings.refresh()
        config = settings.load()
        config['repositories'][0]['github_protection'] = 'optional'
        config['repositories'].append({'id': 11, 'name': 'example/smoke', 'enabled': False,
                                       'github_protection': 'optional'})
        settings.save(config, digest(settings.load()))
        for selected_id in (11, 10, 11):
            self.setup.select(selected_id)
            rows = {row['id']: row for row in settings.load()['repositories']}
            self.assertEqual(set(rows), {10, 11})
            self.assertEqual([row['id'] for row in rows.values() if row['enabled']], [selected_id])
            self.assertTrue(all(row['github_protection'] == 'optional' for row in rows.values()))

    def test_failed_repository_verification_stops_checking_without_starting_workers(self):
        self.connected()
        service, engine, resets = self.service()
        service.github_class.return_value.protection_report.return_value = {'branch':'main','available':False,'requirements':[{'id':'readable','label':'Observe GitHub rules','passed':False,'detail':'Repository rules could not be read.'}]}
        self.setup.queue('check', self.setup.view()['revision'])
        for _ in range(4): service.tick()
        view = self.setup.view()
        self.assertIsNone(view['operation'])
        self.assertIn('Repository protection needs attention', view['error'])
        self.assertEqual(view['repository_check']['requirements'][0]['id'], 'readable')
        self.assertFalse(view['can_start'])
        self.assertFalse(engine)
        self.assertFalse(resets)

    def test_failed_repository_verification_marks_the_github_check_failed(self):
        self.connected()
        service, _, _ = self.service()
        service.github_class.return_value.protection_report.return_value = {'branch':'main','available':False,'requirements':[{'id':'readable','label':'Observe GitHub rules','passed':False,'detail':'Repository rules could not be read.'}]}
        self.setup.queue('check', self.setup.view()['revision'])
        for _ in range(4): service.tick()
        self.assertEqual(self.setup.view()['checks']['github'], 'failed')

    def test_manifest_role_permissions_and_no_admin_token(self):
        origin = 'http://127.0.0.1:8787'
        for role in ROLES:
            with self.subTest(role=role):
                value = self.setup.begin(role,'example','organization',origin,self.browser)
                manifest = value['manifest']
                self.assertEqual(manifest['default_permissions'],ROLE_PERMISSIONS[role])
                self.assertEqual(manifest['url'], origin)
                # Both URL fields are required by GitHub's manifest schema, even
                # though this polling integration never enables webhook delivery.
                self.assertEqual(manifest['hook_attributes']['url'], origin + '/github/events')
                self.assertFalse(manifest['hook_attributes']['active'])
                self.assertEqual(manifest['default_events'], [])
                self.assertNotIn('pem', json.dumps(value))
        self.api.request.assert_not_called()

    def test_one_forge_app_manifest_covers_every_role(self):
        from forge.app_access import APP_PERMISSIONS
        from forge.onboarding import APP
        origin = 'https://forge.example.ts.net'
        value = self.setup.begin(APP, 'example', 'organization', origin, self.browser)
        manifest = value['manifest']
        self.assertEqual(manifest['default_permissions'], APP_PERMISSIONS)
        self.assertEqual(manifest['callback_urls'], [origin + '/auth/github/callback'])
        self.assertNotIn(' app ', manifest['name'])
        state = parse_qs(urlparse(value['action']).query)['state'][0]
        request = self.api.request.side_effect
        def single(method, path, token, payload=None):
            if path.startswith('/app-manifests/'):
                return {'id': 1, 'slug': 'forge-test', 'client_id': 'Iv23liForgeClient',
                        'client_secret': 'c' * 40,
                        'pem': FAKE_APP_KEY}
            if path == '/app':
                return {'id': 1, 'slug': 'forge-test', 'owner': {'login': 'example'}, 'permissions': APP_PERMISSIONS}
            return request(method, path, token, payload)
        self.api.request.side_effect = single
        self.setup.callback(state, 'code', self.browser)
        registered = self.setup.read()['apps'][APP]
        self.assertEqual(registered['client_id'], 'Iv23liForgeClient')
        self.assertNotIn('c' * 40, json.dumps(self.setup.read()))
        self.assertEqual(Credentials(self.root).environment()['FORGE_SETUP_APP_SIGN_IN_SECRET'], 'c' * 40)
        # Per-role registration cannot be mixed in afterwards.
        self.assertEqual(self.setup.begin('review', 'example', 'organization', origin, self.browser),
                         {'install_url': registered['install_url']})
        self.setup.refresh()
        connections = self.setup.settings().load()['connections']
        self.assertEqual(set(connections), set(ROLES))
        self.assertEqual({c['installation_id'] for c in connections.values()}, {101})
        self.assertEqual({c['app_id'] for c in connections.values()}, {1})
        self.assertTrue(all(row['registered'] and row['app_id'] == 1 for row in self.setup.view()['roles'].values()))

    def test_single_app_is_refused_after_per_role_registration(self):
        from forge.onboarding import APP
        self.setup.callback(self.begin(), 'code', self.browser)
        with self.assertRaisesRegex(ValueError, 'reset setup'):
            self.setup.begin(APP, 'example', 'organization', 'https://forge.example', self.browser)

    def test_callback_requires_cookie_nonce_and_is_single_use(self):
        nonce = self.begin()
        for state,browser in [('wrong',self.browser),(nonce,'another-browser')]:
            with self.assertRaises(ValueError): self.setup.callback(state,'code',browser)
        self.api.request.assert_not_called()
        self.setup.callback(nonce,'code',self.browser)
        with self.assertRaises(ValueError): self.setup.callback(nonce,'code',self.browser)
        self.assertEqual(sum(c.args[0]=='POST' for c in self.api.request.call_args_list),1)
        view = json.dumps(self.setup.view())
        self.assertNotIn('PRIVATE KEY', view)
        self.assertNotIn('private_key_env', view)

    def test_callback_network_runs_without_sql_transaction(self):
        state = self.begin()
        request = self.api.request.side_effect
        def network(*args, **kwargs):
            with self.database.write() as db:
                db.execute("INSERT OR REPLACE INTO store_metadata VALUES('parallel','ok')")
            return request(*args, **kwargs)
        self.api.request.side_effect = network
        self.setup.callback(state, 'code', self.browser)

    def test_expired_or_reset_callback_is_rejected(self):
        nonce = self.begin()
        with self.setup.store.transaction() as data: data['challenge']['expires'] = 0
        with self.assertRaises(ValueError): self.setup.callback(nonce,'code',self.browser)
        nonce = self.begin()
        self.setup.queue('reset',generation=self.setup.read()['generation'])
        with self.assertRaises(ValueError): self.setup.callback(nonce,'code',self.browser)
        self.api.request.assert_not_called()

    def test_lost_manifest_exchange_is_not_replayed(self):
        nonce = self.begin()
        self.api.request.side_effect = ValueError('Synthetic transport failure')
        with self.assertRaises(ValueError): self.setup.callback(nonce,'code',self.browser)
        with self.assertRaises(ValueError): self.setup.callback(nonce,'code',self.browser)
        self.assertEqual(self.api.request.call_count,1)

    def test_installation_is_verified_independently(self):
        self.setup.callback(self.begin(),'code',self.browser)
        self.installations = [{'id':999,'app_id':1,'account':{'login':'other'}}]
        self.setup.refresh()
        self.assertFalse(self.setup.settings().load()['connections'])
        self.installations = [{'id':101,'app_id':1,'account':{'login':'example'}}]
        self.setup.refresh()
        self.assertEqual(self.setup.settings().load()['connections']['build']['installation_id'],101)
        self.assertEqual(self.setup.view()['roles']['build']['status'],'ok')

    def test_bounded_setup_cas_rejects_lost_update(self):
        with self.assertRaises(StateError):
            with self.setup.store.transaction() as data:
                data['owner'] = 'stale'
                with self.database.write() as db:
                    db.execute("UPDATE setup_instances SET revision=revision+1 WHERE scope='live'")
        self.assertNotEqual(self.setup.read()['owner'], 'stale')

    def test_probe_launch_and_cleanup_are_separate_from_setup_json(self):
        service, engine, resets = self.service()
        job = service.new_probe(self.setup.runtime(), 'provider')
        with self.setup.store.transaction() as data:
            data['probe_jobs'] = [job]
        recovered = self.setup.read()['probe_jobs'][0]
        self.assertEqual(recovered['id'], job['id'])
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auxiliary_leases').fetchone()[0], 1)
            self.assertNotIn('runner_config', db.execute('SELECT settings_json FROM setup_instances').fetchone()[0])
        recovered.update(fenced=True, cleanup=True)
        with self.setup.store.transaction() as data:
            data['probe_jobs'] = [recovered]
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auxiliary_leases').fetchone()[0], 0)
        with self.setup.store.transaction() as data:
            data['probe_jobs'] = []
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auxiliary_executions').fetchone()[0], 1)

    def test_readiness_runs_cleanup_reuse_and_start_rechecks_github(self):
        self.connected()
        # Enabling uses a real digest in production; fixture supplies an image identity.
        config = self.setup.runtime(); config['image'] = 'sha256:'+'a'*64
        self.setup.save_runtime(config, active=False)
        service,engine,resets = self.service()
        revision = self.setup.view()['revision']
        identifier = self.setup.queue('check',revision)
        self.assertEqual(identifier,self.setup.queue('check',revision))
        for _ in range(16): service.tick()
        self.assertTrue(self.setup.view()['can_start'])
        self.assertEqual(len(set(resets)),3)
        self.assertFalse(engine)
        revision = self.setup.view()['revision']
        self.setup.queue('start',revision)
        service.tick()
        self.assertEqual(self.setup.read()['phase'],'active')
        self.assertTrue(Settings(self.root).configured())
        self.assertTrue(ConfigurationAuthority(self.root).active())
        self.assertTrue(runtime_load(self.root)['merge_enabled'])
        self.assertTrue(Settings(self.root).admission('example/project')['allowed'])
        self.assertEqual(service.github_class.return_value.protection_report.call_count,2)
        self.assertEqual(self.setup.queue('start',revision),self.setup.read()['activation_request'])

    def test_probe_waits_for_memory_then_resumes_without_invalidating_login(self):
        from forge.factory.runner import RunnerCapacity
        self.connected()
        config = self.setup.runtime(); config['image'] = 'sha256:'+'a'*64
        self.setup.save_runtime(config, active=False)
        service, engine, resets = self.service()
        self.setup.queue('check', self.setup.view()['revision'])
        service.tick(); service.tick()
        with patch.object(service.runner_class, 'create', side_effect=RunnerCapacity({'allowed': False, 'reason': 'memory_headroom'})):
            service.tick()
        data = self.setup.read()
        self.assertIsNotNone(data['operation'])
        self.assertIsNone(data.get('error'))
        self.assertFalse(data['probe_jobs'][-1]['fenced'])
        self.assertIn('Waiting for available memory', self.setup.activity(data)['label'])
        for _ in range(16): service.tick()
        self.assertTrue(self.setup.view()['can_start'])
        self.assertFalse(engine)

    def test_capacity_wait_replaces_unstarted_probe_with_consistent_immutable_deadline(self):
        from forge.factory.runner import RunnerCapacity
        from forge.factory.probe_runner import ProbeRunner
        from tests.test_worker_boundary import Engine
        self.connected()
        config = self.setup.runtime(); config['image'] = 'sha256:' + 'a' * 64
        self.setup.save_runtime(config, active=False)
        service, engine, resets = self.service()
        now = [time.time()]
        self.setup.clock = lambda: now[0]
        self.setup.queue('check', self.setup.view()['revision'])
        original_create = service.runner_class.create
        assignments, started = {}, []
        def create(runner, job):
            probe = ProbeRunner(job['runner_config'], environ={'FORGE_CREDENTIAL_ROOT': str(self.root)})
            probe.call = Engine()
            probe.prepare_source(job)
            assignments[job['id']] = json.loads((probe.inputs(job) / 'assignment.json').read_text())
            return original_create(runner, job)
        original_start = service.runner_class.start
        def start(runner, job):
            started.append(assignments[job['id']]['deadline'])
            return original_start(runner, job)
        with patch('forge.setup_service.time.time', side_effect=lambda: now[0]), \
                patch.object(service.runner_class, 'create', create), \
                patch.object(service.runner_class, 'start', start):
            service.tick(); service.tick()
            old = self.setup.read()['probe_jobs'][-1]
            # Container/input creation succeeded, but admission at start lost
            # the memory race. The assignment already has the original clock.
            with patch.object(service.runner_class, 'start', side_effect=RunnerCapacity({'allowed': False, 'reason': 'memory_headroom'})):
                service.tick()
            self.assertEqual(self.setup.read()['operation']['step'], 'probe_wait', self.setup.read().get('probe_error'))
            now[0] += 300
            with patch.object(service.runner_class, 'capacity', return_value={'allowed': False}):
                service.tick()
            self.assertEqual(self.setup.read()['probe_jobs'][-1]['id'], old['id'])
            self.assertEqual(resets, [])
            service.tick()
            fresh = self.setup.read()['probe_jobs'][-1]
            self.assertNotEqual(fresh['id'], old['id'])
            self.assertEqual(fresh['deadline'], now[0] + 180)
            self.assertEqual(fresh['provider_id'], old['provider_id'])
            self.assertEqual(assignments[old['id']]['deadline'], old['deadline'])
            with self.database.read_snapshot() as db:
                row = db.execute('SELECT cleanup FROM auxiliary_executions WHERE id=?', (old['id'],)).fetchone()
                self.assertEqual(row[0], 1)
                self.assertIsNone(db.execute('SELECT 1 FROM auxiliary_leases WHERE execution_id=?', (old['id'],)).fetchone())
            service.tick()
            self.assertEqual(started, [fresh['deadline']])
            for _ in range(16): service.tick()
            self.assertEqual(self.setup.read()['checks']['provider'], 'passed')

    def test_capacity_resume_recovers_after_cleanup_before_replacement_and_keeps_running_probe(self):
        from forge.factory.runner import RunnerCapacity
        self.connected()
        service, engine, resets = self.service()
        self.setup.queue('check', self.setup.view()['revision'])
        service.tick(); service.tick()
        with patch.object(service.runner_class, 'create', side_effect=RunnerCapacity({'allowed': False})):
            service.tick()
        old = self.setup.read()['probe_jobs'][-1]
        with patch.object(service, 'new_probe', side_effect=RuntimeError('interrupted after cleanup')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                service.check(self.setup.read())
        self.assertTrue(self.setup.read()['probe_jobs'][-1]['cleanup'])
        service, _, _ = self.service()
        service.check(self.setup.read())
        self.assertNotEqual(self.setup.read()['probe_jobs'][-1]['id'], old['id'])
        # An uncertain start that is already running must be observed in place.
        data = self.setup.read(); job = data['probe_jobs'][-1]
        service.wait_probe(data, data['operation'])
        with patch.object(service.runner_class, 'inspect', return_value={'status': 'running'}), \
                patch.object(service.runner_class, 'reset') as reset:
            service.check(self.setup.read())
            reset.assert_not_called()
        current = self.setup.read()
        self.assertEqual(current['probe_jobs'][-1]['id'], job['id'])
        self.assertEqual(current['probe_jobs'][-1]['deadline'], job['deadline'])
        self.assertNotIn('capacity_wait_since', current['probe_jobs'][-1])
        self.assertEqual(current['operation']['step'], 'probe')

    def test_capacity_retry_waits_for_verified_cleanup_before_allocating_replacement(self):
        from forge.factory.runner import RunnerCapacity
        self.connected()
        service, engine, resets = self.service()
        self.setup.queue('check', self.setup.view()['revision'])
        service.tick(); service.tick()
        with patch.object(service.runner_class, 'create', side_effect=RunnerCapacity({'allowed': False})):
            service.tick()
        old = self.setup.read()['probe_jobs'][-1]['id']
        with patch.object(service.runner_class, 'reset', side_effect=RunnerError('Cleanup not confirmed')):
            service.tick()
        data = self.setup.read()
        self.assertEqual(data['probe_jobs'][-1]['id'], old)
        self.assertFalse(data['probe_jobs'][-1]['cleanup'])
        self.assertIn('Cleanup not confirmed', data['error'])
        service.tick()
        data = self.setup.read()
        self.assertNotEqual(data['probe_jobs'][-1]['id'], old)
        self.assertIsNone(data['error'])
        self.assertIsNone(data['probe_error'])

    def test_start_rejects_stale_or_unverified_configuration(self):
        self.connected()
        revision = self.setup.view()['revision']
        with self.assertRaises(ValueError): self.setup.queue('start',revision)
        with self.setup.store.transaction() as data: data['readiness']={'revision':revision,'at':0}
        with self.assertRaises(ValueError): self.setup.queue('start',revision)
        with self.assertRaises(ValueError): self.setup.queue('check','old-revision')

    def test_reset_fences_before_cleanup_and_retires_credentials(self):
        self.connected()
        service,engine,resets = self.service()
        config = self.setup.runtime()
        job = service.new_probe(config,'termination')
        service.runner(config).create(job)
        with self.setup.store.transaction() as data: data['probe_jobs']=[job]
        outside = self.root/'external-login'; outside.write_text('shared')
        Settings(self.root).save_config(self.setup.settings().load())
        generation = self.setup.read()['generation']
        request = self.setup.queue('reset',generation=self.setup.read()['generation'])
        self.assertTrue(Settings(self.root).paused())
        self.assertFalse(Settings(self.root).admission('example/project')['allowed'])
        self.assertTrue(Settings(self.root).configured())
        self.assertTrue(engine)
        for _ in range(3): service.tick()
        data = self.setup.read()
        self.assertEqual(data['phase'],'setup')
        self.assertNotEqual(data['generation'],generation)
        self.assertEqual(data['reset_receipt']['id'], request)
        self.assertEqual(data['reset_receipt']['generation'], generation)
        self.assertFalse(engine)
        self.assertEqual(resets, [job['id']])
        self.assertFalse(Settings(self.root).configured())
        self.assertFalse(Settings(self.root).paused())
        self.assertEqual(read_json(self.setup.credentials.root/'index.json'), {})
        self.assertNotIn('test-provider-value', ''.join(
            p.read_text(errors='replace') for p in self.setup.credentials.root.rglob('*') if p.is_file()))
        self.assertTrue(outside.exists())
        self.assertFalse(Settings(self.root).admission('example/project')['allowed'])

    def test_reset_fence_is_sql_and_generation_fences_agents(self):
        previous = self.setup.read()['generation']
        self.setup.queue('reset', generation=previous)
        self.assertTrue(Settings(self.root).paused())
        with self.database.read_snapshot() as db:
            fence = db.execute("SELECT enabled,details_json FROM runtime_controls WHERE name='factory_paused'").fetchone()
        self.assertEqual((fence[0], json.loads(fence[1])), (1, {'generation': previous}))
        self.assertEqual(self.setup.read()['phase'], 'resetting')

    def test_readiness_probe_cycle_start_and_reset_keep_database_and_evidence(self):
        from forge.operator_auth import OperatorAuth
        auth = OperatorAuth(self.root)
        auth.configure('https://forge.example', 'identity-client', 's' * 40, [123])
        self.connected()
        config = self.setup.runtime()
        config['image'] = 'sha256:' + 'a' * 64
        self.setup.save_runtime(config, active=False)
        service, engine, resets = self.service()
        self.setup.queue('check', self.setup.view()['revision'])
        for _ in range(30):
            state = self.setup.read()
            if not state['operation']:
                break
            service.tick()
        self.assertIsNone(self.setup.read()['operation'])
        self.assertFalse(engine)
        self.assertTrue(self.setup.view()['can_start'])
        self.setup.queue('start', self.setup.view()['revision'])
        service.tick()
        self.assertEqual(self.setup.read()['phase'], 'active')
        self.assertTrue(Settings(self.root).load()['repositories'][0]['enabled'])
        from forge.client_connections import active_generation
        old_generation = active_generation(self.root)
        with self.database.write() as db:
            db.execute("INSERT INTO environment_scans(id,repository_id,source_revision,status,created_at) VALUES('pre-reset',10,'old','running',1)")
        from forge.mutation_audit import MutationAudit
        audit = MutationAudit(self.database, '/api/runtime')
        audit.result({'ok': True, 'private': 'never-store-this-response'})
        audit.finish()
        with self.database.read_snapshot() as db:
            saved_audit = dict(db.execute('SELECT * FROM operator_audit WHERE request_id=?', (audit.request_id,)).fetchone())
        self.setup.queue('reset', generation=old_generation)
        service.tick()
        self.assertEqual(self.setup.read()['phase'], 'setup')
        self.assertNotEqual(self.setup.read()['generation'], old_generation)
        self.assertFalse(Settings(self.root).paused())
        self.assertFalse(ConfigurationAuthority(self.root).active())
        self.assertTrue(self.database.path.exists())
        self.assertTrue((self.root / 'state/storage').exists())
        self.assertEqual(OperatorAuth(self.root).config()['client_secret'], 's' * 40)
        self.assertEqual(read_json(self.setup.credentials.root / 'index.json'), {})
        with self.database.read_snapshot() as db:
            retained_audit = dict(db.execute('SELECT * FROM operator_audit WHERE request_id=?', (audit.request_id,)).fetchone())
            self.assertEqual(retained_audit, saved_audit)
            self.assertNotIn('never-store-this-response', str(retained_audit))
            self.assertNotIn('s' * 40, str(list(db.execute('SELECT * FROM operator_audit'))))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auxiliary_executions').fetchone()[0], 3)
            self.assertGreater(db.execute('SELECT COUNT(*) FROM auxiliary_artifacts').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auxiliary_leases').fetchone()[0], 0)
            self.assertGreater(db.execute('SELECT COUNT(*) FROM configuration WHERE active=0').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM repositories WHERE enabled=1').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status FROM environment_scans WHERE id='pre-reset'").fetchone()[0], 'failed')
        self.connected()
        with self.database.read_snapshot() as db:
            row = db.execute("SELECT revision FROM configuration WHERE domain='setup/factory.json' AND active=1").fetchone()
            self.assertGreater(row[0], 1)

    def test_reset_waits_for_uncertain_effect_preserves_history_and_pauses_units(self):
        from forge.store.transitions import Lifecycle
        Lifecycle(self.database).admit_issue(request_id='admit', actor_id='operator', unit_id='old-unit',
            issue_id='old-issue', repository_id=10, repository_name='example/project', issue_number=1,
            title='Old work', body='Do it', requirements={'acceptance': ['done']}, observed_at=1)
        with self.database.write() as db:
            db.execute("INSERT INTO external_effects(id,request_id,unit_id,kind,status,payload_json,created_at,updated_at) "
                       "VALUES('uncertain','uncertain','old-unit','test','uncertain','{}',1,1)")
        service, _, _ = self.service()
        self.setup.queue('reset', generation=self.setup.read()['generation'])
        with patch('forge.factory.sqlite_runtime.cleanup', return_value={'pending': 0}):
            service.reset(self.setup.read())  # first page pauses old units
            service.reset(self.setup.read())
        self.assertEqual(self.setup.read()['phase'], 'resetting')
        self.assertIn('GitHub effect', self.setup.read()['error'])
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_gates WHERE code='operator_pause' AND resolved_at IS NULL").fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM issues').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT status FROM external_effects WHERE id='uncertain'").fetchone()[0], 'uncertain')

    def test_unverified_teardown_keeps_reset_pending_and_config_intact(self):
        self.connected()
        service,engine,_ = self.service()
        job = service.new_probe(self.setup.runtime(),'termination')
        with self.setup.store.transaction() as data: data['probe_jobs']=[job]
        self.setup.queue('reset',generation=self.setup.read()['generation'])
        with patch.object(service,'runner') as runner:
            runner.return_value.reset.side_effect=RunnerError('Engine unavailable')
            service.tick(); service.tick()
        self.assertEqual(self.setup.read()['phase'],'resetting')
        self.assertTrue(Settings(self.root).paused())
        self.assertTrue(self.setup.settings().configured())
        self.assertTrue((self.root/'credentials').exists())
        with self.assertRaises(ValueError): self.setup.connect('codex', api_key='another-value-no-real-secret')
        service.tick(); service.tick()
        self.assertEqual(self.setup.read()['phase'],'setup')

    def test_old_reset_cannot_change_new_generation(self):
        service,_,_ = self.service()
        self.setup.queue('reset',generation=self.setup.read()['generation']); old_state=self.setup.read(); old=old_state['operation']
        for _ in range(3): service.tick()
        with self.assertRaises(StateError): service.update(old,phase='resetting')
        with self.assertRaises(StateError): service.reset(old_state)
        self.assertEqual(self.setup.queue('reset',generation=old['generation']),old['id'])
        self.assertEqual(self.setup.read()['phase'],'setup')
        with self.assertRaises(ValueError): self.setup.queue('reset',generation='unknown-old-generation')

    def test_restart_after_reset_receipt_clears_only_old_fence(self):
        service,_,_=self.service()
        old=self.setup.read()['generation']
        self.setup.queue('reset',generation=self.setup.read()['generation'])
        for _ in range(3): service.tick()
        with self.database.write() as db:
            control(db, 'factory_paused', True, 1, {'generation': old})
        service.tick()
        self.assertFalse(Settings(self.root).paused())
        with self.database.write() as db:
            control(db, 'factory_paused', True, 1, {'generation': self.setup.read()['generation']})
        service.tick()
        self.assertTrue(Settings(self.root).paused())

    def test_reset_same_generation_fence_is_not_cleared_by_old_receipt(self):
        with self.setup.store.transaction() as data:
            data['reset_receipt'] = {'id': 'old', 'generation': 'old-generation'}
        with self.database.write() as db:
            control(db, 'factory_paused', True, 1, {'generation': self.setup.read()['generation']})
        service, _, _ = self.service()
        service.tick()
        self.assertTrue(Settings(self.root).paused())

    def test_reset_read_does_not_create_setup_files(self):
        import shutil
        self.connected(); self.setup.queue('reset',generation=self.setup.read()['generation'])
        shutil.rmtree(self.root/'setup')  # Only sync locks for the setup-scope settings live here.
        self.assertEqual(self.setup.view()['phase'],'resetting')
        self.assertEqual(Onboarding(self.root).view()['phase'],'resetting')
        self.assertFalse((self.root/'setup').exists())

    def test_secret_files_private_and_external_login_preserved(self):
        store=Credentials(self.root)
        store.put('OPENAI_API_KEY','synthetic-provider-credential')
        self.assertEqual(store.environment()['OPENAI_API_KEY'],'synthetic-provider-credential')
        files=list(store.root.iterdir())
        self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in files))
        index=json.loads((store.root/'index.json').read_text())
        path=store.root/index['OPENAI_API_KEY']['name']
        path.chmod(0o644)
        with self.assertRaises(ValueError): store.environment()


class ServiceLoopTests(unittest.TestCase):
    """The loop that keeps the factory cycling, and what it says when it cannot."""

    def test_a_failing_cycle_names_its_reason_in_the_log(self):
        import io
        from forge.setup_service import main
        with tempfile.TemporaryDirectory() as directory:
            selected_store(directory)
            output = io.StringIO()
            def once(_seconds):
                raise KeyboardInterrupt
            service_class = type(Service(directory))
            with patch('sys.argv', ['forge service', '--root', directory]), \
                    patch.object(service_class, 'tick', side_effect=ValueError('Unknown runtime settings')), \
                    patch('forge.setup_service.time.sleep', side_effect=once), \
                    patch('sys.stdout', new=output):
                main()
        printed = output.getvalue()
        self.assertIn('Supervisor enquiry failed', printed)
        self.assertIn('ValueError: Unknown runtime settings', printed)


if __name__ == '__main__':
    unittest.main()
