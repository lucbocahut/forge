import json
import unittest
from unittest.mock import Mock, patch

from forge_client.install import revision
from forge_client.contracts import SetupError
from tests.test_release_artifacts import manifest, REVISION


def release(commit=REVISION, published='2026-11-01T00:00:00Z', **extra):
    return {'draft': False, 'prerelease': False, 'tag_name': 'forge-build-' + commit, 'published_at': published,
            'assets': [{'name': name} for name in ['manifest.json', 'forge-ui-linux-amd64.tar.gz',
                       'forge-runner-linux-amd64.tar.gz', 'forge-runner-full-linux-amd64.tar.gz']], **extra}


class PreparedReleaseSelectionTests(unittest.TestCase):
    def test_default_selects_newest_complete_published_release_not_main_or_draft(self):
        rows = [release('b' * 40, '2026-11-03T00:00:00Z', draft=True),
                release('c' * 40, '2026-11-04T00:00:00Z', prerelease=True),
                release('d' * 40, '2026-11-05T00:00:00Z', assets=[]),
                release('e' * 40, '2026-11-06T00:00:00Z', tag_name='v1.0'),
                release('f' * 40, '2026-10-30T00:00:00Z'), release()]
        run, fetch = Mock(return_value=json.dumps(rows).encode()), Mock(return_value=manifest())
        with patch('forge_client.install.shutil.which', return_value='/usr/bin/gh'):
            self.assertEqual(revision(run=run, fetch_manifest=fetch), REVISION)
        self.assertEqual(run.call_args.args[0][-1], 'repos/lucbocahut/forge/releases?per_page=100')
        fetch.assert_called_once_with(REVISION)

    def test_explicit_sha_is_stable_without_network(self):
        run, fetch = Mock(), Mock()
        self.assertEqual(revision(REVISION, run=run, fetch_manifest=fetch), REVISION)
        run.assert_not_called(); fetch.assert_not_called()

    def test_incomplete_release_or_wrong_manifest_stops_before_host_work(self):
        bad = manifest(); bad['source'] = 'b' * 40
        for rows, value in [([], manifest()), ([release()], bad), ([release()], {})]:
            with self.subTest(rows=rows, manifest=value), patch('forge_client.install.shutil.which', return_value='gh'):
                with self.assertRaises(SetupError) as raised:
                    revision(run=Mock(return_value=json.dumps(rows).encode()), fetch_manifest=Mock(return_value=value))
                self.assertEqual(raised.exception.code, 'prepared_release_unavailable')

    def test_network_failure_has_release_specific_recovery(self):
        with patch('forge_client.install.shutil.which', return_value='gh'):
            with self.assertRaisesRegex(SetupError, 'Wait for the Prepared release workflow'):
                revision(run=Mock(side_effect=SetupError('github_timeout', 'Timeout')))
