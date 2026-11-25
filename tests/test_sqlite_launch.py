"""Read-only startup qualification and prepared-interpreter handoff."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from forge.launch import launch
from forge.host_update import desired_units, Record
from forge.store.db import StoreError
from tests.test_sqlite_identity import selected_store


class Reexec(BaseException):
    pass


class LauncherTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        saved = sys.argv
        self.addCleanup(setattr, sys, 'argv', saved)

    def test_prepared_library_reexec_preserves_target_arguments_and_environment(self):
        arguments = ['--script', '/source/script.py', '--root', str(self.root)]
        with patch('forge.store.db.qualified_sqlite', return_value=False), \
                patch('forge.store.migration.runtime.inspect', return_value={'library_directory': '/private/runtime/lib'}), \
                patch('forge.launch.os.execve', side_effect=Reexec) as execute, \
                patch('forge.launch.runpy.run_path') as start:
            os.environ['FORGE_TEST_CONTEXT'] = 'preserved'
            with self.assertRaises(Reexec):
                launch(arguments)
        executable, command, environment = execute.call_args.args
        self.assertEqual(executable, sys.executable)
        self.assertEqual(command, [sys.executable, '-m', 'forge.launch', *arguments])
        self.assertEqual(environment['LD_LIBRARY_PATH'], '/private/runtime/lib')
        self.assertEqual(environment['FORGE_TEST_CONTEXT'], 'preserved')
        start.assert_not_called()

    def test_library_failure_does_not_reexec_forever_or_launch_unqualified_service(self):
        os.environ['LD_LIBRARY_PATH'] = '/private/runtime/lib'
        with patch('forge.store.db.qualified_sqlite', return_value=False), \
                patch('forge.store.migration.runtime.inspect', return_value={'library_directory': '/private/runtime/lib'}), \
                patch('forge.launch.os.execve') as execute, patch('forge.launch.runpy.run_module') as start:
            with self.assertRaisesRegex(SystemExit, 'did not load'):
                launch(['forge.setup_service', '--root', str(self.root)])
        execute.assert_not_called()
        start.assert_not_called()

    def test_selected_state_cannot_start_without_qualified_interpreter_or_valid_marker(self):
        (self.root / 'state').mkdir()
        (self.root / 'state/storage').write_text('selected')
        with patch('forge.store.db.qualified_sqlite', return_value=False), \
                patch('forge.store.migration.runtime.inspect', return_value=None), patch('forge.launch.runpy.run_module') as start:
            with self.assertRaisesRegex(SystemExit, 'qualification is required; run bin/forge setup initialize'):
                launch(['forge.setup_service', '--root=' + str(self.root)])
        start.assert_not_called()
        # A selected store with a pending migration still launches: the schema
        # upgrade itself runs through the launcher, and services validate on open.
        with patch('forge.store.db.qualified_sqlite', return_value=True), patch('forge.launch.runpy.run_module') as start:
            launch(['forge.store.migration.host', '--root', str(self.root)])
        self.assertEqual(start.call_args.args[0], 'forge.store.migration.host')

    def test_explicit_last_root_and_default_host_setup_root_are_qualified(self):
        checked = []
        with patch('forge.store.db.qualified_sqlite', return_value=True), \
                patch('forge.launch.os.path.lexists', side_effect=lambda path: checked.append(str(path)) or True), \
                patch('forge.launch.runpy.run_module') as start, patch('forge.launch.Path.home', return_value=self.root):
            launch(['forge.host_setup', 'status'])
            self.assertEqual(checked[-1], str(self.root / '.local/share/forge/state/storage'))
            os.environ['FORGE_ROOT'] = '/environment'
            launch(['forge.setup_service', '--root', '/first', '--root=/last'])
            self.assertEqual(checked[-1], '/last/state/storage')
            self.assertEqual(start.call_args.args[0], 'forge.setup_service')
            self.assertEqual(sys.argv, ['forge.setup_service', '--root', '/first', '--root=/last'])

    def test_qualified_start_checks_selected_store_before_script(self):
        events = []
        with patch('forge.store.db.qualified_sqlite', return_value=True), \
                patch('forge.launch.os.path.lexists', side_effect=lambda path: events.append('selected') or True), \
                patch('forge.launch.runpy.run_path', side_effect=lambda *a, **k: events.append('script')):
            launch(['--script', '/source/status.py', '--root', str(self.root)])
        self.assertEqual(events[-2:], ['selected', 'script'])
        with self.assertRaises(SystemExit):
            launch(['unrelated.module'])

    def test_unselected_root_starts_only_host_setup(self):
        with patch('forge.store.db.qualified_sqlite', return_value=True), \
                patch('forge.launch.runpy.run_path') as script, patch('forge.launch.runpy.run_module') as start:
            for arguments in (['--script', '/source/status.py', '--root', str(self.root)],
                              ['forge.setup_service', '--root', str(self.root)]):
                with self.subTest(arguments=arguments), \
                        self.assertRaisesRegex(SystemExit, 'No SQLite store is selected'):
                    launch(arguments)
            script.assert_not_called()
            start.assert_not_called()
            launch(['forge.host_setup', '--root', str(self.root)])
            self.assertEqual(start.call_args.args[0], 'forge.host_setup')



class HostRecordTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / 'runtime'
        self.root.mkdir(mode=0o700)

    def test_selected_host_without_operator_config_can_render_units_and_persist_record(self):
        database = selected_store(self.root)
        units = desired_units(self.root, '127.0.0.1')
        self.assertIn('ExecStart=/usr/bin/python3 -m forge.launch forge.status_api\n', units['forge-status'])
        self.assertNotIn('NITRO_FORGE_PUBLIC_ORIGIN', units['forge-ui'])
        Record(self.root).save(bind='127.0.0.1', source_revision='current')
        self.assertEqual(Record(self.root).data['source_revision'], 'current')
        self.assertFalse((self.root / 'host.json').exists())
        with database.read_snapshot() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM configuration WHERE domain='host.json' AND active=1").fetchone())

if __name__ == '__main__':
    unittest.main()
