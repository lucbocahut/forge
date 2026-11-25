import io
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from tests.test_dashboard import dashboard, signed_in, OPERATOR_CSRF
from tests.sqlite_fixture import selected_store


class SettingsHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        selected_store(temporary.name)
        root=patch.object(dashboard,'WD',temporary.name);root.start();self.addCleanup(root.stop)
        self.session_headers=signed_in(self)

    def handler(self, path, body=None, **headers):
        handler = object.__new__(dashboard.H)
        handler.server = Mock(server_address=('127.0.0.1',8787),read_handles=None)
        handler.headers = {'Host':'127.0.0.1:8787', 'Origin':'http://127.0.0.1:8787',
                           'X-Forge-Action':OPERATOR_CSRF, 'Content-Type':'application/json',
                           **getattr(self,'session_headers',{}), **headers}
        handler.path = path
        handler._send = Mock(); handler.send_error = Mock()
        payload = json.dumps(body).encode()
        handler.rfile = io.BytesIO(payload); handler.headers['Content-Length'] = str(len(payload))
        return handler

    def test_page_and_api_require_trusted_host(self):
        for path in ('/api/issues','/api/settings','/api/factory','/api/factory/output?id=invalid','/api/onboarding','/onboarding/github/callback'):
            handler = self.handler(path, Host='attacker.example:8787')
            handler.do_GET(); handler.send_error.assert_called_once_with(403)

    def test_settings_get_is_local_only(self):
        service = Mock(); service.view.return_value = {'config':{},'revision':'abc'}
        with patch.object(dashboard,'settings_service',return_value=service):
            handler = self.handler('/api/settings'); handler.do_GET()
        result = json.loads(handler._send.call_args.args[0])
        self.assertEqual(result['controls_token'],OPERATOR_CSRF)
        service.start_refresh.assert_not_called(); service.refresh.assert_not_called()

    def test_settings_summary_retains_access_counts_without_heavy_repository_evidence(self):
        service = Mock()
        service.view.return_value = {'config': {}, 'revision': 'r', 'roles': {}, 'repositories': [
            {'id': 1, 'name': 'example/project', 'enabled': True, 'access': {'build': 'ready'},
             'protection_status': 'ready', 'protection': {'large': 'report'}, 'environment': {'large': 'probe'}}]}
        with patch.object(dashboard, 'settings_service', return_value=service):
            handler = self.handler('/api/settings?summary=1'); handler.do_GET()
        result = json.loads(handler._send.call_args.args[0])
        self.assertEqual(result['repositories'][0]['access'], {'build': 'ready'})
        self.assertEqual(result['repositories'][0]['protection_status'], 'ready')
        self.assertNotIn('large', json.dumps(result))

    def test_settings_mutations_reject_cross_origin_and_bad_token(self):
        for path in ('/api/issues/action','/api/issues/refresh','/api/repositories/disable','/api/settings/sync','/api/factory/action','/api/runtime','/api/onboarding'):
            for headers in ({'Origin':'http://attacker.example'}, {'X-Forge-Action':'bad'}, {'Sec-Fetch-Site':'cross-site'}):
                with patch.object(dashboard,'settings_service') as service:
                    handler = self.handler(path,{},**headers); handler.do_POST()
                    handler.send_error.assert_called_once_with(403); service.assert_not_called()

    def test_sync_is_requested_asynchronously(self):
        service = Mock()
        with patch.object(dashboard,'settings_service',return_value=service):
            handler = self.handler('/api/settings/sync',{}); handler.do_POST()
        service.start_refresh.assert_called_once_with(); service.refresh.assert_not_called()
        self.assertTrue(json.loads(handler._send.call_args.args[0])['ok'])

    def test_full_config_posts_are_not_available_to_the_operator_ui(self):
        with patch.object(dashboard,'settings_service') as service:
            handler = self.handler('/api/settings',{'config':{},'revision':'current'})
            handler.do_POST()
            handler.send_error.assert_called_once_with(404)
            service.assert_not_called()

    def test_only_the_operator_ui_may_call_the_api(self):
        with patch.object(dashboard,'UPSTREAM_SECRET','u'*43), patch.object(dashboard,'settings_service') as service:
            service.return_value.view.return_value={'config':{},'revision':'r'}
            for path,method in (('/api/settings','do_GET'),('/api/settings/sync','do_POST')):
                handler=self.handler(path,{}); getattr(handler,method)()
                handler.send_error.assert_called_once_with(403)
                handler=self.handler(path,{},**{'X-Forge-Upstream':'wrong'}); getattr(handler,method)()
                handler.send_error.assert_called_once_with(403)
                handler=self.handler(path,{},**{'X-Forge-Upstream':'u'*43}); getattr(handler,method)()
                handler.send_error.assert_not_called()

    def test_status_api_refuses_network_and_unset_binds(self):
        import os, subprocess, sys
        with tempfile.TemporaryDirectory() as directory:
            for bind,message in (('', 'Set FORGE_BIND_ADDRESS=127.0.0.1'), ('100.64.0.7', 'binds loopback only')):
                env={**os.environ,'FORGE_ROOT':directory,'FORGE_BIND_ADDRESS':bind,'FORGE_PORT':'1'}
                result=subprocess.run([sys.executable,'-m','forge.status_api'],env=env,capture_output=True,text=True,timeout=30)
                self.assertNotEqual(result.returncode,0)
                self.assertIn(message,result.stderr)

    def test_upstream_secret_is_created_private_and_reused(self):
        import os, stat
        with tempfile.TemporaryDirectory() as directory:
            path=os.path.join(directory,'state','ui-upstream.secret')
            first=dashboard.upstream_secret(path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode),0o600)
            self.assertEqual(dashboard.upstream_secret(path),first)
            os.chmod(path,0o644)
            with self.assertRaises(SystemExit): dashboard.upstream_secret(path)

    def test_app_manifest_returns_through_the_configured_sign_in_origin(self):
        with patch('forge.onboarding.Onboarding') as setup, patch.object(dashboard.H,'_browser',return_value='b'*32):
            setup.return_value.begin.return_value={}
            handler=self.handler('/api/onboarding',{'action':'github','role':'builder','owner':'o','account_type':'personal'})
            handler.do_POST()
            self.assertEqual(setup.return_value.begin.call_args.kwargs['origin'],'http://127.0.0.1:8787')
        with patch('forge.onboarding.Onboarding') as setup:
            handler=self.handler('/api/onboarding',{'action':'github','role':'builder','owner':'o','account_type':'personal'},
                                 **{'X-Forwarded-Host':'100.64.0.7:8787'})
            handler.do_POST()
            handler.send_error.assert_called_once_with(403)
            setup.return_value.begin.assert_not_called()

    def test_onboarding_start_and_reset_queue_supervisor_intent(self):
        for action,fields in [('start',{'revision':'verified'}),('reset',{'generation':'current'})]:
            with patch('forge.onboarding.Onboarding') as setup:
                setup.return_value.queue.return_value='intent'
                handler=self.handler('/api/onboarding',{'action':action,**fields})
                handler.do_POST()
                setup.return_value.queue.assert_called_once_with(action,**fields)
                self.assertTrue(json.loads(handler._send.call_args.args[0])['ok'])

    def test_manifest_callback_binds_browser_cookie_and_never_accepts_installation_id(self):
        with patch('forge.onboarding.Onboarding') as setup:
            setup.return_value.callback.return_value='https://github.com/apps/fixture/installations/new'
            handler=self.handler('/onboarding/github/callback?state=nonce&code=code',Cookie='forge_setup='+('a'*43))
            handler._redirect=Mock();handler.do_GET()
            setup.return_value.callback.assert_called_once_with('nonce','code','a'*43)
            handler._redirect.assert_called_once_with('https://github.com/apps/fixture/installations/new')
            setup.reset_mock()
            handler=self.handler('/onboarding/github/callback?installation_id=999')
            handler._redirect=Mock();handler.do_GET()
            setup.return_value.callback.assert_not_called()
            handler._redirect.assert_called_once_with('/onboarding?connection_error=1')

    def test_issue_decision_returns_updated_issue_and_validates_schema(self):
        with patch('forge.factory.store_operator.selected_service') as selected, \
                patch('forge.factory.issues.IssueInventory') as inventory:
            request = selected.return_value.request
            request.return_value = 'intent'
            inventory.return_value.view.return_value = {'issue': {'status': 'queued'}, 'actions': [], 'repositories': []}
            value={'key':'1:1','action':'queue','revision':'current','reason':'Operator decision'}
            handler=self.handler('/api/issues/action', value); handler.do_POST()
            selected.assert_called_once_with(dashboard.WD, actor_id='github:7')
            request.assert_called_once_with('1:1', 'queue', 'current', 'Operator decision', request_id=None)
            self.assertEqual(json.loads(handler._send.call_args.args[0])['issue']['status'], 'queued')
            request.reset_mock()
            handler=self.handler('/api/issues/action', {**value,'force':True}); handler.do_POST()
            request.assert_not_called()
            self.assertFalse(json.loads(handler._send.call_args.args[0])['ok'])


if __name__ == '__main__': unittest.main()
