"""Terminal setup: install the host, connect harness accounts, then open web setup."""
import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import platform
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

from . import host_update
from .onboarding import Onboarding
from .settings import write_json
from .setup_service import Service
from .factory.control import controller_lock, ControllerBusy
from .harnesses.registry import manifests, manifest
from .harnesses.accounts import owner as login_owner
from .store.db import StoreError
from .store.files import StateError

SOURCE = Path(__file__).resolve().parent.parent


def command(argv, **kwargs):
    return subprocess.run(argv, check=True, **kwargs)


def private_root(root):
    root = Path(root).absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError('Use a private runtime directory owned by the Forge service account (mode 0700)')
    return root


def apparmor_enabled():
    path = Path('/sys/module/apparmor/parameters/enabled')
    return path.exists() and path.read_text().strip() == 'Y'


def preflight():
    if platform.system() != 'Linux' or os.getuid() == 0:
        raise ValueError('Run setup on Linux as the non-root Forge service account')
    if not apparmor_enabled():
        raise ValueError('Forge runners require AppArmor for private process isolation. Use an AppArmor-enabled Linux host; see docs/installation.md')
    for binary in ('docker', 'systemctl', 'python3'):
        if not shutil.which(binary):
            raise ValueError('Install Docker Engine with Compose, Python 3, and systemd first; see docs/installation.md')
    command(['docker', 'info'], stdout=subprocess.DEVNULL)
    command(['docker', 'compose', 'version'], stdout=subprocess.DEVNULL)
    user_manager()


def user_manager():
    """Reach the account's systemd user manager, which runs Forge's services.

    `sudo -iu <account>` and `su - <account>` start no login session, so they leave
    XDG_RUNTIME_DIR unset and `systemctl --user` cannot find the manager even though
    lingering keeps it running. Its runtime directory is fixed, so use it directly."""
    runtime = Path('/run/user') / str(os.getuid())
    if not os.environ.get('XDG_RUNTIME_DIR') and runtime.is_dir():
        os.environ['XDG_RUNTIME_DIR'] = str(runtime)
    try:
        command(['systemctl', '--user', 'show-environment'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        raise ValueError("This account's systemd user manager is not running. Enable lingering once as an administrator "
                         '(sudo loginctl enable-linger ' + getpass.getuser() + '), then rerun setup; see docs/installation.md') from None


def initialize(root, name=None):
    """Prepare the qualified SQLite authority before fresh-install services."""
    from .store.db import qualified_sqlite
    if not qualified_sqlite():
        from .store.migration.runtime import prepare
        prepared = prepare(root)
        environment = {**os.environ, 'LD_LIBRARY_PATH': prepared['library_directory']}
        os.execve(sys.executable, [sys.executable, '-m', 'forge.launch', 'forge.host_setup',
                                  'initialize', '--root', str(root), *(['--name', name] if name is not None else [])], environment)
    from .store.fresh import initialize as initialize_store
    store = initialize_store(root)
    if name is not None:
        from .factory_identity import initialize as initialize_identity
        initialize_identity(root, name)
    print('SQLite initialized for instance ' + store.instance_id)


def install(root, bind=None, assume_yes=False):
    """Idempotent: installs a new host or converges an existing one to this checkout.

    Never resets or starts issue processing. Returns True when the runner changed
    and connected accounts must be verified against it."""
    preflight()
    return host_update.reconcile(root, bind, assume_yes)


def update(root, bind=None, assume_yes=False):
    from .configuration_authority import ConfigurationAuthority
    installed = ConfigurationAuthority(root).snapshot('host-runtime.json') is not None
    if not installed:
        raise ValueError('This host has no Forge installation yet; run bin/forge setup first')
    preflight()
    changed = host_update.reconcile(root, bind, assume_yes)
    view = Onboarding(root).view()
    if view['provider_connected'] and (changed or not view['image_verified']):
        print('Verifying connected accounts against the installed runner. New work waits for this check.', flush=True)
        check(root)


class LoginBusy(ValueError):
    """Another caller owns this provider login; retry observation later."""


@contextmanager
def login_lock(root, provider):
    with open(root / ('state-login-' + provider + '.lock'), 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LoginBusy('Another terminal is already connecting this harness') from None
        yield


def login(root, provider, **kwargs):
    manifest(provider)
    with login_lock(root, provider):
        return connect_login(root, provider, **kwargs)


def pending_login(setup, provider, generation, record=None):
    """Keep successful sign-ins private and recoverable until their handoff commits."""
    setup.credentials._private()
    path = setup.credentials.root / ('pending-login-' + provider + '.json')
    if record is not None:
        # Reset owns credential removal; never resurrect an older generation's login.
        with setup.store.transaction() as data:
            if data['generation'] != generation or data['phase'] not in ('setup', 'active'):
                raise ValueError('Forge was reset during login; reconnect from the current setup')
            write_json(path, {'generation': generation, **record})
        return record
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 262144:
            raise ValueError('Saved sign-in must be a private, bounded file owned by the Forge account')
        record = json.load(stream)
    if record.get('generation') != generation:
        path.unlink()
        return None
    return record


def cleanup_login(name):
    owned = subprocess.run(['docker', 'inspect', '--format', '{{index .Config.Labels "forge.login"}}', name],
                           capture_output=True, text=True)
    if owned.returncode == 0:
        if owned.stdout.strip() != name:
            raise ValueError('A login container has unexpected ownership; inspect it before retrying')
        command(['docker', 'rm', '--force', name], stdout=subprocess.DEVNULL)


def login_sandbox(root, item, config):
    """Grok enforces its managed deny list even during device sign-in."""
    if not item.get('login_sandbox'):
        return []
    from .factory.runner import process_isolation, RunnerError
    repair = shlex.join([str(SOURCE / 'bin/forge'), 'update', '--root', str(root)])
    seccomp = Path(root) / 'runner-seccomp.json'
    try:
        isolation = process_isolation({**config, 'provider': item['id']})
        if not seccomp.is_file():
            raise RunnerError('Runner seccomp policy is missing')
    except RunnerError as error:
        raise ValueError(f'{item["name"]} sign-in needs the installed runner isolation policies. Run {repair}, then retry.') from error
    # The image keeps bubblewrap outside the default PATH. Login and the native
    # Grok harness must use the same tool path.
    extra = [*isolation, '--security-opt', 'seccomp=' + str(seccomp),
             '--env', 'PATH=/opt/forge/claude-tools:/usr/local/bin:/usr/bin:/bin']
    probe = '''import shutil, subprocess, sys
if not all(shutil.which(p) for p in (sys.argv[1], "bwrap")):
    sys.exit(10)
check = subprocess.run(["bwrap", "--unshare-user", "--unshare-pid", "--ro-bind", "/", "/",
                        "--proc", "/proc", "--dev", "/dev", "/bin/true"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
sys.exit(11 if check.returncode else 0)
'''
    print(f'Checking {item["name"]} sign-in tools and sandbox in the runner image…', flush=True)
    try:
        command(['docker', 'run', '--rm', '--network', 'none', '--log-driver', 'none', '--read-only',
                 '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                 '--user', str(os.getuid()) + ':' + str(os.getgid()), '--pids-limit', '128', '--memory', '1g',
                 '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m', *extra,
                 '--entrypoint', 'python3', config['image'], '-c', probe, item['command']],
                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        detail = ('The runner image is missing a required sign-in tool.'
                  if isinstance(error, subprocess.CalledProcessError) and error.returncode == 10
                  else 'The runner could not start the sign-in sandbox.')
        raise ValueError(f'{detail} Run {repair}, then retry. Browser sign-in has not started.') from error
    return extra


def connect_login(root, provider, *, api_key=False, token=False, import_login=None, models=None):
    item = manifest(provider)
    if token and not item.get('token_env'):
        raise ValueError(item['name'] + ' has no long-lived token login; use the browser login or --api-key')
    setup = Onboarding(root)
    initial = setup.read()
    if initial['phase'] not in ('setup', 'active') or initial.get('operation'):
        raise ValueError('Finish the current setup operation before signing in')
    generation = initial['generation']
    saved = pending_login(setup, provider, generation) if not api_key and not token and not import_login else None
    kind = 'token' if token else 'api_key'
    if saved:
        print('Finishing the saved ' + item['name'] + ' sign-in; no browser login is needed.', flush=True)
        secret, value, kind = saved['secret'], saved['value'], saved.get('kind', 'api_key')
        models = models if models is not None else saved['models']
        if saved.get('interactive'):
            cleanup_login('forge-login-' + login_owner(root) + '-' + provider)
    elif api_key:
        secret = getpass.getpass(item['name'] + ' API key: ')
        value = None
    elif token:
        # Workers share this token and never refresh it, so several can run at once.
        secret = getpass.getpass(item['name'] + ' long-lived token (from `' + item['token_command'] + '`): ')
        value = None
    elif import_login:
        path = Path(import_login)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise ValueError('Use a regular login file smaller than 64 KiB')
        secret, value = None, path.read_text()
    else:
        config = setup.runtime()
        if not config['image']:
            raise ValueError('Install the runner image first: bin/forge setup install --root ' + str(root))
        isolation = login_sandbox(root, item, config)
        name = 'forge-login-' + login_owner(root) + '-' + provider
        old = subprocess.run(['docker', 'inspect', '--format', '{{index .Config.Labels "forge.login"}}', name],
                             capture_output=True, text=True)
        if old.returncode == 0:
            if old.stdout.strip() != name:
                raise ValueError('A login container has unexpected ownership; inspect it before retrying')
            command(['docker', 'rm', '--force', name], stdout=subprocess.DEVNULL)
        # This container runs only the trusted CLI login command, without a repo,
        # GitHub credential, Docker socket or existing host authentication.
        # Intent precedes container creation; reset can find even an interrupted
        # login without relying on its parent terminal process.
        from .harnesses.accounts import mark_login
        mark_login(root)
        setup.credentials._private()
        with tempfile.TemporaryDirectory(prefix='login-', dir=setup.credentials.root) as temporary:
            home = Path(temporary); home.chmod(0o700)
            for relative, content in item.get('login_files', {}).items():
                path = home / relative
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.write_text(content)
            args = ['docker', 'create', '-it', '--name', name, '--label', 'forge.login=' + name,
                    '--label', 'forge.login-root=' + login_owner(root),
                    '--log-driver', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    '--user', str(os.getuid()) + ':' + str(os.getgid()), '--pids-limit', '128', '--memory', '1g',
                    '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m', '--mount', 'type=bind,source=' + temporary + ',target=/work/home',
                    '--env', 'HOME=/work/home', '--env', 'DISABLE_AUTOUPDATER=1',
                    *isolation,
                    '--entrypoint', item['command'], config['image'], *item['login']]
            try:
                # Creation is serialized with reset intent; attachment does not
                # hold the setup lock. Reset may terminate login at any time.
                with setup.store.transaction() as data:
                    if data['generation'] != generation or data['phase'] not in ('setup', 'active') or data.get('operation'):
                        raise ValueError('Setup changed during login preparation; retry from the current setup')
                    identifier = command(args, capture_output=True, text=True, timeout=30).stdout.strip()
                command(['docker', 'start', '--attach', '--interactive', identifier], timeout=900)
                path = home / item['auth_file']
                if not path.is_file() or path.stat().st_size > 65536:
                    raise ValueError('The harness did not save a usable login; retry or use --api-key')
                value, secret = path.read_text(), None
                # Persist before deleting the temporary login container/home.
                saved = pending_login(setup, provider, generation, {'secret': secret, 'value': value, 'models': models, 'interactive': True})
            finally:
                # Check ownership before cleanup, including interrupted login.
                cleanup_login(name)
    if not saved:
        pending_login(setup, provider, generation, {'secret': secret, 'value': value, 'models': models, 'kind': kind})
    print('Sign-in saved locally. Waiting for the current supervisor cycle to finish…', flush=True)
    try:
        from .sqlite_identity import identity_effect_lock
        with identity_effect_lock(root), controller_lock(root, timeout=120):
            if setup.read()['generation'] != generation:
                raise ValueError('Forge was reset during login; reconnect from the current setup')
            setup.connect(provider, api_key=secret if kind == 'api_key' else None, token=secret if kind == 'token' else None,
                          auth_value=value, models=models)
            from .store.runtime import required, writable
            with required(root).write() as db:
                writable(db)
                db.execute('DELETE FROM provider_cooldowns WHERE provider=?', (provider,))
            (setup.credentials.root / ('pending-login-' + provider + '.json')).unlink(missing_ok=True)
    except ControllerBusy:
        raise ValueError('Sign-in is saved, but the supervisor is still busy. Rerun the same login command to finish; you will not need to sign in again.') from None
    print(item['name'] + ' connected. Run setup check to verify the runner.')


def check(root, provider=None):
    setup = Onboarding(root)
    full = setup.runtime().get('environment_tiers', {}).get('full')
    if full:
        print('Checking database services in the full environment…', flush=True)
        host_update.qualify_runner(full['image'], root / 'runner-seccomp.json', host_update.APPARMOR_PROFILE, full=True)
    item = manifest(provider) if provider else None
    print(('Checking the ' + item['name'] + ' account…') if item else 'Checking coding accounts and worker isolation…', flush=True)
    operation = setup.queue('harness_check', setup.view()['revision'], harness=provider)
    # A scoped check reports its own accounts only; the others keep their verdict.
    scope = (setup.read().get('operation') or {}).get('providers') if provider else None
    service = Service(root)
    started = time.monotonic()
    deadline = started + 900
    last_label, last_report = None, started
    # If the service is running it owns progress. Otherwise this terminal drives
    # the same durable state machine, so closing it cannot lose runner ownership.
    with open(root / 'state-setup-service.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            drive = True
        except BlockingIOError:
            drive = False
        if not drive:
            print('The Forge service owns this check. Ctrl+C stops watching; verification continues. Rerun this command to reattach.', flush=True)
        while time.monotonic() < deadline:
            data = setup.read()
            if not data.get('operation'):
                if data.get('error'):
                    raise ValueError(data['error'])
                for key, value in data.get('provider_checks', {}).items():
                    if scope is not None and key not in scope:
                        continue
                    print(key + ': ' + value + ('. ' + data['provider_errors'][key] if data.get('provider_errors', {}).get(key) else ''), flush=True)
                if item and not any(job.get('kind') != 'provider' for job in data.get('probe_jobs', [])):
                    print(item['name'] + ' verified. Other accounts unchanged.', flush=True)
                else:
                    print('Worker isolation, termination and reuse verified. Only accounts that passed are available for work.', flush=True)
                return
            if data['operation']['id'] != operation:
                raise ValueError('Verification was superseded; inspect setup status')
            label = (setup.activity(data) or {}).get('label', 'Preparing verification')
            now = time.monotonic()
            if label != last_label or now - last_report >= 30:
                elapsed = int(now - started)
                print(f'[{elapsed // 60}m {elapsed % 60:02d}s] {label}', flush=True)
                last_label, last_report = label, now
            if drive:
                service.tick()
            time.sleep(2)
    raise ValueError('Stopped watching after 15 minutes; verification is still pending. ' +
                     ('The Forge service continues it. Rerun setup check to reattach, or use setup status.' if not drive else
                      'Rerun setup check to continue. Saved progress and runner cleanup ownership are retained.'))


def status(root):
    setup = Onboarding(root)
    view = setup.view()
    activity = setup.activity(setup.read())
    if activity:
        print(activity['label'], flush=True)
    for item in view['harnesses']:
        state = ', '.join((m['model'] or 'default model') + ': ' + m['status'] for m in item['models']) or 'not connected'
        print(item['name'] + ' ' + item['version'] + ' — ' + state)
    if view.get('error'):
        print(view['error'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=['initialize', 'install', 'update', 'login', 'check', 'status', 'full-environment'], default=None)
    parser.add_argument('provider', nargs='?', choices=list(manifests()))
    parser.add_argument('--root', default=str(Path.home() / '.local/share/forge'))
    parser.add_argument('--name', help='initialize: factory display name; cannot rename an existing factory')
    parser.add_argument('--bind', help='Operator UI address: loopback (default) or a private tailnet address; kept across updates')
    parser.add_argument('--no-pull', action='store_true', help='update: apply the checkout as it is, without fetching')
    parser.add_argument('--yes', action='store_true', help='Replace service or policy files that were edited outside Forge')
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument('--api-key', action='store_true')
    auth.add_argument('--token', action='store_true',
                      help='Paste a long-lived token (Claude Code: `claude setup-token`); workers share it and run in parallel')
    auth.add_argument('--import-login')
    parser.add_argument('--model', action='append', help='Repeat to rotate equally between models of this harness')
    args = parser.parse_args()
    if args.name is not None and args.action != 'initialize':
        parser.error('--name is only supported with initialize')
    os.umask(0o077)
    try:
        root = private_root(args.root)
        if args.action == 'initialize':
            initialize(root, args.name)
        if args.action == 'update':
            if not args.no_pull and host_update.pull():
                # Run the reconciliation that shipped with the new source, not this one.
                argv = [sys.executable, '-m', 'forge.launch', 'forge.host_setup', 'update', '--no-pull', '--root', str(root)]
                argv += ['--bind', args.bind] if args.bind else []
                argv += ['--yes'] if args.yes else []
                os.execv(sys.executable, argv)
            update(root, args.bind, args.yes)
        if args.action in (None, 'install'):
            if install(root, args.bind, args.yes) and Onboarding(root).view()['provider_connected']:
                print('The runner changed; run bin/forge setup check to verify connected accounts against it.')
        if args.action == 'login':
            if not args.provider:
                parser.error('login requires a harness name')
            login(root, args.provider, api_key=args.api_key, token=args.token, import_login=args.import_login, models=args.model)
        elif args.action == 'check':
            check(root, args.provider)
        elif args.action == 'status':
            status(root)
        elif args.action == 'full-environment':
            host_update.install_full_environment(root)
        elif args.action is None:
            for key, item in manifests().items():
                answer = input('Connect ' + item['name'] + '? [Y/n] ').strip().lower()
                if answer not in ('n', 'no'):
                    login(root, key)
            if Onboarding(root).view()['provider_connected']:
                print('Checking connections with a small model request per entry; provider charges may apply.')
                check(root)
            print('Continue GitHub setup at http://' + host_update.Record(root).data['bind'] + ':8787/onboarding')
            print('For a remote VM, forward port 8787 over SSH to reach the loopback dashboard.')
        return 0
    except (OSError, ValueError, StateError, StoreError, subprocess.SubprocessError) as error:
        print('Setup did not finish: ' + (str(error) if isinstance(error, (ValueError, StateError, StoreError)) else 'Host command failed (' + str(error) + '); correct it and rerun setup.'), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nSetup interrupted. Saved connections are retained; rerun setup to continue.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
