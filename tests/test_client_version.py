import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge_client import version
from forge_client.cli import parser


class VersionTests(unittest.TestCase):
    def test_managed_revision_is_stamped_in_installed_prefix(self):
        with patch.object(version.sys, 'prefix', '/private/client/' + 'a'*40 + '-' + 'b'*32):
            self.assertEqual(version.client_revision(), 'a'*40)

    def test_pip_vcs_install_uses_recorded_commit(self):
        distribution = Mock(); distribution.read_text.return_value = json.dumps({'vcs_info': {'commit_id': 'a'*40}})
        with patch.object(version.sys, 'prefix', '/pip/venv'), patch.object(version.importlib.metadata, 'distribution', return_value=distribution):
            self.assertEqual(version.client_revision(), 'a'*40)

    def test_actual_server_revision_is_compared_instead_of_saved_install_intent(self):
        installation = Mock()
        installation.journal.read.return_value = {'host_key': 'pinned', 'intent': {'revision': 'a'*40}}
        installation.host.rpc.return_value = {'ok': True, 'revision': 'b'*40}
        with patch.object(version, 'client_revision', return_value='a'*40), patch('forge_client.install.Installation', return_value=installation):
            result = version.report('anvil')
        self.assertFalse(result['compatible'])
        self.assertIn('differ', result['warning'])
        self.assertEqual(installation.host.fingerprint, 'pinned')
        namespace = {}; exec(installation.host.rpc.call_args.args[0], namespace)
        with patch('subprocess.run', return_value=SimpleNamespace(stdout='b'*40, returncode=0)) as run, patch('builtins.print') as output:
            self.assertEqual(namespace['main']('unused'), 0)
            self.assertEqual(run.call_args.args[0][:4], ['runuser','-u','forge','--'])
            self.assertIn('HOME=/home/forge', run.call_args.args[0])
        self.assertEqual(json.loads(output.call_args.args[0])['revision'], 'b'*40)

    def test_login_and_help_surface(self):
        args = parser().parse_args(['account', 'login', 'codex', '--ssh', 'anvil', '--json'])
        self.assertEqual((args.provider, args.ssh, args.json), ('codex', 'anvil', True))
        self.assertEqual(parser().parse_args(['self-update', '--revision', 'a'*40]).revision, 'a'*40)


    def test_update_uses_pinned_uv_install_and_verifies_new_command(self):
        commit='a'*40
        replies=[b'/private/tools/bin\n',b'',json.dumps({'client_revision':commit}).encode(),
                 json.dumps({'commands':['setup new']}).encode()]
        with patch.object(version.shutil,'which',return_value='/usr/local/bin/uv'), patch('forge_client.install.revision',return_value=commit), \
                patch.object(version,'run_bounded',side_effect=replies) as run, patch.dict(version.os.environ,{'GH_TOKEN':'private','GITHUB_TOKEN':'private'}):
            result=version.update(revision=commit)
        self.assertEqual(result['command'],'/private/tools/bin/forge')
        self.assertEqual(run.call_args_list[1].args[0],['/usr/local/bin/uv','tool','install','--reinstall',
            'git+https://github.com/lucbocahut/forge.git@'+commit])
        for call in run.call_args_list:
            self.assertNotIn('GH_TOKEN',call.kwargs['environ'])
            self.assertNotIn('GITHUB_TOKEN',call.kwargs['environ'])
            self.assertNotIn('--force',call.args[0])

    def test_update_cannot_report_success_for_wrong_revision_or_missing_uv(self):
        from forge_client.contracts import SetupError
        with patch.object(version.shutil,'which',return_value=None), patch.object(version,'run_bounded') as run:
            with self.assertRaises(SetupError) as raised:version.update(revision='a'*40)
            self.assertEqual(raised.exception.code,'uv_required');run.assert_not_called()
        with patch.object(version.shutil,'which',return_value='/bin/uv'), patch('forge_client.install.revision',return_value='a'*40), \
                patch.object(version,'run_bounded',side_effect=[b'/private/tools/bin',b'',b'{"client_revision":"wrong"}']):
            with self.assertRaises(SetupError) as raised:version.update(revision='a'*40)
            self.assertEqual(raised.exception.code,'client_update_unverified')

    def test_deleted_commands_are_absent_and_new_commands_remain(self):
        from forge_client.cli import execute
        commands=execute(parser().parse_args(['commands']))
        self.assertIn('install --preflight',commands['commands'])
        self.assertIn('setup qualify-boundary',commands['commands'])
        self.assertNotIn('fix-rules',commands['repository_commands'])
        self.assertEqual(set(commands['migration_commands']),{'upgrade'})
