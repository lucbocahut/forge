from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge_client.contracts import SetupError
from forge_client.install_targets import prepare_targets
from forge.installation_smoke import InstallationSmoke
from forge.install_scope import execution_allowed, pending, issue_allowed, check_repositories_in
from forge.onboarding import Onboarding
from forge.settings import DEFAULT
from forge.store.configuration import Configuration
from forge.factory.config import load
from tests.test_sqlite_identity import selected_store


class TargetsTests(unittest.TestCase):
    def setUp(self):
        self.state = {'inputs_digest': 'a'*64, 'intent': {'owner': {'id': 7, 'login': 'owner', 'type': 'User'},
            'smoke_target': 'scratch', 'repositories': [{'target': {'id': 1, 'name': 'owner/project'}}]}}
        self.repositories = {}; self.posts = []; self.save = Mock(); self.fail = False
        self.github = Mock(); self.github.identity.return_value = {'id': 7}
        def request(method, path, payload=None):
            if method == 'GET':
                if path not in self.repositories: raise SetupError('github_not_found', 'missing')
                return deepcopy(self.repositories[path])
            self.posts.append(payload)
            name = 'owner/' + payload['name']
            value = dict(id=100+len(self.posts), full_name=name, description=payload['description'],
                         private=True, owner={'id': 7})
            self.repositories['/repos/'+name] = value
            if self.fail: raise SetupError('github_timeout', 'uncertain')
            return value
        self.github._request.side_effect = request

    def test_batched_targets_are_private_bound_and_replayed(self):
        first = prepare_targets(self.state, self.save, self.github, apply=True)
        self.assertEqual(first['status'], 'targets_ready')
        self.assertEqual(len(first['repositories']), 3)
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(first, prepare_targets(self.state, self.save, self.github, apply=True))
        self.assertEqual(len(self.posts), 2)
        self.assertTrue(all(row['private'] and row['auto_init'] for row in self.posts))

    def test_timeout_recovers_observable_write_without_repeating(self):
        self.fail = True
        with self.assertRaises(SetupError): prepare_targets(self.state, self.save, self.github, apply=True)
        self.fail = False
        self.assertEqual(prepare_targets(self.state, self.save, self.github, apply=True)['status'], 'targets_ready')
        self.assertEqual(len(self.posts), 2)

    def test_unobserved_uncertain_write_is_never_duplicated(self):
        self.fail = True
        with self.assertRaises(SetupError): prepare_targets(self.state, self.save, self.github, apply=True)
        self.repositories.clear()
        self.assertEqual(prepare_targets(self.state, self.save, self.github, apply=True)['status'], 'write_unconfirmed')
        self.assertEqual(len(self.posts), 1)

    def test_marker_and_id_prevent_name_reuse(self):
        prepare_targets(self.state, self.save, self.github, apply=True)
        self.repositories['/repos/owner/forge-smoke-'+'a'*12]['id'] = 999
        with self.assertRaisesRegex(SetupError, 'replaced'): prepare_targets(self.state, self.save, self.github, apply=True)

    def test_selected_target_does_not_create_another_smoke_repository(self):
        self.state['intent']['smoke_target'] = 'owner/project'
        result = prepare_targets(self.state, self.save, self.github, apply=True)
        self.assertEqual(result['smoke'], {'id': 1, 'name': 'owner/project'})
        self.assertEqual(len(self.posts), 1)


class SmokeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.store = selected_store(self.root)
        self.now = 200
        self.setup = Onboarding(self.root, clock=lambda: self.now)
        self.generation = self.setup.read()['generation']
        self.target = {'id': 1, 'name': 'owner/repo'}
        self.github = Mock(); self.github.publisher.return_value = 'forge[bot]'
        self.issue = {'number': 10, 'state': 'open', 'state_reason': None, 'labels': [],
                      'body': '', 'user': {'login': 'forge[bot]'}}
        self.created = []; self.fail = False
        def file_issue(item, title, body, labels):
            self.issue['body'] = body; self.created.append(deepcopy(self.issue))
            if self.fail: raise RuntimeError('response lost')
            return 10
        def call(role, item, method, path, payload=None):
            if path == '': return {'id': 1}
            if path == '/issues/10': return deepcopy(self.issue)
            raise AssertionError((method, path))
        self.github.file_issue.side_effect = file_issue
        self.github.call.side_effect = call
        self.github.pages.side_effect = lambda *args: deepcopy(self.created)
        self.service = InstallationSmoke(self.root, setup=self.setup, github=self.github, clock=lambda: self.now)
        self.parameters = dict(intent_digest='a'*64, repository=self.target, number=None, started_at=100)
        self.binding = dict(intent_digest='a'*64, repository=self.target, number=10, started_at=100,
            repositories=[{'target': self.target, 'merge_policy': 'human', 'allow_workflow_edits': False}], smoke_policy='human')

    def call(self, action, **params):
        return self.service.call(action, params, setup_id='install', generation=self.generation)

    def bind(self):
        self.call('issue', **self.parameters)
        self.call('bind', binding=self.binding)

    def test_controller_issue_creation_recovers_by_marker(self):
        self.fail = True
        with self.assertRaises(RuntimeError): self.call('issue', **self.parameters)
        self.fail = False
        self.assertEqual(self.call('issue', **self.parameters)['number'], 10)
        self.assertEqual(len(self.created), 1)
        self.assertIn('.forge/smoke/', self.created[0]['body'])

    def test_uncertain_missing_issue_never_reposts(self):
        self.fail = True
        with self.assertRaises(RuntimeError): self.call('issue', **self.parameters)
        self.created.clear()
        result = self.call('issue', **self.parameters)
        self.assertEqual(result['status'], 'write_unconfirmed')
        self.assertEqual(self.github.file_issue.call_count, 1)

    def test_existing_issue_is_explicit_and_no_issue_is_created(self):
        self.assertEqual(self.call('issue', **{**self.parameters, 'number': 10})['number'], 10)
        self.github.file_issue.assert_not_called()
        with self.assertRaises(SetupError): self.call('issue', **self.parameters)

    def test_binding_preserves_policy_timer_and_generation(self):
        self.bind()
        self.assertEqual(load(self.root, scope='setup')['repository_merge_policies']['1'], 'human')
        self.assertFalse(load(self.root, scope='setup')['repository_workflow_edits']['1'])
        with self.assertRaises(SetupError): self.call('bind', binding={**self.binding, 'started_at': 101})
        with self.assertRaises(SetupError):
            self.service.call('status', {}, setup_id='different', generation=self.generation)
        with self.setup.store.transaction() as data: data['generation'] = 'reset'
        self.assertIsNone(pending(self.store))
        with self.assertRaises(SetupError): self.call('status')

    def test_scope_excludes_other_issues_human_prs_and_other_repos(self):
        self.bind()
        self.assertTrue(issue_allowed(self.store, 1, 10))
        self.assertFalse(issue_allowed(self.store, 1, 11))
        self.assertFalse(execution_allowed(self.store, {'repo': 'owner/repo', 'number': 10}))
        with patch('forge.install_scope.triage_in', return_value={'id': 'accepted'}):
            self.assertTrue(execution_allowed(self.store, {'repo': 'owner/repo', 'number': 10}))
        self.assertFalse(execution_allowed(self.store, {'repo': 'owner/repo', 'number': 11}))
        self.assertFalse(execution_allowed(self.store, {'repo': 'owner/repo', 'number': 10, 'review_only': True}))
        with self.store.read_snapshot() as db:
            check_repositories_in(db, [{**self.target, 'enabled': True}])
            with self.assertRaises(ValueError): check_repositories_in(db, [{'id': 2, 'name': 'owner/other', 'enabled': True}])

    def test_controller_contention_is_waitable_without_any_smoke_mutation(self):
        from forge.factory.control import controller_lock
        self.bind()
        self.github.reset_mock()
        before = self.service.saved()
        with controller_lock(self.root):
            result = self.call('launch')
        self.assertEqual((result['status'], result['phase']), ('smoke_pending', 'controller'))
        self.assertEqual(self.service.saved(), before)
        self.github.call.assert_not_called()

    def active_smoke(self):
        self.bind()
        with self.setup.store.transaction() as data: data['phase'] = 'active'
        self.issue.update(title='Smoke', updated_at='2026-10-08T10:00:00Z', labels=[{'name': 'ready'}])
        self.github.issue.side_effect = lambda item: (self.issue, 'full-requirements-digest')
        self.github.target.return_value = 'main'
        self.service.prove = Mock(return_value={'status': 'pending'})
        from forge.factory.store_inventory import StoreInventory
        StoreInventory(self.store, Mock(), self.github, config=load(self.root), clock=lambda: self.now).record(
            dict(self.target, enabled=True), self.issue, 'full-requirements-digest')

    def test_launch_reuses_exact_supervisor_triage_instead_of_queueing_twice(self):
        from forge.store.repository_planning import RepositoryPlanning
        self.active_smoke()
        existing = RepositoryPlanning(self.store, clock=lambda: self.now).queue(1,
            request_id='automatic-triage', actor_id='supervisor:nightly-triage', base_branch='main')
        self.assertEqual(self.call('launch')['status'], 'smoke_pending')
        with self.store.read_snapshot() as db:
            runs = list(db.execute('SELECT id FROM repository_triage_runs'))
        self.assertEqual([row[0] for row in runs], [existing['proposal_id']])

    def test_launch_waits_for_nonmatching_pending_capture_without_adopting_it(self):
        from forge.store.repository_planning import RepositoryPlanning
        self.active_smoke()
        existing = RepositoryPlanning(self.store, clock=lambda: 50).queue(1,
            request_id='old-triage', actor_id='supervisor:nightly-triage', base_branch='main')
        result = self.call('launch')
        self.assertEqual((result['status'], result['phase']), ('smoke_pending', 'triage'))
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM repository_triage_runs').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT status FROM triage_proposals WHERE id=?',
                                       (existing['proposal_id'],)).fetchone()[0], 'draft')

    def interrupt_triage(self, plans, proposal_id, execution_id, *, cleanup=True):
        from forge.store.transitions import manifest_hash
        context = plans.context(proposal_id, head='a'*40)
        job = dict(id=execution_id, proposal_id=proposal_id, slot='repository-triage', role='triage',
            repo_id=1, repo='owner/repo', head='a'*40, deadline=self.now+1800, fenced=False, cleanup=False,
            runner_config={}, context_hash=manifest_hash(context))
        plans.reserve(proposal_id, job, context)
        plans.interrupt_for_maintenance(execution_id)
        if cleanup:
            plans.interrupt_for_maintenance(execution_id, cleanup=True)

    def test_maintenance_interruption_queues_deterministic_successors_without_duplicate_runs(self):
        from forge.store.repository_planning import RepositoryPlanning
        self.active_smoke()
        plans = RepositoryPlanning(self.store, clock=lambda: self.now)
        for index in range(3):
            self.call('launch')
            runs = plans.runs()
            self.assertEqual(len(runs), 1)
            identifier = runs[0]['id']
            self.call('launch')
            self.assertEqual(plans.runs()[0]['id'], identifier)
            with self.store.read_snapshot() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM repository_triage_runs').fetchone()[0], index+1)
            self.interrupt_triage(plans, identifier, 'interrupted-'+str(index))
            self.now += 1
        self.call('launch')
        self.assertEqual(len(plans.runs()), 1)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM repository_triage_runs').fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT count(*) FROM repository_triage_runs WHERE error='maintenance_interrupted'").fetchone()[0], 3)

    def test_interruption_before_smoke_start_cannot_create_a_successor(self):
        from forge.store.repository_planning import RepositoryPlanning
        self.active_smoke()
        plans = RepositoryPlanning(self.store, clock=lambda: 50)
        old = plans.queue(1, request_id='old-maintenance', actor_id='operator', base_branch='main')
        self.interrupt_triage(plans, old['proposal_id'], 'old-interrupted')
        with patch('forge.store.repository_planning.RepositoryPlanning.queue') as queue:
            self.call('launch')
        self.assertNotIn(':after:', queue.call_args.kwargs['request_id'])

    def test_generic_failure_does_not_create_a_smoke_successor(self):
        from forge.store.repository_planning import RepositoryPlanning
        self.active_smoke()
        plans = RepositoryPlanning(self.store, clock=lambda: self.now)
        self.call('launch')
        plans.settle_run(plans.runs()[0]['id'], error='triage_result_invalid')
        self.call('launch')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM repository_triage_runs').fetchone()[0], 1)

    def test_terminal_smoke_failure_is_actionable_and_does_not_restart(self):
        from forge.store.repository_planning import RepositoryPlanning
        self.active_smoke()
        plans = RepositoryPlanning(self.store, clock=lambda: self.now)
        self.call('launch')
        proposal = plans.runs()[0]['id']
        plans.settle_run(proposal, error='steward_cycle_limit: request a new proposal')
        for action in ('status', 'launch', 'launch'):
            result = self.call(action)
            self.assertEqual(result['status'], 'smoke_triage_failed')
            self.assertEqual(result['proposal_id'], proposal)
            self.assertEqual(result['error'], 'steward_cycle_limit')
            self.assertIn('Request new repository triage', result['next_action']['instruction'])
        self.assertEqual(plans.runs(), [])
        plans.queue(1, request_id='operator-retry', actor_id='operator', base_branch='main')
        self.assertEqual(self.call('status')['status'], 'smoke_pending')
        self.assertEqual(self.call('launch')['status'], 'smoke_pending')

    def test_terminal_failure_does_not_expose_raw_error_text(self):
        from forge.store.repository_planning import RepositoryPlanning
        self.active_smoke()
        plans = RepositoryPlanning(self.store, clock=lambda: self.now)
        self.call('launch')
        plans.settle_run(plans.runs()[0]['id'], error='unexpected private diagnostic')
        result = self.call('status')
        self.assertEqual(result['error'], 'triage_failed')
        self.assertNotIn('private diagnostic', str(result))

    def test_explicit_retry_preserves_failure_and_replays_one_successor(self):
        from forge.store.repository_planning import RepositoryPlanning
        self.active_smoke()
        plans = RepositoryPlanning(self.store, clock=lambda: self.now)
        self.call('launch')
        failed = plans.runs()[0]['id']
        plans.settle_run(failed, error='steward_cycle_limit: request a new proposal')
        first = self.call('retry', proposal_id=failed)
        self.assertEqual(first['status'], 'smoke_retry_queued')
        self.assertNotEqual(first['proposal_id'], failed)
        self.assertEqual(self.call('retry', proposal_id=failed), first)
        plans.settle_run(first['proposal_id'], error='triage_result_invalid')
        self.assertEqual(self.call('retry', proposal_id=failed), first)
        self.assertEqual(self.call('launch')['status'], 'smoke_triage_failed')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM repository_triage_runs').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT error FROM repository_triage_runs WHERE id=?', (failed,)).fetchone()[0],
                             'steward_cycle_limit: request a new proposal')

    def test_retry_requires_exact_current_failed_run_and_setup(self):
        from forge.store.repository_planning import RepositoryPlanning
        self.active_smoke()
        plans = RepositoryPlanning(self.store, clock=lambda: self.now)
        self.call('launch')
        failed = plans.runs()[0]['id']
        for proposal in (failed, 'unknown'):
            with self.assertRaises(SetupError): self.call('retry', proposal_id=proposal)
        plans.settle_run(failed, error='triage_result_invalid')
        with self.assertRaises(SetupError):
            self.service.call('retry', {'proposal_id': failed}, setup_id='other', generation=self.generation)
        with self.assertRaises(SetupError):
            self.service.call('retry', {'proposal_id': failed}, setup_id='install', generation='old')
        plans.queue(1, request_id='other-active', actor_id='operator', base_branch='main')
        with self.assertRaises(SetupError): self.call('retry', proposal_id=failed)

    def test_retry_refuses_unclean_workers_and_maintenance_interruptions(self):
        from forge.store.repository_planning import RepositoryPlanning
        self.active_smoke()
        plans = RepositoryPlanning(self.store, clock=lambda: self.now)
        self.call('launch')
        failed = plans.runs()[0]['id']
        self.interrupt_triage(plans, failed, 'unclean-retry')
        with self.assertRaises(SetupError): self.call('retry', proposal_id=failed)
        self.call('launch')
        failed = plans.runs()[0]['id']
        self.interrupt_triage(plans, failed, 'still-unclean', cleanup=False)
        with self.store.write() as db:
            db.execute("UPDATE repository_triage_runs SET status='failed',error='triage_result_invalid' WHERE id=?", (failed,))
        with self.assertRaises(SetupError): self.call('retry', proposal_id=failed)

    def test_completed_smoke_triage_does_not_schedule_another_run(self):
        self.active_smoke()
        with patch('forge.installation_smoke.triage_run_in', return_value={'id': 'complete'}), \
             patch.object(self.service, 'accept_keep', return_value=None), \
             patch('forge.store.repository_planning.RepositoryPlanning.queue') as queue:
            self.assertEqual(self.call('launch')['status'], 'smoke_pending')
        queue.assert_not_called()

    def test_launch_never_readds_ready_after_operator_removal(self):
        self.bind()
        with self.setup.store.transaction() as data: data['phase'] = 'active'
        self.issue.update(title='Smoke', updated_at='2026-10-08T10:00:00Z')
        original = self.github.call.side_effect
        def call(role, item, method, path, payload=None):
            if method == 'POST' and path == '/issues/10/labels':
                self.issue['labels'] = [{'name': name} for name in payload['labels']]
                return self.issue['labels']
            return original(role, item, method, path, payload)
        self.github.call.side_effect = call
        self.github.issue.side_effect = lambda item: (self.issue, {'acceptance': ['Smoke']})
        self.github.target.return_value = 'main'
        self.service.prove = Mock(return_value={'status': 'pending'})
        self.assertEqual(self.call('launch')['status'], 'smoke_pending')
        self.issue['labels'] = []
        self.assertEqual(self.call('launch')['status'], 'smoke_pending')
        writes = [row for row in self.github.call.call_args_list if row.args[2] == 'POST']
        self.assertEqual(len(writes), 1)

    def merged(self):
        self.bind()
        with self.setup.store.transaction() as data: data['phase'] = 'active'
        candidate = {'pull_number': 20, 'head_sha': 'h'*40, 'merge_sha': 'm'*40}
        self.service.prove = Mock(return_value={'status': 'merged', 'candidate': candidate,
            'evidence': {'role_jobs': {'build': 'b', 'review': 'r', 'audit': 'a'}, 'merge_effect_id': 'effect'}, 'attempts': {'total': 3}})
        self.github.pr.return_value = {'number': 20, 'merged': True, 'state': 'closed', 'head': {'sha': 'h'*40, 'repo': {'id': 1}},
            'base': {'repo': {'id': 1}}, 'merge_commit_sha': 'm'*40, 'merged_at': '1970-01-01T00:02:30Z'}
        self.issue.update(state='closed', state_reason='completed')
        self.triage = patch('forge.installation_smoke.triage_in', return_value={'id': 'triage', 'execution_id': 't'})
        self.triage.start(); self.addCleanup(self.triage.stop)
        settings = Mock()
        settings.refresh.return_value = True
        settings.view.return_value = {'revision': 'current', 'repositories': [{**self.target, 'access_ready': True, 'protection_ready': True}]}
        settings.config_snapshot.return_value = (deepcopy(DEFAULT), 0)
        self.settings = patch('forge.installation_smoke.Settings', return_value=settings)
        self.settings.start(); self.addCleanup(self.settings.stop)
        checks = patch('forge.installation_smoke.RepositorySetup')
        self.checks = checks.start().return_value
        self.checks.check.return_value = True
        self.addCleanup(checks.stop)
        return candidate

    def test_handover_atomically_records_verified_merge_and_selected_policy(self):
        self.merged()
        result = self.call('handover')
        self.assertEqual(result['status'], 'handover_complete')
        self.assertEqual(result['elapsed_seconds'], 50)
        self.checks.check.assert_called_once_with(1, 'current')
        self.assertIsNone(pending(self.store))
        self.assertEqual(load(self.root)['repository_merge_policies'], {'1': 'human'})
        self.assertEqual(load(self.root)['repository_workflow_edits'], {'1': False})
        self.assertEqual(Configuration(self.store).get('factory.json')['settings']['repositories'], [{**self.target, 'enabled': True}])
        self.assertEqual(self.call('handover'), result)

    def test_handover_requires_exact_github_candidate_and_completed_issue(self):
        self.merged()
        self.github.pr.return_value['head']['sha'] = 'x'*40
        with self.assertRaisesRegex(SetupError, 'exact smoke merge'): self.call('handover')
        self.assertIsNotNone(pending(self.store))
        self.assertIsNone(Configuration(self.store).get('factory.json'))
        self.github.pr.return_value['head']['sha'] = 'h'*40
        self.issue['state_reason'] = 'not_planned'
        with self.assertRaises(SetupError): self.call('handover')

    def test_handover_requires_triage_before_activation(self):
        self.merged()
        with patch('forge.installation_smoke.triage_in', return_value=None):
            self.assertEqual(self.call('handover')['status'], 'smoke_pending')
        self.assertIsNotNone(pending(self.store))

    def test_handover_cas_conflict_rolls_back_scope_release(self):
        self.merged()
        Configuration(self.store).replace('factory.json', deepcopy(DEFAULT), expected_revision=0)
        with self.assertRaises(ValueError): self.call('handover')
        self.assertIsNotNone(pending(self.store))
        self.assertEqual(Configuration(self.store).get('factory.json')['settings']['repositories'], [])

    def test_grouped_unit_cannot_smuggle_an_unrelated_issue_into_smoke(self):
        self.bind()
        from forge.store.transitions import Lifecycle
        life = Lifecycle(self.store, clock=lambda: 120)
        for number in (10, 11):
            life.admit_issue(request_id='admit'+str(number), actor_id='operator', unit_id='unit'+str(number),
                issue_id='issue'+str(number), repository_id=1, repository_name='owner/repo', issue_number=number,
                title='Issue', body='Body', requirements={'acceptance': ['works']}, observed_at=110)
        with patch('forge.install_scope.triage_in', return_value={'id': 'accepted'}):
            self.assertTrue(execution_allowed(self.store, {'repo': 'owner/repo', 'unit_id': 'unit10'}))
        self.assertFalse(execution_allowed(self.store, {'repo': 'owner/repo', 'unit_id': 'unit11'}))
        from forge.store.repository_planning import RepositoryPlanning
        plans = RepositoryPlanning(self.store, clock=lambda: 130)
        plans.begin(1, proposal_id='smoke-triage', base_branch='main', request_id='smoke-triage', actor_id='operator')
        with self.store.read_snapshot() as db:
            members = [row[0] for row in db.execute('SELECT issue_id FROM triage_proposal_inputs WHERE proposal_id=?', ('smoke-triage',))]
        self.assertEqual(members, ['issue10'])


class SmokeAdmissionTests(unittest.TestCase):
    call = SmokeTests.call
    bind = SmokeTests.bind

    def setUp(self):
        SmokeTests.setUp(self)
        self.bind()

    def test_discovery_admits_only_smoke_before_triage_but_cannot_execute(self):
        from forge.factory.config import validate
        from forge.factory.store_inventory import DiscoveryController
        runtime = validate({'enabled': True, 'image': 'sha256:' + 'a'*64})
        settings = Mock(); settings.paused.return_value = False
        settings.admission.return_value = {'allowed': True}
        discovery = DiscoveryController(self.store, settings, self.github, config=runtime, clock=lambda: 150)
        discovery.inventory.refresh = Mock(); discovery.inventory.requested = Mock()
        issue = dict(self.issue, title='Smoke', updated_at='2026-10-08T10:00:00Z', labels=[runtime['ready_label']], assignees=[])
        requirements = {'acceptance': ['Document the smoke merge']}
        for number in (10, 11):
            discovery.inventory.record(dict(self.target, enabled=True), dict(issue, number=number), requirements)
        self.github.issue.return_value = (issue, requirements)
        self.github.ready_approves.return_value = True
        self.github.pulls.return_value = []
        self.github.target.return_value = 'main'; self.github.protections.return_value = True
        with patch('forge.factory.store_inventory.RepositorySetup') as setup:
            setup.return_value.read.return_value = {'repositories': {str(self.target['id']): {'status': 'ready'}}}
            result = discovery.cycle()
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['issue_id'], '1:10')
        with self.store.read_snapshot() as db:
            unit = db.execute("SELECT unit_id FROM unit_issues WHERE issue_id='1:10'").fetchone()[0]
            self.assertEqual(db.execute('SELECT COUNT(*) FROM unit_issues').fetchone()[0], 1)
        self.assertFalse(execution_allowed(self.store, {'repo': 'owner/repo', 'unit_id': unit}))

    def test_planner_waits_for_normal_admission_before_snapshot(self):
        from forge.factory.config import validate
        from forge.factory.repository_planner import RepositoryPlanner
        from forge.factory.store_inventory import StoreInventory
        from forge.store.repository_planning import RepositoryPlanning
        runtime = validate({}); settings = Mock()
        StoreInventory(self.store, settings, self.github, config=runtime, clock=lambda: 150).record(
            self.target, dict(self.issue, title='Smoke', updated_at='2026-10-08T10:00:00Z'), {'acceptance': ['Document the smoke merge']})
        queued = RepositoryPlanning(self.store, clock=lambda: 150).queue(
            1, request_id='smoke-triage', actor_id='operator:installation', base_branch='main')
        planner = RepositoryPlanner(self.store, self.github, config=runtime, settings=settings, observations=Mock())
        planner.refresh = Mock()
        self.assertEqual(planner._start({'id': queued['proposal_id'], 'repository_id': 1}), {'waiting': 'smoke_admission'})
        planner.refresh.assert_not_called()
        settings.admission.assert_not_called()


class TriageSmokeTests(unittest.TestCase):
    call = SmokeTests.call
    bind = SmokeTests.bind

    def setUp(self):
        SmokeTests.setUp(self)
        self.bind()
        from forge.store.transitions import Lifecycle
        from forge.factory.config import validate
        from forge.store.repository_planning import RepositoryPlanning
        self.runtime = validate({})
        self.requirements = {'acceptance': ['Document the successful smoke merge']}
        Lifecycle(self.store, clock=lambda: 150).admit_issue(request_id='admit-smoke', actor_id='supervisor:discovery',
            unit_id='smoke-unit', issue_id='1:10', repository_id=1, repository_name='owner/repo', issue_number=10,
            title='Smoke', body=self.issue['body'], requirements=self.requirements, observed_at=150)
        self.plans = RepositoryPlanning(self.store, clock=lambda: 150)
        self.github.issue.return_value = (self.issue, self.requirements)

    def proposed(self, action='keep', parameters=None, environment=False, suffix=''):
        from forge.steward_subjects import triage_dossiers, triage_schema
        from forge.store.transitions import manifest_hash
        from tests.steward_subject_fixture import output, row_choice
        if environment:
            self.runtime['environment_tiers'] = {'standard': {'image': 'sha256:' + '1'*64}}
        queued = self.plans.queue(1, request_id='smoke-triage'+suffix, actor_id='operator:installation', base_branch='main')
        context = self.plans.context(queued['proposal_id'], head='a'*40)
        job = {'id': 'triage-job'+suffix, 'proposal_id': queued['proposal_id'], 'slot': 'repository-triage',
               'role': 'triage', 'repo_id': 1, 'repo': 'owner/repo', 'head': 'a'*40, 'deadline': 500,
               'created_at': 150, 'fenced': False, 'cleanup': False, 'base_branch': 'main',
               'runner_config': self.runtime, 'steward_mode': 'triage'}
        dossiers = triage_dossiers(context, job)
        context.update(steward_dossiers=dossiers, steward_schema=triage_schema(dossiers, context))
        job['context_hash'] = manifest_hash(context)
        self.plans.reserve(queued['proposal_id'], job, context)
        decision = output(dossiers[0], [row_choice(action, '1:10', parameters or {})])
        if environment:
            decision['brief'] = {'choice_id': 'first', 'instructions': 'Run focused checks.', 'evidence_refs': [],
                                 'environment': {'environment_id': 'standard', 'services': []}}
        result = {'version': 1, 'decisions': [decision], 'metadata': {'reply': 'Review smoke', 'notes': [], 'handoff': None}}
        self.plans.progress(job['id'], capture={'result': result}, fenced=True, cleanup=True)
        self.plans.publish_decisions(queued['proposal_id'], execution_id=job['id'], request_id='publish-smoke'+suffix, actor_id='supervisor:triage')
        self.plans.settle_run(queued['proposal_id'])
        return self.plans.proposal(queued['proposal_id'])

    def ready(self):
        from forge.install_scope import triage_in
        with self.store.read_snapshot() as db:
            return triage_in(db, self.binding)

    def test_admitted_smoke_cannot_execute_until_real_keep_is_accepted(self):
        proposal = self.proposed()
        self.assertIsNone(self.ready())
        self.assertFalse(execution_allowed(self.store, {'repo': 'owner/repo', 'unit_id': 'smoke-unit'}))
        self.assertIsNone(self.service.accept_keep(self.binding, self.github, self.runtime))
        self.assertIsNotNone(self.ready())
        self.assertTrue(execution_allowed(self.store, {'repo': 'owner/repo', 'unit_id': 'smoke-unit'}))
        accepted = self.plans.proposal(proposal['id'])
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual(accepted['accepted_by'], 'operator:installation:install')
        self.assertIsNone(self.service.accept_keep(self.binding, self.github, self.runtime))
        self.assertEqual(self.plans.proposal(proposal['id'])['version'], accepted['version'])
        self.github.issue.assert_called_once()

    def test_new_completed_suggestion_does_not_revoke_accepted_smoke(self):
        accepted = self.proposed()
        self.service.accept_keep(self.binding, self.github, self.runtime)
        before = self.ready()
        self.plans.clock = lambda: 170
        newer = self.proposed(suffix='-after-build')
        self.assertEqual(self.ready(), before)
        self.assertEqual(before['id'], accepted['id'])
        self.assertIsNone(self.service.accept_keep(self.binding, self.github, self.runtime))
        self.assertEqual(self.plans.proposal(newer['id'])['status'], 'proposed')
        self.assertTrue(execution_allowed(self.store, {'repo': 'owner/repo', 'unit_id': 'smoke-unit'}))

    def test_accepted_smoke_does_not_follow_changed_delivery_membership(self):
        self.proposed()
        self.service.accept_keep(self.binding, self.github, self.runtime)
        self.assertIsNotNone(self.ready())
        with self.store.write() as db:
            db.execute("UPDATE unit_issues SET left_at=180,exit_reason='combined' WHERE unit_id='smoke-unit'")
        self.assertIsNone(self.ready())

    def test_accepted_evidence_survives_normal_merge_membership_completion(self):
        self.proposed()
        self.service.accept_keep(self.binding, self.github, self.runtime)
        before = self.ready()
        with self.store.write() as db:
            db.execute("UPDATE unit_issues SET state='done',left_at=180,exit_reason='merged',completion_issue_version=1 WHERE unit_id='smoke-unit'")
            db.execute("UPDATE units SET state='reviewing' WHERE id='smoke-unit'")
            db.execute("UPDATE units SET state='merged',merge_sha=? WHERE id='smoke-unit'", ('b'*40,))
        self.assertEqual(self.ready(), before)

    def test_prior_accepted_smoke_cannot_authorize_changed_requirements(self):
        from forge.store.models import Command
        from forge.store.transitions import Lifecycle
        self.proposed()
        self.service.accept_keep(self.binding, self.github, self.runtime)
        self.assertIsNotNone(self.ready())
        with self.store.read_snapshot() as db:
            version = db.execute("SELECT version FROM units WHERE id='smoke-unit'").fetchone()[0]
        Lifecycle(self.store, clock=lambda: 180).observe_issue(
            Command('changed-smoke', 'supervisor:observation', 'smoke-unit', version), '1:10',
            state='open', title='Smoke', body=self.issue['body'], requirements={'acceptance': ['Different work']},
            remote_updated_at='2026-10-16T10:00:00Z', generation=2)
        self.assertIsNone(self.ready())

    def test_question_is_not_auto_approved_or_runnable(self):
        self.proposed('question', {'question': 'Clarify the scope?'})
        result = self.service.accept_keep(self.binding, self.github, self.runtime)
        self.assertEqual(result['status'], 'smoke_triage_required')
        self.assertIsNone(self.ready())
        self.assertFalse(execution_allowed(self.store, {'repo': 'owner/repo', 'unit_id': 'smoke-unit'}))
        self.github.issue.assert_not_called()

    def test_park_is_not_auto_approved_or_runnable(self):
        self.proposed('park', {'add': []})
        self.assertEqual(self.service.accept_keep(self.binding, self.github, self.runtime)['status'], 'smoke_triage_required')
        self.assertIsNone(self.ready())
        self.github.issue.assert_not_called()

    def test_accepting_question_does_not_unlock_execution(self):
        proposal = self.proposed('question', {'question': 'Clarify the scope?'})
        self.plans.accept(proposal['id'], expected_version=proposal['version'], accept_rows=[proposal['rows'][0]['id']],
            reject_rows=[], request_id='human-question', actor_id='github:7', config=self.runtime)
        self.assertIsNone(self.ready())
        self.assertEqual(self.service.accept_keep(self.binding, self.github, self.runtime)['status'], 'smoke_triage_required')

    def test_changed_remote_requirements_prevent_auto_acceptance(self):
        self.proposed()
        self.github.issue.return_value = (self.issue, {'acceptance': ['changed']})
        self.assertEqual(self.service.accept_keep(self.binding, self.github, self.runtime)['status'], 'smoke_triage_required')
        self.assertIsNone(self.ready())

    def test_keep_environment_uses_admitted_member_and_waits_for_real_effect(self):
        self.proposed(environment=True)
        self.assertIsNone(self.service.accept_keep(self.binding, self.github, self.runtime))
        self.assertIsNone(self.ready())
        self.assertIsNone(self.service.accept_keep(self.binding, self.github, self.runtime))
        with self.store.write() as db:
            environment = db.execute('SELECT choice FROM unit_environment WHERE unit_id=?', ('smoke-unit',)).fetchone()
            self.assertIsNotNone(environment)
            count = db.execute("SELECT COUNT(*) FROM external_effects WHERE unit_id='smoke-unit' AND kind='environment_label'").fetchone()[0]
            self.assertEqual(count, 1)
            db.execute("UPDATE external_effects SET status='confirmed' WHERE unit_id='smoke-unit' AND kind='environment_label'")
        self.assertIsNotNone(self.ready())

    def test_unconfirmed_accepted_metadata_effect_does_not_satisfy_proof(self):
        proposal = self.proposed('relabel', {'add': ['documentation'], 'remove': []})
        accepted = self.plans.accept(proposal['id'], expected_version=proposal['version'],
            accept_rows=[proposal['rows'][0]['id']], reject_rows=[], request_id='human-label', actor_id='github:7', config=self.runtime)
        self.assertIsNone(self.ready())
        self.assertIsNone(self.service.accept_keep(self.binding, self.github, self.runtime))
        with self.store.write() as db:
            db.execute("UPDATE external_effects SET status='confirmed' WHERE id=?", (accepted['effects'][0],))
        self.assertIsNotNone(self.ready())


class SmokeClientTests(unittest.TestCase):
    def setUp(self):
        from forge_client.install_smoke import InstallSmoke
        self.target = {'id': 1, 'name': 'owner/repo'}
        self.state = {'started_at': 100, 'inputs_digest': 'a'*64, 'smoke': {'repository': self.target, 'number': 10,
                      'smoke_policy': 'human'}, 'intent': {'repositories': [
                      {'target': self.target, 'merge_policy': 'human', 'allow_workflow_edits': False}]}}
        self.factory = Mock(); self.factory.workflow.clock.return_value = 200
        self.client = InstallSmoke(self.factory, self.state, Mock())
        self.result = {'status': 'smoke_merged', 'candidate': {'pull_number': 20, 'head_sha': 'h', 'merge_sha': 'm'}}
        self.factory.github.merged_pull.return_value = {'merged': True, 'head_sha': 'h', 'merge_sha': 'm', 'merged_at': '1970-01-01T00:02:30Z'}
        self.factory.github.pilot_issue.return_value = {'state': 'closed', 'number': 10}
        self.factory.call.side_effect = lambda action: self.result if action == 'smoke_status' else {'status': 'handover_complete'}

    def test_busy_binding_cannot_select_or_start_factory(self):
        busy = {'status': 'smoke_pending', 'phase': 'controller'}
        self.factory.call.side_effect = None
        self.factory.call.return_value = busy
        with patch.object(self.client, 'prepare', return_value={'status': 'smoke_repository_ready'}):
            self.assertEqual(self.client.run(apply=True), busy)
        self.assertEqual([call.args[0] for call in self.factory.call.call_args_list], ['smoke_bind'])
        self.factory.check.assert_not_called()

    def test_explicit_retry_calls_bound_host_action_before_normal_launch(self):
        def call(action, **parameters):
            if action == 'smoke_retry':
                self.assertEqual(parameters, {'proposal_id': 'failed-proposal'})
                return {'status': 'smoke_retry_queued'}
            if action == 'status': return {'phase': 'active'}
            return {'status': 'smoke_pending'}
        self.factory.call.side_effect = call
        with patch.object(self.client, 'prepare', return_value={'status': 'smoke_repository_ready'}):
            self.assertEqual(self.client.run(apply=True, retry_proposal='failed-proposal')['status'], 'smoke_pending')
        self.assertEqual([call.args[0] for call in self.factory.call.call_args_list],
                         ['smoke_retry', 'smoke_bind', 'status', 'smoke_launch'])

    def test_readonly_handover_verifies_without_activating(self):
        self.assertEqual(self.client.handover()['status'], 'smoke_verified')
        self.assertEqual(self.factory.call.call_count, 1)

    def test_local_github_readback_precedes_host_handover(self):
        self.assertEqual(self.client.handover(apply=True)['status'], 'handover_complete')
        self.assertEqual([call.args[0] for call in self.factory.call.call_args_list], ['smoke_status', 'smoke_handover'])

    def test_open_issue_or_mismatched_head_never_reaches_handover(self):
        self.factory.github.pilot_issue.return_value['state'] = 'open'
        with self.assertRaises(SetupError): self.client.handover(apply=True)
        self.assertEqual(self.factory.call.call_count, 1)
