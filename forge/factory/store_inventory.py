"""Paged repository inventory and ready-issue admission without a JSON cache."""
import json
import time
import uuid

from ..repository_setup import RepositorySetup
from ..settings import digest
from ..store.models import Command, TransitionError, RequirementsStale, complete_requirements
from ..store.remote_time import order as remote_order
from ..store.queries import Queries, _limit
from ..store.runtime import writable
from ..store.transitions import Lifecycle, manifest_hash
from .github import GitHubUnavailable
from .ownership import ownership


class StoreInventory:
    def __init__(self, store, settings, github, *, config, clock=time.time):
        self.store, self.settings, self.github = store, settings, github
        self.config, self.clock = config, clock

    def _observed(self, db, key, **payload):
        db.execute('INSERT INTO events(transaction_id,issue_id,kind,at,actor_id,payload_json) VALUES(?,?,?,?,?,?)',
                   (str(uuid.uuid4()), key, 'issue_observed', self.clock(), 'supervisor:inventory',
                    json.dumps({'source': 'inventory', **payload})))

    def failure(self, key, reason):
        """Persist one bounded observation, with a retry delay, for this issue."""
        if reason not in {'requirements_stale', 'remote_timestamp_invalid', 'admission_conflict'}:
            reason = 'admission_conflict'
        now = self.clock()
        with self.store.write() as db:
            writable(db)
            row = db.execute('SELECT current_revision_id FROM issues WHERE id=?', (key,)).fetchone()
            if row:
                previous = db.execute('SELECT revision_id,reason_code FROM issue_discovery_observations WHERE issue_id=?', (key,)).fetchone()
                db.execute('INSERT INTO issue_discovery_observations VALUES(?,?,?,?,?) '
                           'ON CONFLICT(issue_id) DO UPDATE SET revision_id=excluded.revision_id,'
                           'reason_code=excluded.reason_code,observed_at=excluded.observed_at,retry_after=excluded.retry_after',
                           (key, row[0], reason, now, now + self.config['availability_retry_seconds']))
                if previous is None or tuple(previous) != (row[0], reason):
                    self._observed(db, key, reason_code=reason)
        return reason

    def clear_failure(self, key):
        with self.store.write() as db:
            writable(db)
            if db.execute('DELETE FROM issue_discovery_observations WHERE issue_id=?', (key,)).rowcount:
                self._observed(db, key, reason_code=None)

    def _label_display(self, repo, key, labels):
        labels_hash=digest(sorted(labels))
        with self.store.write() as db:
            writable(db)
            current=db.execute('SELECT labels_json FROM issues WHERE id=?',(key,)).fetchone()
            if not current or sorted(json.loads(current[0]))!=sorted(labels):return
            # A removed label's actor must not survive a later reapplication.
            db.execute('DELETE FROM issue_label_attributions WHERE issue_id=? AND labels_hash!=?',(key,labels_hash))
            cached=db.execute('SELECT 1 FROM issue_label_attributions WHERE issue_id=? AND labels_hash=?',(key,labels_hash)).fetchone()
        if cached or not set(labels).intersection(self.config['hold_labels']):return
        reader=getattr(self.github,'label_events',None)
        if reader is None:return
        try:events=reader({'repo_id':repo['id'],'repo':repo['name']},int(key.split(':')[-1]))
        except (GitHubUnavailable,ValueError,KeyError,TypeError,OSError):return
        if not isinstance(events,list):return
        with self.store.write() as db:
            writable(db)
            current=db.execute('SELECT labels_json FROM issues WHERE id=?',(key,)).fetchone()
            if current and sorted(json.loads(current[0]))==sorted(labels):
                from ..store.presentation import record_label_display
                record_label_display(db,key,labels,events)
                self._observed(db, key, labels_hash=labels_hash)

    def record(self, repo, remote, requirements=None):
        """Inventory facts cannot overwrite a live unit's ordered observations.

        A listing has no complete comment history. Untracked inventory revisions
        say so explicitly; admission always fetches the full requirements first.
        """
        number = remote.get('number')
        if type(number) is not int or number < 1 or 'pull_request' in remote:
            return None
        from ..install_scope import issue_allowed
        if not issue_allowed(self.store, repo['id'], number):
            return None
        key = f"{repo['id']}:{number}"
        title, body = remote['title'], remote.get('body') or ''
        if not isinstance(title, str) or not isinstance(body, str) or len(body.encode()) > 1048576:
            raise GitHubUnavailable('Issue content exceeds the supported size')
        state = remote.get('state')
        if state not in ('open', 'closed'):
            raise GitHubUnavailable('Issue state is unavailable')
        updated = Lifecycle._remote_time(remote.get('updated_at'))
        labels = [r['name'] if isinstance(r, dict) else r for r in remote.get('labels', [])]
        people = [r['login'] if isinstance(r, dict) else r for r in remote.get('assignees', [])]
        types = {r['login']: r['type'] for r in remote.get('assignees', []) if isinstance(r, dict) and r.get('type')}
        facts = dict(state=state, state_reason=remote.get('state_reason'), title=title, body=body, remote_updated_at=updated,
                     labels=labels, assignees=people, assignee_types=types)
        partial = requirements is None
        if partial:
            requirements = {'source': 'inventory', 'complete': False, 'content_hash': manifest_hash([title, body])}
        unchanged_historical = False
        with self.store.read_snapshot() as db:
            member = db.execute('SELECT u.id,u.version,u.state FROM unit_issues m JOIN units u ON u.id=m.unit_id '
                                'WHERE m.issue_id=? ORDER BY (m.left_at IS NULL) DESC,m.joined_at DESC LIMIT 1', (key,)).fetchone()
            previous = db.execute('SELECT * FROM issues WHERE id=?', (key,)).fetchone()
            if partial and previous and member and member['state'] in ('merged', 'closed', 'dissolved'):
                known = db.execute('SELECT content_hash,requirements_json FROM issue_revisions WHERE id=?',
                                   (previous['current_revision_id'],)).fetchone()
                # Historical units receive no new complete evidence from an
                # identical listing either. Keep their last full observation.
                same_listing = (remote_order(previous['remote_updated_at']) == remote_order(updated)
                    and known['content_hash'] == manifest_hash([title, body])
                    and previous['github_state'] == state and previous['state_reason'] == facts['state_reason']
                    and sorted(json.loads(previous['labels_json'])) == sorted(labels)
                    and sorted(json.loads(previous['assignees_json'])) == sorted(people)
                    and json.loads(previous['assignee_types_json']) == types)
                unchanged_historical = same_listing and complete_requirements(json.loads(known['requirements_json']))
        if previous:self._label_display(repo,key,labels)
        if unchanged_historical:
            return key
        if member:
            if member['state'] not in ('merged', 'closed', 'dissolved'):
                return key
            # Historical units stay terminal, while reopened GitHub facts change.
            if previous and remote_order(previous['remote_updated_at']) > remote_order(updated):
                return key
            Lifecycle(self.store, clock=self.clock).observe_issue(
                Command(str(uuid.uuid4()), 'supervisor:inventory', member['id'], member['version']), key,
                **facts, requirements=requirements, generation=(previous['collection_generation'] if previous else 0) + 1,
                hold_labels=self.config['hold_labels'])
            if complete_requirements(requirements):self.clear_failure(key)
            return key
        with self.store.write() as db:
            writable(db)
            # An operator may admit the issue while the network read was running.
            if db.execute('SELECT 1 FROM unit_issues WHERE issue_id=? LIMIT 1', (key,)).fetchone():
                return key
            previous = db.execute('SELECT * FROM issues WHERE id=?', (key,)).fetchone()
            if previous and remote_order(previous['remote_updated_at']) > remote_order(updated):
                return key
            db.execute('INSERT INTO repositories(id,full_name,enabled) VALUES(?,?,?) ON CONFLICT(id) DO NOTHING',
                       (repo['id'], repo['name'], int(repo.get('enabled', False))))
            content_hash = manifest_hash([title, body])
            observed_at = self.clock()
            # A listing omits comments. It cannot replace unchanged complete
            # requirements or renew the evidence from the last full read.
            if partial and previous and remote_order(previous['remote_updated_at']) == remote_order(updated):
                known = db.execute('SELECT content_hash,requirements_json FROM issue_revisions WHERE id=?',
                                   (previous['current_revision_id'],)).fetchone()
                known_requirements = json.loads(known['requirements_json'])
                if known['content_hash'] == content_hash and complete_requirements(known_requirements):
                    requirements = known_requirements
                    observed_at = previous['observed_at']
            req_hash = manifest_hash(requirements)
            old_revision = db.execute('SELECT id FROM issue_revisions WHERE issue_id=? AND content_hash=? AND requirements_hash=?',
                                      (key, content_hash, req_hash)).fetchone()
            revision = old_revision[0] if old_revision else str(uuid.uuid4())
            if previous is None:
                db.execute('INSERT INTO issues(id,repository_id,github_number,github_state,current_revision_id,observed_at) VALUES(?,?,?,?,?,?)',
                           (key, repo['id'], number, state, revision, self.clock()))
            if old_revision is None:
                db.execute('INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) '
                           'VALUES(?,?,?,?,?,?,?,?)', (revision, key, title, body, content_hash, req_hash, json.dumps(requirements), self.clock()))
                from ..store.presentation import record_revision_display
                record_revision_display(db,revision,body,requirements)
            observed = (state, facts['state_reason'], revision, updated, sorted(labels), sorted(people), types)
            before = ((previous['github_state'], previous['state_reason'], previous['current_revision_id'],
                       previous['remote_updated_at'], sorted(json.loads(previous['labels_json'])),
                       sorted(json.loads(previous['assignees_json'])), json.loads(previous['assignee_types_json']))
                      if previous else None)
            changed = before is None or before[:3]+before[4:] != observed[:3]+observed[4:]
            reopened = previous['reopened_version'] if previous else 0
            if previous and state == 'open' and (previous['github_state'] == 'closed'
                    or facts['state_reason'] == 'reopened' and previous['state_reason'] != 'reopened'):
                reopened = previous['version'] + 1
            db.execute('UPDATE issues SET github_state=?,state_reason=?,current_revision_id=?,remote_updated_at=?,collection_generation=collection_generation+1,'
                       'labels_json=?,assignees_json=?,assignee_types_json=?,observed_at=?,reopened_version=?,version=version+? WHERE id=?',
                       (state, facts['state_reason'], revision, updated, json.dumps(labels), json.dumps(people), json.dumps(types), observed_at, reopened, int(changed), key))
            if changed:
                self._observed(db, key, revision_id=revision, content_hash=content_hash,
                    requirements_hash=req_hash, labels=sorted(labels), state=state,
                    state_reason=facts['state_reason'], planning_changed=True)
        self._label_display(repo,key,labels)
        if not partial and complete_requirements(requirements):self.clear_failure(key)
        return key

    def refresh(self, force=False):
        if self.settings.paused():
            return
        try:
            with self.settings.locked('issue-inventory', blocking=False):
                deadline = min(getattr(self.github, 'deadline', float('inf')), time.monotonic() + 30)
                for repo in self.settings.view()['repositories']:
                    if not repo.get('enabled') or repo.get('access', {}).get('controller') != 'ready' or time.monotonic() >= deadline:
                        continue
                    identity = digest({'name': repo['name'], 'connections': self.settings.load()['connections']})
                    with self.store.read_snapshot() as db:
                        previous = db.execute("SELECT * FROM repository_observations WHERE repository_id=? AND kind='inventory' ORDER BY collection_generation DESC LIMIT 1", (repo['id'],)).fetchone()
                    cursor = json.loads(previous['cursor']) if previous and previous['cursor'] and previous['controller_identity'] == identity else {'state': 'open', 'page': 1}
                    if previous and previous['complete'] and not previous['error_code'] and not force and 0 <= self.clock() - previous['fetched_at'] < 60:
                        continue
                    item = {'repo': repo['name'], 'repo_id': repo['id']}
                    error, complete = None, False
                    details = json.loads(previous['details_json']) if previous and previous['controller_identity'] == identity else {}
                    # Captured scan facts belong to their original observation.
                    details.pop('scan', None)
                    details.pop('scan_issues_present', None)
                    details.pop('issue_errors', None)
                    people, authors, pulls = None, None, None
                    try:
                        if cursor['page'] > 100:
                            raise GitHubUnavailable('Repository inventory exceeds the 10,000-item page limit')
                        rows = self.github.call('controller', item, 'GET', f"/issues?state={cursor['state']}&sort=created&direction=asc&per_page=100&page={cursor['page']}")
                        if not isinstance(rows, list) or len(rows) > 100:
                            raise GitHubUnavailable('Issue inventory is unavailable')
                        issue_errors = []
                        for row in rows:
                            try:
                                self.record(repo, row)
                            except TransitionError as failure:
                                number = row.get('number')
                                reason = self.failure(f"{repo['id']}:{number}", getattr(failure, 'code', 'admission_conflict'))
                                issue_errors.append({'number': number, 'reason_code': reason})
                        if issue_errors:
                            details['issue_errors'] = issue_errors
                            error = 'issue_observation_failed'
                        if len(rows) == 100:
                            cursor['page'] += 1
                        elif cursor['state'] == 'open':
                            cursor = {'state': 'closed', 'page': 1}
                        else:
                            cursor, complete = None, True
                            pulls = self.github.pulls(item)
                            from .merge_checks import ReviewPullsController
                            ReviewPullsController(self.store, self.github, config=self.config,
                                settings=self.settings, clock=self.clock).reconcile(item, pulls)
                        if details.get('collaborators_at') is None or not 0 <= self.clock() - details['collaborators_at'] < 3600:
                            people = self.github.collaborators(item)
                            authors = self.github.recent_authors(item)
                            if not isinstance(people, list) or not isinstance(authors, list):
                                people, authors = None, None
                                raise GitHubUnavailable('Repository collaborators are unavailable')
                            details['collaborators_at'] = self.clock()
                    except (GitHubUnavailable, ValueError, KeyError, TypeError, OSError):
                        error = 'inventory_unavailable'
                    with self.store.write() as db:
                        writable(db)
                        observation = str(uuid.uuid4())
                        generation = db.execute("SELECT COALESCE(MAX(collection_generation),0)+1 FROM repository_observations WHERE repository_id=? AND kind='inventory'", (repo['id'],)).fetchone()[0]
                        db.execute('INSERT INTO repository_observations(id,repository_id,kind,controller_identity,collection_generation,fetched_at,complete,cursor,error_code,details_json,refreshing) '
                                   "VALUES(?,?,'inventory',?,?,?,?,?,?,?,?)", (observation, repo['id'], identity, generation, self.clock(), int(complete),
                                    json.dumps(cursor) if cursor else None, error, json.dumps(details), int(not complete and not error)))
                        if people is not None and authors is not None:
                            db.execute('DELETE FROM inventory_collaborators WHERE repository_id=?', (repo['id'],))
                            for source, entries in [('collaborator', people), ('recent_author', [{'login': login} for login in authors])]:
                                for person in entries:
                                    db.execute('INSERT OR REPLACE INTO inventory_collaborators VALUES(?,?,?,?,?)',
                                        (repo['id'], person['login'], source, observation, json.dumps(person)))
                        if pulls is not None:
                            db.execute('DELETE FROM inventory_pull_summaries WHERE repository_id=?', (repo['id'],))
                            for pull in pulls:
                                db.execute('INSERT INTO inventory_pull_summaries VALUES(?,?,?,?,?,?,?,?)',
                                    (repo['id'], pull['number'], pull.get('title') or '', (pull.get('body') or '')[:262144],
                                     (pull.get('user') or {}).get('login') or '', (pull.get('head') or {}).get('ref') or '',
                                     int(bool(pull.get('forge_owned'))), observation))
                                from ..store.presentation import record_pull_display
                                record_pull_display(db,repo['id'],repo['name'],pull)
        except BlockingIOError:
            return

    def requested(self, limit=50):
        """Consume explicit refresh requests for issues without a live unit."""
        with self.store.read_snapshot() as db:
            rows = db.execute('SELECT q.issue_id,q.requested_at,i.github_number,r.id AS repo_id,r.full_name '
                'FROM operator_observation_requests q JOIN issues i ON i.id=q.issue_id JOIN repositories r ON r.id=i.repository_id '
                "WHERE NOT EXISTS(SELECT 1 FROM unit_issues m JOIN units u ON u.id=m.unit_id WHERE m.issue_id=i.id AND m.left_at IS NULL AND u.state NOT IN ('merged','closed','dissolved')) "
                'ORDER BY q.requested_at,q.issue_id LIMIT ?', (_limit(limit),)).fetchall()
        for row in rows:
            try:
                remote, requirements = self.github.issue(dict(repo=row['full_name'], repo_id=row['repo_id'], number=row['github_number']))
                self.record(dict(id=row['repo_id'], name=row['full_name']), remote, requirements)
                with self.store.write() as db:
                    writable(db)
                    db.execute('DELETE FROM operator_observation_requests WHERE issue_id=? AND requested_at=?', (row['issue_id'], row['requested_at']))
            except (GitHubUnavailable, TransitionError, OSError):
                continue


class DiscoveryController:
    def __init__(self, store, settings, github, *, config, clock=time.time):
        self.store, self.settings, self.github, self.config, self.clock = store, settings, github, config, clock
        self.inventory = StoreInventory(store, settings, github, config=config, clock=clock)

    def cycle(self, *, after=None, limit=50):
        limit = _limit(limit)
        self.inventory.requested(limit)
        self.inventory.refresh()
        if not self.config['enabled'] or self.settings.paused():
            return {'items': [], 'next_cursor': None}
        after = after or ''
        with self.store.read_snapshot() as db:
            rows = db.execute("SELECT i.id,i.github_number,r.id AS repo_id,r.full_name FROM issues i JOIN repositories r ON r.id=i.repository_id "
                "WHERE i.id>? AND r.enabled=1 AND i.github_state='open' AND EXISTS(SELECT 1 FROM json_each(i.labels_json) l WHERE l.value=?) "
                # A delivery closed by the import retirement (migration 041) does
                # not count: its issue is admitted fresh like a new one.
                "AND NOT EXISTS(SELECT 1 FROM unit_issues m JOIN units u ON u.id=m.unit_id WHERE m.issue_id=i.id "
                "AND u.close_code IS NOT 'import_superseded') "
                'AND NOT EXISTS(SELECT 1 FROM issue_discovery_observations d WHERE d.issue_id=i.id '
                'AND d.revision_id=i.current_revision_id AND d.retry_after>?) ORDER BY i.id LIMIT ?', (after, self.config['ready_label'], self.clock(), limit + 1)).fetchall()
        results = []
        setup = RepositorySetup(self.settings).read()['repositories'] if rows else {}
        for row in rows[:limit]:
            from ..install_scope import issue_allowed
            if not issue_allowed(self.store, row['repo_id'], row['github_number']):
                continue
            # Unready repositories wait for Settings; do not spend GitHub reads on them.
            if setup.get(str(row['repo_id']), {}).get('status') != 'ready':
                continue
            item = dict(repo=row['full_name'], repo_id=row['repo_id'], number=row['github_number'])
            try:
                if not self.settings.admission(item['repo'])['allowed']:
                    continue
                remote, requirements = self.github.issue(item)
                labels = {r['name'] if isinstance(r, dict) else r for r in remote.get('labels', [])}
                if remote.get('state') != 'open' or self.config['ready_label'] not in labels:
                    continue
                self.inventory.record(dict(id=row['repo_id'], name=row['full_name'], enabled=True), remote, requirements)
                if labels.intersection(self.config['hold_labels']) or ownership(remote, item, item['repo'], self.github.pulls(item)):
                    continue
                issue = Queries(self.store).issue(row['id'])
                if not complete_requirements(issue['requirements']):
                    raise RequirementsStale('admission requires fresh complete issue requirements')
                if (issue['requirements'] != requirements or issue['content_hash'] != manifest_hash([remote['title'], remote.get('body') or ''])
                        or set(issue['labels']) != labels):
                    continue
                if self.github.ready_approves(item, remote, self.config['ready_label']) is not True:
                    results.append({'issue_id': row['id'], 'pending': 'ready_reapproval'})
                    continue
                target = self.github.target(item)
                if not self.github.protections(item, {'base_ref': target}):
                    continue
                result = Lifecycle(self.store, clock=self.clock).admit_known_issue(request_id=str(uuid.uuid4()), actor_id='supervisor:discovery',
                    unit_id=str(uuid.uuid4()), issue_id=row['id'], expected_issue_version=issue['version'], base_branch=target)
                self.inventory.clear_failure(row['id'])
                results.append({'issue_id': row['id'], **result})
            except TransitionError as error:
                reason = self.inventory.failure(row['id'], getattr(error, 'code', 'admission_conflict'))
                results.append({'issue_id': row['id'], 'reason_code': reason})
            except (GitHubUnavailable, OSError) as error:
                results.append({'issue_id': row['id'], 'error': getattr(error, 'code', type(error).__name__)})
        return {'items': results, 'next_cursor': rows[limit - 1]['id'] if len(rows) > limit else None}
