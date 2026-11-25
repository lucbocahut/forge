"""Role-scoped GitHub effects and authoritative evidence, never terminal inference."""
import json
import re
from datetime import datetime
import time
from urllib.parse import quote

from ..app_access import AccessError, OPTIONAL_PERMISSIONS, ROLE_PERMISSIONS
from .contracts import SHA, candidate, digest, submission
from ..repository_policy import protection_ready, plan_unavailable
from ..request_metrics import RequestBudgetExhausted, deadline as request_deadline

# Ancestry between two recorded commits never changes; one process asks once.
COMPARISONS = {}


def _owned_publication_pr(pr, repo_id, publisher):
    # Author identity comes from GitHub, and publishers from verified App access
    # or retained App history. A branch prefix alone never establishes ownership.
    publishers = {publisher} if isinstance(publisher, str) else set(publisher)
    return (isinstance(pr, dict)
            and (pr.get('user') or {}).get('login') in publishers
            and (pr.get('user') or {}).get('type') == 'Bot'
            and pr.get('state') == 'open' and not pr.get('merged')
            and ((pr.get('head') or {}).get('repo') or {}).get('id') == repo_id
            and ((pr.get('base') or {}).get('repo') or {}).get('id') == repo_id)


def own_build_pr(pr, repo_id, number, publisher):
    # Issue-specific callers must never adopt a grouped delivery as one issue.
    return (_owned_publication_pr(pr, repo_id, publisher)
            and str((pr.get('head') or {}).get('ref') or '').startswith(f'forge/issue-{number}-'))


# Group dispatch uses the delivery UUID and the first eight UUID request digits.
UNIT_BRANCH = re.compile(r'forge/unit-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-[0-9a-f]{8}')


def own_grouped_pr(pr, repo_id, publisher):
    return (_owned_publication_pr(pr, repo_id, publisher)
            and bool(UNIT_BRANCH.fullmatch(str((pr.get('head') or {}).get('ref') or ''))))


def bot_logins(github, role):
    """Current then retired bot logins for a role; adapters without history give one."""
    lookup = getattr(github, 'publishers', None)
    return list(lookup(role)) if lookup else [github.publisher(role)]


def verdict_comment(job, report, marker):
    role = job['role']
    head = job['candidate']['head']
    title = f"forge/{role}: {'passed' if report['outcome'] == 'pass' else 'changes needed'} at {head[:12]}"
    return f"**{title}**\n\n{report['summary'][:60000]}\n\n{marker}"


class GitHubUnavailable(RuntimeError):
    def __init__(self, message, *, status=None, error_types=(), plan_required=False):
        super().__init__(message)
        self.status = status
        self.error_types = error_types
        self.plan_required = plan_required


class GitHubBudgetExhausted(RequestBudgetExhausted):
    """The cycle's observation budget is spent; the next cycle resumes. Not an error to report."""


class GitHub:
    def __init__(self, settings, runtime, *, client=None, clock=time.time):
        self.settings, self.runtime = settings, runtime
        self.client, self.clock = client or settings.client, clock
        self.tokens = {}

    def connection(self, role, repo_id=None):
        role = 'controller' if role == 'recovery' else role
        connection = self.settings.load()['connections'][role]
        if 'installation_ids' in connection and repo_id is not None:
            observation = self.settings.observation()['roles'].get(role, {})
            if observation.get('connection') != digest(connection):
                raise GitHubUnavailable('Installation mapping needs a fresh access sync')
            matches = [r for r in observation.get('repositories', []) if r['id'] == repo_id]
            if len(matches) != 1 or matches[0].get('installation_id') not in connection['installation_ids']:
                raise GitHubUnavailable('Repository installation mapping is unverified')
            return {'app_id': connection['app_id'], 'private_key_env': connection['private_key_env'],
                    'installation_id': matches[0]['installation_id']}
        return connection

    def app_ids(self, role):
        """The role's App now, then the Apps it used before the single Forge App."""
        from ..settings import retired_for
        current = self.connection(role)['app_id']
        return [current] + [row['app_id'] for row in retired_for(self.settings.load(), role) if row['app_id'] != current]

    def publishers(self, role):
        """Bot logins whose earlier work is still Forge's: current first, then retired."""
        from ..settings import retired_for
        current = self.publisher(role)
        retired = [row['slug'] + '[bot]' for row in retired_for(self.settings.load(), role) if row.get('slug')]
        return [current] + [login for login in retired if login != current]

    def token(self, role, repo_id, *, worker=False, extra=None, read_only=False):
        connection = self.connection(role, repo_id)
        key = (role, repo_id, worker, read_only, digest(connection), digest(extra or {}))
        cached = self.tokens.get(key)
        if not worker and cached and cached[1] > self.clock():
            return cached[0]
        permissions = ({'metadata': 'read', 'contents': 'read', 'issues': 'read', 'pull_requests': 'read'}
                       if role == 'recovery' else dict(ROLE_PERMISSIONS[role]))
        if read_only or worker and role in ('review', 'audit'):
            # Worker findings are published by the supervisor after validation.
            permissions = {'metadata': 'read', 'contents': 'read', 'issues': 'read', 'pull_requests': 'read'}
        if role == 'controller' and not worker:
            permissions['administration'] = 'read'
        # Optional grants widen one call only; a role's baseline stays mintable.
        if read_only and any(value != 'read' for value in (extra or {}).values()):
            raise ValueError('Read-only worker credentials cannot be widened')
        permissions.update(extra or {})
        try:
            value = self.client.request('POST', f"/app/installations/{connection['installation_id']}/access_tokens",
                                        self.client.jwt(connection), {'repository_ids': [repo_id], 'permissions': permissions})
            token = value['token']
            if not isinstance(token, str) or not token:
                raise ValueError()
        except RequestBudgetExhausted:
            raise GitHubBudgetExhausted('Cycle observation budget reached; resume next cycle') from None
        except (AccessError, KeyError, ValueError):
            raise GitHubUnavailable('Role credential could not be issued for this repository') from None
        self.tokens[key] = (token, self.clock() + 3000)
        return token

    def call(self, role, repo, method, suffix, payload=None, *, extra=None):
        if time.monotonic() >= min(getattr(self, 'deadline', float('inf')), request_deadline()):
            raise GitHubBudgetExhausted('Cycle observation budget reached; resume next cycle')
        try:
            return self.client.request(method, f"/repos/{repo['repo']}" + suffix,
                                       self.token(role, repo['repo_id'], extra=extra), payload)
        except RequestBudgetExhausted:
            raise GitHubBudgetExhausted('Cycle observation budget reached; resume next cycle') from None
        except AccessError as error:
            raise GitHubUnavailable('GitHub observation or publication is unconfirmed', status=error.status, plan_required=error.plan_required) from None

    def graphql(self, role, repo, query, variables, *, extra=None):
        """Host-owned GraphQL operation; HTTP 200 alone is not success."""
        if (not isinstance(query, str) or not query or len(query.encode()) > 32768
                or not isinstance(variables, dict) or len(json.dumps(variables, allow_nan=False).encode()) > 4 * 1024 * 1024):
            raise ValueError('GraphQL publication request exceeds its contract')
        if time.monotonic() >= min(getattr(self, 'deadline', float('inf')), request_deadline()):
            raise GitHubBudgetExhausted('Cycle observation budget reached; resume next cycle')
        try:
            value = self.client.request('POST', '/graphql', self.token(role, repo['repo_id'], extra=extra),
                                        {'query': query, 'variables': variables})
        except RequestBudgetExhausted:
            raise GitHubBudgetExhausted('Cycle observation budget reached; resume next cycle') from None
        except AccessError as error:
            raise GitHubUnavailable('GitHub publication is unconfirmed', status=error.status, plan_required=error.plan_required) from None
        if not isinstance(value, dict) or value.get('errors') or not isinstance(value.get('data'), dict):
            errors = value.get('errors', []) if isinstance(value, dict) else []
            types = tuple(sorted({row['type'] for row in errors if isinstance(row, dict)
                and isinstance(row.get('type'), str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', row['type'])})) if isinstance(errors, list) else ()
            raise GitHubUnavailable('GitHub publication is unconfirmed', status=200, error_types=types)
        return value['data']

    def prerequisite(self, ref, binding=None):
        """Read one installed repository target, including PR merge provenance."""
        from ..store.repository_requirements import reference
        ref = reference(ref)
        name, number = ref.rsplit('#', 1)
        number = int(number)
        observation = self.settings.observation()['roles'].get('controller', {})
        repositories = observation.get('repositories', [])
        matches = [row for row in repositories if (row['id'] == binding['repository_id'] if binding
                   else row['name'].lower() == name)]
        if len(matches) != 1:
            return {'reference': ref, 'error': 'prerequisite_inaccessible'}
        selected = matches[0]
        item = {'repo_id': selected['id'], 'repo': selected['name'], 'number': number}
        try:
            repository = self.call('controller', item, 'GET', '')
            if repository['id'] != item['repo_id']:
                return {'reference': ref, 'error': 'prerequisite_identity_changed'}
            remote = self.call('controller', item, 'GET', '/issues/' + str(number))
            kind = 'pull_request' if 'pull_request' in remote else 'issue'
            merged = False
            if kind == 'pull_request':
                pull = self.pr(item, number)
                if pull['number'] != number or pull['base']['repo']['id'] != repository['id']:
                    return {'reference': ref, 'error': 'prerequisite_identity_changed'}
                merged = pull['merged']
            return dict(reference=ref, repository_id=repository['id'], github_id=remote['id'], number=remote['number'],
                        kind=kind, state=remote['state'], state_reason=remote.get('state_reason'),
                        merged=merged, updated_at=remote['updated_at'])
        except GitHubBudgetExhausted:
            raise
        except GitHubUnavailable:
            return {'reference': ref, 'error': 'prerequisite_inaccessible'}

    def pages(self, role, repo, suffix, key=None):
        rows = []
        for page in range(1, 101):
            sep = '&' if '?' in suffix else '?'
            data = self.call(role, repo, 'GET', suffix + f'{sep}per_page=100&page={page}')
            batch = data.get(key) if key and isinstance(data, dict) else data
            if not isinstance(batch, list):
                raise GitHubUnavailable('GitHub inventory is malformed')
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise GitHubUnavailable('GitHub inventory exceeded the pagination bound')

    def publisher(self, role):
        row = self.settings.view()['roles']['controller' if role == 'recovery' else role]
        slug = row.get('app_slug')
        if not slug:
            raise GitHubUnavailable('Role identity is not verified')
        return slug + '[bot]'

    def identity(self, role, repo_id):
        """Resolve the configured App bot through a bounded host-only request."""
        login = self.publisher(role)
        if time.monotonic() >= min(getattr(self, 'deadline', float('inf')), request_deadline()):
            raise GitHubBudgetExhausted('Cycle observation budget reached; resume next cycle')
        try:
            value = self.client.request('GET', '/users/' + quote(login, safe=''), self.token(role, repo_id))
        except RequestBudgetExhausted:
            raise GitHubBudgetExhausted('Cycle observation budget reached; resume next cycle') from None
        except AccessError as error:
            raise GitHubUnavailable('Publisher identity is unconfirmed', status=error.status, plan_required=error.plan_required) from None
        if (not isinstance(value, dict) or type(value.get('id')) is not int or value['id'] <= 0
                or value.get('login') != login or value.get('type') != 'Bot'):
            raise GitHubUnavailable('Publisher identity is unconfirmed')
        return {key: value[key] for key in ('id', 'login', 'type')}

    def target(self, item):
        repository = self.call('controller', item, 'GET', '')
        if repository.get('id') != item['repo_id'] or not isinstance(repository.get('default_branch'), str):
            raise GitHubUnavailable('Repository identity or target branch is unverified')
        return repository['default_branch']

    def workflows_digest(self, item):
        """Identity of the deploy surface: the workflow files at the default branch head."""
        repository = self.call('controller', item, 'GET', '')
        branch = repository.get('default_branch')
        if repository.get('id') != item['repo_id'] or not isinstance(branch, str) or not branch:
            raise GitHubUnavailable('Repository identity or target branch is unverified')
        head = (self.call('controller', item, 'GET', '/branches/' + quote(branch, safe='')).get('commit') or {}).get('sha')
        if not isinstance(head, str) or not SHA.fullmatch(head):
            raise GitHubUnavailable('Default branch head could not be verified')
        try:
            files = self.call('controller', item, 'GET', '/contents/.github/workflows?ref=' + quote(head, safe=''))
        except GitHubUnavailable as error:
            if error.status != 404:
                raise
            files = []  # A repository without workflows has a stable, empty digest.
        if not isinstance(files, list) or any(not isinstance(f, dict) or not isinstance(f.get('path'), str)
                                              or not isinstance(f.get('sha'), str) for f in files):
            raise GitHubUnavailable('Workflow inventory is malformed')
        return digest(sorted([f['path'], f['sha']] for f in files)), head

    def file_issue(self, item, title, body, labels):
        """One operator-confirmed proposal, never labelled ready: a human starts it."""
        value = self.call('controller', item, 'POST', '/issues',
                          {'title': title, 'body': body, 'labels': list(labels)})
        number = (value or {}).get('number')
        if type(number) is not int or number <= 0:
            raise GitHubUnavailable('The filed deployment issue could not be verified')
        return number

    def deployments(self, item, environment, limit=5):
        """Newest first, on the optional grant the caller has already verified."""
        suffix = f'/deployments?per_page={int(limit)}'
        if environment:
            suffix += '&environment=' + quote(str(environment), safe='')
        rows = self.call('controller', item, 'GET', suffix, extra=OPTIONAL_PERMISSIONS['controller'])
        if not isinstance(rows, list):
            raise GitHubUnavailable('GitHub deployment inventory is malformed')
        for row in rows:
            if (not isinstance(row, dict) or type(row.get('id')) is not int
                    or not isinstance(row.get('sha'), str) or not SHA.fullmatch(row['sha'])):
                raise GitHubUnavailable('GitHub deployment inventory is malformed')
        return sorted(rows, key=lambda row: row['id'], reverse=True)

    def deployment_statuses(self, item, deployment_id):
        """Newest first by identifier: the latest status is never an ordering guess."""
        rows = self.call('controller', item, 'GET', f'/deployments/{int(deployment_id)}/statuses?per_page=5',
                         extra=OPTIONAL_PERMISSIONS['controller'])
        if not isinstance(rows, list):
            raise GitHubUnavailable('GitHub deployment statuses are malformed')
        for row in rows:
            if (not isinstance(row, dict) or type(row.get('id')) is not int
                    or not isinstance(row.get('state'), str)):
                raise GitHubUnavailable('GitHub deployment statuses are malformed')
        return sorted(rows, key=lambda row: row['id'], reverse=True)

    def contains(self, item, ancestor, descendant):
        """Is the merge in the history of the observed commit? Asked once per pair."""
        key = (item['repo_id'], ancestor, descendant)
        if key in COMPARISONS:
            return COMPARISONS[key]
        value = self.call('controller', item, 'GET',
                          f"/compare/{quote(ancestor, safe='')}...{quote(descendant, safe='')}")
        status = (value or {}).get('status')
        if status not in ('ahead', 'behind', 'identical', 'diverged'):
            raise GitHubUnavailable('GitHub commit comparison is malformed')
        if len(COMPARISONS) > 4096:
            COMPARISONS.clear()
        COMPARISONS[key] = status in ('ahead', 'identical')
        return COMPARISONS[key]

    def issues(self, repo):
        return [i for i in self.pages('controller', repo, '/issues?state=open&labels=' + quote(self.runtime['ready_label']))
                if 'pull_request' not in i]

    def issue(self, item):
        value, requirements, _ = self.issue_inputs(item)
        return value, requirements

    def issue_inputs(self, item):
        """One bounded comment observation for both the digest and frozen notes."""
        value = self.call('controller', item, 'GET', f"/issues/{item['number']}")
        if value.get('number') != item['number'] or 'pull_request' in value:
            raise GitHubUnavailable('Issue identity could not be verified')
        comments = self.pages('controller', item, f"/issues/{item['number']}/comments")
        requirements = digest({'title': value['title'], 'body': value.get('body') or '',
                               'comments': [(c['id'], c['body']) for c in comments if c.get('user', {}).get('type') == 'User']})
        return value, requirements, comments

    def ready_approves(self, item, issue, ready_label):
        """Verify that the current ready label approves this exact issue text."""
        from ..store.remote_time import RemoteTimestampError, instant
        if ready_label not in {row['name'] if isinstance(row, dict) else row for row in issue.get('labels', [])}:
            return False
        node_id = issue.get('node_id')
        if not isinstance(node_id, str) or not node_id:
            return False
        query = '''query IssueReadyApproval($id: ID!, $before: String) {
          node(id: $id) { ... on Issue {
            number title body lastEditedAt
            timelineItems(last: 100, before: $before, itemTypes: [LABELED_EVENT, UNLABELED_EVENT, RENAMED_TITLE_EVENT]) {
              pageInfo { hasPreviousPage startCursor }
              nodes {
                __typename
                ... on LabeledEvent { createdAt label { name } actor { __typename } }
                ... on UnlabeledEvent { label { name } }
              }
            }
          } }
        }'''
        before = None
        try:
            for _ in range(100):
                observed = self.graphql('controller', item, query, {'id': node_id, 'before': before})['node']
                if (observed['number'], observed['title'], observed['body'] or '') != (
                        item['number'], issue['title'], issue.get('body') or ''):
                    return False
                edited = observed['lastEditedAt']
                timeline = observed['timelineItems']
                # Work backwards so a removal or title edit after approval wins.
                for event in reversed(timeline['nodes']):
                    kind = event['__typename']
                    if kind == 'RenamedTitleEvent':
                        return False
                    if kind not in {'LabeledEvent', 'UnlabeledEvent'}:
                        return False
                    if event['label']['name'] == ready_label:
                        # A Forge label write is separate from the revision-checked
                        # operator decision and could race with an edit.
                        if kind != 'LabeledEvent' or event['actor']['__typename'] != 'User':
                            return False
                        applied = instant(event['createdAt'])
                        return edited is None or instant(edited) < applied
                page = timeline['pageInfo']
                if page['hasPreviousPage'] is not True or not page['startCursor'] or page['startCursor'] == before:
                    return False
                before = page['startCursor']
        except (KeyError, TypeError, RemoteTimestampError):
            return False
        return False

    def pulls(self, item):
        rows = self.pages('controller', item, '/pulls?state=open')
        publisher = None
        for pr in rows:
            match = re.match(r'forge/issue-(\d+)-', str((pr.get('head') or {}).get('ref') or ''))
            pr['forge_owned'] = False
            branch = str((pr.get('head') or {}).get('ref') or '')
            if match or UNIT_BRANCH.fullmatch(branch):
                publisher = publisher or set(self.publishers('build') + self.publishers('controller'))
                pr['forge_owned'] = (own_build_pr(pr, item['repo_id'], int(match[1]), publisher) if match
                                     else own_grouped_pr(pr, item['repo_id'], publisher))
        from .ownership import linked_pulls
        return linked_pulls(self,item,rows)

    def collaborators(self, item):
        """People who can approve here: push access, offered as reviewer choices."""
        rows = self.pages('controller', item, '/collaborators?permission=push')
        return [{'login': row['login'], 'name': str(row.get('name') or '')[:120]}
                for row in rows if isinstance(row, dict) and isinstance(row.get('login'), str)][:500]

    def recent_authors(self, item):
        """People whose pull requests were recently merged here: they demonstrably
        ship to this repository, including org members GitHub does not list as
        collaborators to an App."""
        rows = self.call('controller', item, 'GET', '/pulls?state=closed&sort=updated&direction=desc&per_page=100')
        logins = []
        for row in rows if isinstance(rows, list) else []:
            user = row.get('user') or {} if isinstance(row, dict) else {}
            login = user.get('login')
            if row.get('merged_at') and isinstance(login, str) and user.get('type') != 'Bot' and not login.endswith('[bot]') and login not in logins:
                logins.append(login)
        return logins[:30]

    def label_events(self, item, number):
        """Who last applied each label, newest first."""
        rows = self.call('controller', item, 'GET', f'/issues/{number}/events?per_page=100')
        seen, out = set(), []
        for row in reversed(rows if isinstance(rows, list) else []):
            label = ((row or {}).get('label') or {}).get('name')
            if row.get('event') == 'labeled' and isinstance(label, str) and label not in seen:
                seen.add(label)
                out.append({'label': label, 'login': (row.get('actor') or {}).get('login'), 'at': row.get('created_at')})
        return out

    def own_draft(self, item, number, publisher):
        """Resume an owned PR, without treating a draft as a verified candidate."""
        rows = [p for p in self.pulls(item)
                if own_build_pr(p, item['repo_id'], number, publisher)]
        rows.sort(key=lambda p: (str(p.get('updated_at') or ''), p['number']), reverse=True)
        return ({'pr': rows[0]['number'], 'branch': rows[0]['head']['ref'],
                 'superseded': [p['number'] for p in rows[1:]]} if rows else None)

    def environment_label(self, item, issue, label):
        labels = {row['name'] for row in issue.get('labels', [])}
        if label not in labels:
            self.call('controller', item, 'POST', f"/issues/{item['number']}/labels", {'labels': [label]})
        for old in labels.intersection({'env:standard', 'env:full'}) - {label}:
            try:
                self.call('controller', item, 'DELETE', f"/issues/{item['number']}/labels/" + quote(old, safe=''))
            except GitHubUnavailable as error:
                if error.status != 404:
                    raise

    def pr(self, item, number):
        value = self.call('controller', item, 'GET', f'/pulls/{number}')
        if value.get('number') != number or value.get('base', {}).get('repo', {}).get('id') != item['repo_id']:
            raise GitHubUnavailable('PR repository identity could not be verified')
        return value

    def publication(self, job, requirements, policy):
        """Search the full bounded inventory and accept one exact contract, not links."""
        from .delegation_contracts import read_only_build
        if read_only_build(job):
            return None
        matches = []
        for pr in self.pulls(job):
            body = pr.get('body') or ''
            try:
                record = submission(body)
            except ValueError:
                continue
            if record['attempt'] == job['id']:
                matches.append(pr)
        if len(matches) != 1:
            return None
        try:
            pr = self.pr(job, matches[0]['number'])
            value = candidate(pr, job, requirements, policy)
            if job.get('builder_stage') == 'integration':
                manifest = job['input_manifest']
                inputs = [row['commit_oid'] for row in manifest['part_outputs']] + [row['head_oid'] for row in manifest['existing_candidates']]
                if manifest.get('previous_publication'):
                    inputs.append(manifest['previous_publication']['head_oid'])
                if any(not self.contains(job, head, value['head']) for head in inputs):
                    return None
            value['branch'] = pr['head']['ref']
            if value['base'] != job.get('input_manifest', {}).get('base_oid', value['base']):
                value['author'] = {key: pr['user'][key] for key in ('id', 'login', 'type')}
            return value
        except (ValueError, TypeError, KeyError):
            return None

    def ci_evidence(self, job):
        """Bounded check evidence fetched by the controller, not a worker token.

        A snapshot is context, never an approval or a replacement for GitHub's
        required-check gates. No Actions permission or log credential is needed.
        """
        selected = job.get('candidate')
        if not selected:
            return None
        snapshots = {}
        for revision in dict.fromkeys((selected['head'], selected['base'])):
            rows = self.pages('controller', job, f'/commits/{revision}/check-runs?filter=latest', 'check_runs')
            if len(rows) > 100:
                raise GitHubUnavailable('CI snapshot exceeded its check limit')
            checks = []
            for row in rows:
                if row.get('head_sha') != revision:
                    raise GitHubUnavailable('CI check belongs to a different commit')
                if row.get('name', '').startswith('forge/'):
                    continue  # Do not treat our own pending verdict as project CI.
                output = row.get('output') or {}
                check = {key: row.get(key) for key in ('id', 'name', 'status', 'conclusion', 'details_url')}
                check['app_id'] = (row.get('app') or {}).get('id')
                check['summary'] = str(output.get('summary') or '')[:2000]
                check['annotations'] = []
                if row.get('conclusion') in ('failure', 'timed_out', 'action_required'):
                    annotations = self.call('controller', job, 'GET', f"/check-runs/{int(row['id'])}/annotations?per_page=20")
                    if not isinstance(annotations, list):
                        raise GitHubUnavailable('CI annotations are malformed')
                    check['annotations'] = [{key: str(a.get(key) or '')[:2000] for key in
                                             ('path', 'start_line', 'annotation_level', 'title', 'message')}
                                            for a in annotations[:20]]
                    check['annotations_truncated'] = int(output.get('annotations_count') or 0) > 20
                checks.append(check)
            snapshots[revision] = checks
        value = {'observed_at': self.clock(), 'head': selected['head'], 'base': selected['base'],
                 'checks': snapshots, 'scope': 'GitHub check runs and bounded annotations; not Actions logs or legacy commit statuses'}
        if len(json.dumps(value)) > 128000:
            raise GitHubUnavailable('CI evidence exceeded its size limit')
        return value

    def work_details_url(self, item, selected):
        from ..store.db import Store
        from ..store.review_progress import details_url
        database = getattr(self.settings, 'database', None)
        if not isinstance(database, Store):
            return None
        with database.read_snapshot() as db:
            binding = db.execute('SELECT 1 FROM review_pull_requests WHERE repository_id=? AND pull_number=? LIMIT 1',
                                 (item.get('repo_id'), selected.get('pr'))).fetchone()
            if binding is None:
                return None  # Issue-backed work retains its existing navigation.
            row = db.execute('SELECT origin FROM operator_auth_configuration WHERE singleton=1').fetchone()
        return details_url(row['origin'], item.get('repo_id'), selected.get('pr')) if row else None

    def check(self, item, role, candidate_value, external_id, conclusion, summary, *, observe_only=False):
        name = 'forge/merge-policy' if role == 'controller' else 'forge/' + role
        runs = self.pages(role, item, f"/commits/{candidate_value['head']}/check-runs?filter=all", 'check_runs')
        app_id = self.connection(role)['app_id']
        # A lost reply before migration may already have an exact receipt from
        # the retired App. Read it before writing under the new App identity.
        readable = self.app_ids(role)
        observed = [r for r in runs if r.get('name') == name and r.get('app', {}).get('id') in readable
                    and r.get('external_id') == external_id]
        if any(r.get('head_sha') != candidate_value['head'] for r in observed):
            if observe_only:
                return None
            raise GitHubUnavailable('Check publication observed another head')
        for identity in readable:
            if sum(r['app']['id'] == identity for r in observed) > 1:
                raise GitHubUnavailable('Duplicate check publication needs reconciliation')
        details = self.work_details_url(item, candidate_value)
        expected_status = 'completed' if conclusion else 'in_progress'
        receipts = [r for r in observed if (r['app']['id'] == app_id or conclusion is not None)
                    and r.get('head_sha') == candidate_value['head'] and r.get('status') == expected_status and r.get('conclusion') == conclusion
                    and (r.get('output') or {}).get('summary') == summary[:60000]
                    and (details is None or r.get('details_url') == details or r['app']['id'] != app_id)]
        if len(receipts) > 1:
            raise GitHubUnavailable('Duplicate check publication needs reconciliation')
        if receipts:
            return receipts[0]['id']
        # An obsolete retired run is immutable history. Only the current App's
        # run can be updated, and only an exact receipt can finish read-back.
        own = [r for r in observed if r['app']['id'] == app_id]
        # GitHub retains a completed check's conclusion when asked to mark it
        # in_progress again. Keep that verdict until a new revision/verdict is
        # available; trying to reopen it makes verification fail and blocks the
        # very build that must repair it. Review/audit attempts have new IDs.
        if own and own[0].get('head_sha') == candidate_value['head'] and own[0].get('status') == 'completed' and conclusion is None:
            return own[0]['id']
        payload = {'name': name, 'external_id': external_id,
                   'status': 'completed' if conclusion else 'in_progress',
                   'output': {'title': name + (': ' + conclusion if conclusion else ': in progress'),
                              'summary': summary[:60000]}}
        if details is not None:
            payload['details_url'] = details
        if conclusion:
            payload['conclusion'] = conclusion
        if own and own[0].get('head_sha') == candidate_value['head'] and own[0].get('status') == payload['status'] and own[0].get('conclusion') == conclusion and (own[0].get('output') or {}).get('summary') == payload['output']['summary'] and (details is None or own[0].get('details_url') == details):
            return own[0]['id']
        if observe_only:
            return None
        if own:
            # Same external ID is the durable publication idempotency key.
            path = '/check-runs/' + str(own[0]['id'])
            self.call(role, item, 'PATCH', path, payload)
        else:
            payload['head_sha'] = candidate_value['head']
            self.call(role, item, 'POST', '/check-runs', payload)
        after = self.pages(role, item, f"/commits/{candidate_value['head']}/check-runs?filter=all", 'check_runs')
        verified = [r for r in after if r.get('name') == name and r.get('app', {}).get('id') == app_id
                    and r.get('external_id') == external_id and r.get('head_sha') == candidate_value['head']
                    and r.get('status') == payload['status'] and (not conclusion or r.get('conclusion') == conclusion)
                    and (details is None or r.get('details_url') == details)
                    and (conclusion is not None or (r.get('output') or {}).get('summary') == payload['output']['summary'])]
        if len(verified) != 1:
            raise GitHubUnavailable('Published check has not been verified')
        return verified[0]['id']

    def verdict(self, job, report, *, observe_only=False):
        """Post an executor's verdict exactly as returned: a check run plus a comment.

        GitHub never counts an App's approval, so the verdict is not a PR review.
        Review and audit stay separate executors; one App only publishes them.
        A review a role App posted before the move to one App still settles
        its effect, so pending publications survive the migration."""
        candidate_value = job['candidate']
        marker = '<!-- forge-verdict ' + job['id'] + ' -->'
        logins = set(self.publishers(job['role'])) | {job['publisher']}
        reviews = self.pages(job['role'], job, f"/pulls/{candidate_value['pr']}/reviews")
        legacy = [r for r in reviews if marker in (r.get('body') or '') and r.get('user', {}).get('login') in logins
                  and r.get('commit_id') == candidate_value['head']]
        if legacy:
            expected = 'APPROVED' if report['outcome'] == 'pass' else 'CHANGES_REQUESTED'
            if len(legacy) != 1 or legacy[0]['state'] != expected:
                raise GitHubUnavailable('Role review publication is unconfirmed or dismissed')
        else:
            path = f"/issues/{candidate_value['pr']}/comments"
            def owned():
                return [c for c in self.pages(job['role'], job, path) if marker in (c.get('body') or '')
                        and (c.get('user') or {}).get('login') in logins]
            found = owned()
            if not found and observe_only:
                return None
            if not found:
                self.call(job['role'], job, 'POST', path, {'body': verdict_comment(job, report, marker)})
                found = owned()
            if len(found) != 1:
                raise GitHubUnavailable('Verdict comment publication is unconfirmed')
        return self.check(job, job['role'], candidate_value, job['id'],
                          'success' if report['outcome'] == 'pass' else 'failure', report['summary'], observe_only=observe_only)

    def human(self, item, pr):
        approvals = self.approvals(item, pr)
        blocked = any(a['state'] == 'CHANGES_REQUESTED' for a in approvals)
        return any(a['counted'] for a in approvals) and not blocked, blocked

    def approvals(self, item, pr):
        """Each reviewer's latest decision on the PR, and whether it authorizes this candidate.

        `human_reviewers` is an optional allowlist. Without one, any person with
        write access counts, as on GitHub; a public repository's visitors cannot approve."""
        reviews = self.pages('controller', item, f"/pulls/{pr['number']}/reviews")
        latest = {}
        for review in sorted(reviews, key=lambda r: r['id']):
            if review['state'] in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
                latest[review['user']['login'].lower()] = review
        allowlist = {n.lower() for n in self.runtime['human_reviewers']}
        after = (item.get('candidate') or {}).get('accepted_at', 0)
        verification_times = [v['created_at'] for v in item.get('verified_checks', []) if v.get('proof', {}).get('via') == 'human']
        if item.get('verification_request'):
            verification_times.append(item['verification_request']['created_at'])
        verification_after = max(verification_times, default=0)
        def submitted(review):
            try:
                return datetime.fromisoformat(review['submitted_at'].replace('Z', '+00:00')).timestamp()
            except (ValueError, KeyError, TypeError, AttributeError):
                return None
        rows = []
        for name, review in latest.items():
            at = submitted(review)
            if review['user'].get('type') != 'User':
                why = 'Bot reviews do not count'
            elif review['state'] != 'APPROVED':
                why = 'Requested changes' if review['state'] == 'CHANGES_REQUESTED' else 'Approval was dismissed'
            elif name == pr['user']['login'].lower():
                why = 'The pull request author cannot approve it'
            elif review['commit_id'] != pr['head']['sha']:
                why = 'Approved an earlier commit'
            elif after and (at is None or at <= int(after)):
                why = 'Approved before the current change was accepted'
            elif verification_after and (at is None or at <= int(verification_after)):
                why = 'Approved before the verification request'
            elif allowlist and name not in allowlist:
                why = 'Not on the approver list in Settings'
            elif not allowlist and not self.can_write(item, review['user']['login']):
                why = 'No write access to the repository'
            else:
                why = None
            rows.append({'login': review['user']['login'], 'state': review['state'], 'at': at,
                         'counted': why is None, 'why': why})
        return rows

    def can_write(self, item, login):
        try:
            found = self.call('controller', item, 'GET', '/collaborators/' + quote(login, safe='') + '/permission')
        except GitHubBudgetExhausted:
            raise
        except GitHubUnavailable:
            return False
        return isinstance(found, dict) and found.get('permission') in ('admin', 'write')

    def protections(self, item, candidate_value):
        # The final GitHub merge API enforces external rules for this App.
        # Its generic UI merge state cannot account for actor-specific access.
        return protection_ready(self.protection_report(item, candidate_value))

    def protection_report(self, item, candidate_value):
        """Observe effective external rules without imposing a Forge baseline."""
        branch = candidate_value['base_ref']
        repo = item.get('repo', '')
        report = {'repo_id': item.get('repo_id'), 'branch': branch, 'observed_at': self.clock(),
                  'available': True, 'plan_unavailable_sources': [], 'requirements': [], 'sources': [], 'errors': [],
                  'additional_rules': [], 'code_owner_reviews': False}
        reviews = [];required_checks=[]
        try:
            protection = self.call('controller', item, 'GET', '/branches/' + quote(branch, safe='') + '/protection')
            if not isinstance(protection, dict):
                raise GitHubUnavailable('Malformed branch protection')
            review = protection.get('required_pull_request_reviews')
            if review is not None and not isinstance(review, dict):
                raise GitHubUnavailable('Malformed review protection')
            source = {'kind': 'classic', 'name': 'Branch protection',
                      'url': f'https://github.com/{repo}/settings/branches'}
            report['sources'].append(source)
            if review:
                reviews.append((review, source))
            checks=protection.get('required_status_checks')
            if checks is not None:
                if not isinstance(checks,dict) or not isinstance(checks.get('checks',[]),list):
                    raise GitHubUnavailable('Malformed required status checks')
                required_checks.extend((check,source,'app_id') for check in checks.get('checks',[]))
        except GitHubBudgetExhausted:
            raise
        except GitHubUnavailable as error:
            if plan_unavailable(error):
                report['plan_unavailable_sources'].append('classic')
            elif error.status != 404:
                report['errors'].append({'source': 'classic', 'detail':
                    'Branch protection could not be read. Check Controller access and try again.'})
        try:
            rules = self.pages('controller', item, '/rules/branches/' + quote(branch, safe=''))
            for rule in rules:
                if not isinstance(rule, dict) or not isinstance(rule.get('type'), str):
                    raise GitHubUnavailable('Malformed ruleset observation')
                identifier = rule.get('ruleset_id')
                source = {'kind': 'ruleset', 'name': rule.get('ruleset_source') or 'Repository rules',
                          'id': identifier, 'source_type': rule.get('ruleset_source_type'),
                          'url': f'https://github.com/{repo}/rules/{identifier}' if type(identifier) is int
                                 else f'https://github.com/{repo}/rules'}
                if source not in report['sources']:
                    report['sources'].append(source)
                if rule['type'] == 'pull_request':
                    parameters = rule.get('parameters')
                    if not isinstance(parameters, dict):
                        raise GitHubUnavailable('Malformed pull request rule')
                    reviews.append(({**parameters, 'require_code_owner_reviews':
                                     parameters.get('require_code_owner_review', False)}, source))
                else:
                    report['additional_rules'].append({'type': rule['type'], 'source': source})
                    if rule['type']=='required_status_checks':
                        checks=(rule.get('parameters') or {}).get('required_status_checks')
                        if not isinstance(checks,list):raise GitHubUnavailable('Malformed required status checks')
                        required_checks.extend((check,source,'integration_id') for check in checks)
        except GitHubBudgetExhausted:
            raise
        except GitHubUnavailable as error:
            if plan_unavailable(error):
                report['plan_unavailable_sources'].append('rulesets')
            else:
                report['errors'].append({'source': 'rulesets', 'detail':
                    'Repository rules could not be checked. Check GitHub access and try again.'})
        valid = all(type(r.get('required_approving_review_count', 0)) is int
                    and 0 <= r.get('required_approving_review_count', 0) <= 100
                    and type(r.get('dismiss_stale_reviews', r.get('dismiss_stale_reviews_on_push', False))) is bool
                    and type(r.get('require_code_owner_reviews', False)) is bool for r, _ in reviews)
        valid = valid and all(isinstance(check, dict) and isinstance(check.get('context'), str)
                              and bool(check['context']) for check, _, _ in required_checks)
        if not valid:
            report['errors'].append({'source': 'reviews', 'detail': 'GitHub returned incomplete review settings. Check again.'})
        report['available'] = not report['errors']
        if not report['available']:
            report['requirements'] = [{'id': 'readable', 'passed': False,
                'label': 'Check repository review rules', 'detail': report['errors'][0]['detail'], 'sources': []}]
            return report
        count = max((r.get('required_approving_review_count', 0) for r, _ in reviews), default=0)
        report['code_owner_reviews'] = any(r.get('require_code_owner_reviews', False) for r, _ in reviews)
        report['required_approvals'] = count
        from .config import merge_check_scope
        report['merge_check_scope'] = merge_check_scope(self.runtime, item['repo_id'])
        report['requirements'] = [{'id': 'readable', 'label': 'Observe existing GitHub rules',
            'passed': True, 'detail': ('GitHub reports these protections are unavailable on this plan. Forge still enforces Review and Audit.'
                if report['plan_unavailable_sources'] else
                'Forge enforces Review and Audit. Existing GitHub rules remain in force.'),
            'sources': report['sources']}]
        return report

    def gates(self, item):
        candidate_value = item['candidate']
        runs = self.pages('controller', item, f"/commits/{candidate_value['head']}/check-runs?filter=all", 'check_runs')
        for role in ('review', 'audit'):
            accepted = item['evidence'].get(role)
            if not accepted:
                return False
            # Accepted evidence a retired role App published stays valid after
            # the move to one App; the accepted check ID still has to be current.
            apps = self.app_ids(role)
            own = [r for r in runs if r.get('name') == 'forge/' + role and r.get('app', {}).get('id') in apps]
            if not own:
                return False
            # Review-only PRs may share a commit. Each keeps its own exact
            # accepted verdict; the review-policy adapter verifies every peer
            # before publishing success for that shared commit.
            current = (next((run for run in own if run['id'] == accepted['check_id']), None)
                       if item.get('review_source') or item.get('review_peer_scope') else max(own, key=lambda r: r['id']))
            if current is None:
                return False
            if (current.get('head_sha', candidate_value['head']) != candidate_value['head']
                    or current['id'] != accepted['check_id'] or current.get('conclusion') != 'success'
                    or current.get('status') != 'completed'):
                return False
        return True

    def review_policy_current(self,item,external_ids):
        """Observe the aggregate head check; never replace/cancel a sibling run."""
        if not isinstance(external_ids,list) or not 1<=len(external_ids)<=20:
            raise ValueError('Review policy observation requires bounded exact effect identities')
        head=item['candidate']['head'];app_id=self.connection('controller')['app_id']
        runs=self.pages('controller',item,f"/commits/{head}/check-runs?filter=all",'check_runs')
        owned=[run for run in runs if run.get('name')=='forge/merge-policy' and (run.get('app') or {}).get('id')==app_id
               and run.get('external_id') not in item.get('retired_policy_ids', [])]
        if not owned:return False
        latest=max(owned,key=lambda run:run['id'])
        return (latest.get('external_id') in external_ids and latest.get('head_sha')==head
                and latest.get('status')=='completed' and latest.get('conclusion')=='success')

    MERGE_REFUSALS = {
        405: 'GitHub rejected the merge under the repository’s existing rules; check its required approvals and checks',
        409: 'the head moved since verification',
        404: 'the controller cannot see the pull request',
    }

    def merge(self, item):
        candidate_value = item['candidate']
        # The expected SHA makes a concurrent head push a refused merge.
        try:
            value = self.call('controller', item, 'PUT', f"/pulls/{candidate_value['pr']}/merge",
                              {'sha': candidate_value['head'], 'merge_method': self.runtime['merge_method']})
        except GitHubUnavailable as error:
            if error.status is None:
                raise
            # A refused merge is the one GitHub error an operator can act on, so
            # it names the pull request and the likely rule instead of the
            # generic observation message.
            why = self.MERGE_REFUSALS.get(error.status, f'GitHub returned HTTP {error.status}')
            raise GitHubUnavailable(f"GitHub refused to merge {item['repo']}#{candidate_value['pr']}: {why}",
                                    status=error.status, plan_required=error.plan_required) from None
        return value.get('merged') is True

    def labels(self, item):
        wanted = 'forge:' + item['state']
        issue = self.call('controller', item, 'GET', f"/issues/{item['number']}")
        labels = {l['name'] for l in issue.get('labels', [])}
        if wanted not in labels:
            known = {l['name'] for l in self.pages('controller', item, '/labels')}
            if wanted not in known:
                self.call('controller', item, 'POST', '/labels', {'name': wanted, 'color': '64748b', 'description': 'Supervisor projection; not authority to advance an issue'})
            self.call('controller', item, 'POST', f"/issues/{item['number']}/labels", {'labels': [wanted]})
            verified = self.call('controller', item, 'GET', f"/issues/{item['number']}")
            if wanted not in {l['name'] for l in verified.get('labels', [])}:
                raise GitHubUnavailable('Pipeline label projection was not verified')
        for label in labels:
            if label.startswith('forge:') and label != wanted:
                self.call('controller', item, 'DELETE', f"/issues/{item['number']}/labels/" + quote(label, safe=''))
