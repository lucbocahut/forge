"""Public supporting services run against selected normalized state."""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from tests.test_sqlite_identity import selected_store
from tests import test_settings as settings_fixture
from tests import test_deployments as deployment_fixture
from forge.settings import Settings, digest
from forge.app_access import ROLE_PERMISSIONS
from forge.onboarding import Onboarding
from forge.repository_setup import RepositorySetup, observation_key
from forge.repository_environments import RepositoryEnvironments
from forge.factory.config import load, save, validate


FULL = {'image': 'sha256:' + 'b' * 64, 'memory': '6g', 'cpus': 3}


class RuntimeFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root, self.now = Path(temp.name), 1000
        self.database = selected_store(self.root)
        self.client = Mock()
        self.settings = Settings(self.root, client=self.client, clock=lambda: self.now)
        self.config = settings_fixture.configured()
        self.settings.save_config(self.config)
        def discover(connection):
            role = list(ROLE_PERMISSIONS)[connection['app_id'] - 1]
            return {'repositories': [{'id': 10, 'name': 'example/project', 'archived': False}],
                    'permissions': ROLE_PERMISSIONS[role], 'account': 'example'}
        self.client.discover.side_effect = discover
        self.settings.refresh()
        self.repo = {'repo_id': 10, 'repo': 'example/project'}

    def prepare(self):
        setup = RepositorySetup(self.settings)
        setup.save_observation(10, {'repositories': {'10': {
            'name': 'example/project', 'status': 'ready', 'started_at': self.now,
            'revision': observation_key(self.settings.load(), {'id': 10, 'name': 'example/project'}),
            'report': {'observed_at': self.now, 'available': True, 'branch': 'main',
                       'requirements': [{'id': 'reviews', 'passed': True}, {'id': 'stale', 'passed': True}]}}}})
        config = self.settings.load()
        config['repositories'] = [{'id': 10, 'name': 'example/project', 'enabled': True}]
        self.settings.save(config, digest(self.settings.load()))

    def generation(self, value='generation-one'):
        setup = Onboarding(self.root, clock=lambda: self.now)
        setup.read()
        with setup.store.transaction() as data:
            data.update(generation=value, phase='active')
        return setup


class SettingsEnvironmentTests(RuntimeFixture):
    def test_config_access_and_readiness_survive_restart_and_stale_cas_fails(self):
        self.assertEqual(self.settings.load()['repositories'], [])
        self.prepare()
        restarted = Settings(self.root, clock=lambda: self.now)
        self.assertTrue(restarted.admission('example/project')['allowed'])
        self.assertTrue(restarted.view()['repositories'][0]['dispatch_allowed'])
        previous = digest(restarted.load())
        changed = restarted.load()
        changed['sync_interval_seconds'] = 400
        restarted.save(changed, previous)
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.settings.save(self.settings.load(), previous)
        self.assertFalse((self.root / 'factory.json').exists())
        self.assertFalse((self.root / 'state/github-access/access.json').exists())

    def test_access_response_from_previous_setup_cannot_publish(self):
        self.generation()
        old = deepcopy(self.settings.observation())
        discover = self.client.discover.side_effect
        lock, changed = threading.Lock(), []
        def resetting(connection):
            with lock:
                if not changed:
                    with self.database.write() as db:
                        db.execute("UPDATE setup_instances SET generation='generation-two'")
                    changed.append(True)
            return discover(connection)
        self.client.discover.side_effect = resetting
        with self.assertRaisesRegex(ValueError, 'setup changed'):
            self.settings.refresh()
        self.assertEqual(self.settings.observation(), old)

    def test_repository_protection_rejects_previous_setup_generation(self):
        self.generation()
        github = Mock()
        github.call.return_value = {'id': 10, 'default_branch': 'main'}
        github.target.return_value = 'main'
        github.pages.return_value = []
        def report(*args):
            with self.database.write() as db:
                db.execute("UPDATE setup_instances SET generation='generation-two'")
            return {'observed_at': self.now, 'available': True, 'branch': 'main', 'requirements': []}
        github.protection_report.side_effect = report
        service = RepositorySetup(self.settings, github_class=Mock(return_value=github))
        with self.assertRaisesRegex(ValueError, 'setup changed'):
            service.check(10, digest(self.settings.load()))
        self.assertNotEqual(service.read()['repositories']['10']['status'], 'ready')

    def test_environment_scan_confirm_and_late_completion_have_separate_authority(self):
        self.prepare()
        save(self.root, {'environment_tiers': {'full': FULL}})
        service = RepositoryEnvironments(self.settings, github_class=Mock())
        proposal = {**self.repo, 'head': 'a' * 40, 'services': ['mongodb'], 'evidence': ['package.json: mongoose'], 'revision': 'proposal-1'}
        revision = digest(self.settings.load())
        with patch('forge.repository_environments.probe', return_value=proposal) as probe, \
                patch('forge.repository_environments.uuid.uuid4', return_value=Mock(hex='f' * 32)):
            service.start_scan(self.repo, revision, service.view(10)['revision'], background=False)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(load(self.root)['repository_environments'], {})
        environment_revision = service.view(10)['revision']
        service.confirm(self.repo, revision, environment_revision)
        reloaded = RepositoryEnvironments(Settings(self.root, clock=lambda: self.now))
        self.assertEqual(reloaded.view(10)['profile']['services'], ['mongodb'])
        with self.assertRaisesRegex(ValueError, 'changed'):
            service.confirm(self.repo, revision, environment_revision)
        with patch('forge.repository_environments.threading.Thread'), \
                patch('forge.repository_environments.uuid.uuid4', return_value=Mock(hex='0' * 32)):
            service.start_scan(self.repo, revision, service.view(10)['revision'])
        self.assertEqual(service.view(10)['status'], 'running')
        self.assertEqual(service.snapshot()[1]['10'], proposal)
        with patch('forge.repository_environments.probe', return_value={**proposal, 'services': ['redis']}):
            service.scan(self.repo, revision, 'f' * 32)
        self.assertEqual(service.view(10)['status'], 'running')
        self.assertEqual(service.snapshot()[1]['10'], proposal)
        with self.database.write() as db:
            row = db.execute("SELECT id FROM environment_scans WHERE status='running'").fetchone()
            db.execute("UPDATE environment_scans SET status='failed',error='reset' WHERE id=?", (row[0],))
        with patch('forge.repository_environments.probe', return_value={**proposal, 'services': ['redis']}):
            service.scan(self.repo, revision, row[0])
        self.assertEqual(service.view(10)['profile']['services'], ['mongodb'])
        self.assertEqual(service.view(10)['error'], 'reset')
        self.assertEqual(service.snapshot()[1]['10'], proposal)
        with self.database.write() as db:
            db.execute("UPDATE environment_proposals SET status='superseded'")
        self.assertEqual(service.snapshot()[1], {})


class DeploymentAuxiliaryTests(unittest.TestCase):
    def setUp(self):
        deployment_fixture.DeploymentTests.setUp(self)  # Selects the store as self.database.
        self.config['enabled'] = True
    deployments = deployment_fixture.DeploymentTests.deployments
    entry = deployment_fixture.DeploymentTests.entry
    finish = deployment_fixture.DeploymentTests.finish

    def test_restart_between_capture_and_cleanup_keeps_lease_and_exact_capture(self):
        from forge.factory.runner import RunnerError
        service = self.deployments()
        service.tick(self.config)
        identifier = self.finish()
        def failed_reset(runner, job):
            raise RunnerError('cleanup unavailable')
        with patch.object(self.runner_class, 'reset', failed_reset):
            with self.assertRaises(RunnerError):
                service.tick(self.config)
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auxiliary_leases').fetchone()[0], 1)
            self.assertGreater(db.execute('SELECT COUNT(*) FROM auxiliary_artifacts').fetchone()[0], 0)
        # A restart reads durable capture rather than trusting a second worker read.
        with patch.object(self.runner_class, 'capture', side_effect=AssertionError('capture must not repeat')):
            self.deployments().tick(self.config)
        self.assertEqual(self.entry()['status'], 'proposed')
        self.assertFalse(self.engine)
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auxiliary_leases').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT cleanup FROM auxiliary_executions WHERE id=?', (identifier,)).fetchone()[0], 1)
        profile = self.deployments().confirm(7, None)
        self.assertEqual(profile['strategy'], 'header')
        self.assertEqual(self.entry()['status'], 'confirmed')
        self.assertFalse((self.root / 'state/deployments.json').exists())

    def test_capture_and_launch_identity_cannot_be_replaced(self):
        from forge.store.auxiliary import save_in, job_in
        from forge.store.models import TransitionError
        self.deployments().tick(self.config)
        identifier = self.entry()['job']['id']
        with self.database.read_snapshot() as db:
            job = job_in(db, identifier)
        with self.assertRaises(TransitionError):
            with self.database.write() as db:
                save_in(db, 'deployment', '7', {**job, 'head': 'other-head'}, self.now)
        job.update(fenced=True, capture={'logs': 'first', 'result': None})
        with self.database.write() as db:
            save_in(db, 'deployment', '7', job, self.now)
        with self.assertRaises(TransitionError):
            with self.database.write() as db:
                save_in(db, 'deployment', '7', {**job, 'capture': {'logs': 'replaced'}}, self.now)


class InventoryRuntimeTests(RuntimeFixture):
    def remote(self, **values):
        return dict(number=1, title='Ready work', body='Implement this', state='open',
                    updated_at='2026-10-08T10:00:00Z', labels=['ready'], assignees=[], **values)

    def test_ready_admission_fetches_complete_requirements_and_inventory_cannot_change_tracked_unit(self):
        from forge.factory.store_inventory import StoreInventory, DiscoveryController
        from forge.store.queries import Queries
        self.prepare()
        config = validate({'enabled': True, 'image': 'sha256:' + 'a' * 64})
        github = Mock()
        github.call.return_value = [self.remote()]
        github.issue.return_value = self.remote(), {'acceptance': ['complete requirement from comments']}
        github.ready_approves.return_value = True
        github.pulls.return_value = []
        github.target.return_value = 'main'
        github.protections.return_value = True
        github.deadline = float('inf')
        discovery = DiscoveryController(self.database, self.settings, github, config=config, clock=lambda: self.now)
        result = discovery.cycle()
        self.assertEqual(len(result['items']), 1)
        self.assertNotIn('error', result['items'][0])
        github.issue.assert_called_once()
        before = Queries(self.database).issue('10:1')
        inventory = StoreInventory(self.database, self.settings, github, config=config, clock=lambda: self.now)
        inventory.record({'id': 10, 'name': 'example/project', 'enabled': True},
                         {**self.remote(), 'title': 'Listing must not change assignment', 'updated_at': '2026-10-08T11:00:00Z'})
        after = Queries(self.database).issue('10:1')
        self.assertEqual(after['version'], before['version'])
        self.assertEqual(after['current_revision_id'], before['current_revision_id'])

    def test_runtime_components_and_real_cycle_share_selected_store(self):
        from forge.factory.sqlite_runtime import components, cycle
        self.prepare()
        config = validate({})
        save(self.root, config)
        github, runner = Mock(), Mock()
        github.deadline = float('inf')
        github.call.return_value = []
        parts = components(self.root, config=config, runner=runner, github=github, settings=self.settings,
                           clock=lambda: self.now, dispatch=False)
        self.assertEqual(parts['store'].instance_id, self.database.instance_id)
        self.assertIs(parts['results'].publisher, parts['publisher'])
        self.assertEqual(parts['publisher'].sources.store.instance_id, self.database.instance_id)
        for name in ('execution', 'observations', 'results', 'recovery', 'verification', 'merges', 'effects', 'dispatch'):
            self.assertEqual(parts[name].store.instance_id, self.database.instance_id)
        report = cycle(self.root, config=config, runner=runner, github=github, settings=self.settings,
                       clock=lambda: self.now, dispatch=False)
        self.assertTrue({'execution', 'operators', 'observations', 'discovery', 'requirements', 'verification', 'results', 'effects', 'deployments', 'dispatch'} <= set(report['stages']))
        for name, value in report['stages'].items():
            if isinstance(value, dict):
                self.assertNotIn('error', value, (name, value))
        runner.create.assert_not_called()
        with self.database.read_snapshot() as db:
            self.assertIsNotNone(db.execute("SELECT value FROM store_metadata WHERE key='supervisor:heartbeat'").fetchone())

    def test_discovery_skips_unready_repository_before_github_reads(self):
        from forge.factory.store_inventory import DiscoveryController
        self.prepare()
        RepositorySetup(self.settings).save_observation(10, {'repositories': {'10': {
            **RepositorySetup(self.settings).read()['repositories']['10'], 'status': 'needs_setup'}}})
        config = validate({'enabled': True, 'image': 'sha256:' + 'a' * 64})
        github = Mock(); github.deadline = float('inf')
        discovery = DiscoveryController(self.database, self.settings, github, config=config, clock=lambda: self.now)
        discovery.inventory.refresh = Mock()
        discovery.inventory.record({'id': 10, 'name': 'example/project', 'enabled': True}, self.remote())
        self.assertEqual(discovery.cycle()['items'], [])
        github.issue.assert_not_called(); github.pulls.assert_not_called(); github.protections.assert_not_called()

    def test_discovery_does_not_admit_hold_or_external_human_ownership(self):
        from forge.factory.store_inventory import DiscoveryController
        self.prepare()
        config = validate({'enabled': True, 'image': 'sha256:' + 'a' * 64})
        github = Mock()
        github.call.return_value = [self.remote()]
        github.pulls.return_value = []
        github.label_events.return_value = []
        github.deadline = float('inf')
        discovery = DiscoveryController(self.database, self.settings, github, config=config, clock=lambda: self.now)
        for remote in ({**self.remote(), 'labels': ['ready', config['hold_labels'][0]]},
                       {**self.remote(), 'assignees': [{'login': 'human', 'type': 'User'}]}):
            github.issue.return_value = remote, {'acceptance': ['full requirement']}
            self.assertEqual(discovery.cycle()['items'], [])
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM units').fetchone()[0], 0)
        github.protections.assert_not_called()

if __name__ == '__main__':
    unittest.main()
