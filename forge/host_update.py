"""Converge a Forge host to its checked-out source.

`bin/forge setup` and `bin/forge update` run the same reconciliation. Each host
component (runner image, isolation policies, egress, operator UI, services) is
compared with what this source wants; only what changed is rebuilt, qualified,
replaced and restarted. Every prepared artifact is verified and the runner canary passes
before anything in use is touched, so a failed update leaves the host as it was.
An interrupted update is resumed by rerunning. Selected SQLite installations
require a compatible forward update; older JSON-only source is not a rollback.
"""
import difflib
import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from .onboarding import Onboarding
from .settings import write_json
from .factory.control import controller_lock, ControllerBusy
from .configuration_authority import RUNTIME_DOMAINS

SOURCE = Path(__file__).resolve().parent.parent
NODE_IMAGE = 'node:24-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6'
PYTHON_IMAGE = 'python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea'
SECCOMP_URL = 'https://raw.githubusercontent.com/moby/profiles/3c28324314729dbade8287e868eef6338c42807a/seccomp/default.json'
SECCOMP_SHA = '536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74'
APPARMOR_PROFILE = 'forge-runner-private-proc'
APPARMOR_TARGET = Path('/etc/apparmor.d') / APPARMOR_PROFILE
UI_PORT, API_PORT = 8787, 8788
# Everything the runner Dockerfile can see (see .dockerignore). Over-inclusion
# only costs a rebuild; the image is tagged by this digest, so an unchanged
# source never rebuilds and never needs requalification.
RUNNER_INPUTS = ('deploy/runner/Dockerfile', '.dockerignore', 'forge/harnesses', 'forge/factory/job.py',
                 'forge/factory/harness.py', 'forge/factory/startup.py', 'forge/factory/probe.py', 'forge/factory/build_environment.py',
                 'deploy/runner/browser-canary.cjs', 'deploy/runner/gitconfig', 'deploy/runner/gateway.py', 'forge/mirror/frontend.py')
UI_SKIP = {'node_modules', '.output', '.nuxt', '.data', '.npm', 'tests'}
FULL_RUNNER_INPUTS = ('deploy/runner/Dockerfile.full', 'deploy/runner/services-canary.cjs')
SERVICES = ('forge-mirror', 'forge-registry', 'forge-status', 'forge-ui', 'forge-supervisor')
# Disabled leftovers of removed host services. Update deletes exactly these
# unit files when present; any other unit is left alone.
STALE_UNITS = tuple(name + suffix for name in ('forge-activity', 'forge-notify', 'forge-observability',
                                                'forge-babysitter', 'forge-triage')
                    for suffix in ('.service', '.timer'))
RECORD = 'host.json'
# A drop-in that sets any of these makes systemd run something other than the
# unit update writes, silently undoing it. Other drop-ins (NITRO_ALLOWED_HOSTS,
# resource limits) are the operator's and are kept.
OVERRIDING = re.compile(r'^\s*(ExecStart|ExecStartPre|WorkingDirectory)\s*=|^\s*Environment\s*=.*?(?<![A-Z_])'
                        r'(PYTHONPATH|FORGE_ROOT|FORGE_BIND_ADDRESS|FORGE_PORT|HOST|PORT|NITRO_FORGE_API)=', re.M)


def command(argv, **kwargs):
    return subprocess.run(argv, check=True, **kwargs)


def apparmor_help():
    source = SOURCE / 'deploy/runner' / (APPARMOR_PROFILE + '.apparmor')
    return ('Installing the runner AppArmor profile needs root, and sudo did not succeed for this account. '
            'Ask an administrator to run:\n  sudo install -m 0644 ' + str(source) + ' ' + str(APPARMOR_TARGET)
            + '\n  sudo apparmor_parser -r ' + str(APPARMOR_TARGET) + '\nthen rerun this command.')


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def tree_digest(paths, *extra, skip=frozenset()):
    h = hashlib.sha256()
    for value in extra:
        h.update(value.encode() + b'\0')
    for relative in paths:
        path = SOURCE / relative
        files = [path] if path.is_file() else sorted(
            f for f in path.rglob('*') if f.is_file() and '__pycache__' not in f.parts
            and not skip.intersection(f.relative_to(path).parts))
        for file in files:
            h.update(str(file.relative_to(SOURCE)).encode() + b'\0' + file.read_bytes() + b'\0')
    return h.hexdigest()


def source_revision():
    result = subprocess.run(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def pull():
    """Fast-forward the checkout to its upstream. Returns True when it moved."""
    git = ['git', '-C', str(SOURCE)]
    if not (SOURCE / '.git').exists():
        raise ValueError('This source is not a Git checkout; update it yourself, then run bin/forge update --no-pull')
    if command(git + ['status', '--porcelain', '--untracked-files=no'], capture_output=True, text=True).stdout.strip():
        raise ValueError('The Forge checkout has local changes. Commit or discard them, '
                         'or apply the checkout as it is with bin/forge update --no-pull')
    command(git + ['fetch', '--quiet'])
    head = command(git + ['rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    upstream = subprocess.run(git + ['rev-parse', '@{upstream}'], capture_output=True, text=True)
    if upstream.returncode != 0:
        raise ValueError('The Forge checkout has no upstream branch; update it yourself, then run bin/forge update --no-pull')
    if upstream.stdout.strip() == head:
        return False
    if subprocess.run(git + ['merge-base', '--is-ancestor', 'HEAD', '@{upstream}']).returncode != 0:
        raise ValueError('The Forge checkout has diverged from its upstream. Reconcile it, then run bin/forge update --no-pull')
    command(git + ['merge', '--ff-only', '--quiet', '@{upstream}'])
    return True


def check_bind(bind):
    address = ipaddress.ip_address(bind)
    if not address.is_loopback and not (address.version == 4 and address in ipaddress.ip_network('100.64.0.0/10')):
        raise ValueError('Bind the unauthenticated operator UI to loopback or a private tailnet address')
    return bind


def resolve_bind(root, requested, units):
    """Explicit choice, then the recorded one, then what an older install bound."""
    if requested:
        return check_bind(requested)
    recorded = Record(root).data.get('bind')
    if recorded:
        return recorded
    # Before the UI moved in front, forge-status itself served the operator address.
    old = units / 'forge-status.service'
    if old.exists():
        for line in old.read_text().splitlines():
            if 'FORGE_BIND_ADDRESS=' in line:
                value = line.split('FORGE_BIND_ADDRESS=', 1)[1].strip().strip('"')
                if value and value != '127.0.0.1':
                    return check_bind(value)
    return '127.0.0.1'


class Record:
    """What this reconciliation installed: digests of managed files and the
    releases in use. Saved after every managed write so an interrupted update
    never mistakes its own output for an operator's hand edit."""

    def __init__(self, root):
        self.root = Path(root)
        self.path = Path(root) / RECORD
        snapshot = runtime_snapshot(root, RECORD)
        self.revision = snapshot['revision'] if snapshot else 0
        self.credential_ref = snapshot['credential_ref'] if snapshot else None
        self.data = snapshot['settings'] if snapshot else {}
        self.data.setdefault('managed', {})

    def save(self, **values):
        self.data.update(values)
        self.revision = save_runtime_document(self.root, RECORD, self.data,
                                             expected_revision=self.revision, credential_ref=self.credential_ref)

    def managed(self, path, content):
        self.data['managed'][str(path)] = digest_bytes(content)
        self.save()

    def owns(self, path, current):
        return self.data['managed'].get(str(path)) == digest_bytes(current)


def unit_quote(value):
    return json.dumps(str(value).replace('%', '%%'))


def unit(description, execution, environment=(), *, after='network-online.target', extra='', requires=None):
    return ('[Unit]\nDescription=' + description + '\nAfter=' + after + ('\nRequires=' + requires if requires else '') + '\n\n[Service]\nType=simple\n' + extra
            + 'ExecStart=' + execution + '\n'
            + ''.join('Environment=' + unit_quote(value) + '\n' for value in environment)
            + 'Restart=on-failure\nRestartSec=5\nTimeoutStopSec=30\nUMask=0077\n\n[Install]\nWantedBy=default.target\n')


UPSTREAM_SECRET = 'state/ui-upstream.secret'


def ensure_upstream_secret(root):
    """The secret the UI presents to the status API (0600, created once)."""
    import secrets
    path = Path(root) / UPSTREAM_SECRET
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_urlsafe(32))
    return path


def runtime_snapshot(root, name):
    """Read settings and their revision together; keep that revision until saving."""
    from .configuration_authority import ConfigurationAuthority
    return ConfigurationAuthority(root).snapshot(name)


def runtime_document(root, name):
    row = runtime_snapshot(root, name)
    return row['settings'] if row else None


def save_runtime_document(root, name, value, *, expected_revision, credential_ref=None):
    from .configuration_authority import ConfigurationAuthority
    return ConfigurationAuthority(root).replace(name, value, expected_revision=expected_revision,
                                                 credential_ref=credential_ref)


def ui_container(root):
    return 'forge-ui-' + hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:12]


def desired_units(root, bind):
    root = Path(root)
    python = ['PYTHONPATH=' + str(SOURCE), 'FORGE_ROOT=' + str(root)]
    docker = shutil.which('docker') or '/usr/bin/docker'
    name = ui_container(root)
    # The UI runs from a pinned Node image, so the host needs no Node toolchain.
    # Host networking lets it bind the operator address and reach the loopback API.
    ui = [docker, 'run', '--rm', '--name', name, '--network', 'host', '--read-only',
          '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', f'{os.getuid()}:{os.getgid()}',
          '--memory', '512m', '--pids-limit', '256', '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m',
          '--mount', 'type=bind,source=' + str(root / 'ui/current') + ',target=/app,readonly',
          '--mount', 'type=bind,source=' + str(root / UPSTREAM_SECRET) + ',target=/run/forge/upstream.secret,readonly',
          '--env', 'NITRO_FORGE_UPSTREAM_SECRET_FILE=/run/forge/upstream.secret',
          '--env', 'NODE_ENV=production', '--env', 'HOST=' + bind, '--env', 'PORT=' + str(UI_PORT),
          '--env', f'NITRO_FORGE_API=http://127.0.0.1:{API_PORT}', '--env', 'FORGE_ROOT=/app',
          NODE_IMAGE, 'node', '/app/server/index.mjs']
    # Only the public address reaches the UI container. Identity credentials
    # and session state stay in the controller's private state directory.
    identity = None
    if (root / 'state/storage').exists():
        from forge.operator_auth import OperatorAuth
        identity = OperatorAuth(root).config()
        if identity:
            origin = identity['origin']
            from urllib.parse import urlsplit
            ui[-3:-3] = ['--env', 'NITRO_FORGE_PUBLIC_ORIGIN=' + origin,
                         '--env', 'NITRO_ALLOWED_HOSTS=' + urlsplit(origin).netloc]
            if bind == '127.0.0.1' and urlsplit(origin).hostname.endswith('.ts.net'):
                ui[-3:-3] = ['--env', 'NITRO_FORGE_TAILSCALE_IDENTITY=serve']
    ui[-3:-3] = ['--env', 'NUXT_PUBLIC_FORGE_SETUP_PENDING=' + ('false' if identity else 'true')]
    return {
        'forge-mirror': unit('Forge job-bound read-only Git mirrors', '/usr/bin/python3 -m forge.launch forge.mirror_daemon --root ' + unit_quote(root), python),
        'forge-registry': unit('Forge shared read-only package cache', '/usr/bin/python3 -m forge.launch forge.registry_cache --root ' + unit_quote(root), python),
        'forge-status': unit('Forge status API (loopback)', '/usr/bin/python3 -m forge.launch forge.status_api',
                             python + ['FORGE_BIND_ADDRESS=127.0.0.1', 'FORGE_PORT=' + str(API_PORT),
                                       'FORGE_UPSTREAM_SECRET_FILE=' + str(root / UPSTREAM_SECRET)]),
        'forge-ui': unit('Forge operator UI', ' '.join(unit_quote(part) for part in ui),
                         after='network-online.target forge-status.service',
                         # `docker run --rm` removes the container as soon as it
                         # exits, so stopping or replacing it must accept that it
                         # is already gone ("No such container") as done.
                         extra='ExecStartPre=-' + docker + ' rm --force ' + name + '\n'
                               'ExecStop=-' + docker + ' stop --time 10 ' + name + '\n'
                               'ExecStopPost=-' + docker + ' rm --force ' + name + '\n'),
        'forge-supervisor': unit('Forge supervisor', unit_quote(SOURCE / 'bin/forge') + ' service --root ' + unit_quote(root), python,
                                 after='network-online.target forge-mirror.service forge-registry.service',
                                 requires='forge-mirror.service forge-registry.service'),
    }


def artifact_inputs():
    guides = sorted(str(path.relative_to(SOURCE)) for path in (SOURCE / 'docs').glob('*.md'))
    assets = sorted(str(path.relative_to(SOURCE)) for path in (SOURCE / 'docs/assets').glob('*.svg'))
    return {'runner': tree_digest(RUNNER_INPUTS, NODE_IMAGE, PYTHON_IMAGE),
            'full': tree_digest((*RUNNER_INPUTS, *FULL_RUNNER_INPUTS), NODE_IMAGE, PYTHON_IMAGE),
            'ui': tree_digest(('ui', 'tools/ui_dependency_notices.py', *guides, *assets), NODE_IMAGE, skip=UI_SKIP)}


def prepared_release():
    from .release_artifacts import prepared, source_build
    if source_build():
        return None
    inputs = artifact_inputs()
    return prepared(source_revision(), inputs['runner'], inputs['full'], inputs['ui'], NODE_IMAGE, PYTHON_IMAGE)


def runner_image():
    release = prepared_release()
    from .release_artifacts import local_worker
    if release and not local_worker(release.manifest, 'runner'):
        return release.image('runner', run=subprocess.run)
    tag = 'forge-runner:' + tree_digest(RUNNER_INPUTS, NODE_IMAGE, PYTHON_IMAGE)[:16]
    inspect = ['docker', 'image', 'inspect', tag, '--format', '{{.Id}}']
    found = subprocess.run(inspect, capture_output=True, text=True)
    if found.returncode != 0:
        print('Building the runner image…', flush=True)
        command(['docker', 'build', '--build-arg', 'NODE_IMAGE=' + NODE_IMAGE, '--build-arg', 'PYTHON_IMAGE=' + PYTHON_IMAGE,
                 '-f', str(SOURCE / 'deploy/runner/Dockerfile'), '-t', tag, str(SOURCE)])
        found = command(inspect, capture_output=True, text=True)
    return found.stdout.strip()


def full_runner_image(base):
    release = prepared_release()
    from .release_artifacts import local_worker
    if release and not local_worker(release.manifest, 'full'):
        descriptor = release.manifest['artifacts']['runner']
        if base not in {descriptor[key] for key in ('image_id', 'image_manifest_id') if key in descriptor}:
            raise ValueError('Prepared full worker does not match the selected base image.')
        return release.image('full', run=subprocess.run)
    inputs = FULL_RUNNER_INPUTS
    tag = 'forge-runner-full:' + tree_digest(inputs, base)[:16]
    inspect = ['docker', 'image', 'inspect', tag, '--format', '{{.Id}}']
    found = subprocess.run(inspect, capture_output=True, text=True)
    if found.returncode:
        # BuildKit parses a bare sha256 image ID as a registry repository name.
        # Give the verified local image a content-derived tag for FROM.
        base_tag = 'forge-runner-base:' + base.removeprefix('sha256:')
        command(['docker', 'tag', base, base_tag])
        command(['docker', 'build', '--build-arg', 'RUNNER_IMAGE=' + base_tag,
                 '-f', str(SOURCE / 'deploy/runner/Dockerfile.full'), '-t', tag, str(SOURCE)])
        found = command(inspect, capture_output=True, text=True)
    return found.stdout.strip()


def seccomp_policy(root, record):
    """Returns the new policy path, or None when the installed one is current."""
    inputs = digest_bytes((SECCOMP_SHA + (SOURCE / 'deploy/runner/seccomp_profile.py').read_text()).encode())
    installed = Path(root) / 'runner-seccomp.json'
    if installed.exists() and record.data.get('seccomp_inputs') == inputs:
        return None, inputs
    candidate = Path(root) / 'runner-seccomp.json.new'
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / 'seccomp.json'
        with urllib.request.urlopen(SECCOMP_URL, timeout=30) as response:
            source.write_bytes(response.read(1024 * 1024))
        generate = runpy.run_path(str(SOURCE / 'deploy/runner/seccomp_profile.py'))['generate']
        write_json(candidate, generate(source, SECCOMP_SHA))
    if installed.exists() and installed.read_bytes() == candidate.read_bytes():
        candidate.unlink()
        return None, inputs
    return candidate, inputs


def ui_release(root):
    """Stage a prepared UI, or explicitly build source; releases are content-addressed."""
    root = Path(root)
    guides = sorted(str(path.relative_to(SOURCE)) for path in (SOURCE / 'docs').glob('*.md'))
    assets = sorted(str(path.relative_to(SOURCE)) for path in (SOURCE / 'docs/assets').glob('*.svg'))
    release = tree_digest(('ui', 'tools/ui_dependency_notices.py', *guides, *assets), NODE_IMAGE, skip=UI_SKIP)[:16]
    target = root / 'ui/releases' / release
    if (target / 'server/index.mjs').exists():
        return release
    prepared = prepared_release()
    if prepared:
        prepared.ui(target)
        return release
    print('Building the operator UI…', flush=True)
    build = root / 'ui/build'
    build.mkdir(parents=True, exist_ok=True)
    # Keep the npm cache between builds; everything else mirrors the source.
    for child in build.iterdir():
        if child.name != '.npm':
            shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
    for child in (SOURCE / 'ui').iterdir():
        if child.name in UI_SKIP:
            continue
        if child.is_dir():
            shutil.copytree(child, build / child.name, ignore=shutil.ignore_patterns(*UI_SKIP))
        else:
            shutil.copy2(child, build / child.name)
    command(['docker', 'run', '--rm', '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop', 'ALL',
             '--security-opt', 'no-new-privileges', '--mount', 'type=bind,source=' + str(build) + ',target=/ui',
             '--workdir', '/ui', '--env', 'HOME=/ui/.npm', '--env', 'npm_config_cache=/ui/.npm',
             '--env', 'NUXT_TELEMETRY_DISABLED=1', NODE_IMAGE,
             'sh', '-c', 'npm ci --no-audit --no-fund && npm run build'])
    staging = target.with_name(release + '.tmp')
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(build / '.output', staging)
    # The Help pages read <FORGE_ROOT>/docs/<guide>.md; the container sees only the release.
    (staging / 'docs').mkdir()
    for guide in guides:
        shutil.copy2(SOURCE / guide, staging / guide)
    (staging / 'docs/assets').mkdir()
    for asset in assets:
        shutil.copy2(SOURCE / asset, staging / asset)
    os.replace(staging, target)
    return release


def switch_ui(root, release):
    root = Path(root)
    link, temporary = root / 'ui/current', root / 'ui/current.new'
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(Path('releases') / release)
    os.replace(temporary, link)


def prune_ui(root, keep):
    releases = Path(root) / 'ui/releases'
    for child in releases.iterdir() if releases.exists() else ():
        if child.name not in keep:
            shutil.rmtree(child, ignore_errors=True)


def switch_runtime(root, image, profile):
    """Point every runtime configuration at the qualified image, between supervisor cycles."""
    try:
        with controller_lock(root, timeout=120):
            for name in RUNTIME_DOMAINS:
                snapshot = runtime_snapshot(root, name)
                if snapshot is not None:
                    config = snapshot['settings']
                    if config.get('image') != image or config.get('apparmor_profile') != profile:
                        config.update(image=image, apparmor_profile=profile)
                        save_runtime_document(root, name, config, expected_revision=snapshot['revision'],
                                              credential_ref=snapshot['credential_ref'])
    except ControllerBusy:
        raise ValueError('The supervisor stayed busy for two minutes; nothing was switched. Rerun bin/forge update.') from None


def switch_full_environment(root, image):
    with controller_lock(root, timeout=120):
        for name in RUNTIME_DOMAINS:
            snapshot = runtime_snapshot(root, name)
            if snapshot is not None:
                config = snapshot['settings']
                tiers = config.setdefault('environment_tiers', {})
                tiers['full'] = {**{'memory': '4g', 'cpus': 2}, **tiers.get('full', {}), 'image': image}
                save_runtime_document(root, name, config, expected_revision=snapshot['revision'],
                                      credential_ref=snapshot['credential_ref'])


def install_full_environment(root):
    record = Record(root)
    if not record.data.get('image'):
        raise ValueError('Run setup install before adding the full environment')
    base = runner_image()
    image = full_runner_image(base)
    qualify_runner(image, Path(root) / 'runner-seccomp.json', APPARMOR_PROFILE, full=True)
    switch_full_environment(root, image)
    record.save(full_image=image, updated_at=time.time())
    print('Full environment qualified. The supervisor can now assign databases per issue.')


def reachable(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status < 500
    except urllib.error.HTTPError as error:
        return error.code < 500
    except (OSError, ValueError):
        return False


def wait_healthy(bind, timeout=90):
    host = '[' + bind + ']' if ':' in bind else bind
    checks = {'forge-status': f'http://127.0.0.1:{API_PORT}/api/onboarding',
              'forge-ui': f'http://{host}:{UI_PORT}/'}
    deadline = time.monotonic() + timeout
    pending = dict(checks)
    while pending and time.monotonic() < deadline:
        pending = {name: url for name, url in pending.items() if not reachable(url)}
        if pending:
            time.sleep(2)
    if pending:
        raise ValueError(', '.join(pending) + ' did not answer after the update; inspect it with '
                         'journalctl --user -u ' + ' -u '.join(pending))


def stale_units(units):
    return [units / name for name in STALE_UNITS if os.path.lexists(units / name)]


def remove_stale_units(paths):
    for path in paths:
        # Stop and disable first so systemd keeps no wants links or running copy; both are no-ops when already off.
        subprocess.run(['systemctl', '--user', 'disable', '--now', path.name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        path.unlink(missing_ok=True)
        print('Removed stale unit ' + path.name)


def dropins(units):
    """(service, path, overriding) for each drop-in on a service this module manages."""
    for name in SERVICES:
        for path in sorted((units / (name + '.service.d')).glob('*.conf')):
            yield name, path, bool(OVERRIDING.search(path.read_text()))


def confirm(changes, assume_yes, overrides=()):
    if not changes and not overrides:
        return
    if not sys.stdin.isatty():
        if assume_yes:
            return
        # Unit contents can contain opaque credentials. Automated diagnostics
        # retain fixed guidance; interactive operators can review the full diff.
        raise ValueError('Files changed outside Forge would be replaced; inspect the service files and rerun with --yes')
    for path, current, desired in changes:
        print('\n' + str(path) + ' was changed outside Forge and will be replaced:')
        sys.stdout.writelines(difflib.unified_diff(current.splitlines(True), desired.splitlines(True), 'installed', 'update'))
    for _, path, _ in overrides:
        print('\n' + str(path) + ' overrides what this service runs and will be moved to the backups directory:')
        print(path.read_text().rstrip())
    if assume_yes:
        return
    if input('\nReplace them? [y/N] ').strip().lower() not in ('y', 'yes'):
        raise ValueError('Update cancelled; nothing was replaced')


def qualify_boundary_engine():
    from .factory.worker_boundary import WorkerBoundary
    # No resource creation: unsupported engines fail before building or switching.
    WorkerBoundary(lambda args: command(['docker', *args], capture_output=True, text=True),
                   image='sha256:' + '0' * 64).qualify()


def wait_service(root, module, failure, *, timeout=90):
    # Each probe opens and integrity-checks the store, which takes seconds once
    # the database is large; give every probe the remaining time, not a fixed slice.
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = subprocess.run(['/usr/bin/python3', '-m', 'forge.launch', module, '--root', str(root), '--check'],
                                    env={**os.environ, 'PYTHONPATH': str(SOURCE)}, capture_output=True, text=True,
                                    timeout=max(1, deadline - time.monotonic()))
            if result.returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() >= deadline:
            raise ValueError(failure)
        time.sleep(.25)


def wait_mirror(root):
    wait_service(root, 'forge.mirror_daemon',
                 'The job mirror service is unhealthy. Inspect forge-mirror.service and rerun the supported update.')


def wait_registry(root):
    wait_service(root, 'forge.registry_cache',
                 'The package cache service is unhealthy. Inspect forge-registry.service and rerun the supported update.')


def qualify_runner(image, seccomp, profile, *, full=False):
    """Offline canary: tools, memory limits, browser and credential protection in the real sandbox."""
    runpy.run_path(str(SOURCE / 'deploy/build_environment_check.py'))['validate'](image, seccomp, profile, services=full)


def reconcile_services(root, bind, restart=()):
    """Start every managed service; restart only those whose inputs changed."""
    # Migration can stop unchanged units too. Artifact equality does not imply
    # that a service is running; systemctl start leaves active units untouched.
    mirror_action = 'restart' if 'forge-mirror' in restart else 'start'
    command(['systemctl', '--user', mirror_action, 'forge-mirror.service'])
    wait_mirror(root)
    registry_action = 'restart' if 'forge-registry' in restart else 'start'
    command(['systemctl', '--user', registry_action, 'forge-registry.service'])
    wait_registry(root)
    # Both read-only services must be healthy before dispatch can start.
    for action in ('restart', 'start'):
        ordered = [name + '.service' for name in SERVICES if name not in ('forge-mirror', 'forge-registry')
                   and (name in restart) == (action == 'restart')]
        if ordered:
            command(['systemctl', '--user', action, *ordered])
    wait_healthy(bind)


def reconcile(root, bind=None, assume_yes=False):
    """Install or update the host. Returns True when running workers need requalification."""
    root = Path(root)
    fresh = runtime_document(root, 'host-runtime.json') is None
    record = Record(root)
    units = Path.home() / '.config/systemd/user'
    bind = resolve_bind(root, bind, units)
    source = source_revision()
    plan = []

    qualify_boundary_engine()

    # 1. Prepare and qualify everything new. Nothing in use changes here.
    image = runner_image()
    full_image = full_runner_image(image) if record.data.get('full_image') else None
    seccomp, seccomp_inputs = seccomp_policy(root, record)
    apparmor = (SOURCE / 'deploy/runner' / (APPARMOR_PROFILE + '.apparmor')).read_bytes()
    installed_apparmor = APPARMOR_TARGET.read_bytes() if APPARMOR_TARGET.exists() else None
    current_runtime = runtime_document(root, 'host-runtime.json') if not fresh else {}
    # The recorded image covers an update interrupted after switching but before
    # it finished: the rerun still requalifies.
    runtime_changed = (fresh or seccomp is not None or installed_apparmor != apparmor
                       or current_runtime.get('image') != image or record.data.get('image') not in (None, image)
                       or (full_image is not None and record.data.get('full_image') != full_image))
    release = ui_release(root)
    wanted = desired_units(root, bind)
    foreign, unit_changes = [], []
    for name, content in wanted.items():
        path = units / (name + '.service')
        current = path.read_text() if path.exists() else None
        if current == content:
            continue
        unit_changes.append(name)
        if current is not None and not record.owns(path, current.encode()):
            foreign.append((path, current, content))
    if installed_apparmor is not None and installed_apparmor != apparmor and not record.owns(APPARMOR_TARGET, installed_apparmor):
        foreign.append((APPARMOR_TARGET, installed_apparmor.decode(errors='replace'), apparmor.decode()))
    overrides, kept = [], []
    for name, path, overriding in dropins(units):
        (overrides if overriding else kept).append((name, path, overriding))
    for _, path, _ in kept:
        print('Keeping drop-in ' + str(path) + ' (it does not change what the service runs).')
    restart = set(unit_changes) | {name for name, _, _ in overrides}
    if source != record.data.get('source'):
        restart.update(('forge-mirror', 'forge-registry', 'forge-status', 'forge-supervisor'))  # Python services run this checkout.
    if release != record.data.get('ui_release'):
        restart.add('forge-ui')
    if runtime_changed and not fresh:
        plan.append('runner image ' + (current_runtime.get('image') or 'none')[:19] + ' → ' + image[:19])
    if seccomp is not None and not fresh:
        plan.append('runner seccomp policy')
    if installed_apparmor != apparmor and not fresh:
        plan.append('runner AppArmor policy')
    if release != record.data.get('ui_release'):
        plan.append('operator UI release ' + release)
    plan += ['service ' + name for name in unit_changes]
    plan += ['remove overriding drop-in ' + path.name + ' from ' + name for name, path, _ in overrides]
    stale = stale_units(units)
    plan += ['remove stale unit ' + path.name for path in stale]
    if not fresh and not plan and not restart:
        reconcile_services(root, bind)
        print('Forge host is up to date' + (' at ' + source[:12] if source else '') + '.')
        return False
    if not fresh:
        print('Updating: ' + ('; '.join(plan) if plan else 'restart services for source ' + (source or 'unknown')[:12]))
    confirm(foreign, assume_yes, overrides)
    if installed_apparmor != apparmor:
        # The canary runs under the loaded policy, so it is installed first.
        with tempfile.NamedTemporaryFile(dir=root, delete=False) as stream:
            stream.write(apparmor)
        try:
            command(['sudo', 'install', '-m', '0644', stream.name, str(APPARMOR_TARGET)])
            command(['sudo', 'apparmor_parser', '-r', str(APPARMOR_TARGET)])
        except subprocess.CalledProcessError:
            raise ValueError(apparmor_help()) from None
        finally:
            os.unlink(stream.name)
    # Also adopts a profile an administrator installed from this source, so a
    # later change to it is an ordinary update rather than a foreign edit.
    record.managed(APPARMOR_TARGET, apparmor)
    if runtime_changed:
        # Image presence is not readiness: run the offline canary before switching.
        qualify_runner(image, seccomp or root / 'runner-seccomp.json', APPARMOR_PROFILE)
        if full_image:
            qualify_runner(full_image, seccomp or root / 'runner-seccomp.json', APPARMOR_PROFILE, full=True)

    # 2. Switch. Each step is idempotent; an interrupted update converges when rerun.
    if seccomp is not None:
        os.replace(seccomp, root / 'runner-seccomp.json')
    record.save(seccomp_inputs=seccomp_inputs)
    network = root / 'deploy/runner'
    network.mkdir(parents=True, exist_ok=True)
    for name in ('compose.yaml', 'egress.py'):
        shutil.copyfile(SOURCE / 'deploy/runner' / name, network / name)
    # The proxy reads its bind-mounted script as the service account, whose uid
    # is not necessarily 1000. Waiting for its health check catches a proxy that
    # would otherwise crash-loop unnoticed and cut every worker off the network.
    try:
        command(['docker', 'compose', '-p', 'forge', '-f', str(network / 'compose.yaml'), 'up', '-d',
                 '--wait', '--wait-timeout', '60', 'egress'],
                env={**os.environ, 'FORGE_EGRESS_IMAGE': PYTHON_IMAGE, 'FORGE_EGRESS_USER': f'{os.getuid()}:{os.getgid()}'})
    except subprocess.CalledProcessError:
        raise ValueError('The worker egress proxy did not become healthy; see `docker logs forge-egress-1`, '
                         'correct it and rerun this command.') from None
    if not fresh and runtime_changed:
        switch_runtime(root, image, APPARMOR_PROFILE)
        if full_image:
            switch_full_environment(root, full_image)
    switch_ui(root, release)
    ensure_upstream_secret(root)
    units.mkdir(parents=True, exist_ok=True)
    for name in unit_changes:
        path = units / (name + '.service')
        path.write_text(wanted[name])
        record.managed(path, wanted[name].encode())
    if overrides:
        backup = root / 'backups' / time.strftime('unit-dropins-%Y%m%dT%H%M%SZ', time.gmtime())
        for name, path, _ in overrides:
            target = backup / path.parent.name
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), target / path.name)
        print('Moved overriding drop-ins to ' + str(backup))
    remove_stale_units(stale)
    command(['systemctl', '--user', 'daemon-reload'])
    command(['systemctl', '--user', 'enable', *[name + '.service' for name in SERVICES]], stdout=subprocess.DEVNULL)
    reconcile_services(root, bind, SERVICES if fresh else restart)
    prune_ui(root, {release, record.data.get('ui_release')})
    record.save(bind=bind, source=source, image=image, ui_release=release, updated_at=time.time(),
                full_image=full_image,
                previous={key: record.data.get(key) for key in ('source', 'image', 'ui_release')} if not fresh else None)
    if fresh:
        # Written last: a failed installation can be repeated without a false success marker.
        config = Onboarding(root).runtime()
        config.update(image=image, apparmor_profile=APPARMOR_PROFILE)
        save_runtime_document(root, 'host-runtime.json', config, expected_revision=0)
    elif record.data['previous'].get('source') not in (None, source):
        print('Updated to ' + (source or 'unknown')[:12] + '. SQLite history is retained; use a compatible forward update for recovery.')
    return runtime_changed and not fresh
