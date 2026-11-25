"""Revision reporting and pinned uv-managed client updates."""
import importlib.metadata
import json
from pathlib import Path
import re
import sys
import shutil
import os

from .contracts import require
from .github import run_bounded

SHA = re.compile(r'[a-f0-9]{40}')


def client_revision():
    match = re.fullmatch(r'([a-f0-9]{40})-[a-f0-9]{32}', Path(sys.prefix).name)
    if match:
        return match[1]
    try:
        value = json.loads(importlib.metadata.distribution('forge-agent-client').read_text('direct_url.json') or '{}')
        commit = value.get('vcs_info', {}).get('commit_id', '')
        if SHA.fullmatch(commit):
            return commit
    except (importlib.metadata.PackageNotFoundError, ValueError, TypeError):
        pass
    source = Path(__file__).resolve().parents[1]
    if (source / '.git').exists():
        try:
            commit = run_bounded(['git', '-C', str(source), 'rev-parse', 'HEAD']).decode().strip()
            if SHA.fullmatch(commit):
                return commit
        except Exception:
            pass
    return None


def report(ssh=None):
    client = client_revision()
    server = None
    if ssh:
        from .install import Installation
        installation = Installation(ssh)
        state = installation.journal.read() or {}
        installation.host.fingerprint = state.get('host_key')
        program = '''import json, subprocess
def main(payload):
    result = subprocess.run(['runuser','-u','forge','--','env','-i','PATH=/usr/sbin:/usr/bin:/sbin:/bin',
                             'HOME=/home/forge','git','-C','/home/forge/forge','rev-parse','HEAD'],
                            capture_output=True, text=True, timeout=10)
    print(json.dumps({'ok':True,'revision':result.stdout.strip() if result.returncode == 0 else None}))
    return 0
'''
        value = installation.host.rpc(program, {'version': 1}, timeout=30)
        server = value.get('revision')
        require(server is None or isinstance(server, str) and SHA.fullmatch(server), 'invalid_server_revision')
    return {'client_revision': client, 'server_revision': server, 'ssh': ssh,
            'compatible': None if not client or not server else client == server,
            'warning': 'Client and server revisions differ; update the client to the selected server revision before setup.'
                       if client and server and client != server else None}


def update(*, revision=None):
    from .install import revision as resolve_revision
    uv = shutil.which('uv')
    require(uv is not None, 'uv_required', 'Install uv, then rerun forge self-update.')
    commit = resolve_revision(revision)
    environment = {key: value for key, value in os.environ.items() if key not in ('GH_TOKEN', 'GITHUB_TOKEN')}
    directory = run_bounded([uv, 'tool', 'dir', '--bin'], environ=environment).decode().strip()
    require(bool(directory) and Path(directory).is_absolute(), 'invalid_uv_directory')
    run_bounded([uv, 'tool', 'install', '--reinstall',
                 'git+https://github.com/lucbocahut/forge.git@' + commit],
                environ=environment, timeout=900)
    command = str(Path(directory) / 'forge')
    observed = json.loads(run_bounded([command, '--json', 'version'], environ=environment))
    require(isinstance(observed, dict) and observed.get('client_revision') == commit,
            'client_update_unverified', 'The installed CLI revision did not match; inspect the uv installation.')
    surface = json.loads(run_bounded([command, '--json', 'commands'], environ=environment))
    require(isinstance(surface, dict) and 'setup new' in surface.get('commands', []),
            'client_update_unverified', 'The installed CLI command surface did not pass its check.')
    return {'status': 'updated', 'revision': commit, 'command': command}
