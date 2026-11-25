"""Standalone SSH installer payload. Uses only the Python standard library.

Keep this file importable without Forge: the local client sends its own copy to
the selected host before any server checkout exists. No HTTP endpoint runs it.
"""
from contextlib import contextmanager
import base64
import fcntl
import hashlib
import json
import os
import platform
from pathlib import Path
import pwd
import re
import subprocess
import sys
import tempfile
import time

STATE = Path('/var/lib/forge-install')
HOME = Path('/home/forge')
SOURCE = HOME / 'forge'
ROOT = HOME / '.local/share/forge'
REPOSITORY = 'https://github.com/lucbocahut/forge.git'
STEPS = ('packages', 'account', 'source', 'isolation', 'host')
PACKAGES = ('ca-certificates', 'git', 'sudo', 'apparmor', 'apparmor-utils',
            'docker.io', 'docker-buildx', 'docker-compose-v2', 'python3', 'build-essential')
PACKAGE_REVISION = hashlib.sha256(json.dumps(PACKAGES).encode()).hexdigest()
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8',
       'DEBIAN_FRONTEND': 'noninteractive', 'GIT_TERMINAL_PROMPT': '0'}


class InstallError(ValueError):
    pass


def need(value, code):
    if not value:
        raise InstallError(code)


def require_setup_entrypoint(source, module):
    # Setup RPCs require these installed entrypoints; an older checkout needs
    # maintenance before it can understand the current setup protocol.
    need((source / 'forge/launch.py').is_file()
         and (source / (module.replace('.', '/') + '.py')).is_file(),
         'server_maintenance_required')


def run(argv, *, capture=False, timeout=1800, input_file=None):
    result = subprocess.run(argv, check=True, stdin=subprocess.DEVNULL if input_file is None else input_file,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.DEVNULL if capture else None,
                            env=ENV, timeout=timeout)
    if capture:
        need(len(result.stdout) <= 1048576, 'host_command_failed')
        return result.stdout.decode().strip()


def validate_intent(value):
    need(isinstance(value, dict) and set(value) == {'id', 'name', 'revision'}, 'invalid_request')
    need(isinstance(value['id'], str) and re.fullmatch('[a-f0-9]{32}', value['id']), 'invalid_request')
    need(isinstance(value['name'], str) and re.fullmatch('[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}', value['name']), 'invalid_request')
    need(isinstance(value['revision'], str) and re.fullmatch('[a-f0-9]{40}', value['revision']), 'invalid_request')
    return value


def facts():
    need(sys.platform == 'linux' and os.geteuid() == 0, 'administrator_required')
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    need(release.get('ID', '').strip('"') == 'ubuntu' and release.get('VERSION_ID', '').strip('"') == '24.04', 'unsupported_os')
    need(platform.machine() == 'x86_64', 'unsupported_architecture')
    enabled = Path('/sys/module/apparmor/parameters/enabled')
    need(enabled.exists() and enabled.read_text().strip() == 'Y', 'apparmor_required')
    need(Path('/run/systemd/system').is_dir(), 'systemd_required')
    memory = next(int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemTotal:'))
    disk = os.statvfs('/home')
    return {'os': 'ubuntu', 'version': '24.04', 'cpus': os.cpu_count() or 1,
            'memory_mib': memory // 1024, 'free_disk_mib': disk.f_bavail * disk.f_frsize // 1048576}


def private_directory(path, *, create=False):
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    need(path.is_dir() and path.resolve() == path and path.stat().st_uid == 0
         and path.stat().st_mode & 0o077 == 0, 'unsafe_install_state')


def read():
    if not STATE.exists() and not STATE.is_symlink():
        return None
    private_directory(STATE)
    path = STATE / 'installation.json'
    need(not path.is_symlink(), 'unsafe_install_state')
    if not path.exists():
        # A directory can exist after an interruption before the first write.
        need(not any(p.name not in ('control.lock', 'work.lock') for p in STATE.iterdir()), 'install_state_missing')
        return None
    need(path.is_file() and path.stat().st_size <= 65536, 'unsafe_install_state')
    data = json.loads(path.read_text())
    need(isinstance(data, dict) and data.get('version') == 1, 'unsafe_install_state')
    validate_intent(data.get('intent'))
    need(data.get('step') in (*STEPS, 'complete') and isinstance(data.get('completed'), list)
         and all(step in STEPS for step in data['completed']), 'unsafe_install_state')
    return data


def write(data):
    fd, temporary = tempfile.mkstemp(dir=STATE, prefix='.pending-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, sort_keys=True, allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, STATE / 'installation.json')
        fd = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


@contextmanager
def locked(name):
    fd = os.open(STATE / (name + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError('installation_busy') from None
        yield


def check_new_host():
    try:
        pwd.getpwnam('forge')
    except KeyError:
        pass
    else:
        raise InstallError('existing_installation')
    need(not HOME.exists() and not HOME.is_symlink(), 'existing_installation')
    need(not Path('/etc/sudoers.d/forge-installer').exists(), 'existing_installation')


def service(argv, *, capture=False, input_file=None):
    account = pwd.getpwnam('forge')
    need(account.pw_uid > 0 and account.pw_dir == str(HOME) and HOME.resolve() == HOME
         and HOME.stat().st_uid == account.pw_uid, 'existing_installation')
    return run(['runuser', '-u', 'forge', '--', 'env', '-i',
                'PATH=/usr/sbin:/usr/bin:/sbin:/bin', 'HOME=' + str(HOME), 'LANG=C.UTF-8',
                'XDG_RUNTIME_DIR=/run/user/' + str(account.pw_uid), *argv], capture=capture, input_file=input_file)


def unit(data):
    return 'forge-install-' + data['intent']['id']


def running(data):
    result = subprocess.run(['systemctl', 'is-active', unit(data)], env=ENV,
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    return result.stdout.strip() in (b'active', b'activating', b'deactivating', b'reloading')


def source_present(data):
    path = STATE / 'source.pack'
    return bool(data and data.get('source_sha256') and path.is_file() and not path.is_symlink()
                and path.stat().st_size <= 32 * 1024 * 1024
                and hashlib.sha256(path.read_bytes()).hexdigest() == data['source_sha256'])


def view(data, host):
    return {'intent': data['intent'] if data else None, 'host': host,
            'status': ('installed' if data['step'] == 'complete' else
                       'installing' if running(data) else 'interrupted') if data else 'not_installed',
            'step': data['step'] if data else None,
            'completed': data['completed'] if data else [],
            'error': data.get('error') if data else None,
            'source_received': source_present(data),
            'source': str(SOURCE), 'root': str(ROOT), 'account': 'forge'}


def dispatch(request, payload):
    need(isinstance(request, dict), 'invalid_request')
    release_access = request.pop('release_access', None)
    need(set(request) == {'version', 'action', 'intent', 'source', 'upgrade_installer'}, 'invalid_request')
    need(type(request['version']) is int and request['version'] == 1
         and request['action'] in ('plan', 'apply', 'status', 'resume'), 'invalid_request')
    intent = validate_intent(request['intent'])
    need(release_access is None or request['action'] in ('apply', 'resume')
         and isinstance(release_access, dict) and release_access.get('revision') == intent['revision'], 'invalid_request')
    need(type(request['upgrade_installer']) is bool and (not request['upgrade_installer'] or request['action'] == 'resume'), 'invalid_request')
    need(request['action'] in ('apply', 'resume') or request['source'] is None, 'invalid_request')
    host = facts()
    data = read()
    if data:
        need(data['intent'] == intent, 'installation_conflict')
    else:
        check_new_host()
    if request['action'] in ('plan', 'status'):
        return view(data, host)
    private_directory(STATE, create=True)
    with locked('control'):
        data = read()
        if data:
            need(data['intent'] == intent, 'installation_conflict')
            if data['step'] == 'complete' or running(data):
                return view(data, host)
        else:
            check_new_host()
            data = {'version': 1, 'intent': intent, 'step': STEPS[0], 'completed': [],
                    'started_at': time.time(), 'error': None,
                    'payload': hashlib.sha256(payload.encode()).hexdigest()}
            write(data)
        # Replacing a stopped installer is a separate, explicit repair action.
        # Keep its destination, source commit, completed stages, and start time.
        payload_digest = hashlib.sha256(payload.encode()).hexdigest()
        need(data['payload'] == payload_digest or request['upgrade_installer'], 'installer_changed')
        with locked('work'):
            if release_access is not None:
                from forge_release_helpers import stage, ArtifactError
                try:
                    stage(release_access, cache_root=STATE / 'prepared')
                except ArtifactError:
                    raise InstallError('prepared_release_unavailable') from None
            if not source_present(data):
                source = request['source']
                need(isinstance(source, dict) and set(source) == {'sha256', 'data'}
                     and isinstance(source['sha256'], str) and re.fullmatch('[a-f0-9]{64}', source['sha256'])
                     and isinstance(source['data'], str) and len(source['data']) <= 45 * 1024 * 1024, 'source_required')
                pack = base64.b64decode(source['data'], validate=True)
                need(len(pack) <= 32 * 1024 * 1024 and pack.startswith(b'PACK')
                     and hashlib.sha256(pack).hexdigest() == source['sha256'], 'invalid_source')
                fd, temporary = tempfile.mkstemp(prefix='.source-', dir=STATE)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(pack); stream.flush(); os.fsync(stream.fileno())
                    os.replace(temporary, STATE / 'source.pack')
                finally:
                    if os.path.exists(temporary): os.unlink(temporary)
                data['source_sha256'] = source['sha256']
            data['payload'] = payload_digest
            data['error'] = None
            if request['upgrade_installer'] and data.get('package_revision') != PACKAGE_REVISION:
                # A repaired installer can add a missing prerequisite. Reconcile
                # that stage; leave unrelated completed stages and state intact.
                data['completed'] = [step for step in data['completed'] if step != 'packages']
            write(data)
            worker = STATE / 'worker.py'
            need(not worker.is_symlink(), 'unsafe_install_state')
            worker.write_text(payload); worker.chmod(0o600)
        log = STATE / 'install.log'
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        # systemd owns the process: losing SSH or the client cannot kill the job.
        run(['systemd-run', '--quiet', '--collect', '--unit=' + unit(data),
             '--property=Type=exec', '--property=UMask=0077',
             '--property=StandardOutput=append:' + str(log),
             '--property=StandardError=append:' + str(log),
             '/usr/bin/python3', str(worker), '--worker'], capture=True, timeout=30)
        return view(read(), host)


def prepare_service_cache(data):
    # Resume may have completed account creation before assets became available.
    if not (STATE / 'prepared' / data['intent']['revision']).exists():
        return
    from forge_release_helpers import copy_cache, ArtifactError
    account = pwd.getpwnam('forge')
    need(account.pw_dir == str(HOME) and account.pw_uid != 0 and HOME.resolve() == HOME,
         'existing_installation')
    for directory in (HOME / '.cache', HOME / '.cache/forge'):
        need(not directory.is_symlink(), 'unsafe_install_state')
        directory.mkdir(mode=0o700, exist_ok=True)
        need(directory.is_dir(), 'unsafe_install_state')
        os.chown(directory, account.pw_uid, account.pw_gid)
        directory.chmod(0o700)
    try:
        copy_cache(data['intent']['revision'], STATE / 'prepared', HOME / '.cache/forge/prepared',
                   uid=account.pw_uid, gid=account.pw_gid)
    except ArtifactError:
        raise InstallError('prepared_release_unavailable') from None


def install_step(step, data):
    if step == 'packages':
        run(['apt-get', 'update'])
        run(['apt-get', 'install', '-y', *PACKAGES])
        run(['systemctl', 'enable', '--now', 'docker'])
        run(['docker', 'buildx', 'version'], capture=True)
        data['package_revision'] = PACKAGE_REVISION
    elif step == 'account':
        try:
            account = pwd.getpwnam('forge')
        except KeyError:
            run(['useradd', '--create-home', '--home-dir', str(HOME), '--shell', '/bin/bash', 'forge'])
            account = pwd.getpwnam('forge')
        need(account.pw_dir == str(HOME) and account.pw_uid != 0 and HOME.resolve() == HOME,
             'existing_installation')
        run(['usermod', '-aG', 'docker', 'forge'])
        run(['loginctl', 'enable-linger', 'forge'])
        run(['systemctl', 'start', 'user@' + str(account.pw_uid) + '.service'])
        prepare_service_cache(data)
    elif step == 'source':
        revision = data['intent']['revision']
        need(not SOURCE.is_symlink(), 'source_conflict')
        if not SOURCE.exists():
            service(['git', 'init', str(SOURCE)])
        git = ['git', '-C', str(SOURCE)]
        remotes = service(git + ['remote'], capture=True)
        if not remotes:
            service(git + ['remote', 'add', 'origin', REPOSITORY])
        need(service(git + ['remote', 'get-url', 'origin'], capture=True) == REPOSITORY, 'source_conflict')
        need(not service(git + ['status', '--porcelain'], capture=True), 'source_conflict')
        pack = STATE / 'source.pack'
        need(source_present(data), 'invalid_source')
        with pack.open('rb') as stream:
            service(git + ['index-pack', '--stdin'], input_file=stream)
        # Only this commit and its complete tree are transferred, with no
        # operator Git config, authentication, or older source history.
        shallow = SOURCE / '.git/shallow'
        fd = os.open(shallow, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(revision + '\n')
            account = pwd.getpwnam('forge')
            os.fchown(stream.fileno(), account.pw_uid, account.pw_gid)
        need(service(git + ['cat-file', '-t', revision], capture=True) == 'commit', 'invalid_source')
        service(git + ['checkout', '--detach', revision])
        need(service(git + ['rev-parse', 'HEAD'], capture=True) == revision, 'source_conflict')
    elif step == 'isolation':
        source = SOURCE / 'deploy/runner/forge-runner-private-proc.apparmor'
        target = Path('/etc/apparmor.d/forge-runner-private-proc')
        need(not target.is_symlink() and (not target.exists() or target.read_bytes() == source.read_bytes()), 'policy_conflict')
        run(['install', '-m', '0644', str(source), str(target)])
        run(['apparmor_parser', '-r', str(target)])
    elif step == 'host':
        prepare_service_cache(data)
        need(service(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], capture=True) == data['intent']['revision'], 'source_conflict')
        need(not service(['git', '-C', str(SOURCE), 'status', '--porcelain', '--untracked-files=no'], capture=True), 'source_conflict')
        # The client may install an older, explicitly pinned release. Discover
        # options from that checkout before supplying the newer display name.
        setup = [str(SOURCE / 'bin/forge'), 'setup']
        options = service(setup + ['--help'], capture=True)
        initialize = setup + ['initialize', '--root', str(ROOT)]
        if re.search(r'(?m)^\s+--name(?:[ =]|$)', options):
            initialize += ['--name', data['intent']['name']]
        service(initialize)
        service([str(SOURCE / 'bin/forge'), 'setup', 'install', '--root', str(ROOT), '--bind', '127.0.0.1'])
        for name in ('forge-status', 'forge-ui', 'forge-supervisor'):
            need(service(['systemctl', '--user', 'is-active', name], capture=True) == 'active', 'services_unavailable')


def work():
    os.umask(0o077)
    private_directory(STATE)
    with locked('work'):
        data = read()
        try:
            facts()
            for step in STEPS:
                if step in data['completed']:
                    continue
                data.update(step=step, error=None); write(data)
                install_step(step, data)
                data['completed'].append(step)
                data['completed'] = [item for item in STEPS if item in data['completed']]
                write(data)
            data.update(step='complete', error=None, finished_at=time.time()); write(data)
        except Exception as error:
            data['error'] = str(error) if isinstance(error, InstallError) else 'host_command_failed'
            write(data)
            return 1
    return 0


def main(payload=None):
    os.umask(0o077)
    if sys.argv[1:] == ['--worker']:
        return work()
    try:
        raw = sys.stdin.buffer.read(46 * 1024 * 1024 + 1)
        need(len(raw) <= 46 * 1024 * 1024, 'invalid_request')
        result = dispatch(json.loads(raw), payload)
        print(json.dumps({'ok': True, **result}, sort_keys=True))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, InstallError) else 'host_command_failed'
        print(json.dumps({'ok': False, 'error': code}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
