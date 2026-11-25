"""Full-page and interrupted-member observation through the real lifecycle."""
import unittest

from forge.factory.github import GitHubBudgetExhausted
from forge.factory.observation_controller import ObservationController
from forge.store.observation_progress import ObservationProgress
from tests import test_delivery_execution as delivery_helpers
from tests.test_store_observation_controller import GitHub
from tests import test_store_transitions as transition_helpers


class SweepGitHub(GitHub):
    def __init__(self, allowance=None):
        super().__init__()
        self.allowance=allowance
        self.visited=[]
        self.value['title']='Issue {number}'

    def issue(self,item):
        if self.allowance is not None and len(self.visited)>=self.allowance:
            raise GitHubBudgetExhausted('Next cycle')
        self.visited.append(item['number'])
        return {**self.value,'number':item['number'],'title':self.value['title'].format(number=item['number'])},self.requirements


class SweepTests(unittest.TestCase):
    setUp=transition_helpers.StoreTransitionTests.setUp
    admit=transition_helpers.StoreTransitionTests.admit

    def controller(self,github):
        return ObservationController(self.store,github,admission=lambda item:True,clock=lambda:100)

    def test_one_healthy_phase_visits_all_pages_beyond_fifty_units(self):
        for number in range(2,62):self.admit(number)
        github=SweepGitHub()
        report=self.controller(github).cycle(limit=50)
        self.assertEqual(set(github.visited),set(range(1,62)))
        self.assertEqual(report['sweep']['visits'],61)
        self.assertEqual(report['sweep']['pages'],2)
        self.assertTrue(report['sweep']['complete'])
        self.assertIsNone(report['next_cursor'])
        self.assertEqual(report['sweep']['coverage']['stale_units'],0)
        saved=ObservationProgress(self.store).data
        self.assertIsNone(saved['current'])
        self.assertEqual(saved['last']['visits'],61)

    def test_fresh_admission_behind_cursor_requires_another_sweep(self):
        self.admit(2)
        self.controller(SweepGitHub(1)).cycle()
        self.lifecycle.admit_issue(request_id='admit-new',actor_id='operator',unit_id='aaa-new',issue_id='issue-new',
            repository_id=1,repository_name='owner/repository',issue_number=3,title='Issue 3',body='Implement the requirement',
            requirements={'acceptance':['works']},observed_at=100)
        result=self.controller(SweepGitHub()).cycle()
        self.assertEqual(result['sweep']['coverage']['stale_members'],0)
        self.assertTrue(result['sweep']['scope_changed'])
        self.assertFalse(result['sweep']['complete'])
        result=self.controller(SweepGitHub()).cycle()
        self.assertTrue(result['sweep']['complete'])
        self.assertEqual(result['sweep']['visits'],3)

    def test_fresh_recomposition_behind_cursor_requires_another_sweep(self):
        from forge.store.delivery import Delivery
        from forge.store.queries import Queries
        from forge.store.models import Command
        self.admit(2);self.admit(3)
        self.controller(SweepGitHub(2)).cycle()
        queries=Queries(self.store);pins=[]
        for number in (1,2):
            unit=queries.unit('unit-'+str(number));member=unit['members'][0];issue=queries.issue(member['issue_id'])
            pins.append(dict(issue_id=issue['id'],issue_version=issue['version'],requirements_revision_id=issue['current_revision_id'],
                unit_id=unit['id'],unit_version=unit['version'],member_id=member['id']))
        delivery=Delivery(self.store,clock=lambda:100)
        proposal=dict(repository_id=1,base_branch='main',members=pins,dependencies=[],existing_prs=[],rationale='Related work')
        delivery.compose(Command('compose-new','operator','aaa-composed',1),**proposal,
            approval={'approved':True,'composition_hash':delivery.composition_hash(**proposal)})
        result=self.controller(SweepGitHub()).cycle()
        self.assertEqual(result['sweep']['coverage']['stale_members'],0)
        self.assertTrue(result['sweep']['scope_changed'])
        self.assertFalse(result['sweep']['complete'])
        result=self.controller(SweepGitHub()).cycle()
        self.assertTrue(result['sweep']['complete'])
        self.assertEqual(result['sweep']['visits'],3)

    def test_cycle_error_codes_do_not_repeat_errors_from_an_earlier_phase(self):
        from forge.factory.github import GitHubUnavailable
        from forge.store.models import TransitionError
        self.admit(2);self.admit(3)
        controller=self.controller(SweepGitHub())
        calls=[]
        def first(unit,member,issue):
            calls.append(member)
            if len(calls)==1:raise GitHubUnavailable('private first error')
            raise GitHubBudgetExhausted('Later')
        controller.observe_member=first
        result=controller.cycle()
        self.assertEqual(result['error_codes'],['GitHubUnavailable'])
        controller=self.controller(SweepGitHub())
        controller.observe_member=lambda *args:(_ for _ in ()).throw(TransitionError('private second error'))
        result=controller.cycle()
        self.assertEqual(result['error_count'],2)
        self.assertEqual(result['error_codes'],['TransitionError'])
        self.assertEqual(result['sweep']['error_codes'],['GitHubUnavailable','TransitionError'])
        self.assertEqual(result['sweep']['errors'],3)


class GroupSweepTests(unittest.TestCase):
    setUp=delivery_helpers.DeliveryExecutionTests.setUp
    compose=delivery_helpers.DeliveryExecutionTests.compose
    command=delivery_helpers.DeliveryExecutionTests.command

    def controller(self,github):
        github.value.update(title='Issue',body='Scope')
        return ObservationController(self.store,github,admission=lambda item:True,clock=lambda:100)

    def test_interrupted_group_resumes_exact_pending_member_without_charging_failure(self):
        self.compose(61)
        first=SweepGitHub(allowance=7)
        report=self.controller(first).cycle(limit=5)
        self.assertEqual(len(first.visited),7)
        self.assertEqual(report['deferred'],'github_budget')
        self.assertFalse(report['sweep']['complete'])
        self.assertEqual(report['sweep']['coverage']['stale_members'],54)
        cursor=report['next_cursor']
        self.assertEqual(cursor['unit_id'],'batch')
        saved=ObservationProgress(self.store).data['current']
        self.assertEqual(saved['cursor'],cursor)
        self.assertEqual(saved['coverage']['stale_members'],54)
        self.assertFalse(saved['complete'])
        second=SweepGitHub()
        report=self.controller(second).cycle(limit=5)
        self.assertEqual(len(second.visited),54)
        self.assertEqual(set(first.visited).intersection(second.visited),set())
        self.assertEqual(set(first.visited+second.visited),set(range(1,62)))
        self.assertTrue(report['sweep']['complete'])
        self.assertEqual(report['sweep']['phases'],2)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_gates WHERE code='recovery_pending'").fetchone()[0],0)

    def test_observed_group_closure_completes_while_member_decision_remains(self):
        self.compose(2)
        github=SweepGitHub()
        original=github.issue
        def issue(item):
            value,requirements=original(item)
            if item['number']==1:value.update(state='closed',state_reason='completed')
            return value,requirements
        github.issue=issue
        result=self.controller(github).cycle()
        self.assertTrue(result['sweep']['complete'])
        self.assertEqual(result['sweep']['refreshed'],2)
        self.assertEqual(result['sweep']['deferred'],0)
        closed=next(row for row in result['items'] if row.get('pending'))
        self.assertTrue(closed['observed']);self.assertEqual(closed['pending'],'membership_decision')
        with self.store.read_snapshot() as db:
            gate=db.execute("SELECT member_id FROM unit_gates WHERE code='membership_decision' AND resolved_at IS NULL").fetchone()
            self.assertEqual(gate[0],self.members['issue-0'])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_issues WHERE unit_id='batch' AND left_at IS NULL").fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0],0)
        again=self.controller(github).cycle()
        self.assertTrue(again['sweep']['complete'])
        self.assertEqual(again['sweep']['refreshed'],2)

    def test_membership_change_restarts_current_unit_against_its_live_members(self):
        self.compose(3)
        first=SweepGitHub(allowance=1)
        self.controller(first).cycle()
        removed=self.members['issue-'+str(first.visited[0]-1)]
        closed=SweepGitHub()
        closed.value.update(state='closed',state_reason='completed')
        self.controller(closed).observe_member('batch',removed,'issue-'+str(first.visited[0]-1))
        self.delivery.drop_member(self.command(),removed,membership_generation=1,
            dependent_decisions={},evidence={'approved':True},disposition='close')
        second=SweepGitHub()
        result=self.controller(second).cycle()
        self.assertEqual(result['sweep']['scope_restarts'],1)
        self.assertEqual(len(second.visited),2)
        self.assertNotIn(first.visited[0],second.visited)
        self.assertEqual(result['sweep']['coverage']['members'],2)
        self.assertTrue(result['sweep']['scope_changed'])
        self.assertFalse(result['sweep']['complete'])
        self.assertTrue(self.controller(SweepGitHub()).cycle()['sweep']['complete'])

    def test_interrupted_process_is_reported_unknown_even_if_later_members_are_fresh(self):
        self.compose(2)
        progress=ObservationProgress(self.store)
        progress.start(100)
        report=self.controller(SweepGitHub()).cycle()
        self.assertTrue(report['sweep']['interrupted'])
        self.assertFalse(report['sweep']['complete'])
        self.assertEqual(report['sweep']['coverage']['stale_members'],0)
