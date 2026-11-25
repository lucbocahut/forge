"""A running worker's output: a bounded, scrubbed live tail until capture."""
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import patch
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from forge.factory.activity import command_line
from forge.factory.live_output import LiveTails, observe, summarize
from forge.factory.runner import ContainerRunner, RunnerError
from forge.factory.store_views import PipelineViews
from forge.store.db import Store
from forge.store.executions import Executions, Launch
from forge.store.models import Command
from forge.store.transitions import project_unit

CONTAINER = 'c' * 64
CONFIG = {'engine': 'docker', 'engine_socket': '/run/engine.sock', 'provider_environment': ['PROVIDER_KEY']}
INSTALLATION_TOKEN = 'ghs_' + 'X' * 20


def beat(at):
    return json.dumps({'runner': 'alive', 'attempt': 'job', 'at': at})


def tool(name, at, **data):
    return json.dumps({'type': 'forge.activity', 'id': 'toolu_' + name, 'tool': name, 'input': data, 'at': at})


class Engine:
    """The engine CLI as ContainerRunner calls it: inventory, inspect, logs."""

    def __init__(self, logs, job_id):
        self.logs, self.job_id, self.calls = logs, job_id, []
        self.present, self.fail_logs = True, False

    def __call__(self, argv, **kwargs):
        args = argv[3:]
        self.calls.append(args)
        name = 'forge-job-' + self.job_id
        if args[:2] == ['container', 'ls']:
            out = json.dumps({'Names': name, 'ID': CONTAINER}) if self.present else ''
        elif args[:2] == ['container', 'inspect']:
            out = json.dumps([{'Id': CONTAINER, 'Name': '/' + name, 'Config': {'Labels': {'forge.execution': self.job_id, 'forge.slot': 'slot'}},
                               'State': {'Status': 'running', 'ExitCode': 0, 'Running': True, 'StartedAt': '0001-01-01T00:00:00Z'}}])
        elif args[0] == 'logs':
            if self.fail_logs:
                return subprocess.CompletedProcess(argv, 1, b'', b'')
            out = self.logs
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(argv, 0, out.encode(), b'')


class SummaryTests(unittest.TestCase):
    def test_wrappers_do_not_hide_the_command(self):
        self.assertEqual(command_line('timeout 300 safe-build pnpm build 2>&1 | tail -20'), 'Running pnpm build')
        self.assertEqual(command_line('timeout -k 5 60 npm test'), 'Running npm test')
        self.assertEqual(command_line('env CI=1 pytest -q tests'), 'Running pytest tests')

    def test_heartbeats_fold_into_one_time_and_the_latest_activity_leads(self):
        logs = '\n'.join([beat(100), tool('Read', 101, file_path='/work/repository/src/a.ts'), 'plain output',
                          beat(105), tool('Bash', 106, command='timeout 300 safe-build pnpm build 2>&1 | tail -20'), beat(110)])
        view = summarize(logs)
        self.assertEqual(view['heartbeat_at'], 110)
        self.assertNotIn('"alive"', view['text'])
        self.assertIn('plain output', view['text'])
        self.assertEqual(view['activity'], {'text': 'Running pnpm build', 'tool': 'Bash', 'at': 106})

    def test_no_events_means_no_activity(self):
        self.assertEqual(summarize('hello\n'), {'text': 'hello', 'heartbeat_at': None, 'activity': None})
        self.assertEqual(summarize(tool('Edit', 5, file_path='/work/repository/src/foo.ts'))['activity']['text'], 'Editing src/foo.ts')

    def test_reads_are_cached_and_serialized_per_job(self):
        now = [0.0]
        tails, reads = LiveTails(clock=lambda: now[0], ttl=3), []
        read = lambda: reads.append(1) or {'logs': str(len(reads))}
        self.assertEqual(tails.read('a', read), {'logs': '1'})
        now[0] = 2.9
        self.assertEqual(tails.read('a', read), {'logs': '1'})
        self.assertEqual(tails.read('b', read), {'logs': '2'})
        now[0] = 3.5
        self.assertEqual(tails.read('a', read), {'logs': '3'})

    def test_slow_job_does_not_block_another_job_and_same_job_coalesces(self):
        tails = LiveTails()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def slow():
            calls.append('slow')
            entered.set()
            self.assertTrue(release.wait(2))
            return {'logs': 'slow'}
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(tails.read, 'slow', slow)
            self.assertTrue(entered.wait(1))
            same = pool.submit(tails.read, 'slow', slow)
            other = pool.submit(tails.read, 'fast', lambda: {'logs': 'fast'})
            try:
                self.assertEqual(other.result(timeout=1), {'logs': 'fast'})
            finally:
                release.set()
            self.assertEqual(first.result(timeout=1), {'logs': 'slow'})
            self.assertEqual(same.result(timeout=1), {'logs': 'slow'})
        self.assertEqual(calls, ['slow'])

    def test_engine_failures_are_typed_reasons(self):
        class Broken:
            def observe(self, job, lines, timeout):
                raise RunnerError('Container engine operation failed')
        self.assertEqual(observe(Broken(), {}), {'unavailable': 'runner_unavailable'})


class OutputViewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store.create(directory.name)
        self.life = Executions(self.store, clock=lambda: 100.0)
        self.serial = 0
        admitted = self.life.admit_issue(request_id='admit', actor_id='operator', unit_id='u', issue_id='i',
            repository_id=1, repository_name='owner/repo', issue_number=1, title='Issue', body='Body',
            requirements={'acceptance': ['works']}, observed_at=90)
        manifest = dict(requirements_revision_id=admitted['revision_id'], base_oid='base', policy_hash='policy')
        self.job = self.life.reserve_job(self.command(), 'build', slot='slot', execution_fence='fence', input_manifest=manifest,
            member_id=admitted['member_id'], launch=Launch('forge/issue-1', 'builder[bot]', 'claude', 200.0, CONFIG, {}))['job_id']
        # The job's own launch secrets, as prepare() writes them.
        secrets = self.root / 'state/factory/job-secrets'
        secrets.mkdir(parents=True)
        runner = ContainerRunner(CONFIG, environ={'FORGE_CREDENTIAL_ROOT': str(self.root)})
        runner._secrets_path({'id': self.job}).write_text(json.dumps({'github_token': INSTALLATION_TOKEN, 'values': ['oauth-refresh-value-123']}))
        self.engine = Engine('', self.job)
        self.environ = {'FORGE_CREDENTIAL_ROOT': str(self.root), 'PROVIDER_KEY': 'provider-secret-value'}

    def command(self):
        self.serial += 1
        with self.store.read_snapshot() as db:
            version = project_unit(db, 'u')['version']
        return Command(str(self.serial), 'operator', 'u', version)

    def run_job(self):
        for phase, container in (('starting', None), ('running', CONTAINER)):
            self.life.progress(self.command(), self.job, execution_fence='fence', phase=phase, container_id=container)

    def views(self):
        tails = LiveTails()
        live = lambda job: tails.read(job['id'], lambda: observe(ContainerRunner(job['runner_config'], run=self.engine, environ=self.environ), job))
        return PipelineViews(self.store, clock=lambda: 100, live=live)

    def test_a_running_job_shows_its_scrubbed_live_tail(self):
        self.run_job()
        self.engine.logs = '\n'.join([beat(150), f'token {INSTALLATION_TOKEN} provider-secret-value oauth-refresh-value-123',
                                      tool('Bash', 151, command='pnpm build'), beat(155)])
        view = self.views()
        output = view.output(self.job)
        self.assertTrue(output['live'])
        self.assertTrue(output['running'])
        self.assertIsInstance(output['at'], float)
        for secret in (INSTALLATION_TOKEN, 'provider-secret-value', 'oauth-refresh-value-123'):
            self.assertNotIn(secret, output['logs'])
            self.assertNotIn(secret, output['text'])
        self.assertIn('[REDACTED]', output['text'])
        self.assertNotIn('alive', output['text'])
        self.assertEqual(output['heartbeat_at'], 155)
        self.assertEqual(output['activity'], {'text': 'Running pnpm build', 'tool': 'Bash', 'at': 151})
        logs_call = next(call for call in self.engine.calls if call[0] == 'logs')
        self.assertEqual(logs_call[:3], ['logs', '--tail', '200'])
        # A poll within the cache window never reaches the engine again.
        calls = len(self.engine.calls)
        view.output(self.job)
        self.assertEqual(len(self.engine.calls), calls)
        self.assertEqual(view.job(self.job)['branch'], 'forge/issue-1')

    def test_live_engine_read_releases_the_sqlite_snapshot(self):
        self.run_job()
        def live(job):
            self.assertIsNone(self.store._reader.get())
            with self.store.write() as db:
                db.execute("INSERT INTO store_metadata(key,value) VALUES('live-read-test','ok')")
            return {'logs': 'live', 'at': 100, 'running': True}
        self.assertEqual(PipelineViews(self.store, live=live).output(self.job)['logs'], 'live')

    def test_live_observation_uses_one_deadline_across_engine_calls(self):
        now, timeouts = [100.0], []
        def engine(argv, **kwargs):
            timeouts.append(kwargs['timeout'])
            result = self.engine(argv, **kwargs)
            now[0] += 4
            return result
        with patch('forge.factory.runner.time.monotonic', side_effect=lambda: now[0]):
            result = observe(ContainerRunner(CONFIG, run=engine, environ=self.environ), {'id': self.job, 'slot': 'slot'})
        self.assertEqual(timeouts, [10, 6, 2])
        self.assertIn('logs', result)

    def test_an_unreadable_runner_is_a_reason_not_an_empty_log(self):
        self.run_job()
        self.engine.fail_logs = True
        self.assertEqual(self.views().output(self.job), {'logs': '', 'live': True, 'unavailable': 'runner_unavailable'})
        self.engine.fail_logs, self.engine.present = False, False
        self.assertEqual(self.views().output(self.job), {'logs': '', 'live': True, 'unavailable': 'container_missing'})

    def test_a_reserved_job_has_not_started(self):
        self.assertEqual(self.views().output(self.job), {'logs': '', 'live': True, 'unavailable': 'not_started'})
        self.assertEqual(self.engine.calls, [])

    def test_captured_logs_win_and_never_touch_the_engine(self):
        self.run_job()
        self.life.collect(self.command(), self.job, execution_fence='fence',
                          capture={'result': None, 'logs': beat(1) + '\ncaptured line', 'missing': []})
        output = self.views().output(self.job)
        self.assertEqual(output['logs'], beat(1) + '\ncaptured line')
        self.assertEqual(output['text'], 'captured line')
        self.assertFalse(output['live'])
        self.assertEqual(self.engine.calls, [])

    def test_without_a_live_reader_the_view_is_unchanged(self):
        self.run_job()
        self.assertEqual(PipelineViews(self.store, clock=lambda: 100).output(self.job), {'logs': ''})


if __name__ == '__main__':
    unittest.main()
