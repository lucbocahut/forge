import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.store import db as storage
from forge.store import upgrade
from forge.store.migration.upgrade import Upgrade
from forge.store.migration.snapshot import MigrationError
from tests.test_schema_baseline import legacy_catalog


class SchemaUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = storage.Store.create(self.root/'sqlite')
        self.original = storage._migrations()

    def following(self, script):
        return [*self.original, (len(self.original)+1, hashlib.sha256(script.encode()).hexdigest(), script)]

    def test_runtime_refuses_old_schema_until_explicit_atomic_upgrade(self):
        script = 'CREATE TABLE upgrade_probe(value INTEGER);\nINSERT INTO upgrade_probe VALUES(42);\n'
        with patch('forge.store.db._migrations', return_value=self.following(script)):
            target = storage.Store(self.store.root)
            with self.assertRaisesRegex(storage.StoreError, 'explicit schema migration'):
                storage.Store.open(self.store.root)
            self.assertEqual(upgrade.inspect(target)['pending_migrations'], [len(self.original)+1])
            upgrade.apply(target)
            upgrade.apply(target)  # Repeating after a lost response does not reapply it.
            with self.assertRaises(storage.StoreError) as old_runtime:
                with self.store.read_snapshot():
                    pass
            self.assertEqual(old_runtime.exception.code, 'schema_newer')
            with storage.Store.open(self.store.root).read_snapshot() as db:
                self.assertEqual(db.execute('SELECT value FROM upgrade_probe').fetchall()[0][0], 42)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0], len(self.original)+1)

    def test_failed_migration_rolls_back_schema_data_and_version(self):
        script = 'CREATE TABLE upgrade_probe(value INTEGER);\nINSERT INTO missing_table VALUES(42);\n'
        with patch('forge.store.db._migrations', return_value=self.following(script)):
            target = storage.Store(self.store.root)
            with self.assertRaises(storage.sqlite3.DatabaseError):
                upgrade.apply(target)
        with storage.Store.open(self.store.root).read_snapshot() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='upgrade_probe'").fetchone())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0], len(self.original))

    def test_changed_history_and_anchor_are_rejected_before_upgrade(self):
        changed = [(n, 'incorrect' if n == 1 else checksum, script) for n, checksum, script in self.original]
        with patch('forge.store.db._migrations', return_value=changed):
            target = storage.Store(self.store.root)
            with self.assertRaises(storage.StoreError) as result:
                upgrade.apply(target)
            self.assertEqual(result.exception.code, 'schema_checksum')
        self.store.instance_id = 'different-instance'
        with self.assertRaises(storage.StoreError) as result:
            upgrade.inspect(self.store)
        self.assertEqual(result.exception.code, 'instance_mismatch')


class UpgradeHostTests(SchemaUpgradeTests):
    def setUp(self):
        super().setUp()
        from types import SimpleNamespace
        (self.root/'state').mkdir(mode=0o700)
        storage._new_file(self.root/'state/storage', ('forge-sqlite-v1\n'+str(self.store.root)+'\n'+self.store.instance_id+'\n').encode())
        with self.store.write() as db:
            db.executemany('INSERT INTO store_metadata VALUES(?,?)', [('runtime_root',str(self.root)),('runtime_active','true')])
        self.current = 'a'*40
        self.active = ['forge-status.service','forge-ui.service','forge-supervisor.service']
        self.calls = []
        self.fail_update = False
        def command(*args):
            self.calls.append(args)
            if 'stop' in args:
                self.active = []
            if 'start' in args:
                self.active = list(args[3:])
            return ''
        def install(previous, target):
            self.calls.append(('install', previous, target))
            self.current = target
        def run(*args, **kwargs):
            self.calls.append(('update',))
            return SimpleNamespace(returncode=int(self.fail_update))
        def qualify_advanced(*args):
            raise MigrationError('upgrade_source_changed')
        self.host = SimpleNamespace(root=self.root, source=self.root/'source', identity=lambda:self.current,
            units=lambda:list(self.active), prepare_source_update=lambda *args,**kwargs:None, install_source_update=install,
            command=command, run=run, quiet=lambda:None, qualify_advanced_source=qualify_advanced)
        self.upgrader = Upgrade(self.host)
        self.addCleanup(patch.stopall)
        patch('forge.store.migration.upgrade.package_version', return_value='tool').start()


    def test_resumed_drain_requalifies_source_before_fenced_probe_cleanup(self):
        with patch.object(self.upgrader,'blockers',return_value=['uncleaned_jobs']):
            self.assertEqual(self.upgrader.run(revision='b'*40,apply=True)['status'],'draining')
        with patch.object(self.host,'prepare_source_update',side_effect=MigrationError('source_changed')), \
                patch('forge.store.migration.probe_cleanup.cleanup_fenced_probe') as cleanup:
            with self.assertRaisesRegex(MigrationError,'source_changed'):
                self.upgrader.run(revision='b'*40,apply=True)
            cleanup.assert_not_called()
        self.assertEqual(self.upgrader.read()['stage'],'draining')

    def test_source_pack_reaches_both_preparations_without_entering_journal(self):
        pack={'data':'transport-only-pack','sha256':'transport-only-digest'}
        with patch.object(self.host,'prepare_source_update') as prepare:
            self.assertEqual(self.upgrader.run(revision='b'*40,apply=True,source_pack=pack)['status'],'upgraded')
        self.assertEqual(prepare.call_count,2)
        self.assertTrue(all(call.kwargs=={'source_pack':pack} for call in prepare.call_args_list))
        self.assertNotIn('transport-only',self.upgrader.path.read_text())

    def test_plan_is_read_only_and_apply_requires_exact_revision(self):
        self.assertEqual(self.upgrader.run(revision=None)['status'], 'planned')
        self.assertFalse(self.upgrader.path.exists())
        self.assertEqual(self.calls, [])
        with self.assertRaises(MigrationError):
            self.upgrader.run(revision='main', apply=True)
        self.assertEqual(self.calls, [])

    def test_lost_update_resumes_after_committed_schema_without_reapplying(self):
        script = 'CREATE TABLE upgrade_probe(value INTEGER);\nINSERT INTO upgrade_probe VALUES(42);\n'
        with patch('forge.store.db._migrations', return_value=self.following(script)):
            target = storage.Store(self.store.root)
            self.fail_update = True
            with self.assertRaisesRegex(MigrationError, 'upgrade_host_update_failed'):
                self.upgrader.run(revision='b'*40, apply=True)
            self.assertEqual(self.upgrader.read()['stage'], 'reconciling')
            self.assertEqual(self.active, [])
            with target.read_snapshot() as db:
                self.assertEqual(db.execute('SELECT value FROM upgrade_probe').fetchone()[0], 42)
            self.fail_update = False
            self.assertEqual(self.upgrader.run(revision='b'*40, apply=True)['status'], 'upgraded')
            self.assertEqual(len(self.active), 3)
            self.assertEqual(self.upgrader.read()['stage'], 'complete')
            self.assertEqual(len([x for x in self.calls if x[0]=='install']), 1)

    def test_worker_race_restores_services_before_source_or_schema_changes(self):
        with patch.object(self.upgrader, 'blockers', side_effect=[[], [], ['uncleaned_jobs'], ['uncleaned_jobs']]):
            result = self.upgrader.run(revision='b'*40, apply=True)
        self.assertEqual(result['status'], 'draining')
        self.assertEqual(self.current, 'a'*40)
        self.assertEqual(len(self.active), 3)
        self.assertEqual(self.upgrader.read()['stage'], 'draining')
        self.assertFalse(any(x[0] in {'update','install'} for x in self.calls))

    def test_explicit_new_release_repairs_failed_deployment(self):
        self.fail_update = True
        with self.assertRaises(MigrationError):
            self.upgrader.run(revision='b'*40, apply=True)
        self.fail_update = False
        result = self.upgrader.run(revision='c'*40, apply=True)
        self.assertEqual(result['status'], 'upgraded')
        self.assertEqual(self.current, 'c'*40)
        self.assertEqual(len(self.active), 3)

    def test_completed_upgrade_allows_new_maintenance_after_application_release(self):
        self.runtime(enabled=True, model='original')
        with self.store.write() as db:
            db.execute("INSERT INTO store_metadata VALUES('retained-runtime-data','preserved')")
            history = [tuple(row) for row in db.execute('SELECT * FROM schema_migrations ORDER BY version')]
        instance = self.store.instance_id
        self.upgrader.run(revision='b'*40, apply=True)
        # An ordinary application deployment advances source without changing
        # the last completed schema journal. Its current settings/services own
        # the next maintenance boundary, not the completed journal's receipts.
        self.current = 'c'*40
        self.active = ['forge-status.service', 'forge-supervisor.service']
        self.runtime(enabled=False, model='operator-choice')
        self.calls.clear()
        with patch.object(self.host, 'prepare_source_update') as prepare, \
                patch.object(storage.Store, 'create', side_effect=AssertionError('must not reset storage')):
            result = self.upgrader.run(revision='d'*40, apply=True)
        self.assertEqual(result['status'], 'upgraded')
        self.assertEqual(prepare.call_args_list[0].args, ('c'*40, 'd'*40, 'tool'))
        self.assertEqual(prepare.call_args_list[1].args, ('c'*40, 'd'*40, 'tool'))
        self.assertIn(('install', 'c'*40, 'd'*40), self.calls)
        journal = self.upgrader.read()
        self.assertEqual(journal['previous_revision'], 'c'*40)
        self.assertEqual(journal['source_revision'], 'd'*40)
        self.assertEqual(journal['services'], ['forge-status.service', 'forge-supervisor.service'])
        self.assertFalse(journal['drain']['original_enabled'])
        self.assertEqual(self.runtime()[1], {'enabled':False, 'model':'operator-choice'})
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT value FROM store_metadata WHERE key='retained-runtime-data'").fetchone()[0], 'preserved')
            self.assertEqual(db.execute("SELECT value FROM store_metadata WHERE key='instance_id'").fetchone()[0], instance)
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM schema_migrations ORDER BY version')], history)
        # Lost-response replay retains completed same-intent behavior.
        self.calls.clear()
        with patch.object(self.host, 'prepare_source_update') as prepare:
            self.assertEqual(self.upgrader.run(revision='d'*40, apply=True)['status'], 'upgraded')
        prepare.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_completed_replay_does_not_claim_or_restore_a_superseded_source(self):
        self.upgrader.run(revision='b'*40, apply=True)
        saved = self.upgrader.path.read_bytes()
        self.current = 'c'*40
        self.calls.clear()
        with patch.object(self.host, 'prepare_source_update') as prepare:
            with self.assertRaisesRegex(MigrationError, 'upgrade_source_changed'):
                self.upgrader.run(revision='b'*40, apply=True)
        prepare.assert_not_called()
        self.assertEqual(self.current, 'c'*40)
        self.assertEqual(self.upgrader.path.read_bytes(), saved)
        self.assertEqual(self.calls, [])

    def test_completed_journal_does_not_bypass_new_source_qualification(self):
        self.upgrader.run(revision='b'*40, apply=True)
        saved = self.upgrader.path.read_bytes()
        self.current = 'c'*40
        self.calls.clear()
        with patch.object(self.host, 'prepare_source_update', side_effect=MigrationError('source_not_ancestor')) as prepare:
            with self.assertRaisesRegex(MigrationError, 'source_not_ancestor'):
                self.upgrader.run(revision='d'*40, apply=True)
        prepare.assert_called_once_with('c'*40, 'd'*40, 'tool')
        self.assertEqual(self.upgrader.path.read_bytes(), saved)
        self.assertEqual(self.calls, [])

    def test_unfinished_forward_repair_rejects_unowned_source(self):
        self.fail_update = True
        with self.assertRaisesRegex(MigrationError, 'upgrade_host_update_failed'):
            self.upgrader.run(revision='b'*40, apply=True)
        saved = self.upgrader.path.read_bytes()
        self.current = 'c'*40
        self.calls.clear()
        with patch.object(self.host, 'prepare_source_update') as prepare:
            with self.assertRaisesRegex(MigrationError, 'upgrade_source_changed'):
                self.upgrader.run(revision='d'*40, apply=True)
        prepare.assert_not_called()
        self.assertEqual(self.upgrader.path.read_bytes(), saved)
        self.assertEqual(self.calls, [])

    def test_resume_revalidates_source_before_reconciling(self):
        self.fail_update = True
        with self.assertRaises(MigrationError):
            self.upgrader.run(revision='b'*40, apply=True)
        self.calls.clear()
        self.current = 'c'*40
        with self.assertRaisesRegex(MigrationError, 'upgrade_source_changed'):
            self.upgrader.run(revision='b'*40, apply=True)
        self.assertEqual(self.calls, [])

    def advanced_checkout(self):
        self.runtime(enabled=True, model='original')
        self.fail_update = True
        with self.assertRaisesRegex(MigrationError, 'upgrade_host_update_failed'):
            self.upgrader.run(revision='b'*40, apply=True)
        self.fail_update = False
        self.current = 'c'*40
        self.active = ['forge-supervisor.service', 'forge-extra.timer']
        self.calls.clear()

    def test_advanced_checkout_reenters_maintenance_and_preserves_drain_ownership(self):
        self.advanced_checkout()
        with patch.object(self.host, 'qualify_advanced_source') as qualify, \
                patch.object(self.host, 'prepare_source_update') as prepare:
            result = self.upgrader.run(revision='d'*40, apply=True)
        qualify.assert_called_once_with('b'*40, 'c'*40)
        self.assertEqual(prepare.call_args_list[0].args, ('c'*40, 'd'*40, 'tool'))
        self.assertEqual(result['status'], 'upgraded')
        self.assertIn(('install', 'c'*40, 'd'*40), self.calls)
        self.assertIn(('update',), self.calls)
        self.assertTrue(self.runtime()[1]['enabled'])
        self.assertEqual(set(self.active), {'forge-status.service', 'forge-ui.service',
                                          'forge-supervisor.service', 'forge-extra.timer'})
        self.assertEqual(self.upgrader.read()['superseded'], dict(source_revision='b'*40,
                         stage='reconciling', observed_revision='c'*40))

    def test_advanced_checkout_waits_for_workers_and_resumes_new_intent(self):
        self.advanced_checkout()
        with patch.object(self.host, 'qualify_advanced_source'), \
                patch.object(self.upgrader, 'blockers', return_value=['uncleaned_jobs']):
            self.assertEqual(self.upgrader.run(revision='d'*40, apply=True)['status'], 'draining')
        self.assertEqual(self.calls, [])
        self.assertFalse(self.runtime()[1]['enabled'])
        with patch.object(self.host, 'qualify_advanced_source', side_effect=AssertionError('already adopted')):
            self.assertEqual(self.upgrader.run(revision='d'*40, apply=True)['status'], 'upgraded')
        self.assertTrue(self.runtime()[1]['enabled'])
        self.assertEqual(sum(call[0] == 'install' for call in self.calls), 1)

    def test_advanced_checkout_keeps_journal_when_target_qualification_fails(self):
        self.advanced_checkout()
        saved = self.upgrader.path.read_bytes()
        config = self.runtime()
        with patch.object(self.host, 'qualify_advanced_source'), \
                patch.object(self.host, 'prepare_source_update', side_effect=MigrationError('repair_requires_forward_revision')):
            with self.assertRaisesRegex(MigrationError, 'repair_requires_forward_revision'):
                self.upgrader.run(revision='d'*40, apply=True)
        self.assertEqual(self.upgrader.path.read_bytes(), saved)
        self.assertEqual(self.runtime(), config)
        self.assertEqual(self.calls, [])

    def test_advanced_checkout_does_not_override_operator_reenable(self):
        self.advanced_checkout()
        self.runtime(enabled=True, model='operator-choice')
        config = self.runtime()
        with patch.object(self.host, 'qualify_advanced_source'):
            with self.assertRaisesRegex(MigrationError, 'drain_configuration_changed'):
                self.upgrader.run(revision='d'*40, apply=True)
        self.assertEqual(self.runtime(), config)
        self.assertEqual(self.calls, [])

    def test_advanced_checkout_before_schema_commit_is_rejected(self):
        with patch.object(self.upgrader, 'blockers', return_value=['uncleaned_jobs']):
            self.upgrader.run(revision='b'*40, apply=True)
        self.current = 'c'*40
        saved = self.upgrader.path.read_bytes()
        with patch.object(self.host, 'qualify_advanced_source') as qualify:
            with self.assertRaisesRegex(MigrationError, 'upgrade_source_changed'):
                self.upgrader.run(revision='d'*40, apply=True)
        qualify.assert_not_called()
        self.assertEqual(self.upgrader.path.read_bytes(), saved)

    def runtime(self, **changes):
        from forge.store.configuration import Configuration
        from forge.store.migration.drain import Drain
        with upgrade.connection(self.store, write=bool(changes)) as db:
            revision, settings, credential = Drain.current(db)
            if changes:
                db.execute('BEGIN IMMEDIATE')
                revision = Configuration(self.store).replace_in(db, 'runtime.json', {**settings, **changes},
                    expected_revision=revision, credential_ref=credential)
                db.commit()
                settings.update(changes)
            return revision, settings

    def test_apply_drains_then_restores_only_owned_field_after_updater(self):
        self.runtime(enabled=True, model='before')
        workers = [True]
        with patch.object(self.upgrader, 'blockers', side_effect=lambda store:['uncleaned_jobs'] if workers[0] else []):
            result = self.upgrader.run(revision='b'*40, apply=True)
            self.assertEqual(result['status'], 'draining')
            self.assertEqual(result['next_command'], 'forge migrate upgrade')
            self.assertEqual(self.runtime()[1], dict(enabled=False, model='before'))
            self.assertEqual(self.calls, [])
            self.assertEqual(len(self.active), 3)
            self.runtime(model='operator-choice')
            workers[0] = False
            def update(*args, **kwargs):
                from types import SimpleNamespace
                # Agents stay off until the host converged.
                self.assertEqual(self.runtime()[1], dict(enabled=False, model='operator-choice'))
                return SimpleNamespace(returncode=0)
            with patch.object(self.host, 'run', side_effect=update):
                result = self.upgrader.run(revision='b'*40, apply=True)
        self.assertEqual(result['status'], 'upgraded')
        self.assertEqual(self.runtime()[1], dict(enabled=True, model='operator-choice'))
        self.assertTrue(self.upgrader.read()['drain']['restored'])

    def test_operator_reenable_is_preserved_and_requires_explicit_disable(self):
        self.runtime(enabled=True)
        with patch.object(self.upgrader, 'blockers', return_value=['uncleaned_jobs']):
            self.upgrader.run(revision='b'*40, apply=True)
            self.runtime(enabled=True, model='operator-choice')
            before = self.runtime()
            with self.assertRaisesRegex(MigrationError, 'drain_configuration_changed'):
                self.upgrader.run(revision='b'*40, apply=True)
            self.assertEqual(self.runtime(), before)
            self.assertEqual(self.calls, [])
            self.runtime(enabled=False)
            self.assertEqual(self.upgrader.run(revision='b'*40, apply=True)['status'], 'draining')

    def test_disable_commit_is_recognized_after_lost_progress(self):
        self.runtime(enabled=True)
        with patch.object(self.upgrader, 'blockers', side_effect=[[], OSError('disconnect')]):
            with self.assertRaises(OSError):self.upgrader.run(revision='b'*40, apply=True)
        revision, settings = self.runtime()
        self.assertFalse(settings['enabled'])
        with patch.object(self.upgrader, 'blockers', return_value=['uncleaned_jobs']):
            self.assertEqual(self.upgrader.run(revision='b'*40, apply=True)['status'], 'draining')
        self.assertEqual(self.runtime()[0], revision)

    def test_disable_rollback_after_journal_is_resumable(self):
        from forge.store.configuration import Configuration
        self.runtime(enabled=True)
        original = Configuration.replace_in
        def interrupt(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('disconnect before commit')
        with patch.object(Configuration, 'replace_in', side_effect=interrupt, autospec=True):
            with self.assertRaises(OSError):self.upgrader.run(revision='b'*40, apply=True)
        self.assertTrue(self.runtime()[1]['enabled'])
        self.assertEqual(self.upgrader.read()['stage'], 'draining')
        with patch.object(self.upgrader, 'blockers', return_value=['uncleaned_jobs']):
            self.assertEqual(self.upgrader.run(revision='b'*40, apply=True)['status'], 'draining')
        self.assertFalse(self.runtime()[1]['enabled'])

    def test_restore_commit_is_not_replayed_over_a_later_operator_change(self):
        self.runtime(enabled=True)
        save = self.upgrader.save
        def interrupt(value, stage):
            if value.get('drain', {}).get('restored'):
                raise OSError('disconnect after restoration commit')
            return save(value, stage)
        with patch.object(self.upgrader, 'save', side_effect=interrupt):
            with self.assertRaises(OSError):self.upgrader.run(revision='b'*40, apply=True)
        self.assertTrue(self.runtime()[1]['enabled'])
        self.assertEqual(self.upgrader.read()['stage'], 'reconciling')
        self.runtime(enabled=False, model='operator-choice')
        revision = self.runtime()[0]
        self.upgrader.run(revision='b'*40, apply=True)
        self.assertEqual(self.runtime(), (revision, dict(enabled=False, model='operator-choice')))

    def test_new_release_inherits_unfinished_drain_and_restores_original_intent(self):
        self.runtime(enabled=True)
        with patch.object(self.upgrader, 'blockers', return_value=['uncleaned_jobs']):
            self.upgrader.run(revision='b'*40, apply=True)
        self.assertFalse(self.runtime()[1]['enabled'])
        self.upgrader.run(revision='c'*40, apply=True)
        self.assertTrue(self.runtime()[1]['enabled'])
        self.assertEqual(self.current, 'c'*40)

    def test_legacy_041_store_drains_before_target_binary_can_open_it_then_rebases(self):
        with patch('forge.store.db._migrations', return_value=legacy_catalog()):
            self.store = storage.Store.create(self.root/'schema41')
            with self.store.write() as db:
                db.executemany('INSERT INTO store_metadata VALUES(?,?)', [('runtime_root',str(self.root)),('runtime_active','true')])
        (self.root/'state/storage').write_text('forge-sqlite-v1\n'+str(self.store.root)+'\n'+self.store.instance_id+'\n')
        self.runtime(enabled=True)
        with patch.object(self.upgrader, 'blockers', return_value=['uncleaned_jobs']):
            first = self.upgrader.run(revision='b'*40, apply=True)
        self.assertEqual((first['current_schema'], first['legacy_history']), (41, True))
        self.assertEqual(first['status'], 'draining')
        self.assertFalse(self.runtime()[1]['enabled'])
        self.assertEqual(self.upgrader.run(revision='b'*40, apply=True)['status'], 'upgraded')
        self.assertTrue(self.runtime()[1]['enabled'])
        self.assertEqual(upgrade.inspect(storage.Store(self.store.root))['current_schema'], len(self.original))

    def test_legacy_busy_drain_preserves_completed_journal_and_can_retry(self):
        from forge.store.migration.host import failure
        with patch('forge.store.db._migrations', return_value=legacy_catalog()):
            self.store = storage.Store.create(self.root/'schema41')
            with self.store.write() as db:
                db.executemany('INSERT INTO store_metadata VALUES(?,?)',
                    [('runtime_root',str(self.root)),('runtime_active','true')])
        (self.root/'state/storage').write_text('forge-sqlite-v1\n'+str(self.store.root)+'\n'+self.store.instance_id+'\n')
        self.runtime(enabled=True)
        self.upgrader.save(dict(protocol=1, root=str(self.root), source=str(self.host.source),
            authorized=True, previous_revision='9'*40, source_revision=self.current,
            tool_version='previous', services=list(self.active)), 'complete')
        previous = self.upgrader.path.read_bytes()
        with upgrade.connection(self.store, write=True) as writer, patch.object(storage.Store, 'busy_timeout_ms', 1):
            writer.execute('BEGIN IMMEDIATE')
            with self.assertRaises(storage.sqlite3.OperationalError) as raised:
                self.upgrader.run(revision='b'*40, apply=True)
            receipt = failure(raised.exception, dict(root=str(self.root), action='upgrade'))
            self.assertEqual(receipt, dict(ok=False, error='migration_store_busy',
                exception_type='OperationalError', stage='complete', sqlite_errorcode=5,
                sqlite_errorname='SQLITE_BUSY', retryable=True))
            self.assertEqual(self.upgrader.path.read_bytes(), previous)
            self.assertEqual(self.calls, [])
            self.assertTrue(self.runtime()[1]['enabled'])
            writer.rollback()
        self.assertEqual(self.upgrader.run(revision='b'*40, apply=True)['status'], 'upgraded')
        self.assertTrue(self.runtime()[1]['enabled'])
        self.assertEqual(upgrade.inspect(storage.Store(self.store.root))['current_schema'], len(self.original))

    def test_partially_restarted_updater_drains_new_workers_before_retry(self):
        from types import SimpleNamespace
        self.runtime(enabled=True)
        def partial_update(*args, **kwargs):
            self.active = ['forge-supervisor.service', 'forge-extra.timer']
            return SimpleNamespace(returncode=1)
        with patch.object(self.host, 'run', side_effect=partial_update):
            with self.assertRaisesRegex(MigrationError, 'upgrade_host_update_failed'):
                self.upgrader.run(revision='b'*40, apply=True)
        self.assertFalse(self.runtime()[1]['enabled'])
        self.calls.clear()
        with patch.object(self.upgrader, 'blockers', return_value=['uncleaned_jobs']):
            result = self.upgrader.run(revision='b'*40, apply=True)
        self.assertEqual(result['status'], 'draining')
        self.assertFalse(self.runtime()[1]['enabled'])
        self.assertEqual(self.current, 'b'*40)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.active, ['forge-supervisor.service', 'forge-extra.timer'])
        result = self.upgrader.run(revision='b'*40, apply=True)
        self.assertEqual(result['status'], 'upgraded')
        self.assertTrue(self.runtime()[1]['enabled'])
        self.assertEqual(set(self.active), {'forge-status.service','forge-ui.service','forge-supervisor.service','forge-extra.timer'})


    def hold_controller_lease(self, seconds):
        """Stand in for a supervisor cycle that owns the lease for a while."""
        import fcntl
        import threading
        stream = open(self.root/'state-supervisor.lock', 'a')
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def release():
            if not stream.closed:
                fcntl.flock(stream, fcntl.LOCK_UN)
                stream.close()
        timer = threading.Timer(seconds, release)
        timer.start()
        self.addCleanup(release)
        self.addCleanup(timer.join)
        self.addCleanup(timer.cancel)

    def test_transient_updater_failure_finishes_in_one_retry_without_redrain(self):
        from types import SimpleNamespace
        from forge.store.migration.host import failure
        self.runtime(enabled=True, model='before')
        token = 'ghp_' + 'X' * 21
        outputs = [(1, 'Updating: restart services for source bbbbbbbbbbbb\n'
                       'Error response from daemon: No such container: forge-ui-0123456789ab\n'
                       f'token {token} was rejected\n'),
                   (0, 'Forge host is up to date at bbbbbbbbbbbb.\n')]
        def updater(argv, **kwargs):
            self.calls.append(('update',))
            returncode, output = outputs.pop(0)
            # The failed run already restarted the Python services.
            self.active = sorted(set(self.active) | {'forge-status.service', 'forge-supervisor.service'})
            self.assertFalse(self.runtime()[1]['enabled'])
            return SimpleNamespace(returncode=returncode, stdout=output)
        with patch.object(self.host, 'run', side_effect=updater):
            with self.assertRaises(MigrationError) as raised:
                self.upgrader.run(revision='b'*40, apply=True)
            error = raised.exception
            self.assertEqual(error.code, 'upgrade_host_update_failed')
            self.assertIn('No such container: forge-ui-0123456789ab', error.detail)
            self.assertNotIn('ghp_', error.detail)
            self.assertIn('token [REDACTED] was rejected', error.detail)
            receipt = self.upgrader.read()
            self.assertEqual(receipt['stage'], 'reconciling')
            self.assertEqual(receipt['updater']['returncode'], 1)
            self.assertEqual(receipt['updater']['output_tail'], error.detail)
            reply = failure(error, dict(root=str(self.root), action='upgrade'))
            self.assertEqual((reply['error'], reply['stage'], reply['detail']),
                             ('upgrade_host_update_failed', 'reconciling', error.detail))
            # Agents stay off: the restarted supervisor took no new work.
            self.assertEqual(self.runtime()[1], dict(enabled=False, model='before'))
            self.calls.clear()
            # The supervisor is mid-cycle when the operator retries.
            self.hold_controller_lease(0.3)
            result = self.upgrader.run(revision='b'*40, apply=True)
        self.assertEqual(result['status'], 'upgraded')
        self.assertEqual(outputs, [])
        self.assertEqual([call for call in self.calls if call[0] == 'update'], [('update',)])
        self.assertFalse(any('stop' in call for call in self.calls))
        self.assertEqual(self.runtime()[1], dict(enabled=True, model='before'))
        self.assertEqual(set(self.active), {'forge-status.service', 'forge-ui.service', 'forge-supervisor.service'})
        receipt = self.upgrader.read()
        self.assertEqual((receipt['stage'], receipt['updater']['returncode']), ('complete', 0))
        self.assertTrue(receipt['drain']['restored'])

    def test_retry_redrains_only_when_a_worker_started_since_the_stop(self):
        from types import SimpleNamespace
        self.runtime(enabled=True)
        def failed(*args, **kwargs):
            self.active = ['forge-supervisor.service']
            return SimpleNamespace(returncode=1, stdout='')
        with patch.object(self.host, 'run', side_effect=failed):
            with self.assertRaises(MigrationError) as raised:
                self.upgrader.run(revision='b'*40, apply=True)
        self.assertIsNone(raised.exception.detail)
        self.calls.clear()
        # A worker the restarted supervisor started (e.g. an auxiliary check).
        with patch.object(self.upgrader, 'blockers', return_value=['uncleaned_jobs']):
            self.assertEqual(self.upgrader.run(revision='b'*40, apply=True)['status'], 'draining')
        self.assertFalse(any(call[0] == 'update' for call in self.calls))
        self.assertFalse(self.runtime()[1]['enabled'])
        self.assertEqual(self.upgrader.run(revision='b'*40, apply=True)['status'], 'upgraded')
        self.assertTrue(self.runtime()[1]['enabled'])

    def test_drain_waits_out_a_supervisor_cycle_holding_the_controller_lease(self):
        import time
        self.runtime(enabled=True)
        self.hold_controller_lease(0.5)
        started = time.monotonic()
        self.assertEqual(self.upgrader.run(revision='b'*40, apply=True)['status'], 'upgraded')
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        self.assertTrue(self.runtime()[1]['enabled'])

    def test_lease_held_past_the_bound_is_a_retryable_busy_without_side_effects(self):
        from forge.store.migration.host import failure
        self.runtime(enabled=True)
        self.hold_controller_lease(5)
        with patch('forge.store.migration.drain.LEASE_WAIT_SECONDS', 0.3):
            with self.assertRaises(MigrationError) as raised:
                self.upgrader.run(revision='b'*40, apply=True)
        self.assertEqual(raised.exception.code, 'controller_busy')
        reply = failure(raised.exception, dict(root=str(self.root), action='upgrade'))
        self.assertEqual((reply['error'], reply['retryable']), ('controller_busy', True))
        self.assertTrue(self.runtime()[1]['enabled'])
        self.assertEqual(self.calls, [])


class UpgradeTransportTests(unittest.TestCase):
    def test_unexpected_failure_diagnostics_report_upgrade_stage_without_exception_text(self):
        import sqlite3
        from forge.store.migration.host import failure
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            value = failure(sqlite3.IntegrityError('secret row content'), {'root': str(root)})
            self.assertEqual(value['stage'], 'before_journal')
            (root/'.schema-upgrade.json').write_text(json.dumps({'stage': 'migrating'}))
            value = failure(sqlite3.IntegrityError('secret row content'), {'root': str(root)})
            self.assertEqual(value, dict(ok=False, error='unexpected_migration_failure',
                                         exception_type='IntegrityError', stage='migrating'))
            self.assertNotIn('secret', json.dumps(value))
            self.assertEqual(failure(MigrationError('upgrade_source_changed'), {'root': str(root)})['error'],
                             'upgrade_source_changed')

    def test_host_runs_only_the_upgrade_action(self):
        from forge.store.migration.host import run
        for action in ('plan', 'prepare', 'capture', 'resume', 'activate'):
            with self.subTest(action=action), self.assertRaisesRegex(MigrationError, 'unknown_migration_action'):
                run({'action': action, 'root': '/private/runtime', 'source': '/private/source'})

    def test_only_clean_forward_matching_source_is_installed(self):
        import subprocess
        from forge.store.migration.host import Host
        from forge.store.migration.upgrade import package_version
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve(); source = base/'source'; source.mkdir()
            def git(*args):
                return subprocess.run(['git', '-C', str(source), *args], capture_output=True, text=True, check=True).stdout.strip()
            git('init', '-q'); git('config', 'commit.gpgsign', 'false')
            git('config', 'user.email', 'test@example.invalid'); git('config', 'user.name', 'Test')
            (source/'forge').mkdir(); (source/'forge/__init__.py').write_text('version=1\n')
            (source/'forge_client').mkdir(); (source/'forge_client/__init__.py').write_text('# contracts\n')
            (source/'forge_client/contracts.py').write_text('version=1\n')
            git('add', '.'); git('commit', '-qm', 'first'); previous = git('rev-parse', 'HEAD')
            (source/'forge/__init__.py').write_text('version=2\n')
            git('commit', '-qam', 'corrected'); target = git('rev-parse', 'HEAD'); version = package_version(source)
            origin = base/'origin.git'
            subprocess.run(['git', 'clone', '--bare', str(source), str(origin)], capture_output=True, check=True)
            git('remote', 'add', 'origin', str(origin)); git('checkout', '-q', previous)
            (source/'existing-diagnostic.md').write_text('keep this')
            host = Host(base, source); host.identity = lambda: git('rev-parse', 'HEAD')
            with self.assertRaisesRegex(MigrationError, 'repair_bundle_source_mismatch'):
                host.prepare_source_update(previous, target, '1:wrong')
            self.assertEqual(git('rev-parse', 'HEAD'), previous)
            host.prepare_source_update(previous, target, version)
            host.install_source_update(previous, target)
            self.assertEqual(git('rev-parse', 'HEAD'), target)
            host.qualify_advanced_source(previous, target)
            with self.assertRaisesRegex(MigrationError, 'upgrade_source_changed'):
                host.qualify_advanced_source(previous, previous)
            self.assertEqual((source/'existing-diagnostic.md').read_text(), 'keep this')
            host.install_source_update(previous, target)
            (source/'forge_client/contracts.py').write_text('version=999\n')
            changed_contract_version=package_version(source)
            git('checkout','--','forge_client/contracts.py')
            with self.assertRaisesRegex(MigrationError,'repair_bundle_source_mismatch'):
                host.prepare_source_update(previous,target,changed_contract_version)
            git('rm','forge_client/contracts.py');git('commit','-qm','missing shared contract')
            missing_contract_target=git('rev-parse','HEAD');git('checkout','-q',target)
            with self.assertRaisesRegex(MigrationError, 'upgrade_source_changed'):
                host.qualify_advanced_source(missing_contract_target, target)
            with self.assertRaisesRegex(MigrationError,'repair_source_unavailable'):
                host.prepare_source_update(target,missing_contract_target,version)
            self.assertEqual(git('rev-parse','HEAD'),target)
            with self.assertRaisesRegex(MigrationError, 'repair_requires_forward_revision'):
                host.prepare_source_update(target, previous, version)
            (source/'forge/__init__.py').write_text('dirty\n')
            with self.assertRaisesRegex(MigrationError, 'repair_source_not_clean'):
                host.qualify_advanced_source(previous, target)
            with self.assertRaisesRegex(MigrationError, 'repair_source_not_clean'):
                host.prepare_source_update(target, target, version)


class UpgradeHostDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_sqlite_failure_reports_only_structured_diagnostics(self):
        import sqlite3
        from forge.store.migration.host import failure
        with sqlite3.connect(':memory:') as db:
            with self.assertRaises(sqlite3.OperationalError) as raised:
                db.execute('SELECT * FROM private_customer_table')
        receipt = failure(raised.exception, dict(root=str(self.root), action='upgrade'))
        self.assertEqual(receipt['error'], 'unexpected_migration_failure')
        self.assertEqual(receipt['sqlite_errorcode'], sqlite3.SQLITE_ERROR)
        self.assertEqual(receipt['sqlite_errorname'], 'SQLITE_ERROR')
        self.assertNotIn('retryable', receipt)
        self.assertNotIn('private_customer_table', json.dumps(receipt))
        self.assertNotIn('no such table', json.dumps(receipt))

    def test_mirror_writer_is_inventoried_without_a_separate_service_list(self):
        from types import SimpleNamespace
        rows = [{'unit': 'forge-mirror.service', 'active': 'active'},
                {'unit': 'forge-supervisor.service', 'active': 'active'},
                {'unit': 'unrelated.service', 'active': 'active'}]
        from forge.store.migration.host import Host
        host = Host(self.root, self.root / 'source', run=lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(rows)))
        self.assertEqual(host.units(), ['forge-mirror.service', 'forge-supervisor.service'])
