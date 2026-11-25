"""Ordered GitHub observations for every current SQLite delivery member.

Collection generations are reserved before network reads. A late response cannot
replace a newer observation. Issue facts and holds commit together. Repository readiness uses its own
revision-bound lifecycle observation.
"""
import time
import uuid

from ..store.db import StoreError
from ..store.models import Command, TransitionError
from ..store.queries import Queries
from ..store.transitions import Lifecycle, TERMINAL, manifest_hash
from .github import GitHubUnavailable
from ..request_metrics import RequestBudgetExhausted, RequestMetrics, request_scope, deadline
from .ownership import ownership, active_publication_matches


class ObservationController:
    def __init__(self, store, github, *, admission, policy=None, readiness=None,
                 hold_labels=('hold', 'blocked'), ready_label='ready', clock=time.time, monotonic=time.monotonic):
        self.store, self.github, self.admission = store, github, admission
        self.policy, self.hold_labels, self.clock = policy, tuple(hold_labels), clock
        self.ready_label = ready_label
        self.monotonic = monotonic
        self.readiness = readiness
        self.commands = Lifecycle(store, clock=clock)
        self.queries = Queries(store)

    def _snapshot(self, unit_id, member_id):
        unit = self.queries.unit(unit_id)
        if unit['state'] in TERMINAL:
            return None
        member=next((row for row in unit['members'] if row['id']==member_id),None)
        if member is None:
            return None
        issue=self.queries.issue(member['issue_id'])
        with self.store.read_snapshot() as db:
            if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                raise TransitionError('staging imports cannot observe live GitHub state')
            selected = db.execute('SELECT * FROM candidates WHERE id=?', (unit['current_candidate_id'],)).fetchone()
        candidate = dict(selected) if selected else None
        item = dict(repo=issue['repository'], repo_id=issue['repository_id'], number=issue['github_number'],
                    candidate={'pr': candidate['pull_number'],'key':candidate['id']} if candidate else None,unit_state=unit['state'])
        from ..store.operator_views import ownership_context
        item=ownership_context(self.store,item,unit['current_candidate_id'])
        from .ownership import publication_context
        return unit, issue, candidate, publication_context(self.store,unit_id,item)

    def _generation(self, issue):
        key = 'observation_generation:' + issue['id']
        with self.store.write() as db:
            if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                raise TransitionError('staging imports cannot reserve observations')
            row = db.execute('SELECT value FROM store_metadata WHERE key=?', (key,)).fetchone()
            current = db.execute('SELECT collection_generation FROM issues WHERE id=?', (issue['id'],)).fetchone()[0]
            generation = max(int(row[0]) if row else 0, current) + 1
            db.execute('INSERT INTO store_metadata(key,value) VALUES(?,?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(generation)))
        return generation

    @staticmethod
    def _command(unit):
        return Command(str(uuid.uuid4()), 'supervisor:observation', unit['id'], unit['version'])

    def _read_pr(self, item, candidate):
        pr = self.github.pr(item, candidate['pull_number'])
        if (pr.get('number') != candidate['pull_number']
                or (pr.get('head') or {}).get('repo', {}).get('id') != item['repo_id']
                or (pr.get('base') or {}).get('repo', {}).get('id') != item['repo_id']):
            raise GitHubUnavailable('Candidate repository identity is unverified')
        if (type(pr.get('merged')) is not bool
                or any(not isinstance(pr.get(part, {}).get('sha'), str) or not pr[part]['sha'] for part in ('head', 'base'))
                or pr['merged'] and (not isinstance(pr.get('merge_commit_sha'), str) or not pr['merge_commit_sha'])
                or not pr['merged'] and (pr.get('state') not in {'open', 'closed'}
                    or pr.get('state') == 'open' and type(pr.get('draft')) is not bool
                    or not isinstance(pr.get('base', {}).get('ref'), str) or not pr['base']['ref'])):
            raise GitHubUnavailable('Candidate observation is incomplete')
        return pr

    def _external_change(self, unit, detail):
        with self.store.read_snapshot() as db:
            existing = db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND code='external_change' "
                                  'AND resolved_at IS NULL', (unit['id'],)).fetchone()
        if not existing:
            self.commands.open_gate(self._command(unit), 'external_change', details={'reason': detail})
        return {'unit_id': unit['id'], 'observed': True, 'pending': 'external_change'}

    def _release_external_change(self, unit, issue, member_id):
        # Called only after the complete observation accepts current issue and
        # candidate facts. A divergent PR returns earlier and retains its gate.
        with self.store.read_snapshot() as db:
            gates=db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND code='external_change' "
                'AND resolved_at IS NULL AND (member_id IS NULL OR member_id=?)', (unit['id'],member_id)).fetchall()
        for gate in gates:
            resolved=self.commands.resolve_gate(self._command(unit),gate['id'],release='observation',
                evidence={'verified':True,'observed_at':self.clock(),'issue_id':issue['id'],
                          'issue_version':issue['version'],'candidate_id':unit['current_candidate_id']})
            unit={**unit,'version':resolved['version']}

    def step(self, unit_id):
        unit=self.queries.unit(unit_id)
        if unit['state'] in TERMINAL:
            return {'unit_id':unit_id,'terminal':True}
        outcomes={member['id']:self.observe_member(unit_id,member['id'],member['issue_id'])
                  for member in unit['members']}
        if len(outcomes)==1:
            return next(iter(outcomes.values()))
        return {'unit_id':unit_id,'members':outcomes}

    def observe_member(self, unit_id, member_id, issue_id):
        with self.store.read_snapshot() as db:
            requested=db.execute('SELECT requested_at FROM operator_observation_requests WHERE issue_id=?',(issue_id,)).fetchone()
        outcome=self._step_member(unit_id,member_id)
        if requested and (outcome.get('observed') or outcome.get('terminal')):
            with self.store.write() as db:
                db.execute('DELETE FROM operator_observation_requests WHERE issue_id=? AND requested_at=?',(issue_id,requested[0]))
        return outcome

    def _step_member(self, unit_id, member_id):
        snapshot = self._snapshot(unit_id, member_id)
        if snapshot is None:
            return {'unit_id': unit_id, 'terminal': True}
        unit, issue, candidate, item = snapshot
        generation = self._generation(issue)
        remote, requirements = self.github.issue(item)
        if (remote.get('number') != item['number'] or 'pull_request' in remote
                or not isinstance(remote.get('updated_at'), str)):
            raise GitHubUnavailable('Issue observation identity or ordering is unverified')
        labels = [row['name'] if isinstance(row, dict) else row for row in remote.get('labels', [])]
        assignees = [row['login'] if isinstance(row, dict) else row for row in remote.get('assignees', [])]
        observation = dict(state=remote['state'], state_reason=remote.get('state_reason'),
            title=remote['title'], body=remote.get('body') or '', requirements=requirements,
            remote_updated_at=remote['updated_at'], generation=generation, labels=labels,
            assignees=assignees, hold_labels=self.hold_labels,
            assignee_types={row['login']: row['type'] for row in remote.get('assignees', [])
                            if isinstance(row, dict) and row.get('type')})
        member = next(row for row in unit['members'] if row['id'] == member_id)
        content_approved = False
        if (remote['state'] == 'open' and not unit['delivery_approved'] and self.ready_label in labels
                and (manifest_hash([observation['title'], observation['body']]) != issue['content_hash']
                     or issue['current_revision_id'] != member['requirements_revision_id'])):
            try:
                content_approved = self.github.ready_approves(item, remote, self.ready_label) is True
            except GitHubUnavailable:
                pass  # Keep the approved text pinned and retry verification on the next observation.
        remote_pr, observation_error = None, None
        if candidate:
            try:
                remote_pr = self._read_pr(item, candidate)
            except (GitHubUnavailable, RequestBudgetExhausted) as error:
                observation_error = error
        terminal = remote['state'] == 'closed' or remote_pr is not None and remote_pr.get('merged') is True
        # A verified closure must fence its worker even when unrelated inventory
        # or candidate reads are unavailable. It cannot clear ownership/readiness.
        conflict = allowed = None
        if not terminal and observation_error is None:
            try:
                pulls = [pr for pr in self.github.pulls(item)
                         if not active_publication_matches(self.store, unit_id, pr)]
                conflict = ownership(remote, item, item['repo'], pulls) is not None
                allowed = self.admission(item)
                if type(allowed) is not bool:
                    raise TransitionError('admission adapter must return a verified boolean')
            except (GitHubUnavailable, RequestBudgetExhausted) as error:
                observation_error = error
        outcome = self.commands.observe_issue(self._command(unit), issue['id'], **observation,
            admission=None if self.readiness else allowed, ownership_conflict=conflict, content_approved=content_approved)
        if self.readiness and allowed is not None and not outcome.get('stale'):
            self.readiness.observe(unit_id)
        if observation_error:
            # Persist verified issue facts even when a later required read fails.
            # Budget deferral keeps this exact member pending; an unavailable
            # candidate/ownership read cannot count as a complete observation.
            raise observation_error
        if outcome.get('stale'):
            return {'unit_id': unit_id, 'stale': True}
        current = self._snapshot(unit_id, member_id)
        if current is None:
            return {'unit_id': unit_id, 'terminal': True}
        unit, issue, selected, item = current
        # A requirements change may already have superseded the candidate.
        if candidate and (not selected or selected['id'] != candidate['id']):
            self._release_external_change(unit,issue,member_id)
            return {'unit_id': unit_id, 'observed': True, 'invalidated': 'requirements'}
        proof = {'issue_version': issue['version']}
        if selected:
            pr = remote_pr if remote_pr is not None else self._read_pr(item, selected)
            exact_head = pr.get('head', {}).get('sha') == selected['head_oid']
            exact_base = pr.get('base', {}).get('sha') == selected['base_oid']
            if pr.get('merged') is True:
                if not exact_head or not exact_base:
                    return self._external_change(unit, 'merged_candidate_changed')
                with self.store.read_snapshot() as db:
                    busy = db.execute('SELECT 1 FROM jobs WHERE unit_id=? AND cleanup=0', (unit_id,)).fetchone()
                if busy:
                    return self._external_change(unit, 'external_merge_observed')
                merge_sha = pr.get('merge_commit_sha')
                return {'unit_id': unit_id, 'observed': True, **self.commands.confirm_merged(self._command(unit), selected['id'],
                    merge_sha=merge_sha, evidence={'verified': True, 'merged': True, 'merge_sha': merge_sha,
                        'pull_number': selected['pull_number'], 'head_oid': selected['head_oid'],
                        'base_oid': selected['base_oid']})}
            if remote['state'] == 'open' and active_publication_matches(self.store, unit_id, pr):
                return {'unit_id': unit_id, 'observed': True, 'publishing': True}
            if not exact_head:
                return self._external_change(unit, 'candidate_head_changed')
            proof.update(verified=True, merged=False, candidate_id=selected['id'],
                         pull_number=selected['pull_number'], head_oid=selected['head_oid'], observed_at=self.clock())
            if remote['state'] == 'open':
                if pr.get('state') != 'open' or pr.get('draft') is not False:
                    return self._external_change(unit, 'candidate_unavailable')
                if pr.get('base', {}).get('ref') != unit['base_branch']:
                    return self._external_change(unit, 'candidate_target_changed')
                if not exact_base or self.policy is not None and self.policy(item) != selected['policy_hash']:
                    reason = 'base_changed' if not exact_base else 'policy_changed'
                    return {'unit_id': unit_id, 'observed': True, **self.commands.invalidate_candidate(self._command(unit),
                        selected['id'], reason=reason, evidence=proof)}
        if remote['state'] == 'closed' and remote.get('state_reason') in {'completed', 'not_planned', 'duplicate'}:
            # A selected steward mutation retains ownership through a lost
            # reply. Creating a second membership decision for its own closure
            # would invalidate the exact authority needed to finish that intent.
            from ..store.steward_effects import closure_obligation_in
            with self.store.read_snapshot() as db:
                closure=closure_obligation_in(db,unit_id,member_id)
                operator=db.execute("SELECT e.id FROM external_effects e JOIN issue_action_requests q "
                    "ON q.id=e.operator_request_id WHERE e.unit_id=? AND q.issue_id=? "
                    "AND e.operator_step='close-issue' AND e.status IN ('pending','running','uncertain') "
                    "ORDER BY e.created_at,e.id LIMIT 1",(unit_id,issue['id'])).fetchone()
            if closure:
                return {'unit_id':unit_id,'observed':True,'pending':'steward_issue','effect_id':closure}
            if operator:
                return {'unit_id':unit_id,'observed':True,'pending':'operator_issue','effect_id':operator['id']}
            if unit['delivery_approved']:
                with self.store.read_snapshot() as db:
                    existing=db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND member_id=? AND code='membership_decision' AND resolved_at IS NULL",(unit_id,member_id)).fetchone()
                if not existing:
                    self.commands.open_gate(self._command(unit),'membership_decision',member_id=member_id,details={'reason':'member_closed','state_reason':remote['state_reason']})
                return {'unit_id':unit_id,'observed':True,'pending':'membership_decision'}
            return {'unit_id': unit_id, 'observed': True, **self.commands.close_unit(self._command(unit),
                close_code='github_' + remote['state_reason'], evidence=proof)}
        self._release_external_change(unit,issue,member_id)
        return {'unit_id': unit_id, 'observed': True}

    def cycle(self, *, after=None, limit=50):
        # The durable sweep owns its checkpoint. `after` is the shared page
        # interface; a separate caller cursor cannot override this receipt.
        from ..store.queries import _limit
        from ..store.observation_progress import ObservationProgress
        limit=_limit(limit)
        progress=ObservationProgress(self.store)
        state=progress.start(self.clock())
        metrics=RequestMetrics();started=self.monotonic();previous=metrics.view()
        items=[];complete=False
        errors_before=state['errors'];phase_errors=set()
        def checkpoint():
            nonlocal previous,started
            current=metrics.view()
            for field in ('attempts','failures','elapsed_ms'):
                state['requests'][field]+=current[field]-previous[field]
            if current['quota']:
                state['requests']['quota']=current['quota']
            previous=current
            now=self.monotonic();state['active_ms']+=(now-started)*1000;started=now
            progress.save()
        with request_scope(metrics=metrics):
            while self.monotonic()<deadline():
                page=progress.page(state['cursor'],limit)
                if not page:
                    complete=True
                    break
                state['pages']+=1
                for member in page:
                    if self.monotonic()>=deadline():break
                    try:
                        outcome=self.observe_member(member['unit_id'],member['member_id'],member['issue_id'])
                    except RequestBudgetExhausted:
                        break
                    except (GitHubUnavailable,TransitionError,StoreError,OSError) as error:
                        outcome={'unit_id':member['unit_id'],'error':getattr(error,'code',type(error).__name__)}
                    state['visits']+=1
                    if outcome.get('error'):
                        state['errors']+=1
                        phase_errors.add(outcome['error'])
                        state['error_codes']=sorted(set(state['error_codes']+[outcome['error']]))[:10]
                    elif outcome.get('terminal'):state['retired']+=1
                    elif outcome.get('observed'):state['refreshed']+=1
                    else:state['deferred']+=1
                    state['cursor']={key:member[key] for key in ('unit_id','member_id','membership_generation')}
                    if len(items)<100:items.append({**outcome,'member_id':member['member_id']})
                    checkpoint()
                else:
                    continue
                break
        state['in_progress']=False
        checkpoint()
        if complete:
            summary=progress.finish(self.clock())
        else:
            state.update(coverage=progress.coverage(state['started_at'],self.clock()),
                         scope_changed=progress.scope_hash()!=state['scope_hash'],complete=False)
            progress.save()
            summary=state
        return {'items':items,'next_cursor':None if complete else state['cursor'],'checkpointed':True,
                'sweep':summary,'error_count':state['errors']-errors_before,'error_codes':sorted(phase_errors)[:10],
                'deferred':None if complete else 'github_budget'}
