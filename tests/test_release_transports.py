"""Private assets travel only through the existing authenticated SSH channel."""
import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge import release_artifacts
from forge_client import _install_host as remote
from forge_client.install import InstallerHost
from forge_client.migration import BOOTSTRAP, bundle
from tests.test_fresh_install import INTENT, PACK, FACTS, response


ACCESS = {'revision': INTENT['revision'], 'manifest': {},
          'urls': {'ui': 'https://release-assets.githubusercontent.com/file?secret=ephemeral'}}


class ReleaseTransportTests(unittest.TestCase):
    def test_fresh_apply_embeds_helper_code_but_access_only_in_request(self):
        host = InstallerHost('anvil')
        replies = [response(), response(INTENT, 'installing', 'packages')]
        with patch.object(host, 'rpc', side_effect=replies) as rpc, \
                patch('forge_client.install.export_source', return_value=PACK), \
                patch('forge_client.release_delivery.access', return_value=ACCESS) as access:
            host.call('apply', INTENT)
        access.assert_called_once_with(INTENT['revision'])
        self.assertNotIn('release_access', rpc.call_args_list[0].args[1])
        payload, request = rpc.call_args.args
        self.assertEqual(request['release_access'], ACCESS)
        self.assertGreaterEqual(rpc.call_args.kwargs['timeout'], 2700)
        self.assertNotIn('secret=ephemeral', payload)
        namespace = {'__name__': 'forge_installer_test'}
        with patch.dict(sys.modules):
            exec(compile(payload, '<installer>', 'exec'), namespace)
            self.assertTrue(callable(sys.modules['forge_release_helpers'].stage))

    def test_running_installer_does_not_refresh_or_transfer_assets(self):
        host = InstallerHost('anvil')
        with patch.object(host, 'rpc', return_value=response(INTENT, 'installing', 'packages')) as rpc, \
                patch('forge_client.release_delivery.access') as access:
            host.call('resume', INTENT)
        access.assert_not_called()
        self.assertNotIn('release_access', rpc.call_args.args[1])

    def test_root_stages_assets_without_journaling_download_access(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory).resolve() / 'state'
            request = dict(version=1, action='apply', intent=INTENT, source=PACK,
                           upgrade_installer=False, release_access=deepcopy(ACCESS))
            def private(path, create=False):
                if create: path.mkdir(mode=0o700, exist_ok=True)
            with patch.object(remote, 'STATE', state), patch.object(remote, 'facts', return_value=FACTS), \
                    patch.object(remote, 'check_new_host'), patch.object(remote, 'running', return_value=False), \
                    patch.object(remote, 'private_directory', side_effect=private), patch.object(remote, 'run'), \
                    patch.dict(sys.modules, {'forge_release_helpers': release_artifacts}), \
                    patch.object(release_artifacts, 'stage') as stage:
                remote.dispatch(request, 'worker code')
                stage.assert_called_once_with(ACCESS, cache_root=state / 'prepared')
                self.assertNotIn('release_access', request)
                for path in state.iterdir():
                    if path.is_file(): self.assertNotIn(b'secret=ephemeral', path.read_bytes())

    def test_arm64_host_builds_from_source_without_staging_prepared_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory).resolve() / 'state'
            request = dict(version=1, action='apply', intent=INTENT, source=PACK,
                           upgrade_installer=False, release_access=deepcopy(ACCESS))
            def private(path, create=False):
                if create: path.mkdir(mode=0o700, exist_ok=True)
            with patch.object(remote, 'STATE', state), patch.object(remote, 'facts', return_value=FACTS), \
                    patch.object(remote, 'check_new_host'), patch.object(remote, 'running', return_value=False), \
                    patch.object(remote, 'private_directory', side_effect=private), patch.object(remote, 'run'), \
                    patch.dict(sys.modules, {'forge_release_helpers': release_artifacts}), \
                    patch.object(release_artifacts.platform, 'system', return_value='Linux'), \
                    patch.object(release_artifacts.platform, 'machine', return_value='aarch64'), \
                    patch.object(release_artifacts, 'stage') as stage:
                remote.dispatch(request, 'worker code')
                stage.assert_not_called()
                self.assertFalse((state / 'prepared').exists())

    def test_fresh_revision_mismatch_rejected_before_any_stage(self):
        request = dict(version=1, action='apply', intent=INTENT, source=PACK,
                       upgrade_installer=False, release_access={**ACCESS, 'revision': 'b'*40})
        with patch.object(remote, 'facts') as facts:
            with self.assertRaises(remote.InstallError): remote.dispatch(request, 'code')
        facts.assert_not_called()

    def test_host_resume_promotes_assets_with_service_ownership_before_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state, home = root / 'state', root / 'forge'
            (state / 'prepared' / INTENT['revision']).mkdir(parents=True)
            home.mkdir()
            account = SimpleNamespace(pw_dir=str(home), pw_uid=1234, pw_gid=1234)
            events = []
            def service(argv, **kwargs):
                events.append(argv)
                if argv[-2:] == ['rev-parse', 'HEAD']: return INTENT['revision']
                if 'is-active' in argv: return 'active'
                return ''
            def copy(*args, **kwargs):
                events.append('cache')
            with patch.object(remote, 'STATE', state), patch.object(remote, 'HOME', home), \
                    patch.object(remote.pwd, 'getpwnam', return_value=account), \
                    patch.object(remote.os, 'chown') as chown, patch.object(remote, 'service', side_effect=service), \
                    patch.dict(sys.modules, {'forge_release_helpers': release_artifacts}), \
                    patch.object(release_artifacts, 'copy_cache', side_effect=copy) as copied:
                remote.install_step('host', {'intent': INTENT})
            self.assertEqual(events[0], 'cache')
            copied.assert_called_once_with(INTENT['revision'], state / 'prepared', home / '.cache/forge/prepared',
                                           uid=1234, gid=1234)
            self.assertEqual(chown.call_count, 2)
            self.assertEqual((home / '.cache/forge').stat().st_mode & 0o777, 0o700)

    def migration_bootstrap(self, access):
        code = bundle()
        with tempfile.TemporaryDirectory() as directory:
            request = dict(action='upgrade', apply=True, root=directory, source='/srv/forge',
                           source_revision=INTENT['revision'], release_access=deepcopy(access),
                           bundle=base64.b64encode(code).decode(), bundle_digest=hashlib.sha256(code).hexdigest())
            output = io.StringIO()
            with patch('sys.stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()))), \
                    patch('sys.stdout', output), patch('sys.path', list(sys.path)), \
                    patch('forge.store.db.qualified_sqlite', return_value=True), \
                    patch('forge.store.migration.host.run', return_value={'status': 'complete'}) as run, \
                    patch.object(release_artifacts, 'stage') as stage:
                exec(BOOTSTRAP, {'__name__': 'migration_transport_test'})
            return json.loads(output.getvalue()), run, stage

    def test_migration_stages_before_upgrade_and_strips_access_from_request(self):
        result, run, stage = self.migration_bootstrap(ACCESS)
        self.assertTrue(result['ok'])
        stage.assert_called_once_with(ACCESS)
        self.assertNotIn('release_access', run.call_args.args[0])
        self.assertNotIn('secret=ephemeral', json.dumps(run.call_args.args[0]))

    def test_migration_rejects_other_revision_before_stage_or_upgrade(self):
        result, run, stage = self.migration_bootstrap({**ACCESS, 'revision': 'b'*40})
        self.assertEqual(result['error'], 'invalid_release_access')
        stage.assert_not_called()
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
