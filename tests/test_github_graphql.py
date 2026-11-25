import unittest
from unittest.mock import Mock

from forge.app_access import AccessError
from forge.factory.github import GitHub, GitHubUnavailable, GitHubBudgetExhausted


class GraphQLPublicationTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.github = GitHub(Mock(), {}, client=self.client)
        self.github.token = Mock(return_value='synthetic-test-token')
        self.repo = {'repo_id': 12, 'repo': 'example/project'}

    def test_uses_repo_scoped_host_token_and_returns_data(self):
        self.client.request.return_value = {'data': {'createCommitOnBranch': {'commit': {'oid': 'a' * 40}}}}
        data = self.github.graphql('controller', self.repo, 'mutation Example {}', {'input': {'expectedHeadOid': 'b' * 40}})
        self.assertEqual(data, self.client.request.return_value['data'])
        self.github.token.assert_called_once_with('controller', 12, extra=None)
        self.assertEqual(self.client.request.call_args.args[:2], ('POST', '/graphql'))

    def test_partial_data_and_http_success_do_not_confirm_publication(self):
        for value in ({'data': {'commit': {}}, 'errors': [{'type': 'FORBIDDEN', 'message': 'private details'}]},
                      {'errors': [{'type': 'FORBIDDEN private details'}]}, {}, None):
            self.client.request.return_value = value
            with self.subTest(value=value), self.assertRaises(GitHubUnavailable) as caught:
                self.github.graphql('controller', self.repo, 'mutation Example {}', {})
            self.assertNotIn('private details', str(caught.exception))
            self.assertNotIn('private details', repr(caught.exception.error_types))

    def test_http_failure_preserves_status_without_diagnostic_body(self):
        self.client.request.side_effect = AccessError('private response details', status=503)
        with self.assertRaises(GitHubUnavailable) as caught:
            self.github.graphql('controller', self.repo, 'mutation Example {}', {})
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn('private', str(caught.exception))

    def test_publisher_identity_requires_exact_bot_and_keeps_credentials_host_side(self):
        self.github.publisher = Mock(return_value='forge-controller[bot]')
        self.client.request.return_value = {'id': 7, 'login': 'forge-controller[bot]', 'type': 'Bot', 'extra': 'ignored'}
        self.assertEqual(self.github.identity('controller', 12), {'id': 7, 'login': 'forge-controller[bot]', 'type': 'Bot'})
        self.assertEqual(self.client.request.call_args.args[1], '/users/forge-controller%5Bbot%5D')
        for changed in ({'id': True}, {'login': 'another[bot]'}, {'type': 'User'}):
            self.client.request.return_value.update(changed)
            with self.assertRaises(GitHubUnavailable):
                self.github.identity('controller', 12)

    def test_deadline_prevents_token_and_network_calls(self):
        self.github.deadline = 0
        with self.assertRaises(GitHubBudgetExhausted):
            self.github.graphql('controller', self.repo, 'mutation Example {}', {})
        self.github.token.assert_not_called()
        self.client.request.assert_not_called()


class IssueInputTests(unittest.TestCase):
    def test_notes_and_requirements_use_same_comment_observation(self):
        github = GitHub(Mock(), {}, client=Mock())
        github.call = Mock(return_value={'number': 7, 'title': 'A', 'body': 'B'})
        comments = [{'id': 1, 'body': 'human decision', 'user': {'type': 'User'}}]
        github.pages = Mock(return_value=comments)
        value, digest, observed = github.issue_inputs({'repo_id': 12, 'repo': 'example/project', 'number': 7})
        github.pages.assert_called_once()
        self.assertIs(observed, comments)
        self.assertEqual(value['number'], 7)
        self.assertEqual(len(digest), 64)


class ReadyApprovalTests(unittest.TestCase):
    def setUp(self):
        self.github = GitHub(Mock(), {}, client=Mock())
        self.item = {'repo_id': 12, 'repo': 'example/project', 'number': 7}
        self.issue = {'node_id': 'issue-node', 'title': 'Issue', 'body': 'Current text', 'labels': ['go']}
        self.applied = dict(__typename='LabeledEvent', label={'name': 'go'}, actor={'__typename': 'User'},
                            createdAt='2026-10-08T10:00:00Z')
        self.timeline = {'nodes': [self.applied], 'pageInfo': {'hasPreviousPage': False, 'startCursor': 'first'}}
        self.node = dict(number=7, title='Issue', body='Current text', lastEditedAt=None, timelineItems=self.timeline)
        self.github.graphql = Mock(return_value={'node': self.node})

    def approved(self):
        return self.github.ready_approves(self.item, self.issue, 'go')

    def test_body_edit_requires_label_reapplied_strictly_after_edit(self):
        self.assertTrue(self.approved())
        for time in ('2026-10-08T10:00:00Z', '2026-10-08T10:01:00Z', 'invalid'):
            with self.subTest(time=time):
                self.node['lastEditedAt'] = time
                self.assertFalse(self.approved())
        self.node['lastEditedAt'] = '2026-10-08T10:01:00Z'
        self.timeline['nodes'].extend([
            dict(__typename='UnlabeledEvent', label={'name': 'go'}),
            {**self.applied, 'createdAt': '2026-10-08T10:02:00Z'},
        ])
        self.assertTrue(self.approved())

    def test_missing_removed_or_stale_approval_cannot_authorize(self):
        for nodes in ([], [{**self.applied, 'actor': {'__typename': 'Bot'}}],
                      [self.applied, dict(__typename='UnlabeledEvent', label={'name': 'go'})],
                      [self.applied, dict(__typename='RenamedTitleEvent')]):
            with self.subTest(nodes=nodes):
                self.timeline['nodes'] = nodes
                self.assertFalse(self.approved())
        self.timeline['nodes'] = [self.applied]
        self.node['body'] = 'Changed during approval check'
        self.assertFalse(self.approved())

    def test_reads_older_events_when_newer_labels_are_unrelated(self):
        newer = {'nodes': [{**self.applied, 'label': {'name': 'other'}}],
                 'pageInfo': {'hasPreviousPage': True, 'startCursor': 'next'}}
        self.github.graphql.side_effect = [{'node': {**self.node, 'timelineItems': newer}}, {'node': self.node}]
        self.assertTrue(self.approved())
        self.assertEqual(self.github.graphql.call_args.args[3], {'id': 'issue-node', 'before': 'next'})
