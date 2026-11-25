"""Deterministic fresh-host operations shared by the wizard and agent CLI."""
import hashlib
from importlib.resources import files
import json
import re
import shlex
import shutil
import time
import uuid

from .bootstrap import Host, ERRORS as BOOTSTRAP_ERRORS, MESSAGES as BOOTSTRAP_MESSAGES
from .connection import connection_lock
from .contracts import SetupError, require
from .github import run_bounded
from .workflow import Journal
from .ssh_host import discover, environment, pinned_options
from .source import export as export_source

SOURCE = '/home/forge/forge'
ROOT = '/home/forge/.local/share/forge'
REPOSITORY = 'https://github.com/lucbocahut/forge.git'
STEPS = ['packages', 'account', 'source', 'isolation', 'host']
ERRORS = {
    'server_maintenance_required': 'The installed server predates this setup command. Update it on the host with bin/forge update, then continue setup.',
    'factory_setup_failed': 'Factory setup could not complete. Inspect setup status and the server checks before resuming.',
    'storage_required': 'Initialize the selected SQLite runtime before continuing setup.',
    'setup_changed': 'Setup identity or configuration changed. Inspect it before continuing.',
    'setup_complete': 'Factory is active; use forge repo commands for further repositories.',
    'setup_busy': 'A setup check is already running. Check status, then resume.',
    'repository_changed': 'Repository identity or access changed. Refresh access before continuing.',
    'invalid_account': 'Provide a valid provider credential privately.',
    'runner_image_required': 'Install and qualify the runner image before connecting a coding account.',
    'device_login_unsupported': 'This provider uses forge account login PROVIDER --ssh HOST for guided login.',
    'invalid_account_session': 'The saved account session is invalid. Inspect setup before restarting login.',
    'account_response_too_large': 'The provider login runtime returned an oversized response. Inspect its state before retrying.',
    'account_runtime_failed': 'The provider login container could not complete an operation. Resume forge install to reconcile the saved login.',
    'account_owner_changed': 'The login container or private credential directory no longer matches this session. Inspect it before restarting login.',
    'account_session_changed': 'This login belongs to an earlier setup. Reset its owned session before reconnecting.',
    'account_login_running': 'Finish the current device login before starting another one.',
    'invalid_account_login': 'The provider did not save a valid private login. Restart account login explicitly.',
    'renewable_login_required': 'Codex did not save a renewable browser login. Restart account login explicitly.',
    'account_handoff_unconfirmed': 'A different login is now connected. Inspect it before replacing provider credentials.',
    'administrator_required': 'Use root or an SSH account with noninteractive sudo for this VM.',
    'unsupported_os': 'Fresh installation currently supports Ubuntu 24.04. Select a supported VM.',
    'unsupported_architecture': 'Fresh installation requires an x86-64 (amd64) or ARM64 (aarch64) Ubuntu 24.04 server. Select a supported VM.',
    'apparmor_required': 'Use a VM with AppArmor enabled in its kernel.',
    'systemd_required': 'Use a VM running systemd.',
    'existing_installation': 'This host already has a forge account or installation. Use the existing-server commands or select a fresh VM.',
    'installation_conflict': 'This VM belongs to a different saved setup. Resume that setup or select another VM.',
    'installer_changed': 'Resume with the client version that started this installation.',
    'source_required': 'The host needs the pinned source. Use forge setup resume with local GitHub access available.',
    'prepared_release_unavailable': 'Prepared release assets could not be staged. Resume with local GitHub access to obtain fresh download access.',
    'invalid_source': 'The transferred source failed validation. Preserve the installation and inspect the source before retrying.',
    'installation_busy': 'Another installation request is running. Use forge setup status, then resume.',
    'source_conflict': 'The managed source has changed. Installation stopped to preserve it; select a fresh VM or reconcile the source before resuming.',
    'policy_conflict': 'An existing AppArmor policy differs. Installation stopped to preserve it; use a fresh VM or reconcile the policy before resuming.',
    'services_unavailable': 'Services did not become healthy. Use forge setup resume to retry installation.',
    'unsafe_install_state': 'The installer state is not a private regular directory. Inspect the host before resuming.',
    'unrelated_user_unit': 'Reset stopped because the Forge account has an unrelated user service or activation unit. Resolve that workload before planning the reset again.',
    'unsafe_user_units': 'Reset could not verify ownership of the Forge account’s user services. Inspect their unit files and overrides before continuing.',
    'user_manager_busy': 'The Forge user service manager is changing state. Wait for it to settle, then resume the saved reset.',
    'install_state_missing': 'Installer state is missing. Inspect the host before starting another installation.',
    'host_command_failed': 'A host installation step failed. Check its stage with forge setup status, then use forge setup resume to retry.',
    'invalid_request': 'The installer protocol did not match. Use the client version that started setup.',
    'installation_required': 'Finish host installation before configuring private network access.',
    'network_conflict': 'Existing network settings differ. Preserve them and review the selected host or hostname before continuing.',
    'network_download_failed': 'The Tailscale package key could not be downloaded securely. Retry network setup after checking connectivity.',
    'network_not_ready': 'Private network access is not ready. Resume network setup and complete its browser approval.',
    'network_failed': 'Network setup did not finish. Retry forge setup network with --apply; existing settings are preserved.',
}
ERRORS.update({code: BOOTSTRAP_MESSAGES.get(code, 'Operator sign-in setup could not complete. Resume after correcting the reported prerequisite.')
               for code in BOOTSTRAP_ERRORS if code not in ERRORS})


def schemas():
    commands = {}
    for action in ('new', 'plan', 'apply', 'status', 'resume'):
        properties = {'ssh': {'type': 'string', 'description': 'Explicit SSH account and destination'},
                      'wait': {'type': 'integer', 'minimum': 0, 'maximum': 600, 'default': 0}}
        required = ['ssh']
        if action == 'resume':
            properties['upgrade_installer'] = {'type': 'boolean', 'default': False}
        if action in ('new', 'plan'):
            properties.update(name={'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$'},
                              revision={'type': 'string', 'pattern': '^[a-f0-9]{40}$'},
                              host_key={'type': 'string', 'pattern': '^SHA256:[A-Za-z0-9+/]{43}$'},
                              new_run={'type': 'boolean', 'default': False})
            required.append('name')
        commands[action] = {'usage': 'forge setup ' + action + ' --ssh USER@HOST --json',
                            'authority': 'explicit administrative SSH destination',
                            'input_schema': {'type': 'object', 'additionalProperties': False,
                                             'properties': properties, 'required': required},
                            'result_statuses': ['planned', 'installing', 'interrupted', 'installed']}
    commands['host-key'] = {'usage': 'forge setup host-key --ssh USER@HOST --json',
        'authority': 'read-only SSH key discovery; does not trust the key',
        'input_schema': {'type': 'object', 'additionalProperties': False,
                         'properties': {'ssh': {'type': 'string'}}, 'required': ['ssh']},
        'result_statuses': ['unverified_host_key']}
    commands['network'] = {'usage': 'forge setup network --ssh USER@HOST --json',
        'authority': 'explicit administrative SSH destination',
        'input_schema': {'type': 'object', 'additionalProperties': False,
                         'properties': {'ssh': {'type': 'string'}, 'hostname': {'type': 'string'},
                                        'apply': {'type': 'boolean', 'default': False},
                                        'upgrade_installer': {'type': 'boolean', 'default': False},
                                        'wait': {'type': 'integer', 'minimum': 0, 'maximum': 600, 'default': 0}},
                         'required': ['ssh']},
        'result_statuses': ['network_required', 'network_installing', 'tailnet_authorization_required',
                            'tailnet_approval_required', 'network_interrupted', 'network_ready']}
    commands['identity'] = {'usage': 'forge setup identity --ssh USER@HOST --operator LOGIN --json',
        'authority': 'explicit administrative SSH destination; GitHub identity lookup stays local',
        'input_schema': {'type': 'object', 'additionalProperties': False,
                         'properties': {'ssh': {'type': 'string'}, 'operator': {'type': 'array', 'items': {'type': 'string'}},
                                        'client_id': {'type': 'string'}, 'apply': {'type': 'boolean', 'default': False},
                                        'resume': {'type': 'boolean', 'default': False},
                                        'secret_stdin': {'type': 'boolean', 'default': False}}, 'required': ['ssh']},
        'result_statuses': ['identity_app_required', 'identity_secret_required', 'planned', 'sign_in_required']}
    commands['sign-in'] = {**commands['identity'], 'usage': 'forge setup sign-in --ssh USER@HOST --json', 'alias_for': 'identity'}
    commands['qualify-boundary'] = {
        'usage': 'forge setup qualify-boundary --ssh HOST [--image sha256:IMAGE_ID] [--apply] --json',
        'authority': 'explicit administrative SSH destination; disposable native Docker canaries only',
        'input_schema': {'type': 'object', 'additionalProperties': False, 'properties': {
            'ssh': {'type': 'string'}, 'image': {'type': 'string', 'pattern': '^sha256:[0-9a-f]{64}$'},
            'apply': {'type': 'boolean', 'default': False}}, 'required': ['ssh']},
        'result_statuses': ['planned', 'qualified']}
    from .factory_setup import schemas as factory_schemas
    commands.update(factory_schemas())
    return commands


def revision(value=None, *, run=run_bounded, fetch_manifest=None):
    if value is not None:
        require(isinstance(value, str) and re.fullmatch('[a-f0-9]{40}', value),
                'invalid_revision', 'Use a full 40-character Forge commit SHA.')
        return value
    executable = shutil.which('gh')
    require(executable, 'github_cli_missing', 'Install the GitHub CLI locally before selecting a prepared Forge release.')
    from forge.release_artifacts import ASSETS, ArtifactError, manifest_shape, artifact_kinds
    from .release_delivery import read_manifest
    try:
        raw = run([executable, 'api', '--hostname', 'github.com', '--method', 'GET',
                   '-H', 'Accept: application/vnd.github+json',
                   'repos/lucbocahut/forge/releases?per_page=100'])
        releases = json.loads(raw)
        require(isinstance(releases, list), 'prepared_release_unavailable')
        required_assets = {'manifest.json', ASSETS['ui']}
        candidates = [item for item in releases if isinstance(item, dict)
                      and item.get('draft') is False and item.get('prerelease') is False
                      and isinstance(item.get('tag_name'), str)
                      and re.fullmatch(r'forge-build-[a-f0-9]{40}', item['tag_name'])
                      and isinstance(item.get('published_at'), str)
                      and isinstance(item.get('assets'), list)
                      and required_assets <= {asset.get('name') for asset in item['assets'] if isinstance(asset, dict)}]
        require(bool(candidates), 'prepared_release_unavailable')
        selected = max(candidates, key=lambda item: item['published_at'])
        commit = selected['tag_name'].removeprefix('forge-build-')
        manifest = fetch_manifest(commit) if fetch_manifest else read_manifest(commit, run=run)
        manifest_shape(manifest, commit)
        require({ASSETS[kind] for kind in artifact_kinds(manifest)} <=
                {asset.get('name') for asset in selected['assets'] if isinstance(asset, dict)},
                'prepared_release_unavailable')
        return commit
    except (SetupError, ArtifactError, ValueError, TypeError):
        raise SetupError('prepared_release_unavailable',
                         'No complete prepared Forge release could be verified. Wait for the Prepared release workflow '
                         'or supply --revision with an already published full commit SHA.') from None


class InstallerHost:
    def __init__(self, ssh, *, fingerprint=None, run=run_bounded):
        # Reuse the existing strict destination validation.
        Host(ssh, SOURCE, ROOT)
        self.ssh, self.run, self.fingerprint = ssh, run, fingerprint

    def call(self, action, intent):
        helper = files('forge').joinpath('release_artifacts.py').read_text()
        payload = ('import sys, types\n_release = types.ModuleType("forge_release_helpers")\n'
                   + 'exec(compile(' + repr(helper) + ', "<forge-release-helpers>", "exec"), _release.__dict__)\n'
                   + 'sys.modules["forge_release_helpers"] = _release\n'
                   + files('forge_client').joinpath('_install_host.py').read_text())
        source, release_access = None, None
        if action in ('apply', 'resume'):
            observed = self.call('status', intent)
            if observed['status'] in ('not_installed', 'interrupted'):
                if not observed['source_received']:
                    source = export_source(intent['revision'])
                from .release_delivery import access
                release_access = access(intent['revision'])
        request = {'version': 1, 'action': action, 'intent': intent, 'source': source,
                   'upgrade_installer': bool(getattr(self, 'upgrade_installer', False) and action == 'resume')}
        if release_access is not None:
            request['release_access'] = release_access
        result = self.rpc(payload, request, timeout=3000 if release_access is not None else 180)
        return self.validate(result, intent)

    def rpc(self, payload, request, *, timeout=180):
        executable = shutil.which('ssh')
        require(executable, 'ssh_missing', 'Install the OpenSSH client on this computer.')
        code = 'import sys; payload=' + repr(payload) + '; ns={"__name__":"forge_installer"}; exec(compile(payload,"<forge-installer>","exec"),ns); sys.exit(ns["main"](payload))'
        python = '/usr/bin/python3 -c ' + shlex.quote(code)
        remote = 'if [ "$(id -u)" = 0 ]; then exec ' + python + '; else exec sudo -n ' + python + '; fi'
        argv = [executable, '-T', '-a', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=15', '-o', 'ClearAllForwardings=yes', '-o', 'ForwardX11=no',
                '-o', 'PermitLocalCommand=no']
        try:
            with pinned_options(self.ssh, self.fingerprint) as options:
                raw = self.run(argv + options + [self.ssh, remote],
                               payload=request,
                               input_limit=46 * 1024 * 1024, environ=environment(), timeout=timeout, allowed_codes=(0, 1))
                result = json.loads(raw)
        except SetupError as error:
            if error.code in ('host_key_changed', 'invalid_host_key', 'host_key_unavailable'):
                raise
            raise SetupError('ssh_unavailable', 'Could not authenticate over SSH. Check the account and key, then run forge setup resume.') from None
        except Exception:
            raise SetupError('ssh_unavailable', 'Could not reach the installer. Verify the SSH account, trusted host key, Python 3 and noninteractive sudo. Use forge setup resume after correcting access.') from None
        require(isinstance(result, dict), 'invalid_host_response', 'The host returned an invalid installation response.')
        if result.get('ok') is not True:
            code = result.get('error')
            code = code if isinstance(code, str) and code in ERRORS else 'host_command_failed'
            reason = ERRORS[code]
            raise SetupError(code, reason)
        return result

    @staticmethod
    def validate(result, intent):
        require(set(result) == {'ok', 'intent', 'host', 'status', 'step', 'completed', 'error', 'source', 'root', 'account', 'source_received'}
                and result['intent'] in (None, intent)
                and result['status'] in ('not_installed', 'installing', 'interrupted', 'installed')
                and result['step'] in (None, *STEPS, 'complete')
                and isinstance(result['completed'], list) and all(s in STEPS for s in result['completed'])
                and result['error'] in (None, *ERRORS)
                and result['source'] == SOURCE and result['root'] == ROOT and result['account'] == 'forge'
                and type(result['source_received']) is bool,
                'invalid_host_response', 'The host returned a mismatched installation response.')
        host = result['host']
        require(isinstance(host, dict) and set(host) == {'os', 'version', 'cpus', 'memory_mib', 'free_disk_mib'}
                and host['os'] == 'ubuntu' and host['version'] == '24.04'
                and all(type(host[k]) is int and 0 <= host[k] < 2**40 for k in ('cpus', 'memory_mib', 'free_disk_mib')),
                'invalid_host_response', 'The host returned invalid prerequisite information.')
        require((result['status'] == 'not_installed') == (result['intent'] is None)
                and (result['status'] != 'installed' or result['step'] == 'complete' and result['completed'] == STEPS),
                'invalid_host_response', 'The host returned inconsistent installation progress.')
        return result


class Installation:
    def __init__(self, ssh, *, host=None, storage=None, clock=time.time, sleep=time.sleep):
        self.host = host or InstallerHost(ssh)
        self.ssh, self.clock, self.sleep = ssh, clock, sleep
        self.key = 'setup-' + hashlib.sha256(ssh.encode()).hexdigest()
        self.journal = storage or Journal('fresh-host', ssh)

    def execute(self, action, *, name=None, commit=None, fingerprint=None, wait=0, new_run=False, upgrade_installer=False):
        require(action in ('plan', 'apply', 'status', 'resume'), 'invalid_action', 'Choose plan, apply, status or resume.')
        require(type(wait) is int and 0 <= wait <= 600, 'invalid_wait', 'Use --wait between 0 and 600 seconds.')
        require(type(new_run) is bool and (not new_run or action == 'plan'), 'invalid_action', '--new-run is only for a new plan after rebuilding the VM.')
        require(type(upgrade_installer) is bool and (not upgrade_installer or action == 'resume'), 'invalid_action', 'Only resume may explicitly upgrade a stopped installer.')
        with connection_lock(self.key, self.journal.root):
            state = self.journal.read()
            previous = state if new_run else None
            if new_run:
                state = None
            if state:
                require(state.get('ssh') == self.ssh and isinstance(state.get('intent'), dict), 'invalid_journal', 'Saved setup is not valid for this destination.')
                require(name is None or name == state['intent']['name'], 'setup_conflict', 'Resume with the saved factory name.')
                require(commit is None or commit == state['intent']['revision'], 'setup_conflict', 'Resume with the saved source revision.')
                require(fingerprint is None or fingerprint == state.get('host_key'), 'setup_conflict', 'Resume with the saved host key. A replaced VM needs a new setup.')
            else:
                require(action == 'plan', 'setup_not_found', 'Start with forge setup new for this SSH destination.')
                require(isinstance(name, str) and re.fullmatch('[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}', name),
                        'name_required', 'Choose a factory name of 1–64 letters, digits, spaces, dots, underscores or hyphens.')
                require(fingerprint is None or isinstance(fingerprint, str) and re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', fingerprint),
                        'invalid_host_key', 'Use a verified ED25519 SHA256 fingerprint.')
                state = {'version': 1, 'ssh': self.ssh, 'started_at': self.clock(), 'host_key': fingerprint,
                         'intent': {'id': uuid.uuid4().hex, 'name': name, 'revision': revision(commit)},
                         'milestones': {}, 'last_status': None}
                if previous:
                    # Refuse to detach the current local run from an existing host.
                    # A new run is allowed only after read-only confirmation that
                    # the remote installer has no installation state.
                    self.host.fingerprint = fingerprint
                    observed = self.host.call('plan', state['intent'])
                    require(observed['status'] == 'not_installed', 'setup_conflict', 'A new run requires a fresh VM. The previous setup is retained.')
                    archive = Journal('archived-setup', previous['intent']['id'], root=self.journal.root)
                    archive.save(previous)
                # Save the selected revision and identity before any remote mutation.
                self.journal.save(state)
            self.host.fingerprint = state.get('host_key')
            self.host.upgrade_installer = upgrade_installer
            response = self.host.call(action, state['intent'])
            deadline = time.monotonic() + wait
            while response['status'] == 'installing' and time.monotonic() < deadline:
                self.sleep(min(2, max(0, deadline - time.monotonic())))
                response = self.host.call('status', state['intent'])
            state['last_status'] = response['status']
            if response['status'] == 'installed':
                state['milestones'].setdefault('host_installed', self.clock())
            self.journal.save(state)
            return self.present(state, response)

    def present(self, state, response):
        status = response['status']
        next_action = {
            'not_installed': {'command': ['forge', 'setup', 'apply', '--ssh', self.ssh],
                              'instruction': 'Apply this installation plan.'},
            'installing': {'command': ['forge', 'setup', 'status', '--ssh', self.ssh],
                           'instruction': 'Installation continues on the VM. Check progress or resume watching.'},
            'interrupted': {'command': ['forge', 'setup', 'resume', '--ssh', self.ssh],
                            'instruction': ERRORS.get(response['error'], 'Resume the saved installation; completed steps are retained.')},
            'installed': {'command': ['forge', 'setup', 'network', '--ssh', self.ssh],
                          'instruction': 'Host installation passed. Private network access, operator sign-in, GitHub Apps, coding accounts and first-merge validation are still required.'},
        }[status]
        return {'status': 'planned' if status == 'not_installed' else status,
                'setup_id': state['intent']['id'], 'name': state['intent']['name'], 'ssh': self.ssh,
                'revision': state['intent']['revision'], 'host': response['host'], 'step': response['step'],
                'completed': response['completed'], 'error': response['error'],
                'source': SOURCE, 'root': ROOT, 'service_account': 'forge',
                'plan': ['Export and transfer the selected Forge commit using local GitHub access',
                         'Install Ubuntu host packages and Docker Compose', 'Create the dedicated forge service account',
                         'Check out the transferred source', 'Install the runner AppArmor policy',
                         'Build and qualify workers and UI; start loopback services'],
                'setup_elapsed_seconds': max(0, self.clock() - state['started_at']),
                'milestones': state['milestones'], 'next_action': next_action}


def execute(args):
    require(args.ssh, 'ssh_required', 'Provide --ssh with the intended SSH account and host, or run forge setup new interactively.')
    if args.setup_command == 'network':
        from .network import execute as network
        return network(args)
    if args.setup_command == 'identity':
        from .identity import execute as identity
        return identity(args)
    if args.setup_command == 'host-key':
        observed = discover(args.ssh)
        return {k: v for k, v in observed.items() if k != 'key'} | {'status': 'unverified_host_key',
                'next_action': 'Compare this fingerprint with the VM provider console or another trusted source, then pass --host-key to forge setup new.'}
    return Installation(args.ssh).execute('plan' if args.setup_command == 'new' else args.setup_command,
                                         name=getattr(args, 'name', None), commit=getattr(args, 'revision', None),
                                         fingerprint=getattr(args, 'host_key', None), wait=args.wait,
                                         new_run=getattr(args, 'new_run', False),
                                         upgrade_installer=getattr(args, 'upgrade_installer', False))


def wizard(args, *, ask=input, tell=print, installation=Installation, network_wizard=None, factory_wizard=None):
    """Presentation only. Every state change is an ordinary Installation operation."""
    tell('Set up a new Forge factory')
    ssh = args.ssh or ask('SSH account and host (for example root@203.0.113.10): ').strip()
    workflow = installation(ssh)
    saved = workflow.journal.read()
    name = args.name or (saved['intent']['name'] if saved else ask('Factory name: ').strip())
    fingerprint = getattr(args, 'host_key', None)
    if (not saved or getattr(args, 'new_run', False)) and fingerprint is None:
        if ask('Is this host already trusted in your local SSH configuration? [y/N] ').strip().lower() not in ('y', 'yes'):
            observed = discover(ssh)
            tell('SSH host fingerprint: ' + observed['fingerprint'])
            if ask('Verify this fingerprint using your VM provider. Trust this host? [y/N] ').strip().lower() not in ('y', 'yes'):
                return {'status': 'host_trust_required', 'ssh': ssh, 'fingerprint': observed['fingerprint']}
            fingerprint = observed['fingerprint']
    result = workflow.execute('plan', name=name, commit=args.revision, fingerprint=fingerprint,
                              new_run=getattr(args, 'new_run', False))
    tell('Factory: ' + result['name'] + '\nDestination: ' + result['ssh'] + '\nForge commit: ' + result['revision'])
    tell('Host: Ubuntu 24.04, ' + str(result['host']['cpus']) + ' CPUs, ' + str(result['host']['memory_mib']) + ' MiB memory')
    for step in result['plan']:
        tell('  • ' + step)
    if result['status'] == 'installed':
        if network_wizard is None:
            from .network import wizard as network_wizard
        result = network_wizard(workflow, ask=ask, tell=tell)
        if result.get('status') == 'sign_in_required':
            if factory_wizard is None:
                from .factory_setup import wizard as factory_wizard
            return factory_wizard(workflow, ask=ask, tell=tell)
        return result
    if result['status'] != 'installing':
        if ask('Install on this VM? [y/N] ').strip().lower() not in ('y', 'yes'):
            return result
        result = workflow.execute('apply' if result['status'] == 'planned' else 'resume')
    try:
        while result['status'] == 'installing':
            tell('Installing: ' + str(result['step']) + '. Ctrl+C stops watching; installation continues.')
            result = workflow.execute('status', wait=10)
    except KeyboardInterrupt:
        tell('Installation continues. Run forge setup resume --ssh ' + shlex.quote(ssh) + ' to check progress.')
        return {'status': 'installing', 'next_action': {'command': ['forge', 'setup', 'resume', '--ssh', ssh]}}
    tell(result['next_action']['instruction'])
    if result['status'] == 'installed':
        if network_wizard is None:
            from .network import wizard as network_wizard
        result = network_wizard(workflow, ask=ask, tell=tell)
        if result.get('status') == 'sign_in_required':
            if factory_wizard is None:
                from .factory_setup import wizard as factory_wizard
            return factory_wizard(workflow, ask=ask, tell=tell)
        return result
    return result
