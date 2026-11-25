import unittest
from unittest.mock import Mock

from forge.factory.github import GitHubUnavailable
from forge.factory.observation_controller import ObservationController
from forge.store.models import VersionConflict
from tests import test_store_transitions as helpers


class GitHub:
    def __init__(self):
        self.value = {'number': 1, 'state': 'open', 'title': 'Issue 1', 'body': 'Implement the requirement',
                      'updated_at': '2026-10-07T00:00:00Z', 'labels': [], 'assignees': []}
        self.requirements = {'acceptance': ['works']}
        self.pull_rows = []
        self.after_issue = None
        self.failure = False

    def issue(self, item):
        if self.failure:
            raise GitHubUnavailable('not observable')
        if self.after_issue:
            self.after_issue()
        return self.value.copy(), self.requirements

    def pulls(self, item):
        return self.pull_rows


class ObservationTests(unittest.TestCase):
    setUp = helpers.StoreTransitionTests.setUp
    admit = helpers.StoreTransitionTests.admit
    view = helpers.StoreTransitionTests.view
    command = helpers.StoreTransitionTests.command
    reserve = helpers.StoreTransitionTests.reserve
    cleanup = helpers.StoreTransitionTests.cleanup
    accept_build = helpers.StoreTransitionTests.accept_build

    def controller(self, github=None, admission=True):
        return ObservationController(self.store, github or GitHub(), admission=lambda item: admission,
                                     clock=lambda: 100.0)

    def test_body_edit_fences_work_and_requires_reapplied_ready_label(self):
        job = self.reserve()
        github = GitHub()
        github.value.update(body='Edited requirement', labels=['ready'])
        github.requirements = {'acceptance': ['edited']}
        github.ready_approves = Mock(return_value=False)
        controller = self.controller(github)

        def revisions():
            with self.store.read_snapshot() as db:
                return tuple(db.execute('SELECT i.current_revision_id,m.requirements_revision_id FROM issues i '
                    'JOIN unit_issues m ON m.issue_id=i.id WHERE m.unit_id=?', ('unit-1',)).fetchone())

        original, _ = revisions()
        controller.step('unit-1')
        edited, pinned = revisions()
        self.assertNotEqual(edited, original)
        self.assertEqual(pinned, original)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT fenced FROM jobs WHERE id=?', (job['job_id'],)).fetchone()[0], 1)
        self.cleanup(job)
        self.assertEqual(self.view()['reason_code'], 'membership_decision')

        github.ready_approves.side_effect = GitHubUnavailable('Approval history is unavailable')
        controller.step('unit-1')
        self.assertEqual(revisions(), (edited, original))
        github.ready_approves.side_effect = None
        github.ready_approves.return_value = True
        controller.step('unit-1')
        self.assertEqual(revisions(), (edited, edited))
        self.assertIsNone(self.view()['reason_code'])

        github.ready_approves.reset_mock()
        github.requirements = {'acceptance': ['edited', 'comment']}
        controller.step('unit-1')
        current, pinned = revisions()
        self.assertNotEqual(current, edited)
        self.assertEqual(current, pinned)
        github.ready_approves.assert_not_called()

    def test_refresh_consumes_only_the_captured_operator_request(self):
        with self.store.write() as db:
            db.execute("INSERT INTO operator_observation_requests VALUES('issue-1','operator',1)")
        github = GitHub()
        def newer_request():
            with self.store.write() as db:
                db.execute("UPDATE operator_observation_requests SET requested_at=2 WHERE issue_id='issue-1'")
        github.after_issue = newer_request
        self.controller(github).step('unit-1')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT requested_at FROM operator_observation_requests').fetchone()[0], 2)
        github.after_issue = None
        self.controller(github).step('unit-1')
        with self.store.read_snapshot() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM operator_observation_requests').fetchone())

    def test_failed_refresh_retains_operator_request(self):
        with self.store.write() as db:
            db.execute("INSERT INTO operator_observation_requests VALUES('issue-1','operator',1)")
        github = GitHub(); github.failure = True
        with self.assertRaises(GitHubUnavailable):
            self.controller(github).step('unit-1')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM operator_observation_requests').fetchone()[0], 1)

    def test_hold_observation_fences_worker_and_removal_resumes_without_budget_reset(self):
        job = self.reserve()
        github = GitHub()
        github.value['labels'] = [{'name': 'hold'}]
        controller = self.controller(github)
        controller.step('unit-1')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT fenced FROM jobs WHERE id=?', (job['job_id'],)).fetchone()[0], 1)
        self.cleanup(job)
        self.assertEqual(self.view()['reason_code'], 'label_hold')
        github.value['labels'] = []
        controller.step('unit-1')
        self.assertIsNone(self.view()['reason_code'])
        self.assertEqual(self.view()['charged_failures'], 0)

    def test_setup_repair_resumes_but_does_not_clear_operator_pause(self):
        self.controller(admission=False).step('unit-1')
        self.assertEqual(self.view()['reason_code'], 'repository_setup')
        self.lifecycle.pause(self.command())
        self.controller(admission=True).step('unit-1')
        self.assertEqual(self.view()['reason_code'], 'operator_pause')
        self.lifecycle.resume(self.command())
        self.assertIsNone(self.view()['reason_code'])

    def test_human_assignment_and_wip_open_owned_gate_then_clear(self):
        github = GitHub()
        controller = self.controller(github)
        github.value['assignees'] = [{'login': 'human', 'type': 'User'}]
        controller.step('unit-1')
        self.assertEqual(self.view()['reason_code'], 'ownership_conflict')
        github.value['assignees'] = []
        github.value['labels'] = [{'name': 'wip'}]
        controller.step('unit-1')
        self.assertEqual(self.view()['reason_code'], 'ownership_conflict')
        github.value['labels'] = []
        controller.step('unit-1')
        self.assertIsNone(self.view()['reason_code'])

    def test_late_response_cannot_overwrite_newer_collection(self):
        github = GitHub()
        controller = self.controller(github)
        newer = GitHub()
        newer.value['title'] = 'New observation'
        github.after_issue = lambda: self.controller(newer).step('unit-1')
        with self.assertRaises(VersionConflict):
            controller.step('unit-1')
        self.assertEqual(controller.queries.issue('issue-1')['title'], 'New observation')

    def test_outage_preserves_existing_readiness_gate(self):
        github = GitHub()
        controller = self.controller(github, admission=False)
        controller.step('unit-1')
        github.failure = True
        result = controller.cycle()
        self.assertEqual(result['items'][0]['error'], 'GitHubUnavailable')
        self.assertEqual(self.view()['reason_code'], 'repository_setup')

    def test_completed_elsewhere_closes_as_done_and_preserves_pause_history(self):
        github = GitHub()
        github.value.update(state='closed', state_reason='completed')
        self.lifecycle.pause(self.command())
        self.controller(github).step('unit-1')
        self.assertEqual(self.view()['state'], 'closed')
        self.assertEqual(self.view()['close_code'], 'github_completed')
        self.assertEqual(self.view()['members'][0]['projected_state'], 'done')

    def test_custom_hold_label_is_authoritative(self):
        github = GitHub()
        github.value['labels'] = [{'name': 'operator-hold'}]
        controller = ObservationController(self.store, github, admission=lambda item: True,
            hold_labels=['operator-hold'], clock=lambda: 100)
        controller.step('unit-1')
        self.assertEqual(self.view()['reason_code'], 'label_hold')

    def test_completed_issue_is_recorded_despite_unavailable_pull_inventory(self):
        from unittest.mock import patch
        github = GitHub()
        github.value.update(state='closed', state_reason='completed')
        with patch.object(github, 'pulls', side_effect=GitHubUnavailable('inventory unavailable')):
            self.controller(github).step('unit-1')
        self.assertEqual(self.view()['state'], 'closed')

    def test_closed_issue_fences_worker_before_cleanup_without_ownership_reads(self):
        from unittest.mock import patch
        from forge.store.models import TransitionError
        job = self.reserve()
        github = GitHub()
        github.value.update(state='closed', state_reason='completed')
        with patch.object(github, 'pulls', side_effect=GitHubUnavailable('inventory unavailable')):
            with self.assertRaises(TransitionError):
                self.controller(github).step('unit-1')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT fenced FROM jobs WHERE id=?', (job['job_id'],)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT github_state FROM issues WHERE id='issue-1'").fetchone()[0], 'closed')
        self.cleanup(job)
        self.controller(github).step('unit-1')
        self.assertEqual(self.view()['state'], 'closed')

    def test_bot_identity_is_updated_with_observation(self):
        github = GitHub()
        github.value['assignees'] = [{'login': 'automation', 'type': 'Bot'}]
        controller = self.controller(github)
        controller.step('unit-1')
        self.assertEqual(controller.queries.issue('issue-1')['assignee_types'], {'automation': 'Bot'})
        self.assertIsNone(self.view()['reason_code'])

    def test_external_merge_fences_live_review_then_confirms_after_cleanup(self):
        github = GitHub()
        build = self.reserve()
        self.accept_build(build)
        self.cleanup(build)
        selected = self.view()['current_candidate_id']
        with self.store.read_snapshot() as db:
            identity = dict(db.execute('SELECT * FROM candidates WHERE id=?', (selected,)).fetchone())
        manifest = {key: identity[key] for key in ('head_oid', 'base_oid', 'policy_hash', 'requirements_hash', 'input_manifest_hash')}
        manifest['candidate_id'] = selected
        review = self.lifecycle.reserve_job(self.command(), 'review', slot='slot-1', execution_fence='fence-slot-1',
            input_manifest=manifest, candidate_id=selected)
        github.pr = lambda item, number: {'number': 1, 'merged': True, 'merge_commit_sha': 'merge',
            'head': {'sha': 'head', 'repo': {'id': 1}}, 'base': {'sha': 'base', 'repo': {'id': 1}}}
        controller = self.controller(github)
        self.assertEqual(controller.step('unit-1')['pending'], 'external_change')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT fenced FROM jobs WHERE id=?', (review['job_id'],)).fetchone()[0], 1)
        self.cleanup(review)
        controller.step('unit-1')
        self.assertEqual(self.view()['state'], 'merged')
        self.assertEqual(self.view()['merge_sha'], 'merge')

    def test_verified_changed_candidate_is_complete_while_external_change_stays_blocked(self):
        github=GitHub()
        build=self.reserve();self.accept_build(build);self.cleanup(build)
        selected=self.view()['current_candidate_id']
        github.pr=lambda item,number: {'number':1,'merged':False,'state':'open','draft':False,
            'head':{'sha':'external-head','repo':{'id':1}},'base':{'sha':'base','ref':'main','repo':{'id':1}}}
        result=self.controller(github).cycle()
        self.assertTrue(result['sweep']['complete'])
        self.assertEqual(result['sweep']['refreshed'],1)
        self.assertEqual(result['items'][0]['pending'],'external_change')
        self.assertEqual(self.view()['reason_code'],'external_change')
        self.assertEqual(self.view()['current_candidate_id'],selected)

    def test_exact_repaired_candidate_observation_releases_only_external_change(self):
        github=GitHub()
        build=self.reserve();self.accept_build(build);self.cleanup(build)
        self.lifecycle.open_gate(self.command(),'external_change',details={'reason':'candidate_unavailable'})
        self.lifecycle.open_gate(self.command(),'environment',details={'reason':'database'})
        github.pr=lambda item,number: {'number':1,'merged':False,'state':'open','draft':False,
            'head':{'sha':'head','repo':{'id':1}},'base':{'sha':'base','ref':'main','repo':{'id':1}}}
        self.assertTrue(self.controller(github).step('unit-1')['observed'])
        self.assertEqual(self.view()['reason_code'],'environment')
        self.assertIsNotNone(self.view()['current_candidate_id'])
        with self.store.read_snapshot() as db:
            gate=db.execute("SELECT resolution FROM unit_gates WHERE code='external_change'").fetchone()
            self.assertEqual(gate[0],'observation')

    def test_policy_change_invalidates_candidate_and_preserves_operator_pause(self):
        github = GitHub()
        build = self.reserve()
        self.accept_build(build)
        self.cleanup(build)
        self.lifecycle.pause(self.command())
        selected = self.view()['current_candidate_id']
        github.pr = lambda item, number: {'number': 1, 'merged': False, 'state': 'open', 'draft': False,
            'head': {'sha': 'head', 'repo': {'id': 1}},
            'base': {'sha': 'base', 'ref': 'main', 'repo': {'id': 1}}}
        controller = ObservationController(self.store, github, admission=lambda item: True,
                                           policy=lambda item: 'changed-policy', clock=lambda: 100)
        controller.step('unit-1')
        self.assertEqual(self.view()['state'], 'building')
        self.assertIsNone(self.view()['current_candidate_id'])
        self.assertEqual(self.view()['reason_code'], 'operator_pause')
        self.assertEqual(self.view()['charged_failures'], 0)
        with self.store.read_snapshot() as db:
            self.assertIsNotNone(db.execute('SELECT id FROM candidates WHERE id=?', (selected,)).fetchone())

    def test_verified_closure_fences_worker_when_candidate_read_spends_budget(self):
        from forge.factory.github import GitHubBudgetExhausted
        build=self.reserve();self.accept_build(build);self.cleanup(build)
        selected=self.view()['current_candidate_id']
        with self.store.read_snapshot() as db:
            identity=dict(db.execute('SELECT * FROM candidates WHERE id=?',(selected,)).fetchone())
        manifest={key:identity[key] for key in ('head_oid','base_oid','policy_hash','requirements_hash','input_manifest_hash')}
        manifest['candidate_id']=selected
        review=self.lifecycle.reserve_job(self.command(),'review',slot='review',execution_fence='review-fence',
            candidate_id=selected,input_manifest=manifest)
        github=GitHub();github.value.update(state='closed',state_reason='completed')
        github.pr=lambda *args:(_ for _ in ()).throw(GitHubBudgetExhausted('Later'))
        result=self.controller(github).cycle()
        self.assertEqual(result['deferred'],'github_budget')
        self.assertFalse(result['sweep']['complete'])
        self.assertIsNone(result['next_cursor'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT github_state FROM issues').fetchone()[0],'closed')
            self.assertEqual(db.execute('SELECT fenced FROM jobs WHERE id=?',(review['job_id'],)).fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0],0)

    def test_import_staging_never_calls_github(self):
        with self.store.write() as db:
            db.execute("INSERT INTO store_metadata VALUES('import_only','true')")
        github = GitHub()
        github.after_issue = lambda: self.fail('staging must not inspect live GitHub')
        from forge.store.db import StoreError
        with self.assertRaises(StoreError):
            self.controller(github).cycle()
