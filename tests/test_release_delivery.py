import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from forge.release_artifacts import ASSETS
from forge_client import release_delivery as delivery
from forge_client.contracts import SetupError
from forge_client.install import revision
from tests.test_prepared_release_selection import release
from tests.test_release_artifacts import REVISION, manifest, local_manifest


def fixture(value=None):
    value = manifest() if value is None else value
    raw = json.dumps(value).encode()
    assets = [{'id': index + 10, 'name': name, 'size': value['artifacts'][kind]['size'],
               'digest': 'sha256:' + value['artifacts'][kind]['sha256']}
              for index, (kind, name) in enumerate(ASSETS.items()) if kind in value['artifacts']]
    assets.append({'id': 42, 'name': 'manifest.json', 'size': len(raw)})
    return value, raw, release(assets=assets)


def redirect(url='https://release-assets.githubusercontent.com/path?signature=temporary', code=302):
    return urllib.error.HTTPError('https://api.github.com/asset', code, 'redirect', {'Location': url}, io.BytesIO())


class ReleaseDeliveryTests(unittest.TestCase):
    def test_default_revision_uses_authenticated_manifest_read_for_private_repository(self):
        value, raw, metadata = fixture()
        run = Mock(side_effect=[json.dumps([metadata]).encode(), json.dumps(metadata).encode(), raw])
        with patch('shutil.which', return_value='gh'):
            self.assertEqual(revision(run=run), REVISION)
        manifest_call = run.call_args_list[-1]
        self.assertEqual(manifest_call.args[0][-1], 'repos/lucbocahut/forge/releases/assets/42')
        self.assertIn('Accept: application/octet-stream', manifest_call.args[0])
        self.assertEqual(manifest_call.kwargs['output_limit'], 128 * 1024)

    def test_local_workers_release_selects_and_transfers_only_ui(self):
        value, raw, metadata = fixture(local_manifest())
        run = Mock(side_effect=[json.dumps([metadata]).encode(), json.dumps(metadata).encode(), raw])
        with patch('shutil.which', return_value='gh'):
            self.assertEqual(revision(run=run), REVISION)
        run = Mock(side_effect=[json.dumps(metadata).encode(), raw, b'gho_local_only'])
        opener = Mock(); opener.open.side_effect = redirect()
        with patch('shutil.which', return_value='gh'):
            result = delivery.access(REVISION, run=run, opener=opener)
        self.assertEqual(set(result['urls']), {'ui'})
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(result['manifest']['workers'], value['workers'])

    def test_worker_download_requires_a_published_worker_not_a_local_recipe(self):
        value, raw, metadata = fixture(local_manifest())
        run = Mock(side_effect=[json.dumps(metadata).encode(), raw])
        with patch('shutil.which', return_value='gh'), self.assertRaises(SetupError) as raised:
            delivery.access(REVISION, ['runner'], run=run)
        self.assertEqual(raised.exception.code, 'invalid_prepared_assets')
        self.assertEqual(run.call_count, 2)

    def test_incomplete_v1_does_not_become_local_build(self):
        value, raw, metadata = fixture()
        metadata['assets'] = [asset for asset in metadata['assets'] if asset['name'] != ASSETS['runner']]
        run = Mock(side_effect=[json.dumps(metadata).encode(), raw])
        with patch('shutil.which', return_value='gh'), self.assertRaises(SetupError):
            delivery.read_manifest(REVISION, run=run)

    def test_scoped_access_returns_only_selected_assets_and_no_personal_token(self):
        value, raw, metadata = fixture()
        run = Mock(side_effect=[json.dumps(metadata).encode(), raw, b'gho_local_only\n'])
        opener = Mock(); opener.open.side_effect = redirect()
        with patch('shutil.which', return_value='gh'):
            result = delivery.access(REVISION, ['runner'], run=run, opener=opener)
        self.assertEqual(set(result), {'revision', 'manifest', 'urls'})
        self.assertEqual(set(result['urls']), {'runner'})
        self.assertNotIn('gho_local_only', json.dumps(result))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.github.com/repos/lucbocahut/forge/releases/assets/11')
        self.assertEqual(request.get_header('Authorization'), 'Bearer gho_local_only')
        self.assertIsNone(delivery._NoRedirect().redirect_request(None, None, 302, '', {}, result['urls']['runner']))

    def test_unexpected_authority_or_network_error_cannot_leak_urls(self):
        value, raw, metadata = fixture()
        urls = ['http://release-assets.githubusercontent.com/path?secret=value',
                'https://github.com/path?secret=value',
                'https://user:password@release-assets.githubusercontent.com/path?secret=value',
                'https://release-assets.githubusercontent.com:443/path?secret=value',
                'https://release-assets.githubusercontent.com/path?secret=value#fragment']
        failures = [redirect(url) for url in urls] + [urllib.error.URLError('https://example.invalid?secret=value')]
        for failure in failures:
            run = Mock(side_effect=[json.dumps(metadata).encode(), raw, b'gho_local_only'])
            opener = Mock(); opener.open.side_effect = failure
            with self.subTest(failure=type(failure).__name__), patch('shutil.which', return_value='gh'):
                with self.assertRaises(SetupError) as raised:
                    delivery.access(REVISION, ['ui'], run=run, opener=opener)
            self.assertEqual(raised.exception.code, 'prepared_release_access_unavailable')
            self.assertNotIn('secret=value', str(raised.exception))
            self.assertNotIn('gho_local_only', str(raised.exception))

    def test_metadata_binding_rejects_drafts_duplicates_and_wrong_sizes(self):
        value, raw, original = fixture()
        for change in ({'draft': True}, {'tag_name': 'other'}, {'assets': original['assets'] + [original['assets'][0]]}):
            run = Mock(return_value=json.dumps({**original, **change}).encode())
            with patch('shutil.which', return_value='gh'), self.assertRaises(SetupError):
                delivery.metadata(REVISION, run=run)
        changed = json.loads(json.dumps(original)); changed['assets'][0]['size'] += 1
        run = Mock(side_effect=[json.dumps(changed).encode(), raw])
        with patch('shutil.which', return_value='gh'), self.assertRaises(SetupError):
            delivery.read_manifest(REVISION, run=run)

    def test_no_selected_assets_does_not_read_personal_token(self):
        value, raw, metadata = fixture()
        run = Mock(side_effect=[json.dumps(metadata).encode(), raw])
        with patch('shutil.which', return_value='gh'):
            self.assertEqual(delivery.access(REVISION, [], run=run)['urls'], {})
        self.assertEqual(run.call_count, 2)


if __name__ == '__main__':
    unittest.main()
