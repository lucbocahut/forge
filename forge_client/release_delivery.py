"""Read private prepared releases using the operator's local GitHub session.

GitHub credentials stay in this process. Neither credentials nor temporary asset
URLs belong in a CLI result, installation journal or remote command argument.
"""
import json
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request

from forge.release_artifacts import ASSETS, ArtifactError, release_tag, manifest_shape, artifact_kinds
from .contracts import SetupError, require
from .github import run_bounded

REPOSITORY = 'lucbocahut/forge'
MANIFEST_LIMIT = 128 * 1024


def _gh(run, *arguments, output_limit=4 * 1024 * 1024):
    executable = shutil.which('gh')
    require(executable, 'github_cli_missing', 'Install GitHub CLI and sign in locally to read the Forge release.')
    return run([executable, *arguments], output_limit=output_limit)


def metadata(revision, *, run=run_bounded):
    tag = release_tag(revision)
    try:
        value = json.loads(_gh(run, 'api', '--hostname', 'github.com', '--method', 'GET',
                              '-H', 'Accept: application/vnd.github+json',
                              'repos/' + REPOSITORY + '/releases/tags/' + tag))
        require(isinstance(value, dict) and value.get('tag_name') == tag
                and value.get('draft') is False and value.get('prerelease') is False
                and isinstance(value.get('assets'), list), 'prepared_release_unavailable')
        needed = {'manifest.json', *ASSETS.values()}
        selected = {}
        for asset in value['assets']:
            if not isinstance(asset, dict) or asset.get('name') not in needed:
                continue
            name = asset['name']
            require(name not in selected and type(asset.get('id')) is int and asset['id'] > 0
                    and type(asset.get('size')) is int and 0 < asset['size'] < 2 * 1024**3,
                    'prepared_release_unavailable')
            selected[name] = asset
        require({'manifest.json', ASSETS['ui']} <= set(selected) and selected['manifest.json']['size'] <= MANIFEST_LIMIT,
                'prepared_release_unavailable')
        return selected
    except (SetupError, ArtifactError, ValueError, TypeError):
        raise SetupError('prepared_release_unavailable',
                         'The prepared Forge release is unavailable. Check local GitHub access and wait for its release workflow.') from None


def read_manifest(revision, *, assets=None, run=run_bounded):
    assets = assets if assets is not None else metadata(revision, run=run)
    try:
        raw = _gh(run, 'api', '--hostname', 'github.com', '--method', 'GET',
                  '-H', 'Accept: application/octet-stream',
                  'repos/' + REPOSITORY + '/releases/assets/' + str(assets['manifest.json']['id']),
                  output_limit=MANIFEST_LIMIT)
        require(len(raw) == assets['manifest.json']['size'], 'prepared_release_unavailable')
        manifest = json.loads(raw)
        manifest_shape(manifest, revision)
        require({ASSETS[kind] for kind in artifact_kinds(manifest)} <= set(assets),
                'prepared_release_unavailable')
        for kind, descriptor in manifest['artifacts'].items():
            asset = assets[ASSETS[kind]]
            require(asset['size'] == descriptor['size'], 'prepared_release_unavailable')
            if asset.get('digest') is not None:
                require(asset['digest'] == 'sha256:' + descriptor['sha256'], 'prepared_release_unavailable')
        return manifest
    except (SetupError, ArtifactError, ValueError, TypeError, KeyError):
        raise SetupError('prepared_release_unavailable',
                         'The prepared Forge manifest could not be verified through local GitHub access.') from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, target):
        return None


def _asset_url(asset_id, token, opener):
    request = urllib.request.Request(
        'https://api.github.com/repos/' + REPOSITORY + '/releases/assets/' + str(asset_id),
        headers={'Accept': 'application/octet-stream', 'Authorization': 'Bearer ' + token,
                 'X-GitHub-Api-Version': '2022-11-28', 'Cache-Control': 'no-cache'})
    try:
        with opener.open(request, timeout=30):
            # A direct binary response cannot be handed to another machine.
            raise SetupError('prepared_release_access_unavailable', 'GitHub did not return a downloadable asset location.')
    except urllib.error.HTTPError as response:
        try:
            require(response.code == 302, 'prepared_release_access_unavailable')
            value = response.headers.get('Location', '')
            parsed = urllib.parse.urlsplit(value)
            require(0 < len(value) <= 16384 and parsed.scheme == 'https'
                    and parsed.hostname == 'release-assets.githubusercontent.com'
                    and not parsed.username and not parsed.password and parsed.port is None
                    and not parsed.fragment and parsed.path.startswith('/') and bool(parsed.query),
                    'prepared_release_access_unavailable')
            return value
        finally:
            response.close()


def access(revision, kinds=None, *, run=run_bounded, opener=None):
    """Return an ephemeral SSH payload; callers must never journal or print it.

    The local GitHub token authorizes only requests to the fixed GitHub API.
    Redirects are stopped locally, so that token never reaches an asset host.
    The returned URLs grant time-limited access only to these release files.
    """
    assets = metadata(revision, run=run)
    manifest = read_manifest(revision, assets=assets, run=run)
    available = artifact_kinds(manifest)
    kinds = available if kinds is None else tuple(kinds)
    require(len(set(kinds)) == len(kinds) and all(kind in available for kind in kinds),
            'invalid_prepared_assets', 'Select only artifacts delivered by this release.')
    if not kinds:
        return {'revision': revision, 'manifest': manifest, 'urls': {}}
    try:
        token = _gh(run, 'auth', 'token', '--hostname', 'github.com', output_limit=16384).decode().strip()
        require(bool(re.fullmatch(r'[A-Za-z0-9_]+', token)), 'prepared_release_access_unavailable')
        client = opener or urllib.request.build_opener(_NoRedirect())
        urls = {kind: _asset_url(assets[ASSETS[kind]]['id'], token, client) for kind in kinds}
        return {'revision': revision, 'manifest': manifest, 'urls': urls}
    except (SetupError, urllib.error.URLError, TimeoutError, ValueError, UnicodeError):
        # HTTP exceptions can contain signed URLs. Keep the boundary error fixed.
        raise SetupError('prepared_release_access_unavailable',
                         'Could not obtain temporary access to the prepared Forge files. Check local GitHub sign-in and resume; '
                         'no GitHub credential was sent to the server.') from None
