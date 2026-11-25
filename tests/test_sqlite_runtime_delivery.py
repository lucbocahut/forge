"""One delivery through the production selected-runtime composition.

Only the GitHub/App API and container runner are fakes. Settings admission,
configuration reloads, discovery, prerequisites and every delivery stage use SQL.
"""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from forge.app_access import ROLE_PERMISSIONS
from forge.factory.config import save
from forge.factory.sqlite_runtime import components, cycle
from forge.repository_setup import RepositorySetup, observation_key
from forge.settings import Settings, digest
from forge.store.queries import Queries
from tests.test_settings import configured
from tests.test_sqlite_identity import selected_store
from tests.test_store_sqlite_supervisor import GitHub as DeliveryGitHub, Runner as DeliveryRunner, MERGE


class GitHub(DeliveryGitHub):
    def __init__(self):
        super().__init__()
        self.remote['labels'] = ['ready']
        self.deadline = float('inf')

    def ready_approves(self, item, issue, ready_label):
        return ready_label in issue['labels']

    def protection_report(self, item, candidate):
        return dict(repo_id=item['repo_id'], branch=candidate['base_ref'], available=True, observed_at=100.0,
                    requirements=[{'id': 'reviews', 'passed': True}, {'id': 'stale', 'passed': True}],
                    bypass={'status': 'ready'})

    def discover(self, connection):
        role = list(ROLE_PERMISSIONS)[connection['app_id'] - 1]
        return {'repositories': [{'id': 1, 'name': 'owner/repo', 'archived': False}],
                'permissions': ROLE_PERMISSIONS[role], 'account': 'owner'}

    def call(self, role, item, method, path):
        if method == 'GET' and path.startswith('/issues?'):
            return [deepcopy(self.remote)] if 'state=open' in path else []
        return super().call(role, item, method, path)

    def pages(self, role, item, path, key=None):
        assert role=='controller' and path=='/issues?state=open' and key is None
        return self.call(role,item,'GET',path)

    def collaborators(self, item):
        return []

    def recent_authors(self, item):
        return []

    def environment_label(self, item, issue, target):
        self.remote['labels'] = [label for label in self.remote['labels'] if not label.startswith('env:')] + [target]


class Runner(DeliveryRunner):
    """The selected runtime also executes the repository's read-only triage job."""
    def __init__(self,github):
        super().__init__(github)
        self.planning_contexts={}

    def prepare(self,job):
        super().prepare(job)
        if job['role']=='triage':
            self.planning_contexts[job['id']]=deepcopy(job['context'])

    def capture(self,job):
        if job['role']!='triage':
            return super().capture(job)
        context=self.planning_contexts[job['id']]
        if 'steward_dossiers' in context:
            from tests.steward_subject_fixture import output, row_choice
            decisions=[output(dossier,[row_choice('keep',dossier['authority']['anchor_issue_id'])])
                       for dossier in context['steward_dossiers']]
            return {'result':{'version':1,'decisions':decisions,'metadata':{'reply':'Keep current work','notes':[],'handoff':None}},
                    'logs':'Read-only planning complete','execution':{'exit_code':0}}
        return {'result':{'version':1,'attempt':job['id'],'role':'triage','outcome':'pass',
            'summary':'Keep the current delivery plan','rows':[
                {'action':'keep','members':[issue['issue_id']],'reason':'Existing work remains useful',
                 'rule_quote':None,'parameters':{}} for issue in self.planning_contexts[job['id']]['issues']]},
            'logs':'Read-only planning complete','execution':{'exit_code':0}}


class SelectedRuntimeDeliveryTests(unittest.TestCase):
    def test_production_cycle_discovers_builds_reviews_audits_and_merges_after_restart(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root, now = Path(temporary.name), [100.0]
        store = selected_store(root)
        github = GitHub()
        runner = Runner(github)
        settings = Settings(root, client=github, clock=lambda: now[0])
        settings.save_config(configured())
        settings.refresh()
        repository = {'id': 1, 'name': 'owner/repo', 'enabled': True}
        RepositorySetup(settings).save_observation(1, {'repositories': {'1': {
            'name': 'owner/repo', 'status': 'ready', 'started_at': now[0],
            'revision': observation_key(settings.load(), repository),
            'report': {'observed_at': now[0], 'available': True, 'branch': 'main',
                       'requirements': [{'id': 'reviews', 'passed': True}, {'id': 'stale', 'passed': True}]}}}})
        config = settings.load()
        config['repositories'] = [repository]
        settings.save(config, digest(settings.load()))
        save(root, dict(enabled=True, merge_enabled=True, human_required=True, image='sha256:' + '1' * 64))
        self.assertTrue(Settings(root, clock=lambda: now[0]).admission('owner/repo')['allowed'])
        self.assertFalse((root/'factory.json').exists())
        self.assertFalse((root/'runtime.json').exists())
        with store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM units').fetchone()[0], 0)

        def tick():
            now[0] += 1
            # Public production entrypoint reloads selected configuration and
            # constructs all controllers, settings and callbacks after restart.
            report = cycle(root, runner=runner, github=github, clock=lambda: now[0])
            for name, value in report['stages'].items():
                if isinstance(value, dict):
                    self.assertNotIn('error', value, (name, value))
                    for item in value.get('items', []):
                        self.assertIsNone(item.get('error'), (name, item))
            return report

        parts = components(root, runner=runner, github=github, clock=lambda: now[0])
        self.assertIs(parts['results'].verdicts, parts['effects'])
        del parts, settings
        tick()
        issue = Queries(store).issue('1:1')
        unit_id = issue['lifecycle']['unit_id']
        self.assertIsNotNone(unit_id)
        self.assertEqual(Queries(store).unit(unit_id)['state'], 'building')
        self.assertEqual(runner.launched, ['triage'])
        for expected in ('reviewing', 'auditing', 'merging'):
            tick()
            self.assertEqual(Queries(store).unit(unit_id)['state'], expected)
        self.assertEqual(Queries(store).unit(unit_id)['reason_code'], 'approval_required')
        self.assertEqual(github.merge_calls, 0)
        self.assertEqual(runner.launched, ['triage', 'build', 'review', 'audit'])
        self.assertCountEqual(runner.cleaned, runner.launched)
        self.assertIn('env:standard', github.remote['labels'])
        from forge.factory.first_merge import proof
        pilot = dict(repository={'id':1,'name':'owner/repo'}, number=1, started_at=99, clock=lambda:now[0])
        self.assertEqual(proof(root, **pilot)['status'], 'waiting')
        github.approved = True
        tick()
        self.assertEqual(Queries(store).unit(unit_id)['state'], 'merging')
        self.assertEqual(github.merge_calls, 0)
        with store.read_snapshot() as db:
            retry_at = db.execute("SELECT due_at FROM next_actions WHERE unit_id=? AND owner='merges'", (unit_id,)).fetchone()[0]
        self.assertGreater(retry_at, now[0])
        now[0] = retry_at - 1
        tick()
        self.assertEqual(Queries(store).unit(unit_id)['state'], 'merged')
        self.assertEqual(Queries(store).issue('1:1')['lifecycle']['state'], 'done')
        self.assertEqual(github.merge_calls, 1)
        evidence = proof(root, **pilot)
        self.assertEqual(evidence['status'], 'merged')
        self.assertEqual(evidence['candidate']['merge_sha'], MERGE)
        self.assertEqual(set(evidence['evidence']['role_jobs']), {'build','review','audit'})
        self.assertTrue(evidence['evidence']['approval_hold_observed'])
        self.assertEqual(proof(root, **{**pilot, 'started_at':now[0]})['status'], 'waiting')
        tick()
        self.assertEqual(github.merge_calls, 1)
        self.assertEqual(runner.launched, ['triage', 'build', 'review', 'audit'])
        self.assertEqual(runner.containers, {})
        with store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT status FROM repository_triage_runs').fetchone()[0], 'complete')
            self.assertEqual(db.execute('SELECT status FROM triage_proposals').fetchone()[0], 'proposed')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM triage_proposal_rows WHERE status='accepted'").fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT merge_sha FROM units WHERE id=?', (unit_id,)).fetchone()[0], MERGE)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_leases').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE outcome='success' AND cleanup=1").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM external_effects WHERE kind IN ('github_verdict','github_merge') AND status='confirmed'").fetchone()[0], 3)
