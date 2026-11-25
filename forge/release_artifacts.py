"""Prepared release assets from the Forge repository, verified before use.

The publisher is the same GitHub repository trusted for Forge source. A manifest
binds each asset's checksum and build inputs to one exact source commit. Version 1
ships worker images; version 2 declares local worker builds and ships only the UI.
Host-specific runner qualification runs for both delivery methods.
"""
from functools import lru_cache
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import time
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
import urllib.parse

REPOSITORY = 'lucbocahut/forge'
ASSETS = {'ui': 'forge-ui-linux-amd64.tar.gz', 'runner': 'forge-runner-linux-amd64.tar.gz',
          'full': 'forge-runner-full-linux-amd64.tar.gz'}
SHA256 = re.compile(r'[0-9a-f]{64}')
IMAGE_ID = re.compile(r'sha256:[0-9a-f]{64}')


class ArtifactError(ValueError):
    pass


def source_build():
    value = os.environ.get('FORGE_BUILD_FROM_SOURCE', '')
    if value not in ('', '0', '1'):
        raise ArtifactError('FORGE_BUILD_FROM_SOURCE must be 0 or 1.')
    return value == '1'


def require(value, message):
    if not value:
        raise ArtifactError(message)


def release_tag(revision):
    require(isinstance(revision, str) and re.fullmatch(r'[0-9a-f]{40}', revision),
            'Prepared artifacts require an exact source commit. For a development checkout, explicitly set FORGE_BUILD_FROM_SOURCE=1.')
    return 'forge-build-' + revision


def asset_url(revision, name):
    return 'https://github.com/' + REPOSITORY + '/releases/download/' + release_tag(revision) + '/' + name


def artifact_kinds(value):
    """Prepared payloads are explicit; v2 builds both workers on the host."""
    require(isinstance(value, dict) and type(value.get('version')) is int
            and value['version'] in (1, 2), 'Unsupported prepared manifest version.')
    return tuple(ASSETS) if value['version'] == 1 else ('ui',)


def local_worker(value, kind):
    require(kind in ('runner', 'full'), 'Invalid worker kind.')
    return value['version'] == 2 and value['workers'][kind]['delivery'] == 'local-build'


def validate_manifest(value, revision, inputs, node_image, python_image):
    kinds = artifact_kinds(value)
    keys = {'version', 'source', 'platform', 'node_image', 'python_image', 'artifacts'}
    require(set(value) == keys | ({'workers'} if value['version'] == 2 else set()),
            'Invalid prepared artifact manifest.')
    require(value['source'] == revision and value['platform'] == 'linux/amd64'
            and value['node_image'] == node_image and value['python_image'] == python_image,
            'Prepared artifacts do not match this source revision, platform or pinned toolchain.')
    require(isinstance(value['artifacts'], dict) and set(value['artifacts']) == set(kinds), 'Incomplete prepared release.')
    if value['version'] == 2:
        require(isinstance(value['workers'], dict) and set(value['workers']) == {'runner', 'full'},
                'Incomplete local worker build policy.')
        for kind, worker in value['workers'].items():
            require(isinstance(worker, dict) and set(worker) == {'delivery', 'inputs'}
                    and worker['delivery'] == 'local-build', 'Invalid local worker build policy.')
            require(isinstance(worker['inputs'], str) and SHA256.fullmatch(worker['inputs'])
                    and worker['inputs'] == inputs[kind], 'Local ' + kind + ' build inputs differ from the checked-out source.')
    for kind, asset in value['artifacts'].items():
        keys = {'filename', 'sha256', 'size', 'inputs'} | ({'image_id'} if kind != 'ui' else set())
        allowed_keys = (keys, keys | {'image_manifest_id'}) if kind != 'ui' else (keys,)
        require(isinstance(asset, dict) and set(asset) in allowed_keys and asset['filename'] == ASSETS[kind], 'Invalid prepared asset descriptor.')
        require(isinstance(asset['sha256'], str) and SHA256.fullmatch(asset['sha256'])
                and type(asset['size']) is int and 0 < asset['size'] < 2 * 1024**3,
                'Invalid prepared asset checksum or size.')
        require(isinstance(asset['inputs'], str) and SHA256.fullmatch(asset['inputs'])
                and asset['inputs'] == inputs[kind], 'Prepared ' + kind + ' build inputs differ from the checked-out source. '
                'Use a clean published revision or explicitly set FORGE_BUILD_FROM_SOURCE=1 for development.')
        if kind != 'ui':
            require(isinstance(asset['image_id'], str) and IMAGE_ID.fullmatch(asset['image_id']), 'Invalid prepared runner image ID.')
            if 'image_manifest_id' in asset:
                require(isinstance(asset['image_manifest_id'], str) and IMAGE_ID.fullmatch(asset['image_manifest_id']),
                        'Invalid prepared runner manifest ID.')
    return value


def read_manifest(revision):
    cached = cache_directory(revision) / 'manifest.json'
    if cached.exists():
        return cached_manifest(revision)
    try:
        with urllib.request.urlopen(asset_url(revision, 'manifest.json'), timeout=30) as response:
            raw = response.read(128 * 1024 + 1)
    except (urllib.error.URLError, TimeoutError) as error:
        raise ArtifactError('Prepared artifacts are unavailable for ' + str(revision) + '. Wait for its Prepared release workflow '
                            'or select a published release. Development builds require explicit FORGE_BUILD_FROM_SOURCE=1.') from error
    require(len(raw) <= 128 * 1024, 'Prepared artifact manifest is too large.')
    try:
        return json.loads(raw)
    except (UnicodeError, ValueError) as error:
        raise ArtifactError('Invalid prepared artifact manifest JSON.') from error


@lru_cache(maxsize=4)
def prepared(revision, runner_inputs, full_inputs, ui_inputs, node_image, python_image):
    require(platform.system() == 'Linux' and platform.machine() in ('x86_64', 'amd64'),
            'Prepared Forge artifacts currently support Linux amd64. Other development targets require explicit FORGE_BUILD_FROM_SOURCE=1.')
    manifest = read_manifest(revision)
    inputs = {'runner': runner_inputs, 'full': full_inputs, 'ui': ui_inputs}
    return Release(revision, validate_manifest(manifest, revision, inputs, node_image, python_image))


class Release:
    def __init__(self, revision, manifest):
        self.revision, self.manifest = revision, manifest

    def download(self, kind, destination):
        require(kind in artifact_kinds(self.manifest), 'This release builds the worker locally.')
        asset = self.manifest['artifacts'][kind]
        digest, size = hashlib.sha256(), 0
        cached = cache_directory(self.revision) / asset['filename']
        if cached.exists():
            private_file(cached)
            source = cached.open('rb')
        else:
            source = None
        try:
            with source or urllib.request.urlopen(asset_url(self.revision, asset['filename']), timeout=120) as response, open(destination, 'wb') as target:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    require(size <= asset['size'], 'Prepared asset exceeds its declared size.')
                    digest.update(chunk)
                    target.write(chunk)
        except (urllib.error.URLError, TimeoutError) as error:
            raise ArtifactError('Could not download prepared ' + kind + ' artifact. Resume setup to retry.') from error
        require(size == asset['size'] and digest.hexdigest() == asset['sha256'], 'Prepared ' + kind + ' artifact checksum mismatch.')

    def image(self, kind, *, run=subprocess.run):
        require(kind in artifact_kinds(self.manifest), 'This release builds the worker locally.')
        asset = self.manifest['artifacts'][kind]
        # Docker's classic store identifies images by config digest; containerd
        # uses the OCI manifest digest. Both pins come from the published archive.
        identities = tuple(dict.fromkeys(asset[key] for key in ('image_id', 'image_manifest_id') if key in asset))
        def installed():
            for identity in identities:
                found = run(['docker', 'image', 'inspect', identity, '--format', '{{.Id}} {{.Os}}/{{.Architecture}}'],
                            capture_output=True, text=True)
                if found.returncode:
                    continue
                parts = found.stdout.strip().split()
                require(len(parts) == 2 and parts[0] in identities and parts[1] == 'linux/amd64',
                        'Loaded runner differs from the prepared image ID or platform.')
                return parts[0]
            return None
        image = installed()
        if image is None:
            print('Downloading the prepared ' + ('full worker' if kind == 'full' else 'worker') + ' image…', flush=True)
            with tempfile.TemporaryDirectory(prefix='forge-release-') as temporary:
                archive = Path(temporary) / ASSETS[kind]
                self.download(kind, archive)
                run(['docker', 'image', 'load', '--input', str(archive)], check=True)
            image = installed()
        require(image is not None, 'Loaded runner differs from the prepared image ID or platform.')
        return image

    def ui(self, target):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        print('Downloading the prepared operator UI…', flush=True)
        with tempfile.TemporaryDirectory(prefix='.prepared-', dir=target.parent) as temporary:
            archive = Path(temporary) / ASSETS['ui']
            staging = Path(temporary) / 'output'
            self.download('ui', archive)
            staging.mkdir()
            # Python's data filter rejects traversal and links outside the release.
            with tarfile.open(archive, 'r:gz') as bundle:
                bundle.extractall(staging, filter='data')
            require((staging / 'server/index.mjs').is_file(), 'Prepared UI has no server entry point.')
            if target.exists():
                shutil.rmtree(target)
            os.replace(staging, target)


# The CLI obtains asset-specific signed URLs with its local GitHub access.
# Only validated bytes enter this cache; URLs remain in the SSH request memory.
def cache_directory(revision, cache_root=None):
    release_tag(revision)
    return Path(cache_root or Path.home() / '.cache/forge/prepared') / revision


def private_file(path, *, uid=None):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and not stat.S_IMODE(info.st_mode) & 0o077
            and info.st_uid == (os.geteuid() if uid is None else uid) and info.st_nlink == 1,
            'Prepared cache file must be private and owned by the service account.')


def private_directory(path, *, uid=None, gid=None):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    owner = os.geteuid() if uid is None else uid
    require(stat.S_ISDIR(info.st_mode) and not stat.S_IMODE(info.st_mode) & 0o077
            and info.st_uid in (os.geteuid(), owner), 'Prepared cache directory must be private and owned.')
    if uid is not None:
        os.chown(path, uid, gid if gid is not None else -1)


def manifest_shape(value, revision):
    kinds = artifact_kinds(value)
    require(isinstance(value.get('artifacts'), dict) and set(value['artifacts']) == set(kinds),
            'Invalid prepared artifact manifest.')
    require(isinstance(value.get('node_image'), str) and isinstance(value.get('python_image'), str),
            'Invalid prepared toolchain descriptor.')
    descriptors = dict(value['artifacts'])
    if value['version'] == 2:
        require(isinstance(value.get('workers'), dict) and set(value['workers']) == {'runner', 'full'},
                'Incomplete local worker build policy.')
        descriptors.update(value['workers'])
    inputs = {}
    for kind, item in descriptors.items():
        require(isinstance(item, dict) and isinstance(item.get('inputs'), str)
                and SHA256.fullmatch(item['inputs']), 'Invalid prepared build inputs.')
        inputs[kind] = item['inputs']
    # Actual toolchain and tree pins are checked again by prepared() before use.
    return validate_manifest(value, revision, inputs, value['node_image'], value['python_image'])


def cached_manifest(revision, cache_root=None):
    path = cache_directory(revision, cache_root) / 'manifest.json'
    private_file(path)
    require(path.stat().st_size <= 128 * 1024, 'Prepared artifact manifest is too large.')
    try:
        return manifest_shape(json.loads(path.read_bytes()), revision)
    except (UnicodeError, ValueError) as error:
        raise ArtifactError('Invalid cached prepared artifact manifest.') from None


def verified_file(path, asset, *, uid=None):
    if not path.exists():
        return False
    private_file(path, uid=uid)
    if path.stat().st_size != asset['size']:
        return False
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024): checksum.update(chunk)
    return checksum.hexdigest() == asset['sha256']


def cache_status(revision, cache_root=None):
    directory = cache_directory(revision, cache_root)
    if not (directory / 'manifest.json').exists():
        return {'revision': revision, 'cached': []}
    manifest = cached_manifest(revision, cache_root)
    return {'revision': revision, 'cached': [kind for kind, asset in manifest['artifacts'].items()
            if verified_file(directory / asset['filename'], asset)]}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        raise ArtifactError('Prepared asset download redirected outside its signed request.')


def _signed_url(value):
    require(isinstance(value, str) and len(value) <= 16384, 'Invalid prepared asset download URL.')
    try:
        parsed = urllib.parse.urlsplit(value)
        allowed = (parsed.scheme == 'https' and parsed.hostname == 'release-assets.githubusercontent.com'
                   and not parsed.username and not parsed.password and parsed.port is None
                   and not parsed.fragment and bool(parsed.query))
    except ValueError:
        allowed = False
    require(allowed, 'Prepared assets must use GitHub release asset storage.')
    return value


def _atomic_file(path, write, *, uid=None, gid=None):
    descriptor, temporary = tempfile.mkstemp(prefix='.receiving-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            write(stream)
            stream.flush(); os.fsync(stream.fileno())
            if uid is not None: os.fchown(stream.fileno(), uid, gid if gid is not None else -1)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def stage(access, cache_root=None):
    require(isinstance(access, dict) and set(access) == {'revision', 'manifest', 'urls'},
            'Invalid prepared asset handoff.')
    revision = access['revision']; directory = cache_directory(revision, cache_root)
    manifest = manifest_shape(access['manifest'], revision)
    require(isinstance(access['urls'], dict) and set(access['urls']) <= set(artifact_kinds(manifest)), 'Invalid prepared asset selection.')
    urls = {kind: _signed_url(url) for kind, url in access['urls'].items()}
    raw = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    require(len(raw) <= 128 * 1024, 'Prepared artifact manifest is too large.')
    private_directory(directory.parent); private_directory(directory)
    if (directory / 'manifest.json').exists():
        require(cached_manifest(revision, cache_root) == manifest, 'Prepared release manifest changed for this source revision.')
    else:
        _atomic_file(directory / 'manifest.json', lambda stream: stream.write(raw))
    opener = urllib.request.build_opener(_NoRedirect())
    for kind, url in urls.items():
        asset = manifest['artifacts'][kind]; target = directory / asset['filename']
        if verified_file(target, asset): continue
        def download(stream):
            checksum, size, deadline = hashlib.sha256(), 0, time.monotonic() + 900
            try:
                with opener.open(url, timeout=120) as response:
                    while chunk := response.read(1024 * 1024):
                        require(time.monotonic() <= deadline, 'Prepared asset download timed out. Resume setup to retry.')
                        size += len(chunk)
                        require(size <= asset['size'], 'Prepared asset exceeds its declared size.')
                        checksum.update(chunk); stream.write(chunk)
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
                raise ArtifactError('Prepared asset download failed. Resume the CLI to obtain fresh download access.') from None
            require(size == asset['size'] and checksum.hexdigest() == asset['sha256'],
                    'Prepared ' + kind + ' artifact checksum mismatch.')
        _atomic_file(target, download)
    return cache_status(revision, cache_root)


def copy_cache(revision, source_root, destination_root, *, uid=None, gid=None):
    """Promote the bootstrap cache to the service account without URL metadata."""
    manifest = cached_manifest(revision, source_root)
    source = cache_directory(revision, source_root)
    target = cache_directory(revision, destination_root)
    private_directory(target.parent, uid=uid, gid=gid)
    private_directory(target, uid=uid, gid=gid)
    existing = target / 'manifest.json'
    if existing.exists():
        private_file(existing, uid=uid)
        require(existing.stat().st_size <= 128 * 1024, 'Prepared artifact manifest is too large.')
        try:
            previous = manifest_shape(json.loads(existing.read_bytes()), revision)
        except (UnicodeError, ValueError):
            raise ArtifactError('Invalid cached prepared artifact manifest.') from None
        require(previous == manifest, 'Prepared release manifest changed for this source revision.')
    for asset in manifest['artifacts'].values():
        if not verified_file(source / asset['filename'], asset): continue
        if verified_file(target / asset['filename'], asset, uid=uid): continue
        def copy(stream):
            with (source / asset['filename']).open('rb') as original:
                shutil.copyfileobj(original, stream, 1024 * 1024)
        _atomic_file(target / asset['filename'], copy, uid=uid, gid=gid)
    raw = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    _atomic_file(target / 'manifest.json', lambda stream: stream.write(raw), uid=uid, gid=gid)
    return {'revision': revision, 'cached': [kind for kind, asset in manifest['artifacts'].items()
            if verified_file(target / asset['filename'], asset, uid=uid)]}
