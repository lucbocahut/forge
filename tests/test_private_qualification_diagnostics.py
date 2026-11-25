"""Private failure evidence survives cleanup without entering public reports."""
import base64
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge import qualification_diagnostics as evidence
from forge.factory.config import DEFAULT
from forge.factory.harness import schema
from forge.factory.runner import ContainerRunner, RunnerError
from forge_client.cli import main
from forge_client.contracts import SetupError
from forge_client.qualification_diagnostics import execute
from forge_client.role_qualification import failure_report


class PrivateQualificationDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.job = dict(id='qualification-job', role='review', runner_config={'provider': 'codex'})
        self.notes = dict(id=self.job['id'], role='review', candidate={'key': 'candidate'})
        self.message = 'Schema failure: private provider detail'
        self.capture = dict(result=None, execution=dict(running=False, exit_code=1, oom_killed=False),
            execution_envelope=dict(version=1, attempt=self.job['id'], harness_status='provider_error',
                                    exit_code=1, provider_error={'code': 'unknown'}),
            logs=json.dumps({'type':'turn.failed','error':{'message':self.message}}))

    def retain(self, capture=None, **kwargs):
        return evidence.retain(self.root, capture or self.capture, self.job, self.notes,
                               source_revision='a'*40, image='sha256:'+'b'*64, **kwargs)

    def test_runner_scrubs_issued_credentials_before_private_storage(self):
        opaque = 'opaque-credential-"quoted"-value'
        fake_api_key = 'sk-' + 'X' * 20
        runner = ContainerRunner({**DEFAULT, 'provider_environment':['OPENAI_API_KEY']},
            environ={'FORGE_CREDENTIAL_ROOT':str(self.root), 'OPENAI_API_KEY':fake_api_key})
        secret_path = runner._secrets_path(self.job)
        secret_path.parent.mkdir(parents=True)
        (self.root / 'state').chmod(0o700)
        secret_path.write_text(json.dumps({'values':[opaque]}))
        text = self.message + ' ' + opaque + ' ' + fake_api_key
        events = [{'type':'item.completed','item':{'type':'agent_message','text':'MODEL TEXT'}},
                  {'type':'item.completed','item':{'type':'command_execution','output':'TOOL TEXT'}},
                  {'type':'turn.failed','error':{'message':text,'unknown':'EXCLUDED FIELD'}}]
        def call(args, **kwargs):
            if args[0] == 'logs':
                return SimpleNamespace(stdout='\n'.join(map(json.dumps,events)).encode())
            raise RunnerError('missing artifact')
        with patch.object(runner, 'inspect', return_value={**self.capture['execution'],'id':'container'}), \
                patch.object(runner, 'call', side_effect=call):
            capture = runner.capture(self.job)
        capture['execution_envelope'] = self.capture['execution_envelope']
        identifier = self.retain(capture, secrets=runner._scrub_values(self.job))
        data = evidence.read(self.root, identifier)
        record = json.loads(data)
        self.assertIn('[REDACTED]', record['terminal_error']['message'])
        for forbidden in (opaque, fake_api_key, 'MODEL TEXT', 'TOOL TEXT', 'EXCLUDED FIELD'):
            self.assertNotIn(forbidden, data.decode())
        expected = json.dumps(schema(self.notes), sort_keys=True, separators=(',', ':')).encode()
        self.assertEqual(record['schema_sha256'], hashlib.sha256(expected).hexdigest())
        self.assertEqual(record['output_schema'], schema(self.notes))
        self.assertEqual(record['source_revision'], 'a'*40)
        self.assertEqual(record['job_id'], self.job['id'])
        target = evidence.directory(self.root)/identifier
        self.assertEqual(stat.S_IMODE(target.stat().st_mode),0o600)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode),0o700)
        report = failure_report(dict(status='failed', diagnostic_id=identifier, terminal_error=record['terminal_error'], logs=capture['logs']))
        self.assertEqual(report['diagnostic_id'], identifier)
        self.assertNotIn(self.message, json.dumps(report))
        self.assertNotIn('terminal_error', report)

    def test_only_matching_sealed_final_provider_failure_is_retained(self):
        for change in ('job','exit','running','oom','result','completed','model','harness','provider'):
            with self.subTest(change=change):
                capture = deepcopy(self.capture); job = deepcopy(self.job)
                if change=='job':capture['execution_envelope']['attempt']='another-job'
                if change=='exit':capture['execution']['exit_code']=2
                if change=='running':capture['execution']['running']=True
                if change=='oom':capture['execution']['oom_killed']=True
                if change=='result':capture['result']={}
                if change=='completed':capture['logs']+='\n'+json.dumps({'type':'turn.completed'})
                if change=='model':capture['logs']=json.dumps({'type':'item.completed','item':{'type':'agent_message','text':self.message}})
                if change=='harness':capture['execution_envelope']['harness_status']='ok'
                if change=='provider':job['runner_config']['provider']='claude'
                self.assertIsNone(evidence.terminal_error(capture,job))
        self.assertFalse((self.root/'state').exists())

    def test_retained_tail_error_observation_still_uses_terminal_failure(self):
        # The public diagnostic lists nonterminal observations only. Seeing
        # `error` there does not mean the sealed failure lacked turn.failed.
        self.capture['logs'] = '\n'.join(map(json.dumps, [
            {'type': 'thread.started'}, {'type': 'turn.started'},
            {'type': 'error', 'message': 'intermediate error'},
            {'type': 'turn.failed', 'error': {'message': self.message}},
        ]))
        self.assertEqual(evidence.terminal_error(self.capture, self.job)['message'], self.message)
        self.capture['logs'] = json.dumps({'type': 'error', 'message': self.message})
        self.assertIsNone(evidence.terminal_error(self.capture, self.job))

    def test_private_bounds_and_symlink_refusal(self):
        self.capture['logs']=json.dumps({'type':'turn.failed','error':{'message':'x'*12000}})
        identifier=self.retain()
        record=json.loads(evidence.read(self.root,identifier))
        self.assertEqual(len(record['terminal_error']['message']),evidence.MAX_MESSAGE)
        self.assertTrue(record['terminal_error']['truncated'])
        target=evidence.directory(self.root)/identifier
        target.unlink();target.symlink_to(self.root/'external')
        with self.assertRaises(OSError):evidence.read(self.root,identifier)
        target.unlink();target.parent.rmdir();target.parent.symlink_to(self.root)
        with self.assertRaises(RuntimeError):self.retain()

    def test_record_is_immutable_and_oversized_schema_is_not_written(self):
        fixed=SimpleNamespace(hex='c'*32)
        with patch.object(evidence.uuid,'uuid4',return_value=fixed):
            identifier=self.retain();before=evidence.read(self.root,identifier)
            with self.assertRaises(FileExistsError):self.retain()
            self.assertEqual(evidence.read(self.root,identifier),before)
        with patch('forge.factory.harness.schema',return_value={'description':'x'*evidence.MAX_SCHEMA}):
            with self.assertRaises(ValueError):self.retain()

    def installation(self, identifier):
        installation=Mock();installation.journal.read.return_value={}
        installation.host.rpc.return_value={'ok':True,'content':base64.b64encode(evidence.read(self.root,identifier)).decode()}
        return installation

    def test_cli_exports_only_to_new_private_file_and_stdout_has_metadata(self):
        identifier=self.retain();installation=self.installation(identifier)
        target=self.root/'export.json';output=io.StringIO()
        with patch('forge_client.qualification_diagnostics.Installation',return_value=installation),patch('sys.stdout',output):
            code=main(['setup','qualification-diagnostic','--ssh','anvil','--id',identifier,'--output',str(target),'--json'])
        self.assertEqual(code,0)
        self.assertNotIn(self.message,output.getvalue())
        self.assertNotIn('content',json.loads(output.getvalue()))
        self.assertEqual(target.read_bytes(),evidence.read(self.root,identifier))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode),0o600)
        installation.host.rpc.reset_mock()
        for path in (target,self.root/'link'):
            if path.name=='link':path.symlink_to(target)
            with self.assertRaises(SetupError):
                execute(SimpleNamespace(ssh='anvil',id=identifier,output=str(path)),installation=installation)
        with self.assertRaises(SetupError):
            execute(SimpleNamespace(ssh='anvil',id=identifier,output='-'),installation=installation)
        installation.host.rpc.assert_not_called()

    def test_invalid_transport_removes_new_file_without_exposing_contents(self):
        identifier=self.retain();installation=self.installation(identifier)
        installation.host.rpc.return_value={'ok':True,'content':self.message}
        target=self.root/'export.json'
        with self.assertRaises(SetupError) as caught:
            execute(SimpleNamespace(ssh='anvil',id=identifier,output=str(target)),installation=installation)
        self.assertNotIn(self.message,str(caught.exception))
        self.assertFalse(target.exists())
