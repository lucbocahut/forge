"""Fresh requirements after unchanged GitHub reads."""
import json
from pathlib import Path
from unittest.mock import Mock, patch

from forge.store.remote_time import normalize
from forge.store.models import TransitionError
from forge.factory.config import validate
from forge.factory.store_inventory import StoreInventory, DiscoveryController
from forge.factory.store_views import PipelineViews
from forge.factory.sqlite_supervisor import SqliteSupervisor
from tests.test_sqlite_runtime_support import RuntimeFixture

SCHEMA = Path(__file__).resolve().parents[1] / 'forge/store/schema'


class DiscoveryTimestampTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.prepare()
        self.github = Mock()
        self.github.deadline = float('inf')
        self.github.call.return_value = []
        self.github.pulls.return_value = []
        self.github.collaborators.return_value = []
        self.github.recent_authors.return_value = []
        self.github.target.return_value = 'main'
        self.github.protections.return_value = True
        self.github.ready_approves.return_value = True
        self.config = validate({'enabled': True, 'image': 'sha256:' + 'a' * 64})
        self.discovery = DiscoveryController(self.database, self.settings, self.github, config=self.config, clock=lambda: self.now)
        self.remote = dict(number=1, title='Ready issue', body='Full body', state='open',
                           updated_at='2026-10-03T16:02:51Z', labels=['ready'], assignees=[])
        self.repo = {'id': 10, 'name': 'example/project', 'enabled': True}

    def test_edited_ready_issue_waits_for_reapproval_even_on_first_seen_revision(self):
        edited = {**self.remote, 'body': 'Edited requirement', 'updated_at': '2026-10-03T16:03:00Z'}
        self.discovery.inventory.record(self.repo, edited)
        self.github.issue.return_value = edited, {'complete': True}
        self.github.ready_approves.return_value = False

        self.assertEqual(self.discovery.cycle()['items'], [{'issue_id': '10:1', 'pending': 'ready_reapproval'}])
        self.assertEqual(self.discovery.cycle()['items'], [{'issue_id': '10:1', 'pending': 'ready_reapproval'}])
        self.github.target.assert_not_called()
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM units').fetchone()[0], 0)

        self.github.ready_approves.return_value = True
        self.assertIn('unit_id', self.discovery.cycle()['items'][0])

    def test_import_shaped_raw_timestamp_is_refreshed_and_admitted_without_remote_edit(self):
        self.discovery.inventory.record(self.repo, self.remote, {'legacy_body_hash': 'old', 'complete': False})
        # Reproduce a pre-upgrade imported row independently of normalization.
        # Restore the canonical schema after exercising the Python boundary.
        with self.database.write() as db:
            trigger = db.execute("SELECT sql FROM sqlite_master WHERE name='issues_observed_in_order'").fetchone()[0]
            db.execute('DROP TRIGGER issues_observed_in_order')
            db.execute("UPDATE issues SET remote_updated_at=?,collection_generation=0 WHERE id='10:1'", (self.remote['updated_at'],))
        self.github.issue.return_value = self.remote, {'complete': True, 'acceptance': ['Full requirements']}
        result = self.discovery.cycle()
        self.assertIn('unit_id', result['items'][0])
        with self.database.write() as db:
            row = db.execute("SELECT remote_updated_at,collection_generation FROM issues WHERE id='10:1'").fetchone()
            self.assertEqual(row['remote_updated_at'], normalize(self.remote['updated_at']))
            self.assertGreater(row['collection_generation'], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM units').fetchone()[0], 1)
            db.execute(trigger)

    def test_per_issue_stale_requirements_backoff_and_projection_leave_global_errors_clear(self):
        self.discovery.inventory.record(self.repo, self.remote, {'complete': False})
        self.github.issue.return_value = self.remote, {'complete': False}
        result = self.discovery.cycle()
        self.assertEqual(result['items'], [{'issue_id': '10:1', 'reason_code': 'requirements_stale'}])
        self.assertEqual(SqliteSupervisor._errors({'discovery': result}), {})
        calls = self.github.issue.call_count
        self.discovery.cycle()
        self.assertEqual(self.github.issue.call_count, calls)
        views = PipelineViews(self.database, runtime=self.config, clock=lambda: self.now)
        row = views.issues(key='10:1')['issue']
        self.assertEqual((row['status'], row['reason_code']), ('attention', 'requirements_stale'))
        self.assertIn('fresh read', row['operator_status']['reason'])
        self.assertEqual(views.issues(state='attention')['total'], 1)
        self.assertEqual(views.factory(section='board')['issues'][0]['key'], '10:1')
        self.now += self.config['availability_retry_seconds'] + 1
        self.github.issue.return_value = self.remote, {'complete': True}
        self.assertIn('unit_id', self.discovery.cycle()['items'][0])
        self.assertNotEqual(views.issues(key='10:1')['issue']['reason_code'], 'requirements_stale')

    def test_row_transition_failure_does_not_stop_later_rows_or_repositories(self):
        other = {'id': 20, 'name': 'example/second', 'enabled': True}
        settings = Mock()
        settings.paused.return_value = False
        settings.locked = self.settings.locked
        settings.load.return_value = self.settings.load()
        settings.view.return_value = {'repositories': [{**repo, 'access': {'controller':'ready'}} for repo in (self.repo, other)]}
        self.github.call.return_value = [self.remote, {**self.remote, 'number': 2}]
        inventory = StoreInventory(self.database, settings, self.github, config=self.config, clock=lambda:self.now)
        original = inventory.record
        def record(repo, row):
            if repo['id'] == 10 and row['number'] == 1:
                raise TransitionError('one row cannot be applied')
            return original(repo, row)
        with patch.object(inventory, 'record', side_effect=record):
            inventory.refresh()
        with self.database.read_snapshot() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT id FROM issues ORDER BY id')], ['10:2','20:1','20:2'])
            rows = db.execute('SELECT repository_id,error_code,details_json FROM repository_observations WHERE kind=\'inventory\' ORDER BY repository_id').fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['error_code'], 'issue_observation_failed')
            self.assertEqual(json.loads(rows[0]['details_json'])['issue_errors'], [{'number':1,'reason_code':'admission_conflict'}])
            self.assertIsNone(rows[1]['error_code'])
