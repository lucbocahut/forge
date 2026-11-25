import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.app_access import (AppClient, AccessError, ROLE_PERMISSIONS, OPTIONAL_PERMISSIONS,
                              missing_permissions, recommended_permissions)
from forge.settings import Settings, DEFAULT, ROLES, digest, read_json, validate
from forge.store.configuration import ConfigurationConflict
from tests.test_sqlite_identity import selected_store


def configured():
    config = copy.deepcopy(DEFAULT)
    config['connections'] = {role: {'app_id': n, 'installation_id': n + 100,
                                   'private_key_env': 'FORGE_' + role.upper() + '_KEY_FILE'}
                             for n, role in enumerate(ROLES, 1)}
    return config


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = selected_store(self.root)
        self.client = Mock()
        self.now = 1000
        self.settings = Settings(self.root, client=self.client, clock=lambda: self.now)
        self.config = configured()
        self.settings.save_config(self.config)
        def discover(connection):
            role = ROLES[connection['app_id'] - 1]
            return {'repositories': [{'id': 10, 'name': 'example/project', 'archived': False}],
                    'permissions': ROLE_PERMISSIONS[role], 'account': 'example',
                    'app_slug': 'example-' + role,
                    'installation_url': 'https://github.com/settings/installations/101'}
        self.client.discover.side_effect = discover

    def test_saved_repository_projection_does_not_open_credentials(self):
        from forge.factory.config import validate as runtime_config
        with patch('forge.credentials.Credentials.environment', side_effect=AssertionError('read-only view opened credentials')), \
                patch('forge.settings.AppClient', side_effect=AssertionError('read-only view created client')):
            settings = Settings(self.root, clock=lambda: self.now)
            settings.repository_views(runtime=runtime_config({}))

    def test_explicit_refresh_initializes_one_client_before_parallel_discovery(self):
        with patch('forge.credentials.Credentials.environment', return_value={'QUALIFIED': 'local'}) as environment, \
                patch('forge.settings.AppClient', return_value=self.client) as client:
            settings = Settings(self.root, clock=lambda: self.now)
            environment.assert_not_called(); client.assert_not_called()
            self.assertTrue(settings.refresh())
            self.assertTrue(settings.refresh())
            environment.assert_called_once_with()
            client.assert_called_once_with(environ={'QUALIFIED': 'local'})
            self.assertEqual(self.client.discover.call_count, 8)

    def prepare(self, identifier=10, name='example/project'):
        from forge.repository_setup import RepositorySetup, observation_key
        row={'id':identifier,'name':name}
        RepositorySetup(self.settings).save_observation(identifier, {'repositories':{str(identifier):{
            'revision':observation_key(self.settings.load(),row),'name':name,'status':'ready','started_at':self.now,
            'report':{'observed_at':self.now,'available':True,'branch':'main',
                      'requirements':[{'id':'reviews','passed':True},{'id':'stale','passed':True}]}}}})

    def enable(self):
        config = copy.deepcopy(self.config)
        config['repositories'] = [{'id': 10, 'name': 'example/project', 'enabled': True}]
        return self.settings.save(config, digest(self.settings.load()))

    def test_discovery_never_enables_repositories(self):
        self.settings.refresh()
        row = self.settings.view()['repositories'][0]
        self.assertTrue(row['access_ready']); self.assertFalse(row['enabled'])
        self.assertFalse(row['dispatch_allowed'])
        self.assertEqual(self.settings.load()['repositories'], [])
        with self.assertRaisesRegex(ValueError, 'finish repository setup'):
            self.enable()
        self.prepare()
        row = self.enable()['repositories'][0]
        self.assertTrue(row['dispatch_allowed'])

    def test_enable_requires_all_roles_and_permissions(self):
        self.settings.refresh()
        observed = self.settings.observation()
        observed['roles']['audit']['permissions'] = {'contents': 'read'}
        self.settings.save_observation(observed)
        with self.assertRaisesRegex(ValueError, 'verify access'):
            self.enable()
        self.assertEqual(self.settings.view()['repositories'][0]['access']['audit'], 'missing_permissions')

    def test_failed_sync_keeps_history_and_intent_but_blocks_dispatch(self):
        self.settings.refresh(); self.prepare(); self.enable()
        self.client.discover.side_effect = AccessError('GitHub could not be reached')
        self.settings.refresh()
        row = self.settings.view()['repositories'][0]
        self.assertTrue(row['enabled']); self.assertFalse(row['dispatch_allowed'])
        self.assertEqual(row['access']['build'], 'unknown')
        self.assertEqual(row['name'], 'example/project')
        self.assertFalse(self.settings.admission('example/project')['allowed'])

    def test_confirmed_removal_differs_from_unknown(self):
        self.settings.refresh(); self.prepare(); self.enable()
        self.client.discover.side_effect = lambda c: {'repositories': [], 'permissions': ROLE_PERMISSIONS[ROLES[c['app_id']-1]]}
        self.settings.refresh()
        row = self.settings.view()['repositories'][0]
        self.assertEqual(row['access']['build'], 'missing_access')
        self.assertTrue(row['enabled'])

    def test_archive_blocks_activation_and_existing_dispatch(self):
        self.settings.refresh(); self.prepare(); self.enable()
        observed = self.settings.observation()
        observed['roles']['build']['repositories'][0]['archived'] = True
        self.settings.save_observation(observed)
        self.assertEqual(self.settings.view()['repositories'][0]['access']['build'], 'archived')
        self.assertFalse(self.settings.admission('example/project')['allowed'])

    def test_expired_or_future_observation_cannot_authorize_dispatch(self):
        self.settings.refresh(); self.prepare(); self.enable()
        for now in (3000, 999):
            self.now = now
            self.assertFalse(self.settings.admission('example/project')['allowed'])
            self.assertEqual(self.settings.view()['roles']['build']['status'], 'stale')

    def test_replacement_app_cannot_reuse_previous_inventory(self):
        self.settings.refresh(); self.prepare(); self.enable()
        config = self.settings.load(); config['connections']['build']['app_id'] = 90
        self.settings.save(config, digest(self.settings.load()))
        self.assertEqual(self.settings.view()['roles']['build']['status'], 'unknown')
        self.assertFalse(self.settings.admission('example/project')['allowed'])

    def test_config_change_during_sync_cannot_authorize_old_identity(self):
        previous = self.client.discover.side_effect
        def discover(connection):
            if connection['app_id'] == 1:
                changed = copy.deepcopy(self.config)
                changed['connections']['build']['app_id'] = 90
                self.settings.save_config(changed)
            return previous(connection)
        self.client.discover.side_effect = discover
        self.settings.refresh()
        self.assertEqual(self.settings.view()['roles']['build']['status'], 'unknown')
        self.assertFalse(self.settings.view()['repositories'][0]['access_ready'])

    def test_renamed_repository_requires_explicit_config_update(self):
        self.settings.refresh(); self.prepare(); self.enable()
        observed = self.settings.observation()
        for row in observed['roles'].values(): row['repositories'][0]['name'] = 'example/renamed'
        self.settings.save_observation(observed)
        self.assertEqual(self.settings.view()['repositories'][0]['access']['build'], 'name_changed')
        self.assertFalse(self.settings.admission('example/project')['allowed'])

    def test_agent_edit_conflicts_are_not_overwritten(self):
        revision = digest(self.config)
        changed = copy.deepcopy(self.config); changed['sync_interval_seconds'] = 600
        self.settings.save_config(changed)  # A concurrent edit lands first.
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.settings.save(self.config, revision)
        self.assertEqual(self.settings.load(), changed)

    def test_invalid_config_and_duplicate_json_fail_closed(self):
        document = self.root / 'operator-input.json'
        document.write_text('{"version":1,"version":1}')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            read_json(document)
        with self.assertRaises(ValueError):
            self.settings.save_config({**configured(), 'secret': 'private'})
        self.assertEqual(self.settings.load(), self.config)
        with self.database.write() as db:
            db.execute("DELETE FROM configuration WHERE domain=?", (self.settings.domain,))
        self.assertEqual(self.settings.admission('example/project'),
                         {'allowed': False, 'reason': 'settings_unavailable'})
        for mutate in (lambda c: c.update(secret='private'),
                       lambda c: c['connections']['build'].update(private_key_env='synthetic key content'),
                       lambda c: c['connections']['review'].update(app_id=1)):
            config = configured(); mutate(config)
            with self.assertRaises(ValueError): validate(config)

    def test_sync_is_singleton_and_restart_recovers_interrupted_marker(self):
        with self.settings.locked('sync'):
            self.assertFalse(self.settings.refresh())
        self.client.discover.assert_not_called()
        self.settings.save_sync({'status': 'running', 'started_at': 900})
        self.assertEqual(self.settings.view()['sync']['status'], 'interrupted')
        self.assertTrue(self.settings.refresh())
        self.assertEqual(self.settings.view()['sync']['status'], 'complete')

    def test_secret_exception_text_is_not_persisted(self):
        self.client.discover.side_effect = RuntimeError('a-secret-that-must-not-be-persisted')
        self.settings.refresh()
        self.assertNotIn('a-secret-that-must-not-be-persisted', json.dumps(self.settings.view()))
        with self.database.read_snapshot() as db:
            stored = [list(row) for table in ('access_role_observations', 'access_repository_observations', 'runtime_controls')
                      for row in db.execute('SELECT * FROM ' + table)]
        self.assertTrue(stored)
        self.assertNotIn('a-secret-that-must-not-be-persisted', json.dumps(stored))

    def test_sync_cli_returns_failure_when_access_is_unknown(self):
        from contextlib import redirect_stdout
        from forge.settings import main
        self.client.discover.side_effect = AccessError('Access unavailable')
        with patch('forge.settings.Settings', return_value=self.settings), redirect_stdout(io.StringIO()):
            self.assertEqual(main(['sync','--root',str(self.root)]),1)

    def test_deployment_profile_is_optional_and_shape_checked(self):
        profile = {'strategy': 'header', 'deploy': {'workflow': 'deploy.yaml', 'environment': 'production', 'trigger': 'push:main'},
                   'domain': 'academy.example.org', 'exposure': {'kind': 'header', 'path': None, 'field': None, 'header': 'X-Build-Commit'},
                   'workflows_digest': 'digest-one', 'confirmed_at': 1000, 'exposure_issue': 41}
        config = copy.deepcopy(self.config)
        config['repositories'] = [{'id': 10, 'name': 'example/project', 'enabled': True}]
        self.assertEqual(validate(copy.deepcopy(config))['repositories'][0]['enabled'], True)
        config['repositories'][0]['deployment'] = profile
        self.assertIs(validate(copy.deepcopy(config))['repositories'][0]['deployment']['exposure_issue'], 41)
        for override in ({'exposure_issue': None}, {'domain': None}, {'strategy': 'unobservable'},
                         {'exposure': {'kind': 'none', 'path': None, 'field': None, 'header': None}},
                         {'confirmed_at': 1000.5}):
            config['repositories'][0]['deployment'] = {**profile, **override}
            validate(copy.deepcopy(config))
        for override in ({'strategy': 'guess'}, {'domain': 'https://academy.example.org/health'},
                         {'domain': 'academy.example.org/health'}, {'domain': 12}, {'exposure_issue': 'forty-one'},
                         {'exposure_issue': 0}, {'workflows_digest': None}, {'confirmed_at': 'now'},
                         {'exposure': {'kind': 'invented', 'path': None, 'field': None, 'header': None}},
                         {'deploy': {'workflow': 'deploy.yaml'}}, {'proposal': None}):
            config['repositories'][0]['deployment'] = {**profile, **override}
            with self.assertRaises(ValueError):
                validate(copy.deepcopy(config))
        config['repositories'][0]['deployment'] = profile
        self.assertNotEqual(digest(validate(copy.deepcopy(config))),
                            digest({**config, 'repositories': [{'id': 10, 'name': 'example/project', 'enabled': True}]}))

    def test_deployment_reads_are_recommended_and_never_required(self):
        # A live installation must keep minting controller tokens without the new grant.
        self.assertNotIn('deployments', ROLE_PERMISSIONS['controller'])
        self.assertEqual(OPTIONAL_PERMISSIONS['controller'], {'deployments': 'read'})
        granted = dict(ROLE_PERMISSIONS['controller'])
        self.assertEqual(missing_permissions('controller', granted), [])
        self.assertEqual(recommended_permissions('controller', granted), ['deployments: read'])
        self.assertEqual(recommended_permissions('controller', {**granted, 'deployments': 'read'}), [])
        self.assertEqual(recommended_permissions('build', {}), [])
        self.settings.refresh()
        role = self.settings.view()['roles']['controller']
        self.assertEqual(role['status'], 'ok')
        self.assertEqual(role['missing_permissions'], [])
        self.assertEqual(role['recommended_permissions'], {'deployments': 'read'})
        self.assertEqual(role['missing_recommended'], ['deployments: read'])
        self.assertEqual(self.settings.view()['roles']['build']['recommended_permissions'], {})
        self.assertEqual(self.settings.view()['roles']['build']['missing_recommended'], [])

    def control(self, name, enabled, details='{}'):
        with self.database.write() as db:
            db.execute('INSERT INTO runtime_controls(name,enabled,updated_at,details_json) VALUES(?,?,?,?) '
                       'ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,details_json=excluded.details_json',
                       (name, int(enabled), self.now, details))

    def test_saved_configuration_mirrors_enabled_repositories(self):
        self.settings.refresh(); self.prepare(); self.enable()
        with self.database.read_snapshot() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT id,full_name,enabled FROM repositories')],
                             [(10, 'example/project', 1)])
        config = self.settings.load(); config['repositories'] = []
        self.settings.save(config, digest(self.settings.load()))
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT enabled FROM repositories WHERE id=10').fetchone()[0], 0)
        self.assertEqual(Settings(self.root).load(), config)

    def test_stale_storage_revision_cannot_replace_configuration(self):
        _, revision = self.settings.config_snapshot()
        changed = copy.deepcopy(self.config); changed['sync_interval_seconds'] = 600
        self.settings.save_config(changed, expected_revision=revision)
        with self.assertRaisesRegex(ConfigurationConflict, 'changed'):
            self.settings.save_config(self.config, expected_revision=revision)
        self.assertEqual(self.settings.load(), changed)

    def test_observation_round_trips_repository_flags_and_order(self):
        self.settings.refresh()
        observed = self.settings.observation()
        observed['roles']['build']['repositories'] = [
            {'id': 11, 'name': 'example/second', 'archived': False, 'disabled': True},
            {'id': 10, 'name': 'example/project', 'archived': True, 'disabled': False}]
        self.settings.save_observation(observed)
        self.assertEqual(Settings(self.root).observation(), observed)
        self.assertEqual(self.settings.sync()['status'], 'complete')

    def test_reset_fence_blocks_saves_sync_and_dispatch(self):
        self.settings.refresh(); self.prepare(); self.enable()
        self.control('factory_paused', True)
        self.assertTrue(self.settings.paused())
        self.assertFalse(self.settings.refresh())
        with self.assertRaisesRegex(ValueError, 'reset'):
            self.settings.save(self.settings.load(), digest(self.settings.load()))
        self.assertEqual(self.settings.admission('example/project'), {'allowed': False, 'reason': 'factory_paused'})
        self.control('factory_paused', False)
        self.assertTrue(self.settings.admission('example/project')['allowed'])

    def test_onboarding_without_activation_blocks_dispatch(self):
        self.settings.refresh(); self.prepare(); self.enable()
        self.control('onboarding_active', True)
        self.assertEqual(self.settings.admission('example/project'), {'allowed': False, 'reason': 'setup_not_started'})
        self.control('factory_active', True)
        self.assertTrue(self.settings.admission('example/project')['allowed'])

    def test_generation_guard_rejects_writes_for_a_replaced_setup(self):
        with self.database.write() as db:
            db.execute("INSERT INTO setup_instances VALUES('live','generation-two','setup','','','{}',1)")
        self.assertEqual(self.settings.generation(), 'generation-two')
        with self.assertRaisesRegex(ValueError, 'setup changed'):
            self.settings.save_config(self.config, expected_generation='generation-one')
        with self.assertRaisesRegex(ValueError, 'setup changed'):
            self.settings.save_observation({'roles': {}}, expected_generation='generation-one')
        self.settings.save_observation({'roles': {}}, expected_generation='generation-two')

    def test_repository_names_cannot_address_parent_checkout(self):
        for name in ('example/..', 'example/.', '../project', 'example/a/b'):
            config = configured()
            config['repositories'] = [{'id':10, 'name':name, 'enabled':False}]
            with self.assertRaises(ValueError): validate(config)


class AppClientTests(unittest.TestCase):
    def setUp(self):
        self.connection = configured()['connections']['build']
        self.requests = []
        def request(method, path, token, payload=None):
            self.requests.append((method, path, payload))
            if path == '/app': return {'id': 1, 'slug': 'example-build'}
            if path == '/app/installations/101':
                return {'id':101, 'app_id':1, 'permissions':ROLE_PERMISSIONS['build'],
                        'account':{'login':'example','type':'Organization'}}
            if path.endswith('/access_tokens'): return {'token':'synthetic-token'}
            if 'page=1' in path: return {'total_count':1,'repositories':[{'id':10,'full_name':'example/project'}]}
            raise AssertionError('Unexpected request')
        self.request = request
        self.client = AppClient(request=request)
        self.client.jwt = Mock(return_value='synthetic-jwt')

    def test_read_only_discovery_and_no_secret_in_result(self):
        value = self.client.discover(self.connection)
        self.assertEqual(value['repositories'][0]['id'],10)
        self.assertEqual([r for r in self.requests if r[0]!='GET'],
                         [('POST','/app/installations/101/access_tokens',{'permissions':{'metadata':'read'}})])
        self.assertNotIn('synthetic-token',json.dumps(value))
        self.assertEqual(value['installation_url'],'https://github.com/organizations/example/settings/installations/101')

    def test_mismatched_app_never_creates_installation_token(self):
        self.connection['app_id'] = 99
        with self.assertRaisesRegex(AccessError,'does not match'):
            self.client.discover(self.connection)
        self.assertEqual(len(self.requests),1)

    def test_duplicate_pagination_is_rejected(self):
        base = self.request
        def request(method,path,token,payload=None):
            if path.startswith('/installation/repositories'):
                return {'total_count':100,'repositories':[{'id':1,'full_name':'example/project'}]*100}
            return base(method,path,token,payload)
        self.client.request = request
        with self.assertRaisesRegex(AccessError,'inconsistent'):
            self.client.discover(self.connection)

    def test_partial_inventory_cannot_be_treated_as_removal(self):
        base = self.request
        def request(method,path,token,payload=None):
            if path.startswith('/installation/repositories'):
                return {'total_count':2,'repositories':[{'id':10,'full_name':'example/project'}]}
            return base(method,path,token,payload)
        self.client.request = request
        with self.assertRaisesRegex(AccessError,'incomplete'):
            self.client.discover(self.connection)

    def test_suspended_installation_cannot_mint_token(self):
        base = self.request
        def request(method,path,token,payload=None):
            value = base(method,path,token,payload)
            if path == '/app/installations/101': value['suspended_at'] = '2026-09-11T00:00:00Z'
            return value
        self.client.request = request
        with self.assertRaisesRegex(AccessError,'suspended'):
            self.client.discover(self.connection)
        self.assertFalse(any(method == 'POST' for method,_,_ in self.requests))

    def test_keys_and_signing_errors_do_not_escape(self):
        run=Mock(return_value=Mock(returncode=1,stdout=b'',stderr=b'private detail'))
        client=AppClient(environ={'FORGE_BUILD_KEY_FILE':'/private/synthetic.pem'},run=run)
        with self.assertRaises(AccessError) as caught:
            client.jwt(self.connection)
        self.assertNotIn('/private',str(caught.exception)); self.assertNotIn('private detail',str(caught.exception))
        self.assertNotIn('GH_TOKEN',json.dumps(run.call_args.kwargs,default=str))


class DiscussionModelTests(unittest.TestCase):
    def test_discussion_model_defaults_to_the_provider_default(self):
        from forge.factory.config import DEFAULT as RUNTIME, validate as runtime
        self.assertIsNone(RUNTIME['discussion_model'])
        self.assertIsNone(runtime({})['discussion_model'])

    def test_discussion_model_maps_installed_providers_to_models(self):
        from forge.factory.config import validate as runtime
        self.assertEqual(runtime({'discussion_model': {'claude': 'claude-haiku-4-5'}})['discussion_model'],
                         {'claude': 'claude-haiku-4-5'})
        for bad in ('claude-haiku-4-5', {'nobody': 'fast'}, {'claude': ''}, {'claude': None},
                    {'claude': 'two words'}, ['claude'], {'claude': 'x' * 200}):
            with self.assertRaisesRegex(ValueError, 'discussion_model'):
                runtime({'discussion_model': bad})


class ReliabilityPolicyTests(unittest.TestCase):
    def test_partial_override_preserves_other_bounds(self):
        from forge.factory.config import DEFAULT as RUNTIME, validate as runtime
        config = runtime({'reliability': {'stall_seconds': 600}})
        self.assertEqual(config['reliability'], {**RUNTIME['reliability'], 'stall_seconds': 600})

    def test_retry_policies_reject_unbounded_or_inconsistent_values(self):
        from forge.factory.config import validate as runtime
        for value in ({'claim_retest_seconds': []}, {'claim_retest_seconds': [300, 300]},
                      {'claim_retest_seconds': [300, 30]}, {'claim_retest_seconds': [True]},
                      {'effect_max_attempts': 0}, {'effect_uncertainty_seconds': 0},
                      {'retry_seconds': 100, 'effect_retry_cap_seconds': 99}, {'unknown': 1}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                runtime({'reliability': value})


if __name__ == '__main__': unittest.main()


class MirrorDenySettingsTests(unittest.TestCase):
    def test_repository_deny_list_preserves_ids_and_names(self):
        config = configured()
        config['repositories'] = [{'id': 10, 'name': 'example/project', 'enabled': False,
            'mirror_denied_repositories': [{'canonical_name': 'example/secret', 'repository_id': 20}]}]
        self.assertEqual(validate(config), config)

    def test_rejects_ambiguous_or_invalid_deny_entries(self):
        from forge.settings import mirror_denied_repositories
        for rows in ([{'canonical_name': 'example/secret', 'repository_id': True}],
                     [{'canonical_name': 'example/secret'}, {'canonical_name': 'Example/Secret'}],
                     [{'canonical_name': 'example/secret', 'repository_id': 20},
                      {'canonical_name': 'example/renamed', 'repository_id': 20}],
                     [{'canonical_name': '../secret'}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                mirror_denied_repositories(rows)
