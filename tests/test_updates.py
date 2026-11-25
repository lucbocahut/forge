import io
import json
import unittest
from unittest.mock import Mock, patch

from forge.app_access import AccessError
from forge.release_artifacts import ASSETS
from forge.updates import UpdateCheck, public_manifest
from tests.test_release_artifacts import manifest, local_manifest

OLD, NEW = 'a' * 40, 'b' * 40


def release(revision=NEW, **changes):
    return dict(tag_name='forge-build-' + revision, published_at='2026-11-07T12:00:00Z',
                assets=[{'name': name} for name in [*ASSETS.values(), 'manifest.json']], **changes)


class UpdateTests(unittest.TestCase):
    def published_manifest(self, revision):
        value = manifest(); value['source'] = revision
        return value

    def check(self, responses, fetch_manifest=None):
        request = Mock(side_effect=responses)
        checker = UpdateCheck(request=request, clock=lambda: 100, monotonic=lambda: 100,
                              fetch_manifest=fetch_manifest or self.published_manifest)
        return checker, request

    def test_only_complete_published_builds_are_offered(self):
        incomplete = release(); incomplete['assets'] = []
        checker, request = self.check([[release(OLD), release(draft=True), release(prerelease=True), incomplete]])
        view = checker.view(OLD)
        self.assertEqual(view['status'], 'current')
        self.assertEqual(view['latest']['revision'], OLD)
        self.assertEqual(request.call_count, 1)
        self.assertIsNone(request.call_args.args[2], 'checks do not send personal or App credentials')

    def test_ui_only_release_is_discovered_for_manifest_validation_by_installer(self):
        candidate = release(OLD)
        candidate['assets'] = [{'name': 'manifest.json'}, {'name': ASSETS['ui']}]
        value = local_manifest(); value['source'] = OLD
        checker, _ = self.check([[candidate]], fetch_manifest=lambda revision: value)
        self.assertEqual(checker.view(OLD)['status'], 'current')

    def test_incomplete_v1_is_not_offered_as_a_local_worker_release(self):
        candidate = release(OLD)
        candidate['assets'] = [{'name': 'manifest.json'}, {'name': ASSETS['ui']}]
        checker, _ = self.check([[candidate]])
        result = checker.view(OLD)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['latest'])

    def test_corrupt_or_unavailable_manifest_is_not_an_update(self):
        for value in ({}, {'version': 3}):
            checker, _ = self.check([[release()]], fetch_manifest=lambda revision: value)
            result = checker.view(OLD)
            self.assertEqual(result['status'], 'unavailable')
            self.assertIsNone(result['latest'])
        checker, _ = self.check([[release()]], fetch_manifest=Mock(side_effect=OSError('network')))
        self.assertEqual(checker.view(OLD)['status'], 'unavailable')

    def test_public_manifest_is_bounded_and_has_no_credentials(self):
        response = io.BytesIO(json.dumps(manifest()).encode())
        response.status, response.headers = 200, {}
        with patch('forge.updates.urllib.request.urlopen', return_value=response) as fetch:
            self.assertEqual(public_manifest(OLD), manifest())
        self.assertEqual(fetch.call_args.args, ('https://github.com/lucbocahut/forge/releases/download/forge-build-' + OLD + '/manifest.json',))
        self.assertIn('timeout', fetch.call_args.kwargs)
        response = io.BytesIO(b'x' * (128 * 1024 + 1)); response.status, response.headers = 200, {}
        with patch('forge.updates.urllib.request.urlopen', return_value=response), self.assertRaises(ValueError):
            public_manifest(OLD)

    def test_comparison_prevents_offering_a_downgrade(self):
        for github, status in [('ahead', 'available'), ('behind', 'ahead'), ('diverged', 'diverged')]:
            with self.subTest(github=github):
                checker, request = self.check([[release()], {'status': github}])
                self.assertEqual(checker.view(OLD)['status'], status)
                self.assertIn(OLD + '...' + NEW, request.call_args.args[1])
                self.assertIsNone(request.call_args.args[2])

    def test_unknown_local_build_does_not_claim_up_to_date(self):
        checker, request = self.check([[release()]])
        self.assertEqual(checker.view(None)['status'], 'unknown')
        self.assertEqual(request.call_count, 1)
        checker, _ = self.check([[release()], AccessError('missing commit', status=404)])
        self.assertEqual(checker.view(OLD)['status'], 'unknown')

    def test_private_repo_and_rate_limits_are_not_success(self):
        for status in [404, 403, 429]:
            checker, _ = self.check([AccessError('private upstream details', status=status)])
            view = checker.view(OLD)
            self.assertEqual(view['status'], 'unavailable')
            self.assertIsNone(view['latest'])
            self.assertNotIn('private upstream details', view['error'])

    def test_missing_releases_is_explicit(self):
        checker, _ = self.check([[]])
        self.assertEqual(checker.view(OLD)['status'], 'no_release')

    def test_checks_are_cached_and_manual_refresh_is_bounded(self):
        now = [100]
        request = Mock(return_value=[release(OLD)])
        checker = UpdateCheck(request=request, clock=lambda: now[0], monotonic=lambda: now[0], fetch_manifest=self.published_manifest)
        checker.view(OLD)
        now[0] += 30
        checker.view(OLD, refresh=True)
        self.assertEqual(request.call_count, 1)
        now[0] += 31
        checker.view(OLD)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(checker.view(OLD, refresh=True)['checked_at'], now[0])
        self.assertEqual(request.call_count, 2)
        now[0] += 3601
        checker.view(OLD)
        self.assertEqual(request.call_count, 3)

    def test_installed_version_change_invalidates_cache(self):
        checker, request = self.check([[release()], {'status': 'ahead'}, [release()]])
        self.assertEqual(checker.view(OLD)['status'], 'available')
        self.assertEqual(checker.view(NEW)['status'], 'current')
        self.assertEqual(request.call_count, 3)
