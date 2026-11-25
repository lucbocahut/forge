import hashlib
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge import host_update
from forge import release_artifacts as artifacts

REVISION = 'a' * 40
INPUTS = {name: str(index) * 64 for index, name in enumerate(artifacts.ASSETS)}
BASE, FULL = 'sha256:' + 'b' * 64, 'sha256:' + 'c' * 64


def manifest(payload=b'archive'):
    return {'version': 1, 'source': REVISION, 'platform': 'linux/amd64', 'node_image': 'node@sha256:fixed',
            'python_image': 'python@sha256:fixed', 'artifacts': {
                kind: {'filename': filename, 'sha256': hashlib.sha256(payload).hexdigest(), 'size': len(payload),
                       'inputs': INPUTS[kind], **({'image_id': BASE if kind == 'runner' else FULL} if kind != 'ui' else {})}
                for kind, filename in artifacts.ASSETS.items()}}


def local_manifest(payload=b'archive'):
    value = manifest(payload)
    value['version'] = 2
    value['workers'] = {kind: {'delivery': 'local-build', 'inputs': INPUTS[kind]} for kind in ('runner', 'full')}
    value['artifacts'] = {'ui': value['artifacts']['ui']}
    return value


def validated(value):
    return artifacts.validate_manifest(value, REVISION, INPUTS, 'node@sha256:fixed', 'python@sha256:fixed')


class ManifestTests(unittest.TestCase):
    def test_exact_source_platform_pins_and_inputs_are_required(self):
        self.assertEqual(validated(manifest())['source'], REVISION)
        for field, value in [('source', 'd' * 40), ('platform', 'linux/arm64'), ('node_image', 'node:latest')]:
            candidate = manifest(); candidate[field] = value
            with self.subTest(field=field), self.assertRaises(artifacts.ArtifactError):
                validated(candidate)
        candidate = manifest(); candidate['artifacts']['runner']['inputs'] = 'e' * 64
        with self.assertRaisesRegex(artifacts.ArtifactError, 'build inputs differ'):
            validated(candidate)

    def test_descriptors_cannot_choose_another_download_source_or_unpinned_image(self):
        for field, value in [('filename', 'https://attacker.invalid/archive'), ('image_id', 'forge:latest'),
                             ('size', True), ('size', 2 * 1024**3)]:
            candidate = manifest(); candidate['artifacts']['runner'][field] = value
            with self.subTest(field=field), self.assertRaises(artifacts.ArtifactError):
                validated(candidate)

    def test_source_fallback_is_explicit_and_skips_downloads(self):
        with patch.dict('os.environ', {'FORGE_BUILD_FROM_SOURCE': '1'}), patch.object(artifacts, 'prepared') as prepared:
            self.assertIsNone(host_update.prepared_release())
            prepared.assert_not_called()
        with patch.dict('os.environ', {'FORGE_BUILD_FROM_SOURCE': 'yes'}):
            with self.assertRaises(artifacts.ArtifactError): artifacts.source_build()

    def test_arm64_linux_always_builds_from_source(self):
        for system, machine, expected in [('Linux', 'aarch64', True), ('Linux', 'arm64', True),
                                          ('Linux', 'x86_64', False), ('Darwin', 'arm64', False)]:
            with self.subTest(system=system, machine=machine), patch.dict('os.environ', {'FORGE_BUILD_FROM_SOURCE': ''}), \
                    patch.object(artifacts.platform, 'system', return_value=system), \
                    patch.object(artifacts.platform, 'machine', return_value=machine), \
                    patch.object(artifacts, 'prepared') as prepared, \
                    patch.object(host_update, 'artifact_inputs', return_value=INPUTS), \
                    patch.object(host_update, 'source_revision', return_value=REVISION):
                self.assertEqual(artifacts.source_build(), expected)
                self.assertEqual(host_update.prepared_release() is None, expected)
                self.assertEqual(prepared.called, not expected)

    def test_missing_manifest_does_not_build_from_source(self):
        with patch.object(artifacts.urllib.request, 'urlopen', side_effect=artifacts.urllib.error.URLError('release not published')):
            with self.assertRaisesRegex(artifacts.ArtifactError, 'Wait for its Prepared release workflow'):
                artifacts.read_manifest(REVISION)

    def test_unsupported_platform_is_explicit(self):
        artifacts.prepared.cache_clear()
        with patch.object(artifacts.platform, 'machine', return_value='aarch64'), patch.object(artifacts, 'read_manifest') as fetch:
            with self.assertRaisesRegex(artifacts.ArtifactError, 'Linux amd64'):
                artifacts.prepared(REVISION, INPUTS['runner'], INPUTS['full'], INPUTS['ui'], 'node', 'python')
            fetch.assert_not_called()


class LocalWorkerReleaseTests(unittest.TestCase):
    def test_policy_is_complete_explicit_and_bound_to_source_inputs(self):
        value = local_manifest()
        self.assertEqual(validated(value), value)
        self.assertEqual(artifacts.manifest_shape(value, REVISION), value)
        self.assertEqual(artifacts.artifact_kinds(value), ('ui',))
        for mutate in (
            lambda v: v.pop('workers'),
            lambda v: v['workers'].pop('full'),
            lambda v: v['workers']['runner'].update(delivery='prepared'),
            lambda v: v['workers']['runner'].update(inputs='e' * 64),
            lambda v: v['artifacts'].update(runner=manifest()['artifacts']['runner']),
            lambda v: v['artifacts'].pop('ui'),
        ):
            candidate = local_manifest(); mutate(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(artifacts.ArtifactError):
                validated(candidate)

    def test_host_builds_workers_but_keeps_prepared_ui(self):
        release = artifacts.Release(REVISION, validated(local_manifest()))
        missing = subprocess.CompletedProcess([], 1, '', '')
        built = subprocess.CompletedProcess([], 0, BASE + '\n', '')
        def unpack(target):
            (target / 'server').mkdir(parents=True)
            (target / 'server/index.mjs').write_text('prepared')
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(host_update, 'prepared_release', return_value=release), \
                patch.object(host_update.subprocess, 'run', return_value=missing), \
                patch.object(host_update, 'command', return_value=built) as command, \
                patch.object(release, 'image') as image, patch.object(release, 'ui', side_effect=unpack):
            self.assertEqual(host_update.runner_image(), BASE)
            self.assertEqual(host_update.full_runner_image(BASE), BASE)
            key = host_update.ui_release(Path(temporary))
            self.assertEqual((Path(temporary) / 'ui/releases' / key / 'server/index.mjs').read_text(), 'prepared')
            builds = [call.args[0] for call in command.call_args_list if call.args[0][:2] == ['docker', 'build']]
            self.assertEqual(len(builds), 2)
            self.assertTrue(any('Dockerfile.full' in str(argv) for argv in builds))
            image.assert_not_called()

    def test_missing_or_corrupt_ui_never_becomes_a_source_ui_build(self):
        release = artifacts.Release(REVISION, validated(local_manifest()))
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(host_update, 'prepared_release', return_value=release), \
                patch.object(artifacts.urllib.request, 'urlopen', return_value=io.BytesIO(b'bad')), \
                patch.object(host_update, 'command') as command:
            with self.assertRaises(artifacts.ArtifactError):
                host_update.ui_release(Path(temporary))
            command.assert_not_called()
        with self.assertRaises(artifacts.ArtifactError):
            release.image('runner')

    def test_only_ui_is_staged_promoted_and_reused(self):
        payload = b'archive'
        access = {'revision': REVISION, 'manifest': local_manifest(payload), 'urls': {
            'ui': 'https://release-assets.githubusercontent.com/release/ui?sig=temporary'}}
        opener = Mock(); opener.open.side_effect = lambda *a, **kw: io.BytesIO(payload)
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(artifacts.urllib.request, 'build_opener', return_value=opener):
            cache, promoted = Path(temporary) / 'cache', Path(temporary) / 'service'
            self.assertEqual(artifacts.stage(access, cache)['cached'], ['ui'])
            self.assertEqual(artifacts.stage(access, cache)['cached'], ['ui'])
            self.assertEqual(opener.open.call_count, 1)
            self.assertEqual(artifacts.copy_cache(REVISION, cache, promoted)['cached'], ['ui'])
            self.assertEqual(artifacts.cached_manifest(REVISION, promoted), access['manifest'])
            access['urls']['runner'] = access['urls']['ui']
            with self.assertRaises(artifacts.ArtifactError):
                artifacts.stage(access, cache)


class ConsumptionTests(unittest.TestCase):
    def test_corrupt_download_is_never_loaded_into_docker(self):
        release = artifacts.Release(REVISION, manifest())
        run = Mock(return_value=subprocess.CompletedProcess([], 1, '', ''))
        with patch.object(artifacts.urllib.request, 'urlopen', return_value=io.BytesIO(b'corrupt')):
            with self.assertRaisesRegex(artifacts.ArtifactError, 'checksum mismatch'):
                release.image('runner', run=run)
        self.assertFalse(any('load' in call.args[0] for call in run.call_args_list))

    def test_prepared_image_is_downloaded_once_then_checked_by_full_image_id_and_platform(self):
        release = artifacts.Release(REVISION, manifest())
        run = Mock(side_effect=[subprocess.CompletedProcess([], 1, '', ''), subprocess.CompletedProcess([], 0, '', ''),
                              subprocess.CompletedProcess([], 0, BASE + ' linux/amd64\n', '')])
        with patch.object(artifacts.urllib.request, 'urlopen', return_value=io.BytesIO(b'archive')) as fetch:
            self.assertEqual(release.image('runner', run=run), BASE)
        self.assertEqual(fetch.call_args.args[0], artifacts.asset_url(REVISION, artifacts.ASSETS['runner']))
        self.assertEqual(run.call_args_list[1].args[0][:3], ['docker', 'image', 'load'])
        cached = Mock(return_value=subprocess.CompletedProcess([], 0, BASE + ' linux/amd64\n', ''))
        with patch.object(release, 'download') as download:
            self.assertEqual(release.image('runner', run=cached), BASE)
            download.assert_not_called()
        wrong = Mock(return_value=subprocess.CompletedProcess([], 0, BASE + ' linux/arm64\n', ''))
        with self.assertRaisesRegex(artifacts.ArtifactError, 'image ID or platform'):
            release.image('runner', run=wrong)

    def ui_archive(self, *, traversal=False):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as bundle:
            entry = tarfile.TarInfo('../escape' if traversal else 'server/index.mjs')
            payload = b'export default {}'
            entry.size = len(payload)
            bundle.addfile(entry, io.BytesIO(payload))
        return stream.getvalue()

    def test_ui_is_extracted_only_after_checksum_validation(self):
        payload = self.ui_archive(); release = artifacts.Release(REVISION, manifest(payload))
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'ui'
            with patch.object(artifacts.urllib.request, 'urlopen', return_value=io.BytesIO(payload)):
                release.ui(target)
            self.assertEqual((target / 'server/index.mjs').read_bytes(), b'export default {}')
            with patch.object(artifacts.urllib.request, 'urlopen', return_value=io.BytesIO(b'bad')):
                with self.assertRaises(artifacts.ArtifactError): release.ui(target)
            self.assertEqual((target / 'server/index.mjs').read_bytes(), b'export default {}')

    def test_ui_archive_cannot_write_outside_its_release(self):
        payload = self.ui_archive(traversal=True); release = artifacts.Release(REVISION, manifest(payload))
        with tempfile.TemporaryDirectory() as temporary, patch.object(artifacts.urllib.request, 'urlopen', return_value=io.BytesIO(payload)):
            target = Path(temporary) / 'ui'
            with self.assertRaises(tarfile.FilterError): release.ui(target)
            self.assertFalse(target.exists())
            self.assertFalse((Path(temporary) / 'escape').exists())

    def test_host_uses_prepared_images_and_ui_without_building(self):
        release = Mock(manifest=manifest())
        release.image.side_effect = [BASE, FULL]
        def unpack(target):
            (target / 'server').mkdir(parents=True)
            (target / 'server/index.mjs').write_text('prepared')
        release.ui.side_effect = unpack
        with tempfile.TemporaryDirectory() as temporary, patch.object(host_update, 'prepared_release', return_value=release), patch.object(host_update, 'command') as command:
            self.assertEqual(host_update.runner_image(), BASE)
            self.assertEqual(host_update.full_runner_image(BASE), FULL)
            key = host_update.ui_release(Path(temporary))
            self.assertEqual((Path(temporary) / 'ui/releases' / key / 'server/index.mjs').read_text(), 'prepared')
            command.assert_not_called()
            self.assertEqual([call.args[0] for call in release.image.call_args_list], ['runner', 'full'])

    def test_full_image_is_lazy_and_requires_the_release_base(self):
        release = Mock(manifest=manifest())
        with patch.object(host_update, 'prepared_release', return_value=release):
            host_update.runner_image()
            release.image.assert_called_once()
            with self.assertRaisesRegex(ValueError, 'selected base image'):
                host_update.full_runner_image('sha256:' + 'd' * 64)
            release.image.assert_called_once()


class PrivateDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.cache = Path(temporary.name) / 'prepared'
        self.payload = b'archive'
        self.access = {'revision': REVISION, 'manifest': manifest(self.payload), 'urls': {
            kind: 'https://release-assets.githubusercontent.com/release/' + kind + '?sig=private-temporary-value'
            for kind in artifacts.ASSETS}}

    def stage(self, payload=None):
        opener = Mock()
        opener.open.side_effect = lambda *a, **kw: io.BytesIO(self.payload if payload is None else payload)
        with patch.object(artifacts.urllib.request, 'build_opener', return_value=opener):
            result = artifacts.stage(self.access, self.cache)
        return result, opener

    def test_signed_urls_never_persist_and_verified_assets_are_reused(self):
        result, opener = self.stage()
        self.assertEqual(set(result['cached']), set(artifacts.ASSETS))
        self.assertEqual(opener.open.call_count, 3)
        self.assertNotIn('private-temporary-value', str(result))
        for path in (self.cache / REVISION).iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(b'private-temporary-value', path.read_bytes())
        result, opener = self.stage()
        opener.open.assert_not_called()
        self.assertEqual(set(result['cached']), set(artifacts.ASSETS))

    def test_missing_or_corrupt_files_resume_without_redownloading_verified_assets(self):
        self.stage()
        (self.cache / REVISION / artifacts.ASSETS['full']).unlink()
        (self.cache / REVISION / artifacts.ASSETS['runner']).write_bytes(b'corrupt')
        self.assertEqual(artifacts.cache_status(REVISION, self.cache)['cached'], ['ui'])
        result, opener = self.stage()
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(set(result['cached']), set(artifacts.ASSETS))

    def test_failed_stream_never_promotes_partial_asset_or_prints_signed_url(self):
        opener = Mock()
        opener.open.side_effect = artifacts.urllib.error.URLError(self.access['urls']['ui'])
        with patch.object(artifacts.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(artifacts.ArtifactError) as caught:
                artifacts.stage(self.access, self.cache)
        self.assertNotIn('private-temporary-value', str(caught.exception))
        self.assertEqual(list((self.cache / REVISION).iterdir()), [self.cache / REVISION / 'manifest.json'])
        with self.assertRaisesRegex(artifacts.ArtifactError, 'checksum mismatch'):
            self.stage(b'corrupt')
        self.assertEqual(artifacts.cache_status(REVISION, self.cache)['cached'], [])

    def test_url_authority_and_manifest_revision_are_checked_before_writes(self):
        for url in ('http://release-assets.githubusercontent.com/file', 'https://attacker.invalid/file',
                    'https://user:secret@release-assets.githubusercontent.com/file',
                    'https://release-assets.githubusercontent.com:443/file',
                    'https://release-assets.githubusercontent.com/file#fragment'):
            with self.subTest(url=url):
                self.access['urls']['ui'] = url
                with self.assertRaises(artifacts.ArtifactError): artifacts.stage(self.access, self.cache)
                self.assertFalse(self.cache.exists())
        self.access['urls'] = {}
        self.access['manifest']['source'] = 'f' * 40
        with self.assertRaises(artifacts.ArtifactError): artifacts.stage(self.access, self.cache)

    def test_manifest_cannot_change_for_a_cached_source_revision(self):
        self.stage()
        self.access['manifest']['artifacts']['ui']['sha256'] = 'e' * 64
        with self.assertRaisesRegex(artifacts.ArtifactError, 'manifest changed'):
            artifacts.stage(self.access, self.cache)

    def test_cached_source_still_requires_exact_inputs_and_toolchain(self):
        self.stage(); artifacts.prepared.cache_clear()
        with patch.object(artifacts.Path, 'home', return_value=self.cache.parent), \
                patch.object(artifacts, 'cache_directory', side_effect=lambda revision, cache_root=None: self.cache / revision), \
                patch.object(artifacts.platform, 'system', return_value='Linux'), \
                patch.object(artifacts.platform, 'machine', return_value='x86_64'), \
                patch.object(artifacts.urllib.request, 'urlopen') as remote:
            with self.assertRaisesRegex(artifacts.ArtifactError, 'build inputs differ'):
                artifacts.prepared(REVISION, 'f' * 64, INPUTS['full'], INPUTS['ui'], 'node@sha256:fixed', 'python@sha256:fixed')
            release = artifacts.prepared(REVISION, INPUTS['runner'], INPUTS['full'], INPUTS['ui'], 'node@sha256:fixed', 'python@sha256:fixed')
            target = self.cache.parent / 'consumed'
            release.download('runner', target)
            self.assertEqual(target.read_bytes(), self.payload)
            remote.assert_not_called()
        artifacts.prepared.cache_clear()

    def test_bootstrap_copy_keeps_only_verified_bytes_and_private_modes(self):
        self.stage()
        (self.cache / REVISION / artifacts.ASSETS['full']).write_bytes(b'corrupt')
        destination = self.cache.parent / 'service-cache'
        result = artifacts.copy_cache(REVISION, self.cache, destination)
        self.assertEqual(set(result['cached']), {'runner', 'ui'})
        self.assertEqual(set(artifacts.cache_status(REVISION, destination)['cached']), {'runner', 'ui'})
        self.assertEqual((destination / REVISION).stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in (destination / REVISION).iterdir()))

    def test_redirects_from_signed_storage_are_refused(self):
        handler = artifacts._NoRedirect()
        with self.assertRaises(artifacts.ArtifactError):
            handler.redirect_request(None, None, 302, '', {}, 'https://attacker.invalid/file')


class PortableImageIdentityTests(unittest.TestCase):
    def setUp(self):
        self.oci = 'sha256:' + 'd' * 64
        self.value = manifest()
        self.value['artifacts']['runner']['image_manifest_id'] = self.oci
        self.release = artifacts.Release(REVISION, self.value)

    def test_optional_oci_pin_is_explicit_and_validated(self):
        self.assertEqual(validated(self.value)['artifacts']['runner']['image_manifest_id'], self.oci)
        self.value['artifacts']['runner']['image_manifest_id'] = 'forge:mutable'
        with self.assertRaises(artifacts.ArtifactError): validated(self.value)
        self.value['artifacts']['runner']['image_manifest_id'] = self.oci
        self.value['artifacts']['ui']['image_manifest_id'] = self.oci
        with self.assertRaises(artifacts.ArtifactError): validated(self.value)

    def test_classic_store_uses_config_id_without_loading_again(self):
        run = Mock(return_value=subprocess.CompletedProcess([], 0, BASE + ' linux/amd64\n', ''))
        with patch.object(self.release, 'download') as download:
            self.assertEqual(self.release.image('runner', run=run), BASE)
        download.assert_not_called()
        self.assertEqual(run.call_count, 1)

    def test_containerd_store_returns_pinned_manifest_id_after_load(self):
        calls = []
        loaded = False
        def run(argv, **kwargs):
            nonlocal loaded
            calls.append(argv)
            if argv[2] == 'load':
                loaded = True
                return subprocess.CompletedProcess(argv, 0, 'Loaded image ID: untrusted diagnostic', '')
            identity = argv[3]
            found = loaded and identity == self.oci
            return subprocess.CompletedProcess(argv, 0 if found else 1,
                self.oci + ' linux/amd64\n' if found else '', '')
        with patch.object(self.release, 'download') as download:
            self.assertEqual(self.release.image('runner', run=run), self.oci)
        download.assert_called_once()
        self.assertEqual([argv[3] for argv in calls if argv[2] == 'inspect'], [BASE, self.oci, BASE, self.oci])
        with patch.object(self.release, 'download') as download:
            self.assertEqual(self.release.image('runner', run=run), self.oci)
            download.assert_not_called()

    def test_containerd_lookup_can_resolve_config_pin_to_exact_manifest_pin(self):
        run = Mock(return_value=subprocess.CompletedProcess([], 0, self.oci + ' linux/amd64\n', ''))
        self.assertEqual(self.release.image('runner', run=run), self.oci)
        self.assertEqual(run.call_count, 1)

    def test_unpinned_digest_or_wrong_platform_is_rejected(self):
        for identity, platform in [('sha256:' + 'e' * 64, 'linux/amd64'), (self.oci, 'linux/arm64')]:
            with self.subTest(identity=identity, platform=platform):
                run = Mock(return_value=subprocess.CompletedProcess([], 0, identity + ' ' + platform, ''))
                with self.assertRaisesRegex(artifacts.ArtifactError, 'image ID or platform'):
                    self.release.image('runner', run=run)
        # Old manifests do not acquire permission to accept another store ID.
        legacy = artifacts.Release(REVISION, manifest())
        with self.assertRaises(artifacts.ArtifactError):
            legacy.image('runner', run=Mock(return_value=subprocess.CompletedProcess([], 0, self.oci + ' linux/amd64', '')))

    def test_full_runner_accepts_either_published_base_identity(self):
        release = Mock(manifest=self.value)
        release.image.return_value = FULL
        with patch.object(host_update, 'prepared_release', return_value=release):
            self.assertEqual(host_update.full_runner_image(BASE), FULL)
            self.assertEqual(host_update.full_runner_image(self.oci), FULL)
            with self.assertRaisesRegex(ValueError, 'selected base image'):
                host_update.full_runner_image('sha256:' + 'f' * 64)
        self.assertEqual(release.image.call_count, 2)
