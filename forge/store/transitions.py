"""Inactive, transactional unit-of-one lifecycle commands.

These commands do not launch workers, inspect GitHub, or choose batch membership.
The caller supplies independently verified evidence. Every accepted mutation and
its event share one SQLite transaction; external effects belong outside it.
"""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import math
import sqlite3
import time
import uuid

from . import codes, status
from .presentation import record_revision_display
from .remote_time import normalize as remote_time, order as remote_order
from .models import (
    Candidate, Command, GateCode, IdempotencyConflict, Role, TransitionError,
    VersionConflict, RequirementsStale, InvalidDelegationPlan, complete_requirements,
)

TERMINAL = codes.TERMINAL_STATES
GATE_PRIORITY = {code: (gate.priority, index) for index, (code, gate) in enumerate(codes.GATES.items())}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def manifest_hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _id(request, kind):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"forge:{request}:{kind}"))


def _unit(db, unit_id):
    row = db.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()
    if row is None:
        raise TransitionError("unknown unit")
    return dict(row)


def _gates(db, unit_id):
    rows = db.execute(
        "SELECT * FROM unit_gates WHERE unit_id = ? AND resolved_at IS NULL", (unit_id,)
    ).fetchall()
    scopes={}
    job_ids=sorted({row['job_id'] for row in rows if row['job_id'] is not None})
    if job_ids:
        # Unresolved job gates consume these immutable scopes. Start
        # from their exact IDs, rather than walking every retained unit job.
        for row in db.execute('SELECT p.job_id,p.member_id FROM json_each(?) requested '
                'CROSS JOIN jobs j ON j.id=requested.value CROSS JOIN job_members p ON p.job_id=j.id '
                'WHERE j.unit_id=?', (json.dumps(job_ids),unit_id)):
            scopes.setdefault(row['job_id'],[]).append(row['member_id'])
    return sorted((dict(row)|{'job_member_ids':scopes.get(row['job_id'],[])} for row in rows), key=lambda g: (GATE_PRIORITY[g["code"]], g["created_at"], g["id"]))


def applicable_gates(db, unit, member_id=None, candidate_id=None, *, job_id=None):
    """All hard gates for this assignment, in the common display order.

    External issue dependencies require a proof for the pinned requirements
    revision. Member dependencies remain blocked until the batch evaluator
    validates their exact candidate inputs.
    Derived blockers have no gate ID and are not stored as a second authority.
    """
    assignment_members = ([row[0] for row in db.execute('SELECT member_id FROM job_members WHERE job_id=?', (job_id,))]
                          if job_id is not None else None)
    return _scope_gates(_collect_gates(db, unit), unit, member_id, candidate_id,
                        job_id=job_id, assignment_members=assignment_members)


def _collect_gates(db, unit):
    """Collect canonical unit facts; mutation callers always collect afresh."""
    gates = _gates(db, unit["id"])
    # Claims belong to their captured requirements, even after a different job
    # takes the same work. Process waits still belong only to their source job.
    pins = None
    current = []
    for gate in gates:
        details = json.loads(gate['details_json'])
        claim = (gate.get('job_id') is not None and
                 ((details.get('source') == 'build_claim' and gate['code'] in {'dependency', 'decision_required'})
                  or (details.get('source') == 'worker_claim' and gate['code'] == 'environment')))
        if claim:
            if pins is None:
                pins = dict(db.execute('SELECT id,requirements_revision_id FROM unit_issues WHERE unit_id=? AND left_at IS NULL', (unit['id'],)))
            gate['claim_member_ids'] = [member for member, revision in details['requirements_by_member'].items()
                                        if pins.get(member) == revision]
            if not gate['claim_member_ids']:
                continue
        current.append(gate)
    gates = current
    repository = db.execute("SELECT enabled FROM repositories WHERE id = ?", (unit["repository_id"],)).fetchone()
    if repository is None or not repository["enabled"]:
        gates.append({"id": None, "unit_id": unit["id"], "member_id": None, "candidate_id": None,
                      "subject_key": "unit", "code": "repository_setup", "created_at": 0.0,
                      "resolved_at": None, "details_json": _json({"reason": "repository_disabled"})})
    for row in db.execute(
        "SELECT m.id, i.state_reason FROM unit_issues m JOIN issues i ON i.id = m.issue_id "
        "WHERE m.unit_id = ? AND m.left_at IS NULL AND i.github_state = 'closed'", (unit["id"],),
    ):
        gates.append({"id": None, "unit_id": unit["id"], "member_id": row["id"], "candidate_id": None,
                      "subject_key": f"member:{row['id']}", "code": "external_change", "created_at": 0.0,
                      "resolved_at": None, "details_json": _json({"reason": "issue_closed", "state_reason": row["state_reason"]})})
    from .delivery_inputs import member_dependency_problem_in
    contribution_cache={}
    for row in db.execute("SELECT id,issue_id,requirements_revision_id FROM unit_issues WHERE unit_id=? AND left_at IS NULL", (unit['id'],)):
        external = db.execute("SELECT 1 FROM issue_dependencies d JOIN issues p ON p.id=d.prerequisite_issue_id "
            "WHERE d.issue_id=? AND d.requirements_revision_id=? "
            "AND NOT EXISTS(SELECT 1 FROM unit_issues m WHERE m.unit_id=? AND m.issue_id=p.id AND m.left_at IS NULL) "
            "AND NOT(p.github_state='closed' AND p.state_reason='completed') "
            "AND NOT COALESCE((SELECT clear FROM dependency_checks c WHERE c.member_id=? AND c.requirements_revision_id=? "
            "ORDER BY generation DESC LIMIT 1),0)",
            (row['issue_id'],row['requirements_revision_id'],unit['id'],row['id'],row['requirements_revision_id'])).fetchone()
        if external or member_dependency_problem_in(db, unit, row['id'],contribution_cache=contribution_cache):
            gates.append({"id": None, "unit_id": unit["id"], "member_id": row["id"], "candidate_id": None,
                          "subject_key": f"member:{row['id']}", "code": "dependency", "created_at": 0.0,
                          "resolved_at": None, "details_json": _json({"reason": "prerequisite_output_pending" if not external else "external_dependency_pending"})})
    for row in db.execute(
        "SELECT m.id FROM unit_issues m JOIN issues i ON i.id = m.issue_id "
        "JOIN issue_revisions current ON current.id = i.current_revision_id "
        "JOIN issue_revisions pinned ON pinned.id = m.requirements_revision_id "
        "WHERE m.unit_id = ? AND m.left_at IS NULL "
        "AND (current.requirements_hash != pinned.requirements_hash OR current.content_hash != pinned.content_hash)",
        (unit["id"],),
    ):
        gates.append({"id": None, "unit_id": unit["id"], "member_id": row["id"], "candidate_id": None,
                      "subject_key": f"member:{row['id']}", "code": "membership_decision", "created_at": 0.0,
                      "resolved_at": None, "details_json": _json({"reason": "requirements_revision_changed"})})
    return gates


def _scope_gates(gates, unit, member_id=None, candidate_id=None, *, job_id=None, assignment_members=None):
    """Apply the same assignment scope and priority to fresh or snapshot facts."""
    def belongs(gate):
        if 'claim_member_ids' in gate:
            members = assignment_members if job_id is not None else [member_id] if member_id is not None else None
            return members is None or bool(set(gate['claim_member_ids']).intersection(members))
        if job_id is not None:
            return gate.get('job_id') is None or gate['job_id'] == job_id
        if member_id is not None:
            # Process waits belong to every issue in their immutable scope.
            return gate.get('job_id') is None or member_id in gate.get('job_member_ids', [])
        return True
    return sorted((gate for gate in gates
                   if belongs(gate) and (gate["candidate_id"] is None or gate["candidate_id"] == candidate_id)
                   and (gate["member_id"] is None or gate["member_id"] == member_id
                        or unit["state"] not in {"building"})),
                  key=lambda g: (GATE_PRIORITY[g["code"]], g["created_at"], g["id"] or g["subject_key"]))


def _charges(db, unit):
    return db.execute(
        "SELECT COALESCE(SUM(CASE b.kind WHEN 'charge' THEN 1 ELSE -1 END), 0) "
        "FROM budget_entries b JOIN attempts a ON a.id = b.attempt_id "
        "WHERE (a.unit_id = ? AND a.budget_generation = ?) OR a.id IN "
        "(SELECT attempt_id FROM unit_budget_lineage WHERE unit_id=? AND budget_generation=?)",
        (unit["id"], unit["budget_generation"], unit["id"], unit["budget_generation"]),
    ).fetchone()[0]


def issue_state(issue, unit=None, blocker=None, member=None):
    """One issue-state rule for unit cards and issue reads.

    Callers choose the current membership, or the latest historical delivery.
    A reopened observation supersedes an old delivery's completion for the issue,
    while the delivery itself stays merged forever.
    """
    if issue['github_state'] == 'closed':
        if issue['state_reason'] == 'completed':
            return 'done'
        if issue['state_reason'] in {'not_planned', 'duplicate'}:
            return 'dropped'
        return 'blocked'  # Unknown closure provenance needs an observation.
    if unit is None:
        return 'untracked'
    if unit['state'] == 'merged':
        completed_version = member.get('completion_issue_version') if member else None
        if completed_version is not None:
            return 'untracked' if issue.get('reopened_version', 0) > completed_version else 'done'
        # Imported history predates completion versions. Preserve an explicit
        # reopened observation, including after GitHub clears its state_reason.
        return 'untracked' if issue['state_reason'] == 'reopened' or issue.get('reopened_version', 0) else 'done'
    if unit['state'] in TERMINAL:
        return 'untracked'
    if blocker:
        return 'blocked'
    return 'in_unit'


def project_unit(db, unit_id):
    """Canonical stage/blocker projection; callers must hold a read snapshot."""
    unit = _unit(db, unit_id)
    members = []
    for row in db.execute(
        "SELECT m.*, i.github_state, i.state_reason, i.github_number FROM unit_issues m "
        "JOIN issues i ON i.id = m.issue_id WHERE m.unit_id = ? AND (m.left_at IS NULL OR ? IN ('merged','closed')) ORDER BY m.id",
        (unit_id, unit["state"]),
    ):
        member = dict(row)
        gates = applicable_gates(db, unit, member["id"], unit["current_candidate_id"])
        blocker = gates[0] if gates and unit["state"] not in TERMINAL else None
        # Old in-memory schema fixtures omit the later observation columns.
        if 'completion_issue_version' in member:
            member['reopened_version'] = db.execute('SELECT reopened_version FROM issues WHERE id=?', (member['issue_id'],)).fetchone()[0]
        state = issue_state(member, unit, blocker, member)
        member.update(current_blocker=blocker, projected_state=state)
        members.append(member)
    gates = applicable_gates(db, unit, candidate_id=unit["current_candidate_id"])
    blocker = gates[0] if gates else None
    if blocker is None and members:
        # An independent runnable/active part keeps the whole unit unblocked.
        runnable = any(m["current_blocker"] is None and m["state"] in {"pending", "building", "failed"} for m in members)
        blocked = [m["current_blocker"] for m in members if m["current_blocker"]]
        if blocked and (unit["state"] not in {"building"} or not runnable):
            blocker = min(blocked, key=lambda g: (GATE_PRIORITY[g["code"]], g["created_at"], g["id"] or g["subject_key"]))
    blocker = None if unit["state"] in TERMINAL else blocker
    from .delivery_inputs import approved_in
    unit.update(delivery_approved=approved_in(db,unit_id), stage_code=unit["state"], current_blocker=blocker,
                display_status=status.describe(blocker["code"] if blocker else unit["state"]),
                reason_code=blocker["code"] if blocker else None,
                members=members, charged_failures=_charges(db, unit))
    from .scheduling import current_in, execution_condition_in
    unit['execution_condition'] = execution_condition_in(db, unit,
        gates=[member['current_blocker'] for member in members if member['current_blocker']],
        action=current_in(db, unit))
    return unit


class Lifecycle:
    def __init__(self, store, *, clock=time.time):
        self.store = store
        self.clock = clock

    def _event(self, db, command, kind, payload, *, job_id=None, issue_id=None):
        db.execute(
            "INSERT INTO events(transaction_id,unit_id,issue_id,job_id,kind,at,actor_id,payload_json) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (command.request_id, command.unit_id, issue_id, job_id, kind, self.clock(), command.actor_id, _json(payload)),
        )

    def _replay(self, db, command, operation, payload):
        row = db.execute("SELECT * FROM decisions WHERE request_id = ?", (command.request_id,)).fetchone()
        if row is None:
            return None
        result = json.loads(row["result_json"])
        target = row["unit_id"] if row["unit_id"] is not None else result.get("unit_id")
        stored_payload = row["payload_json"]
        requested_payload = payload
        if operation == 'observe_issue':
            saved = json.loads(stored_payload)
            if {'title', 'body', 'requirements'} <= saved.keys():
                # Retained full-text decisions still replay exactly. Their
                # immutable text becomes the same command hashes; no row changes.
                saved['content_hash'] = manifest_hash([saved.pop('title'), saved.pop('body')])
                saved['requirements_hash'] = manifest_hash(saved.pop('requirements'))
            # Revision IDs are derived journal references, not command inputs.
            # A rejected observation may acquire a revision through a later
            # command; its original rejection must still replay unchanged.
            saved.pop('revision_id', None)
            requested_payload = {key:value for key,value in payload.items() if key != 'revision_id'}
            stored_payload = _json(saved)
        if (target, row["actor_id"], row["expected_version"], row["command"], stored_payload) != (
            command.unit_id, command.actor_id, command.expected_version, operation, _json(requested_payload)
        ):
            raise IdempotencyConflict("request identity already belongs to a different command")
        if row["status"] == "rejected":
            errors = {"TransitionError": TransitionError, "VersionConflict": VersionConflict,
                      "IntegrityError": sqlite3.IntegrityError, "InvalidDelegationPlan": InvalidDelegationPlan}
            raise errors.get(result["error_type"], TransitionError)(result["message"])
        if row["status"] != "applied":
            raise TransitionError("request is not an applied command")
        return result

    @contextmanager
    def _command_transaction(self, command, operation, payload):
        """Persist semantic rejection after rolling back every attempted state edit.

        Busy/I/O/commit failures remain retryable storage failures and are not
        recorded as decisions. Replays check the full actor/version/payload.
        """
        if (not all(isinstance(value, str) and value for value in
                    (command.request_id, command.actor_id, command.unit_id))
                or type(command.expected_version) is not int or command.expected_version < 1):
            raise TransitionError("request, actor, unit and positive expected version are required")
        rejection = None
        with self.store.write() as db:
            if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                raise TransitionError("staging imports cannot accept lifecycle commands")
            prior = db.execute("SELECT 1 FROM decisions WHERE request_id = ?", (command.request_id,)).fetchone()
            if prior:
                yield db  # _replay returns or raises the original result; no new journal row.
            else:
                db.execute("SAVEPOINT lifecycle_command")
                try:
                    yield db
                except (TransitionError, sqlite3.IntegrityError) as error:
                    db.execute("ROLLBACK TO lifecycle_command")
                    db.execute("RELEASE lifecycle_command")
                    target = db.execute("SELECT id FROM units WHERE id = ?", (command.unit_id,)).fetchone()
                    unit_id = target[0] if target else None
                    result = {"unit_id": command.unit_id, "error_type": type(error).__name__, "message": str(error)}
                    db.execute(
                        "INSERT INTO decisions(id,request_id,unit_id,actor_id,expected_version,command,payload_json,status,result_json,created_at) "
                        "VALUES(?,?,?,?,?,?,?,'rejected',?,?)",
                        (_id(command.request_id, "decision"), command.request_id, unit_id, command.actor_id,
                         command.expected_version, operation, _json(payload), _json(result), self.clock()),
                    )
                    db.execute(
                        "INSERT INTO events(transaction_id,unit_id,kind,at,actor_id,payload_json) "
                        "VALUES(?,?,'decision_rejected',?,?,?)",
                        (command.request_id, unit_id, self.clock(), command.actor_id,
                         _json({"command": operation, **result})),
                    )
                    rejection = error
                else:
                    db.execute("RELEASE lifecycle_command")
        if rejection is not None:
            raise rejection

    @staticmethod
    def _lifecycle_summary(projected):
        blocker = projected["current_blocker"]
        return {"stage_code": projected["stage_code"], "current_candidate_id": projected["current_candidate_id"],
                "current_blocker": {"id": blocker["id"], "code": blocker["code"], "subject_key": blocker["subject_key"]} if blocker else None}

    def _save(self, db, command, operation, payload, values, *, initial=False, before=None, advance_version=True):
        if not initial and advance_version:
            db.execute("UPDATE units SET version = version + 1, updated_at = ? WHERE id = ?", (self.clock(), command.unit_id))
        projected = project_unit(db, command.unit_id)
        primary = projected["current_blocker"]
        db.execute("UPDATE units SET primary_gate_id = ? WHERE id = ?", (primary["id"] if primary else None, command.unit_id))
        result = {"unit_id": command.unit_id, "version": projected["version"], **values}
        db.execute(
            "INSERT INTO decisions(id,request_id,unit_id,actor_id,expected_version,command,payload_json,status,result_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,'applied',?,?)",
            (_id(command.request_id, "decision"), command.request_id, command.unit_id, command.actor_id,
             command.expected_version, operation, _json(payload), _json(result), self.clock()),
        )
        from ..mutation_audit import command_effect_in
        command_effect_in(db, command, clock=self.clock)
        after = self._lifecycle_summary(projected)
        if before != after:
            self._event(db, command, "lifecycle_changed", {"before": before, "after": after})
        self._event(db, command, "decision_applied", {"command": operation, "version": result["version"], "before": before, "after": after})
        return result

    def _apply(self, command, operation, payload, change, *, version_changed=None, journal_changed=None, prepare_payload=None):
        if not command.request_id or not command.actor_id or type(command.expected_version) is not int or command.expected_version < 1:
            raise TransitionError("request, actor and positive expected version are required")
        with self._command_transaction(command, operation, payload) as db:
            if prepare_payload is not None:
                prepare_payload(db)
            previous = self._replay(db, command, operation, payload)
            if previous is not None:
                return previous
            unit = _unit(db, command.unit_id)
            if unit["version"] != command.expected_version:
                raise VersionConflict("unit changed; read its current version before retrying")
            before = self._lifecycle_summary(project_unit(db, command.unit_id))
            values = change(db, unit)
            if journal_changed is not None and not journal_changed():
                return {'unit_id': command.unit_id, 'version': unit['version'], **values}
            return self._save(db, command, operation, payload, values, before=before,
                              advance_version=version_changed() if version_changed is not None else True)

    def admit_issue(self, *, request_id, actor_id, unit_id, issue_id, repository_id, repository_name,
                    issue_number, title, body, requirements, observed_at, base_branch="main"):
        """Admit one observed open issue. Repository readiness is checked by the caller."""
        command = Command(request_id, actor_id, unit_id, 1)
        payload = dict(issue_id=issue_id, repository_id=repository_id, repository_name=repository_name,
                       issue_number=issue_number, title=title, body=body, requirements=requirements,
                       observed_at=observed_at, base_branch=base_branch)
        if not all((request_id, actor_id, unit_id, issue_id, repository_name, base_branch)):
            raise TransitionError("admission identities must be nonempty")
        with self._command_transaction(command, "admit_issue", payload) as db:
            previous = self._replay(db, command, "admit_issue", payload)
            if previous is not None:
                return previous
            repository = db.execute("SELECT full_name,enabled FROM repositories WHERE id = ?", (repository_id,)).fetchone()
            if repository is None:
                db.execute("INSERT INTO repositories(id,full_name) VALUES(?,?)", (repository_id, repository_name))
            elif repository[0] != repository_name or not repository[1]:
                raise TransitionError("repository identity does not match or is disabled")
            revision_id, member_id = _id(request_id, "revision"), _id(request_id, "member")
            db.execute(
                "INSERT INTO issues(id,repository_id,github_number,github_state,current_revision_id,observed_at) "
                "VALUES(?,?,?,'open',?,?)", (issue_id, repository_id, issue_number, revision_id, observed_at),
            )
            db.execute(
                "INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (revision_id, issue_id, title, body, manifest_hash([title, body]),
                                           manifest_hash(requirements), _json(requirements), observed_at),
            )
            record_revision_display(db,revision_id,body,requirements)
            db.execute(
                "INSERT INTO units(id,repository_id,base_branch,state,created_at,updated_at) VALUES(?,?,?,'building',?,?)",
                (unit_id, repository_id, base_branch, self.clock(), self.clock()),
            )
            db.execute(
                "INSERT INTO unit_issues(id,unit_id,issue_id,repository_id,base_branch,requirements_revision_id,state,joined_at) "
                "VALUES(?,?,?,?,?,?,'pending',?)", (member_id, unit_id, issue_id, repository_id, base_branch, revision_id, self.clock()),
            )
            self._event(db, command, "issue_admitted", {"member_id": member_id, "revision_id": revision_id}, issue_id=issue_id)
            return self._save(db, command, "admit_issue", payload, {"member_id": member_id, "revision_id": revision_id}, initial=True)

    def _open_gate(self, db, command, code, details, member_id=None, candidate_id=None, *, job_id=None):
        if code in {'cleanup_pending','result_pending','recovery_pending'} and details.get('job_id'):
            job_id=details['job_id']; member_id=None; candidate_id=None
        code = GateCode(code).value
        if sum(value is not None for value in (member_id,candidate_id,job_id))>1:
            raise TransitionError("a gate has one subject")
        rule = codes.GATES[code]
        scope = "job" if job_id else "member" if member_id else "candidate" if candidate_id else "unit"
        if scope not in rule.scopes or (rule.candidate and candidate_id is None):
            raise TransitionError("gate scope does not match its registered rule")
        subject = f"job:{job_id}" if job_id else f"member:{member_id}" if member_id else f"candidate:{candidate_id}" if candidate_id else "unit"
        existing = db.execute(
            "SELECT id FROM unit_gates WHERE unit_id = ? AND code = ? AND subject_key = ? AND resolved_at IS NULL",
            (command.unit_id, code, subject),
        ).fetchone()
        if existing:
            raise TransitionError("this subject already has an unresolved gate with that code")
        gate_id = _id(command.request_id, f"gate:{command.unit_id}:{code}:{subject}")
        db.execute(
            "INSERT INTO unit_gates(id,unit_id,candidate_id,member_id,subject_key,code,details_json,created_at,job_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (gate_id, command.unit_id, candidate_id, member_id, subject, code, _json(details), self.clock(), job_id),
        )
        self._event(db, command, "gate_opened", {"gate_id": gate_id, "code": code, "subject": subject})
        return gate_id

    def open_gate(self, command, code, *, details, member_id=None, candidate_id=None):
        code = GateCode(code).value
        if code in {"operator_pause", "budget_exhausted", "cleanup_pending", "recovery_pending", "result_pending"}:
            raise TransitionError("this gate is managed by its dedicated lifecycle command")
        payload = dict(code=code, details=details, member_id=member_id, candidate_id=candidate_id)
        def change(db, unit):
            self._live(unit)
            if candidate_id and candidate_id != unit["current_candidate_id"]:
                raise TransitionError("gate does not refer to the current candidate")
            if code == "external_change":
                self._fence(db, command)
            return {"gate_id": self._open_gate(db, command, code, details, member_id, candidate_id)}
        return self._apply(command, "open_gate", payload, change)

    def resolve_gate(self, command, gate_id, *, release, evidence):
        if not evidence:
            raise TransitionError("gate resolution requires evidence")
        def change(db, unit):
            gate = db.execute("SELECT * FROM unit_gates WHERE id = ? AND unit_id = ? AND resolved_at IS NULL", (gate_id, unit["id"])).fetchone()
            if gate is None:
                raise TransitionError("gate is not unresolved on this unit")
            rule = codes.GATES[gate["code"]]
            if release != rule.release:
                raise TransitionError("release rule does not match this gate")
            if gate["code"] in {"forge_problem", "operator_pause", "budget_exhausted", "verification_human", "verification_ci"} or rule.release == "transition":
                raise TransitionError("this gate requires its dedicated lifecycle command")
            self._release_evidence(gate, release, evidence)
            if gate["candidate_id"] and gate["candidate_id"] != unit["current_candidate_id"]:
                raise TransitionError("evidence is for an obsolete candidate")
            self._resolve(db, command, gate_id, evidence)
            return {"gate_id": gate_id}
        return self._apply(command, "resolve_gate", {"gate_id": gate_id, "release": release, "evidence": evidence}, change)

    def _release_evidence(self, gate, release, evidence):
        if not isinstance(evidence, dict) or not evidence:
            raise TransitionError("resolution requires structured evidence")
        if release in {"observation", "recheck", "evidence"}:
            if evidence.get("verified") is not True or not isinstance(evidence.get("observed_at"), (int, float)) or evidence["observed_at"] < gate["created_at"]:
                raise TransitionError("resolution requires a fresh verified observation")
        if release == "evidence" and evidence.get("candidate_id") != gate["candidate_id"]:
            raise TransitionError("evidence must identify the gated candidate")
        if release == "time":
            retry_at = json.loads(gate["details_json"]).get("retry_at")
            if not isinstance(retry_at, (int, float)) or self.clock() < retry_at:
                raise TransitionError("retry time has not elapsed")

    def _resolve(self, db, command, gate_id, evidence, *, resolution=None):
        gate = db.execute("SELECT code FROM unit_gates WHERE id = ?", (gate_id,)).fetchone()
        resolution = resolution or codes.GATES[gate["code"]].release
        db.execute("UPDATE unit_gates SET resolved_at = ?, resolution = ? WHERE id = ?", (self.clock(), resolution, gate_id))
        self._event(db, command, "gate_resolved", {"gate_id": gate_id, "resolution": resolution, "evidence": evidence})

    @staticmethod
    def _live(unit):
        if unit["state"] in TERMINAL:
            raise TransitionError("unit is terminal")

    def pause(self, command):
        def change(db, unit):
            self._live(unit)
            gate_id = self._open_gate(db, command, "operator_pause", {})
            self._fence(db, command)
            return {"gate_id": gate_id}
        return self._apply(command, "pause", {}, change)

    def resume(self, command):
        def change(db, unit):
            self._live(unit)
            gate = db.execute("SELECT id FROM unit_gates WHERE unit_id = ? AND code = 'operator_pause' AND resolved_at IS NULL", (unit["id"],)).fetchone()
            if gate is None:
                raise TransitionError("unit has no operator pause")
            self._resolve(db, command, gate[0], {"operator": command.actor_id})
            return {}
        return self._apply(command, "resume", {}, change)

    def _fence(self, db, command):
        db.execute("UPDATE units SET fencing_generation = fencing_generation + 1 WHERE id = ?", (command.unit_id,))
        jobs = db.execute("SELECT id, member_id FROM jobs WHERE unit_id = ? AND cleanup = 0 AND fenced = 0", (command.unit_id,)).fetchall()
        db.execute("UPDATE jobs SET fenced = 1 WHERE unit_id = ? AND cleanup = 0", (command.unit_id,))
        for job in jobs:
            self._ensure_cleanup_gate(db, command, job["id"], job["member_id"])
            self._event(db, command, "attempt_fenced", {}, job_id=job[0])

    def fence_member_in(self, db, command, member_id, *, reason, include_descendants=True):
        """Fence affected physical assignments without revoking independent parts."""
        unit = _unit(db,command.unit_id)
        from .job_scope import affected_jobs_in
        scope = affected_jobs_in(db, unit, {member_id}, include_descendants=include_descendants)
        jobs = scope['jobs']
        for job in jobs:
            db.execute('UPDATE jobs SET fenced=1 WHERE id=?',(job['id'],))
            db.execute('UPDATE job_execution_state SET result_revoked=1 WHERE job_id=?',(job['id'],))
            if not job['cleanup']:
                self._ensure_cleanup_gate(db,command,job['id'],job['member_id'])
            self._event(db,command,'attempt_fenced',{'reason':reason,'member_id':member_id},job_id=job['id'])
        return {'member_ids':scope['member_ids'],'job_ids':[job['id'] for job in jobs]}

    def _ensure_cleanup_gate(self, db, command, job_id, member_id):
        subject = f"job:{job_id}"
        exists = db.execute("SELECT 1 FROM unit_gates WHERE unit_id = ? AND code = 'cleanup_pending' AND subject_key = ? AND resolved_at IS NULL", (command.unit_id, subject)).fetchone()
        if not exists:
            self._open_gate(db, command, "cleanup_pending", {"job_id": job_id}, member_id)

    def reserve_job(self, command, role, *, slot, execution_fence, input_manifest, member_id=None, candidate_id=None, launch=None):
        role = Role(role).value
        if role not in {"build", "review", "audit"}:
            raise TransitionError("this role requires the later execution adapter")
        payload = dict(role=role, slot=slot, execution_fence=execution_fence, input_manifest=input_manifest,
                       member_id=member_id, candidate_id=candidate_id)
        if launch is not None:
            from .executions import Launch
            if not isinstance(launch, Launch):
                raise TransitionError('launch must be a Launch specification')
            payload['launch'] = launch.payload()
        def change(db, unit):
            self._live(unit)
            expected = {"build": {"building"}, "review": {"reviewing"}, "audit": {"auditing"}}[role]
            if unit["state"] not in expected:
                raise TransitionError("role is not eligible in this phase")
            if not slot or not execution_fence or not isinstance(input_manifest, dict) or not input_manifest:
                raise TransitionError("reservation requires slot, execution fence and input manifest")
            if role == "build":
                from .delivery_inputs import approved_in
                if approved_in(db,unit["id"]) or len(self._members(db, unit["id"])) > 1:
                    raise TransitionError("grouped builds require their owning builder and tracked delegation")
                member = db.execute("SELECT * FROM unit_issues WHERE unit_id = ? AND id = ? AND left_at IS NULL", (unit["id"], member_id)).fetchone()
                if member is None or member["state"] not in {"pending", "failed", "building"}:
                    raise TransitionError("build requires a current pending member")
                if not all(isinstance(input_manifest.get(k), str) and input_manifest[k] for k in ("requirements_revision_id", "base_oid", "policy_hash")):
                    raise TransitionError("build manifest requires requirements, base and policy identities")
                if input_manifest.get("requirements_revision_id") != member["requirements_revision_id"]:
                    raise TransitionError("input requirements revision is stale")
                if candidate_id is not None:
                    raise TransitionError("build assignment cannot consume a combined candidate")
            elif member_id is not None or not candidate_id or candidate_id != unit["current_candidate_id"]:
                raise TransitionError("review and audit require the current unit candidate")
            if role != "build":
                identity = db.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
                if input_manifest.get("candidate_id") != candidate_id or any(input_manifest.get(key) != identity[key] for key in ("head_oid", "base_oid", "policy_hash", "requirements_hash", "input_manifest_hash")):
                    raise TransitionError("manifest must pin the exact current candidate")
            if applicable_gates(db, unit, member_id, candidate_id):
                raise TransitionError("assignment is blocked by an applicable gate")
            if _charges(db, unit) >= 3:
                raise TransitionError("unit failure budget is exhausted")
            busy = db.execute(
                "SELECT 1 FROM jobs WHERE unit_id = ? AND cleanup = 0 AND (member_id IS ? OR member_id IS NULL OR ? IS NULL)",
                (unit["id"], member_id, member_id),
            ).fetchone()
            if busy:
                raise TransitionError("prior assignment still requires verified cleanup")
            attempt_id, job_id = _id(command.request_id, "attempt"), _id(command.request_id, "job")
            db.execute(
                "INSERT INTO attempts(id,unit_id,member_id,membership_generation,assignment_generation,input_manifest_json,budget_generation,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (attempt_id, unit["id"], member_id, unit["membership_generation"], unit["fencing_generation"], _json(input_manifest), unit["budget_generation"], self.clock()),
            )
            db.execute(
                "INSERT INTO jobs(id,unit_id,attempt_id,member_id,candidate_id,role,phase,created_at) VALUES(?,?,?,?,?,?,'reserved',?)",
                (job_id, unit["id"], attempt_id, member_id, candidate_id, role, self.clock()),
            )
            db.execute("INSERT INTO job_leases(slot,job_id,execution_fence,acquired_at) VALUES(?,?,?,?)", (slot, job_id, execution_fence, self.clock()))
            if launch is not None:
                from .executions import insert_launch
                insert_launch(db, job_id, slot, execution_fence, payload['launch'], self.clock())
            if role == "build":
                db.execute("UPDATE units SET state = 'building' WHERE id = ?", (unit["id"],))
                db.execute("UPDATE unit_issues SET state = 'building' WHERE id = ?", (member_id,))
            self._event(db, command, "attempt_reserved", payload, job_id=job_id)
            return {"job_id": job_id, "attempt_id": attempt_id}
        return self._apply(command, "reserve_job", payload, change)

    def _job(self, db, unit, job_id, execution_fence, input_manifest):
        job = db.execute("SELECT j.*, a.assignment_generation, a.membership_generation, a.budget_generation, a.input_manifest_json FROM jobs j JOIN attempts a ON a.id = j.attempt_id WHERE j.id = ? AND j.unit_id = ?", (job_id, unit["id"])).fetchone()
        lease = db.execute("SELECT execution_fence FROM job_leases WHERE job_id = ?", (job_id,)).fetchone()
        captured = db.execute('SELECT l.execution_fence,e.capture_authorized,e.result_revoked FROM job_launches l '
                              'JOIN job_execution_state e ON e.job_id=l.job_id '
                              'WHERE l.job_id=? AND e.captured_at IS NOT NULL', (job_id,)).fetchone()
        owner = lease[0] if lease else captured['execution_fence'] if captured and job and job['cleanup'] else None
        if job is None or owner != execution_fence:
            raise TransitionError("result does not own the execution lease")
        if job["input_manifest_json"] != _json(input_manifest):
            raise TransitionError("result input manifest differs from its assignment")
        if job["outcome"] is not None:
            raise TransitionError("assignment already has a result")
        current = ((not job["fenced"] or bool(captured and captured['capture_authorized'] and not captured['result_revoked']))
                   and unit['state'] not in TERMINAL
                   and job["assignment_generation"] == unit["fencing_generation"]
                   and job["membership_generation"] == unit["membership_generation"]
                   and job["budget_generation"] == unit["budget_generation"]
                   and (job["candidate_id"] is None or job["candidate_id"] == unit["current_candidate_id"]))
        pins=db.execute('SELECT p.requirements_revision_id,m.requirements_revision_id AS current_revision,m.left_at FROM job_members p JOIN unit_issues m ON m.id=p.member_id WHERE p.job_id=?',(job_id,)).fetchall()
        if job['role']!='recovery':
            from .review_pulls import review_only_in, source_in
            if review_only_in(db, unit['id']):
                source = source_in(db, job['candidate_id'])
                identity = db.execute('SELECT * FROM candidates WHERE id=?', (job['candidate_id'],)).fetchone()
                current = bool(current and not pins and job['role'] in {'review', 'audit'} and source and identity
                    and source['head']['sha'] == identity['head_oid'] and source['base']['sha'] == identity['base_oid']
                    and input_manifest.get('candidate_id') == identity['id']
                    and all(input_manifest.get(key) == identity[key] for key in
                            ('head_oid', 'base_oid', 'policy_hash', 'requirements_hash', 'input_manifest_hash')))
            else:
                current=bool(current and pins and all(pin['left_at'] is None and pin['requirements_revision_id']==pin['current_revision'] for pin in pins))
        delegation = db.execute('SELECT a.assignment_generation,a.membership_generation FROM job_delegations d '
            'JOIN delegation_plans p ON p.id=d.plan_id JOIN jobs j ON j.id=p.parent_job_id '
            'JOIN attempts a ON a.id=j.attempt_id WHERE d.job_id=?', (job_id,)).fetchone()
        if delegation:
            current = bool(current and delegation['assignment_generation'] == unit['fencing_generation']
                           and delegation['membership_generation'] == unit['membership_generation'])
        return dict(job), current

    def _result(self, db, command, job, evidence, outcome):
        content = _json(evidence).encode()
        db.execute("INSERT INTO job_artifacts(job_id,kind,sequence,media_type,digest,content,size) VALUES(?,'result',0,'application/json',?,?,?)", (job["id"], hashlib.sha256(content).hexdigest(), content, len(content)))
        db.execute("UPDATE jobs SET phase = CASE WHEN cleanup=1 THEN 'cleaned' ELSE 'finished' END, outcome = ? WHERE id = ?", (outcome, job["id"]))
        # Cards retain the validated plain-language summary, independently of
        # lazy transcript artifacts. Stale or recovery results cannot replace
        # the current delivery's explanation.
        if outcome in {'success','failure','rejected','deferred'} and job['role'] in {'build','review','audit'}:
            from ..factory.contracts import result as worker_result, summary as worker_summary
            try:
                report=worker_result(evidence.get('report'),{'id':job['id'],'role':job['role'],'candidate':{'key':job['candidate_id']}})
                display=worker_summary(report,job['role'],self.clock())
            except (ValueError,TypeError,KeyError):
                display=None
            if display:
                db.execute('INSERT INTO unit_context(unit_id,summary_json) VALUES(?,?) ON CONFLICT(unit_id) DO UPDATE SET summary_json=excluded.summary_json',
                           (command.unit_id,_json(display)))
        if job['role'] == 'recovery' and outcome in {'deferred', 'memory_retry'}:
            db.execute("UPDATE attempt_failures SET investigation='pending' WHERE attempt_id="
                       '(SELECT failed_attempt_id FROM recovery_assignments WHERE job_id=?)', (job['id'],))
        self._event(db, command, "attempt_finished", {"outcome": outcome}, job_id=job["id"])
        if not job['cleanup'] and _unit(db, command.unit_id)['state'] not in TERMINAL:
            self._ensure_cleanup_gate(db, command, job["id"], job["member_id"])
        for gate in db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND code='result_pending' "
                               "AND resolved_at IS NULL AND json_extract(details_json,'$.job_id')=?",
                               (command.unit_id, job['id'])).fetchall():
            self._resolve(db, command, gate[0], {'result_recorded': job['id']})

    def finish_job(self, command, job_id, *, execution_fence, input_manifest, evidence, candidate=None, gates=()):
        """Accept independently checked build publication or technical pass evidence."""
        if not isinstance(evidence, dict) or not evidence:
            raise TransitionError("result requires structured verified evidence")
        if candidate is not None and not isinstance(candidate, Candidate):
            raise TransitionError("candidate must be a Candidate identity")
        if not isinstance(gates, (list, tuple)) or any(
                not isinstance(gate, dict) or set(gate) != {'code', 'details'}
                or gate['code'] not in {'verification_human', 'verification_ci', 'approval_required'}
                or not isinstance(gate['details'], dict) or not gate['details'] for gate in gates):
            raise TransitionError('completion gates require candidate-bound verification or approval details')
        if len({gate['code'] for gate in gates}) != len(gates):
            raise TransitionError('completion gates must be unique')
        payload = dict(job_id=job_id, execution_fence=execution_fence, input_manifest=input_manifest,
                       evidence=evidence, candidate=asdict(candidate) if candidate else None)
        # Preserve replay identities for callers predating completion gates.
        if gates:
            payload['gates'] = list(gates)
        def change(db, unit):
            job, current = self._job(db, unit, job_id, execution_fence, input_manifest)
            if db.execute('SELECT 1 FROM job_tasks WHERE job_id=?',(job_id,)).fetchone():
                raise TransitionError('task executions return outputs; only owning integration may publish')
            if not current:
                self._result(db, command, job, evidence, "stale_success")
                return {"accepted": False}
            if any(g['code'] not in {'result_pending', 'cleanup_pending'}
                   or json.loads(g['details_json']).get('job_id') != job_id
                   for g in applicable_gates(db, unit, job["member_id"], job["candidate_id"])):
                raise TransitionError("a gate prevents accepting this result")
            expected = {"build": {"building", "publishing"}, "review": {"reviewing"}, "audit": {"auditing"}}
            if unit["state"] not in expected.get(job["role"], set()):
                raise TransitionError("result phase is no longer current")
            if job["role"] == "build":
                count = db.execute("SELECT COUNT(*) FROM unit_issues WHERE unit_id = ? AND left_at IS NULL", (unit["id"],)).fetchone()[0]
                if count != 1:
                    raise TransitionError("batch part acceptance belongs to the later batch implementation")
                revision = db.execute("SELECT r.requirements_hash FROM unit_issues m JOIN issue_revisions r ON r.id = m.requirements_revision_id WHERE m.id = ?", (job["member_id"],)).fetchone()
                if (candidate is None or candidate.requirements_hash != revision[0]
                        or candidate.input_manifest_hash != manifest_hash(input_manifest)
                        or candidate.base_oid != input_manifest["base_oid"]
                        or candidate.policy_hash != input_manifest["policy_hash"]
                        or evidence.get("head_oid") != candidate.head_oid
                        or evidence.get("base_oid") != candidate.base_oid
                        or evidence.get("pull_number") != candidate.pull_number):
                    raise TransitionError("publication evidence does not match the assigned candidate")
                from .publications import require_receipt_in
                require_receipt_in(db,job,candidate,evidence)
                candidate_id = _id(command.request_id, "candidate")
                db.execute("INSERT INTO candidates(id,unit_id,pull_number,branch,head_oid,base_oid,policy_hash,requirements_hash,membership_generation,input_manifest_hash,accepted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (candidate_id, unit["id"], candidate.pull_number, candidate.branch, candidate.head_oid, candidate.base_oid, candidate.policy_hash, candidate.requirements_hash, unit["membership_generation"], candidate.input_manifest_hash, self.clock()))
                db.execute("UPDATE units SET current_candidate_id = ? WHERE id = ?", (candidate_id, unit["id"]))
                db.execute("UPDATE unit_issues SET state = 'built' WHERE id = ?", (job["member_id"],))
                self._event(db, command, "candidate_selected", {"candidate_id": candidate_id}, job_id=job_id)
                target = "reviewing"
            else:
                if candidate is not None or evidence.get("candidate_id") != job["candidate_id"] or evidence.get("verdict") != "pass":
                    raise TransitionError("passing verdict must name the exact current candidate")
                target = {"review": "auditing", "audit": "merging"}[job["role"]]
                candidate_id = job["candidate_id"]
            db.execute("INSERT INTO candidate_evidence(id,unit_id,candidate_id,attempt_id,kind,verdict,actor_id,observed_at,details_json) VALUES(?,?,?,?,?,'pass',?,?,?)", (_id(command.request_id, "evidence"), unit["id"], candidate_id, job["attempt_id"], "publication" if job["role"] == "build" else job["role"], command.actor_id, self.clock(), _json(evidence)))
            self._result(db, command, job, evidence, "success")
            from .runtime_requirements import learn_memory_in
            learn_memory_in(db, unit, job, self.clock())
            for gate in gates:
                self._open_gate(db, command, gate['code'], gate['details'], candidate_id=candidate_id)
            db.execute("UPDATE units SET state = ? WHERE id = ?", (target, unit["id"]))
            self._event(db, command, "phase_changed", {"before": unit["state"], "after": target}, job_id=job_id)
            return {"accepted": True, "phase": target}
        return self._apply(command, "finish_job", payload, change)

    def defer_job(self, command, job_id, *, execution_fence, input_manifest, evidence, code, details):
        """Save a verified wait and its gate together, without charging a failure.

        A wait is not a successful review/audit. Releasing the gate retries that
        phase; any existing candidate and failure history stay intact.
        """
        if code not in {'forge_problem', 'environment', 'provider_unavailable', 'decision_required', 'dependency', 'verification_human', 'verification_ci'}:
            raise TransitionError('unsupported worker deferral gate')
        if not isinstance(evidence, dict) or not evidence or not isinstance(details, dict) or not details:
            raise TransitionError('deferral requires structured evidence and gate details')
        if code == 'forge_problem':
            from ..factory.startup_problems import validated_receipt
            receipt = {key:details.get(key) for key in ('version','phase','exception','file','line','signature')}
            if details.get('owner') != 'forge' or validated_receipt({**receipt,'attempt':job_id}, job_id) is None:
                raise TransitionError('Forge problem requires trusted startup evidence')
        if code == 'provider_unavailable':
            retry_at = details.get('retry_at')
            if type(retry_at) not in (int, float) or not math.isfinite(retry_at) or retry_at <= self.clock():
                raise TransitionError('provider deferral requires a future finite retry time')
        payload = dict(job_id=job_id, execution_fence=execution_fence, input_manifest=input_manifest,
                       evidence=evidence, code=code, details=details)
        def change(db, unit):
            job, current = self._job(db, unit, job_id, execution_fence, input_manifest)
            self._result(db, command, job, evidence, 'deferred' if current else 'stale_deferred')
            if not current:
                return {'accepted': False, 'charged': False}
            if code == 'forge_problem':
                previous = db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND code='forge_problem' AND resolved_at IS NULL "
                    "AND json_extract(details_json,'$.reason')='harness_startup'", (unit['id'],)).fetchone()
                gate_id = previous['id'] if previous else self._open_gate(db, command, code, {**details, 'job_id': job_id})
                # Every result keeps its own immutable evidence. Sharing a unit
                # blocker must not discard a simultaneous task's capture or grow
                # an unbounded JSON list inside that blocker.
                pins = list(db.execute('SELECT member_id FROM job_members WHERE job_id=?', (job_id,)))
                if not pins:
                    raise TransitionError('Forge startup failure has no immutable assigned scope')
                db.executemany('INSERT INTO forge_problem_jobs(job_id,gate_id,member_id,details_json) VALUES(?,?,?,?)',
                    [(job_id,gate_id,pin[0],_json(details)) for pin in pins])
                from .execution_problems import draft_in
                draft_in(db, job_id, details, self.clock())
                return {'accepted': True, 'charged': False, 'gate_id': gate_id, 'gate_ids': [gate_id]}
            member_id = job['member_id'] if code not in {'forge_problem', 'provider_unavailable', 'verification_human', 'verification_ci'} else None
            candidate_id = job['candidate_id'] if code in {'verification_human', 'verification_ci'} else None
            if code in {'verification_human', 'verification_ci'} and candidate_id is None:
                raise TransitionError('verification deferral requires an existing exact candidate')
            task = db.execute('SELECT 1 FROM job_tasks WHERE job_id=?',(job_id,)).fetchone()
            owner = db.execute('SELECT 1 FROM builder_assignments WHERE job_id=?',(job_id,)).fetchone()
            scoped = bool(task and code in {'environment','decision_required','dependency'}
                          or owner and code in {'environment','dependency'})
            subjects = ([row[0] for row in db.execute('SELECT member_id FROM job_members WHERE job_id=? ORDER BY member_id',(job_id,))]
                        if scoped else [member_id])
            gate_ids = []
            for subject in subjects:
                value = {**details, 'job_id': job_id}
                if code == 'dependency' and subject is not None:
                    pin = db.execute('SELECT r.requirements_json FROM job_members p JOIN issue_revisions r ON r.id=p.requirements_revision_id WHERE p.job_id=? AND p.member_id=?',(job_id,subject)).fetchone()
                    if pin is None:
                        raise TransitionError('dependency wait has no exact assigned member requirements')
                    requirements = json.loads(pin[0])
                    if 'requirements' in details and details['requirements'] != requirements:
                        raise TransitionError('dependency wait quotes different assigned requirements')
                    value['requirements'] = requirements
                previous = (db.execute("SELECT * FROM unit_gates WHERE unit_id=? AND member_id=? AND code=? AND resolved_at IS NULL",
                                       (unit['id'],subject,code)).fetchone() if scoped else None)
                if previous is not None:
                    # One canonical member/code gate can represent several exact
                    # worker waits. Keep its original reason and immutable gate
                    # lineage; every additional reason is in the named decision.
                    value = json.loads(previous['details_json'])
                    waits = value.get('worker_waits', [])
                    value = {**value, 'worker_waits': [*waits, {'job_id': job_id, 'request_id': command.request_id}],
                             'previous_gate_id': previous['id'], 'source': 'worker_waits'}
                    if code == 'dependency':
                        if value.get('requirements') is not None and value['requirements'] != requirements:
                            raise TransitionError('combined dependency waits must pin the current member requirements')
                        value.update(requirements=requirements,
                            issues=sorted(set(value.get('issues',[])) | set(details['issues'])),
                            blocked=sorted({number for number in value.get('blocked',[]) if type(number) is int} | set(details['blocked'])))
                    elif code == 'environment':
                        value['required_services']=sorted(set(value.get('required_services',[])) | set(details.get('required_services',[])))
                    self._resolve(db, command, previous['id'], {'additional_worker_wait': job_id}, resolution='superseded')
                gate_ids.append(self._open_gate(db, command, code, value, subject, candidate_id))
            gate_id = gate_ids[0] if len(gate_ids)==1 else None
            if code == 'provider_unavailable':
                launch = db.execute('SELECT provider,provider_id,runner_config_json FROM job_launches WHERE job_id=?', (job_id,)).fetchone()
                if launch is not None:
                    config = json.loads(launch['runner_config_json'])
                    account = config.get('provider_auth_file_env') or launch['provider_id'] or launch['provider']
                    previous = db.execute('SELECT retry_at FROM provider_cooldowns WHERE account_ref=?', (account,)).fetchone()
                    if previous is None or previous[0] <= details['retry_at']:
                        db.execute('INSERT INTO provider_cooldowns(account_ref,provider,reason_code,retry_at,observed_at,source_job_id) '
                            'VALUES(?,?,?,?,?,?) ON CONFLICT(account_ref) DO UPDATE SET provider=excluded.provider,reason_code=excluded.reason_code,'
                            'retry_at=excluded.retry_at,observed_at=excluded.observed_at,source_job_id=excluded.source_job_id',
                            (account, launch['provider'], str(details.get('reason') or 'provider_unavailable'), details['retry_at'], self.clock(), job_id))
            return {'accepted': True, 'charged': False, 'gate_id': gate_id, 'gate_ids': gate_ids}
        return self._apply(command, 'defer_job', payload, change)

    def record_failure(self, command, job_id, *, execution_fence, input_manifest, evidence, charge=True):
        if not isinstance(evidence, dict) or not evidence or type(charge) is not bool:
            raise TransitionError("failure requires structured evidence and a boolean charge classification")
        payload = dict(job_id=job_id, execution_fence=execution_fence, input_manifest=input_manifest, evidence=evidence, charge=charge)
        def change(db, unit):
            job, current = self._job(db, unit, job_id, execution_fence, input_manifest)
            if job['role'] == 'recovery':
                raise TransitionError('investigation failure requires finish_recovery, never another failure')
            self._result(db, command, job, evidence, "failure" if current else "stale_failure")
            if current:
                from .recovery import record_failure_facts
                record_failure_facts(db, unit, job, evidence, self.clock(), store=self.store)
            charged = bool(current and charge)
            if charged:
                charge_id = _id(command.request_id, "charge")
                db.execute("INSERT INTO budget_entries(id,attempt_id,kind,reason_code,created_at) VALUES(?,?,'charge','execution_failure',?)", (charge_id, job["attempt_id"], self.clock()))
                self._event(db, command, "budget_charged", {"charge_id": charge_id}, job_id=job_id)
            if current:
                if job["member_id"]:
                    db.execute("UPDATE unit_issues SET state = 'failed' WHERE id = ?", (job["member_id"],))
                self._open_gate(db, command, "recovery_pending", {"job_id": job_id}, job["member_id"], job["candidate_id"])
                if _charges(db, unit) >= 3:
                    self._open_gate(db, command, "budget_exhausted", {"limit": 3})
                    self._fence(db, command)
            return {"accepted": current, "charged": charged}
        return self._apply(command, "record_failure", payload, change)

    def correct_charge(self, command, charge_id, *, evidence):
        if not isinstance(evidence, dict) or not evidence:
            raise TransitionError("charge correction requires verified evidence")
        def change(db, unit):
            charge = db.execute("SELECT b.*, a.budget_generation FROM budget_entries b JOIN attempts a ON a.id = b.attempt_id WHERE b.id = ? AND a.unit_id = ? AND b.kind = 'charge'", (charge_id, unit["id"])).fetchone()
            if charge is None:
                raise TransitionError("charge does not belong to this unit")
            db.execute("INSERT INTO budget_entries(id,attempt_id,kind,corrects_id,reason_code,created_at) VALUES(?,?,'correction',?,'verified_correction',?)", (_id(command.request_id, "correction"), charge["attempt_id"], charge_id, self.clock()))
            self._event(db, command, "budget_corrected", {"charge_id": charge_id, "evidence": evidence})
            if _charges(db, unit) < 3:
                for gate in db.execute("SELECT id FROM unit_gates WHERE unit_id = ? AND code = 'budget_exhausted' AND resolved_at IS NULL", (unit["id"],)).fetchall():
                    self._resolve(db, command, gate[0], {"charge_correction": charge_id})
            return {}
        return self._apply(command, "correct_charge", {"charge_id": charge_id, "evidence": evidence}, change)

    def verify_cleanup(self, command, job_id, *, execution_fence, evidence):
        if not isinstance(evidence, dict) or evidence.get("process_stopped") is not True or evidence.get("resources_released") is not True:
            raise TransitionError("cleanup needs verified process and resource termination")
        def change(db, unit):
            job = db.execute("SELECT * FROM jobs WHERE id = ? AND unit_id = ? AND cleanup = 0", (job_id, unit["id"])).fetchone()
            lease = db.execute("SELECT execution_fence FROM job_leases WHERE job_id = ?", (job_id,)).fetchone()
            if job is None or lease is None or lease[0] != execution_fence:
                raise TransitionError("cleanup does not own this live lease")
            if not job["fenced"] and job["phase"] != "finished":
                raise TransitionError("active job must be fenced before cleanup")
            db.execute("UPDATE jobs SET fenced = 1, cleanup = 1, phase = 'cleaned' WHERE id = ?", (job_id,))
            db.execute("DELETE FROM job_leases WHERE job_id = ?", (job_id,))
            self._event(db, command, "cleanup_verified", evidence, job_id=job_id)
            for gate in db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND code='cleanup_pending' AND job_id=? AND resolved_at IS NULL",(unit['id'],job_id)).fetchall():
                self._resolve(db,command,gate[0],{'cleanup_verified':job_id})
            return {}
        return self._apply(command, "verify_cleanup", {"job_id": job_id, "execution_fence": execution_fence, "evidence": evidence}, change)

    @staticmethod
    def _members(db, unit_id):
        return [dict(row) for row in db.execute(
            "SELECT * FROM unit_issues WHERE unit_id = ? AND left_at IS NULL ORDER BY id", (unit_id,))]

    def _singleton(self, db, unit):
        members = self._members(db, unit["id"])
        if len(members) != 1:
            raise TransitionError("this command requires a unit of one")
        return members[0]

    @staticmethod
    def _quiescent(db, unit_id, *, allow_confirmed_merge=False):
        if db.execute("SELECT 1 FROM jobs WHERE unit_id = ? AND cleanup = 0", (unit_id,)).fetchone():
            raise TransitionError("all workers require verified cleanup before this operation")
        if not allow_confirmed_merge and (db.execute(
            "SELECT 1 FROM merge_requests WHERE unit_id = ? AND status IN ('pending','uncertain')", (unit_id,)
        ).fetchone() or db.execute(
            "SELECT 1 FROM external_effects WHERE unit_id = ? AND status IN ('pending','running','uncertain')",
            (unit_id,),
        ).fetchone()):
            raise TransitionError("external effects must be reconciled before this operation")

    def _invalidate(self, db, command, unit, reason, evidence):
        self._live(unit)
        from .review_pulls import review_only_in, invalidate_in
        if review_only_in(db, unit['id']):
            invalidate_in(self, db, command, unit, reason, evidence)
            return
        members = self._members(db, unit['id'])
        from .delivery_inputs import approved_in
        grouped = approved_in(db,unit["id"]) or len(members) > 1
        if db.execute("SELECT 1 FROM merge_requests WHERE unit_id = ? AND status IN ('pending','uncertain')", (unit['id'],)).fetchone():
            raise TransitionError('merge intent must be reconciled before rework')
        candidate_id = unit['current_candidate_id']
        self._fence(db, command)
        if candidate_id:
            for gate in db.execute('SELECT id FROM unit_gates WHERE unit_id=? AND candidate_id=? AND resolved_at IS NULL', (unit['id'],candidate_id)).fetchall():
                self._resolve(db,command,gate[0],{'reason':reason},resolution='superseded')
            self._event(db,command,'candidate_superseded',{'candidate_id':candidate_id,'reason':reason,'evidence':evidence})
        affected = set(evidence.get('affected_member_ids', ())) if grouped else {member['id'] for member in members}
        if not affected.issubset({member['id'] for member in members}):
            raise TransitionError('rework scope includes a member outside this delivery')
        if reason in {'base_changed','policy_changed','requirements_changed','merge_conflict'}:
            affected = {member['id'] for member in members}
        if grouped and affected:
            from .delivery_inputs import dependencies_in
            while True:
                descendants = {member['id'] for member in members if set(dependencies_in(db,unit,member['id'])) & affected}
                if descendants.issubset(affected):
                    break
                affected.update(descendants)
            from .task_inputs import affected_tasks_in
            for task_id in affected_tasks_in(db,unit['id'],affected):
                db.execute('INSERT OR IGNORE INTO part_output_invalidations(output_id,reason,evidence_json,created_at) '
                    'SELECT id,?,?,? FROM part_outputs WHERE task_id=?',(reason,_json(evidence),self.clock(),task_id))
        target = 'building'
        db.execute('UPDATE units SET current_candidate_id=NULL,primary_gate_id=NULL,state=? WHERE id=?',(target,unit['id']))
        for member_id in affected:
            db.execute("UPDATE unit_issues SET state='pending' WHERE id=?",(member_id,))
        for failed in db.execute("SELECT j.id,j.member_id FROM jobs j JOIN attempt_failures f ON f.attempt_id=j.attempt_id "
                "WHERE j.unit_id=? AND f.recovery_required=1 AND f.investigation!='complete'",(unit['id'],)).fetchall():
            if not db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND code='recovery_pending' AND resolved_at IS NULL "
                              "AND json_extract(details_json,'$.job_id')=?",(unit['id'],failed['id'])).fetchone():
                self._open_gate(db,command,'recovery_pending',{'job_id':failed['id']},failed['member_id'] if grouped else members[0]['id'])
        self._event(db,command,'phase_changed',{'before':unit['state'],'after':target,'reason':reason})

    def invalidate_candidate(self, command, candidate_id, *, reason, evidence):
        """Invalidate exact candidate facts without removing an operator pause."""
        if reason not in {"base_changed", "requirements_changed", "candidate_changed", "policy_changed", "merge_conflict"}:
            raise TransitionError("unsupported invalidation reason")
        if not isinstance(evidence, dict) or evidence.get("verified") is not True:
            raise TransitionError("candidate invalidation requires verified evidence")
        def change(db, unit):
            if not candidate_id or candidate_id != unit["current_candidate_id"]:
                raise TransitionError("invalidation must identify the current candidate")
            self._invalidate(db, command, unit, reason, evidence)
            return {"invalidated_candidate_id": candidate_id}
        return self._apply(command, "invalidate_candidate", dict(candidate_id=candidate_id, reason=reason, evidence=evidence), change)

    def reject_job(self, command, job_id, *, execution_fence, input_manifest, evidence):
        """Record a candidate-bound technical rejection, charge once, then rework."""
        if not isinstance(evidence, dict) or evidence.get("verdict") != "fail":
            raise TransitionError("rejection requires a structured failing verdict")
        payload = dict(job_id=job_id, execution_fence=execution_fence, input_manifest=input_manifest, evidence=evidence)
        def change(db, unit):
            job, current = self._job(db, unit, job_id, execution_fence, input_manifest)
            if job["role"] not in {"review", "audit"} or evidence.get("candidate_id") != job["candidate_id"]:
                raise TransitionError("rejection must identify the review or audit candidate")
            if not current:
                self._result(db, command, job, evidence, "stale_failure")
                return {"accepted": False, "charged": False}
            self._result(db, command, job, evidence, "rejected")
            db.execute("INSERT INTO candidate_evidence(id,unit_id,candidate_id,attempt_id,kind,verdict,actor_id,observed_at,details_json) VALUES(?,?,?,?,?,'fail',?,?,?)",
                       (_id(command.request_id, "evidence"), unit["id"], job["candidate_id"], job["attempt_id"], job["role"], command.actor_id, self.clock(), _json(evidence)))
            from .review_pulls import review_only_in
            if review_only_in(db, unit['id']):
                self._invalidate(db, command, unit, "technical_rejection", evidence)
                return {"accepted": True, "charged": False, "phase": unit['state']}
            charge_id = _id(command.request_id, "charge")
            db.execute("INSERT INTO budget_entries(id,attempt_id,kind,reason_code,created_at) VALUES(?,?,'charge','technical_rejection',?)", (charge_id, job["attempt_id"], self.clock()))
            self._event(db, command, "budget_charged", {"charge_id": charge_id}, job_id=job_id)
            self._invalidate(db, command, unit, "technical_rejection", evidence)
            from .recovery import record_failure_facts
            record_failure_facts(db, unit, job, evidence, self.clock(), store=self.store, retry_stage='build')
            self._open_gate(db, command, 'recovery_pending', {'job_id': job_id})
            if _charges(db, unit) >= 3:
                self._open_gate(db, command, "budget_exhausted", {"limit": 3})
            return {"accepted": True, "charged": True, "phase": _unit(db,unit["id"])["state"]}
        return self._apply(command, "reject_job", payload, change)

    def retry(self, command, *, evidence):
        """An explicit operator retry creates a new budget epoch after cleanup."""
        if not isinstance(evidence, dict) or evidence.get("approved") is not True:
            raise TransitionError("retry requires an explicit operator approval")
        def change(db, unit):
            self._live(unit)
            self._quiescent(db, unit["id"])
            from .recovery import pending_mandatory
            if pending_mandatory(db, unit['id']):
                raise TransitionError('mandatory investigations must complete before retry')
            if _charges(db, unit) == 0:
                raise TransitionError("there are no failures to retry")
            self._fence(db, command)
            for gate in db.execute("SELECT id FROM unit_gates WHERE unit_id = ? AND code IN ('budget_exhausted','recovery_pending') AND resolved_at IS NULL", (unit["id"],)).fetchall():
                self._resolve(db, command, gate[0], {"retry": command.request_id})
            db.execute("UPDATE units SET budget_generation = budget_generation + 1 WHERE id = ?", (unit["id"],))
            db.execute("UPDATE unit_issues SET state = 'pending' WHERE unit_id = ? AND left_at IS NULL AND state = 'failed'", (unit["id"],))
            self._event(db, command, "budget_reset", {"before": unit["budget_generation"], "after": unit["budget_generation"] + 1, "evidence": evidence})
            return {"budget_generation": unit["budget_generation"] + 1}
        return self._apply(command, "retry", {"evidence": evidence}, change)

    def reconcile_recovery(self, command, job_id, *, evidence, reverse_charge=False):
        """Complete a verified recovery after cleanup; preserve unrelated gates."""
        if not isinstance(evidence, dict) or evidence.get("verified") is not True or type(reverse_charge) is not bool:
            raise TransitionError("recovery requires verified structured evidence")
        def change(db, unit):
            self._live(unit)
            job = db.execute("SELECT * FROM jobs WHERE id = ? AND unit_id = ?", (job_id, unit["id"])).fetchone()
            if job is None or not job["cleanup"] or job["outcome"] != "failure":
                raise TransitionError("recovery requires a cleaned failed assignment")
            if db.execute('SELECT 1 FROM attempt_failures WHERE attempt_id=? AND recovery_required=1', (job['attempt_id'],)).fetchone():
                raise TransitionError('typed failures require their one recovery assignment')
            gates = [g for g in _gates(db, unit["id"]) if g["code"] == "recovery_pending" and json.loads(g["details_json"]).get("job_id") == job_id]
            if not gates:
                raise TransitionError("assignment has no pending recovery")
            if reverse_charge:
                charge = db.execute("SELECT id FROM budget_entries WHERE attempt_id = ? AND kind = 'charge'", (job["attempt_id"],)).fetchone()
                if charge is None:
                    raise TransitionError("assignment has no charge to correct")
                db.execute("INSERT INTO budget_entries(id,attempt_id,kind,corrects_id,reason_code,created_at) VALUES(?,?,'correction',?,'verified_recovery',?)", (_id(command.request_id, "correction"), job["attempt_id"], charge[0], self.clock()))
                self._event(db, command, "budget_corrected", {"charge_id": charge[0], "evidence": evidence})
                if _charges(db, unit) < 3:
                    for gate in _gates(db, unit["id"]):
                        if gate["code"] == "budget_exhausted":
                            self._resolve(db, command, gate["id"], {"charge_correction": charge[0]})
            for gate in gates:
                self._resolve(db, command, gate["id"], evidence)
            if job["member_id"]:
                db.execute("UPDATE unit_issues SET state = 'pending' WHERE id = ? AND state = 'failed'", (job["member_id"],))
            return {"recovered_job_id": job_id, "corrected": reverse_charge}
        return self._apply(command, "reconcile_recovery", dict(job_id=job_id, evidence=evidence, reverse_charge=reverse_charge), change)

    _remote_time = staticmethod(remote_time)

    def observe_repository_readiness(self, command, *, repository_id, base_branch, allowed,
                                     access_allowed, reason, source_revision, read_authority, valid_until):
        """Apply a current setup observation without changing configuration or budgets."""
        if (type(allowed) is not bool or type(access_allowed) is not bool
                or allowed and not access_allowed or not isinstance(reason, str)
                or not isinstance(source_revision, str) or access_allowed and not source_revision):
            raise TransitionError('repository readiness requires explicit access and implementation evidence')
        if (not isinstance(read_authority, dict) or set(read_authority) != {'domain', 'scope', 'revision'}
                or any(not isinstance(value, str) or not value for value in read_authority.values())
                or access_allowed and (type(valid_until) not in {float, int} or not math.isfinite(valid_until))):
            raise TransitionError('repository readiness requires its exact persisted read authority')
        details = dict(source='repository_readiness', repository_id=repository_id,
                       base_branch=base_branch, access_allowed=access_allowed,
                       reason=reason, source_revision=source_revision)
        changed = False
        def change(db, unit):
            nonlocal changed
            from .repository_readiness import read_revision
            if (read_revision(db, domain=read_authority['domain'], scope=read_authority['scope'], repository_id=repository_id)
                    != read_authority['revision'] or access_allowed and self.clock() >= valid_until):
                raise VersionConflict('Repository readiness changed before the lifecycle observation')
            if unit['repository_id'] != repository_id or unit['base_branch'] != base_branch:
                raise TransitionError('repository readiness does not match delivery identity')
            self._live(unit)
            gates = [gate for gate in _gates(db, unit['id']) if gate['code'] == 'repository_setup']
            owned = [gate for gate in gates if json.loads(gate['details_json']).get('source')
                     in {'repository_readiness', 'controller_observation'}]
            if allowed:
                for gate in owned:
                    self._resolve(db, command, gate['id'], {**details, 'verified': True, 'observed_at': self.clock()})
                    changed = True
            elif not gates or owned and any(json.loads(gate['details_json']) != details for gate in owned):
                for gate in owned:
                    self._resolve(db, command, gate['id'], {'superseded_by': details})
                self._open_gate(db, command, 'repository_setup', details)
                if not access_allowed:
                    self._fence(db, command)
                # Protection readiness gates new work. Expired/unavailable
                # observations do not revoke an already admitted execution.
                # Publication and merge still independently verify live policy.
                changed = True
            return {'allowed': allowed, 'unchanged': not changed}
        return self._apply(command, 'observe_repository_readiness', {**details, 'allowed': allowed,
                           'read_authority': read_authority, 'valid_until': valid_until}, change,
                           version_changed=lambda: changed, journal_changed=lambda: changed)

    def observe_issue(self, command, issue_id, *, state, title, body, requirements,
                      remote_updated_at, generation, state_reason=None, labels=(), assignees=(),
                      hold_labels=("hold", "blocked"), admission=None, ownership_conflict=None, assignee_types=None,
                      content_approved=False):
        """Apply an ordered authoritative observation to a current or historical member.

        Revised issue text becomes eligible for standalone work with verified approval.
        Reopening updates GitHub facts only. A terminal unit stays terminal; the
        operator may subsequently admit the issue into a new unit.
        """
        remote_updated_at = self._remote_time(remote_updated_at)
        if state not in {"open", "closed"} or type(generation) is not int or generation < 1:
            raise TransitionError("observation requires a valid state and positive generation")
        if state_reason not in {None, "completed", "not_planned", "duplicate", "reopened"}:
            raise TransitionError("unknown GitHub closure reason")
        if state == "open" and state_reason not in {None, "reopened"}:
            raise TransitionError("open issue cannot retain a closure reason")
        if not isinstance(title, str) or not isinstance(body, str) or not all(isinstance(v, str) for v in (*labels, *assignees)):
            raise TransitionError("issue text and observed identities must be strings")
        if not all(isinstance(label, str) and label for label in hold_labels):
            raise TransitionError('hold labels must be nonempty strings')
        if admission is not None and type(admission) is not bool:
            raise TransitionError('admission observation must be boolean')
        if ownership_conflict is not None and type(ownership_conflict) is not bool:
            raise TransitionError('ownership observation must be boolean')
        if assignee_types is not None and (not isinstance(assignee_types, dict)
                or any(login not in assignees or kind not in {'User', 'Bot', 'Organization', 'Mannequin'}
                       for login, kind in assignee_types.items())):
            raise TransitionError('assignee types must identify the observed GitHub assignees')
        content_hash, requirements_hash = manifest_hash([title, body]), manifest_hash(requirements)
        payload = dict(issue_id=issue_id, state=state, content_hash=content_hash, requirements_hash=requirements_hash,
                       remote_updated_at=remote_updated_at, generation=generation, state_reason=state_reason,
                       labels=sorted(set(labels)), assignees=sorted(set(assignees)))
        if tuple(hold_labels) != ('hold', 'blocked') or admission is not None or ownership_conflict is not None:
            payload.update(hold_labels=sorted(set(hold_labels)), admission=admission, ownership_conflict=ownership_conflict)
        if assignee_types is not None:
            payload['assignee_types'] = assignee_types
        if content_approved is True:
            payload['content_approved'] = True
        # Observation ordering/freshness is distinct from the facts and gates
        # authorized by an operator proposal. Other commands keep their CAS bump.
        authority_changed = False
        def prepare_payload(db):
            existing = db.execute('SELECT id FROM issue_revisions WHERE issue_id=? AND content_hash=? AND requirements_hash=?',
                                  (issue_id, content_hash, requirements_hash)).fetchone()
            payload['revision_id'] = existing[0] if existing else _id(command.request_id, 'revision')

        def change(db, unit):
            nonlocal authority_changed
            old = db.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            member = db.execute("SELECT * FROM unit_issues WHERE unit_id = ? AND issue_id = ? ORDER BY joined_at DESC LIMIT 1", (unit["id"], issue_id)).fetchone()
            owner = db.execute("SELECT unit_id FROM unit_issues WHERE issue_id = ? AND left_at IS NULL", (issue_id,)).fetchone()
            if old is None or member is None or (owner and owner[0] != unit["id"]):
                raise TransitionError("observation must use the issue's current owning unit")
            old_order = (remote_order(old["remote_updated_at"]), old["collection_generation"])
            new_order = (remote_order(remote_updated_at), generation)
            if new_order < old_order:
                return {"issue_id": issue_id, "stale": True, "revision_id": old["current_revision_id"]}
            revision = db.execute("SELECT * FROM issue_revisions WHERE id = ?", (old["current_revision_id"],)).fetchone()
            changed = (state != old["github_state"] or state_reason != old["state_reason"] or content_hash != revision["content_hash"]
                       or requirements_hash != revision["requirements_hash"] or payload["labels"] != json.loads(old["labels_json"])
                       or payload["assignees"] != json.loads(old["assignees_json"])
                       or assignee_types is not None and assignee_types != json.loads(old["assignee_types_json"]))
            if new_order == old_order:
                if changed:
                    raise TransitionError("conflicting observations share the same ordering identity")
            authority_changed = changed
            existing = db.execute("SELECT id FROM issue_revisions WHERE issue_id = ? AND content_hash = ? AND requirements_hash = ?", (issue_id, content_hash, requirements_hash)).fetchone()
            revision_id = existing[0] if existing else _id(command.request_id, "revision")
            if not existing:
                db.execute("INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) VALUES(?,?,?,?,?,?,?,?)", (revision_id, issue_id, title, body, content_hash, requirements_hash, _json(requirements), self.clock()))
                record_revision_display(db,revision_id,body,requirements)
            reopened_version = old['reopened_version']
            if state == 'open' and (old['github_state'] == 'closed' or state_reason == 'reopened' and old['state_reason'] != 'reopened'):
                reopened_version = old['version'] + 1
            elif not reopened_version and old['state_reason'] == 'reopened':
                reopened_version = old['version']
            observed_at = self.clock() if new_order > old_order else old["observed_at"]
            db.execute("UPDATE issues SET github_state = ?, state_reason = ?, current_revision_id = ?, remote_updated_at = ?, collection_generation = ?, labels_json = ?, assignees_json = ?, observed_at = ?, reopened_version = ?, version = version + ? WHERE id = ?", (state, state_reason, revision_id, remote_updated_at, generation, _json(payload["labels"]), _json(payload["assignees"]), observed_at, reopened_version, int(changed), issue_id))
            if assignee_types is not None:
                db.execute('UPDATE issues SET assignee_types_json=? WHERE id=?', (_json(assignee_types), issue_id))
            if member["left_at"] is None and unit["state"] not in TERMINAL:
                requirements_changed = (requirements_hash != revision["requirements_hash"]
                                        or content_hash != revision["content_hash"])
                pinned_changed = member["requirements_revision_id"] != revision_id
                if requirements_changed or (content_approved is True and pinned_changed):
                    ambiguous = db.execute("SELECT 1 FROM merge_requests WHERE unit_id = ? AND status IN ('pending','uncertain')", (unit["id"],)).fetchone()
                    from .delivery_inputs import approved_in
                    if ambiguous or approved_in(db, unit["id"]):
                        if requirements_changed:
                            self.fence_member_in(db, command, member["id"], reason="issue_observation_changed")
                            if not any(g["code"] == "membership_decision" and g["member_id"] == member["id"] for g in _gates(db, unit["id"])):
                                self._open_gate(db, command, "membership_decision", {"reason": "requirements_changed", "revision_id": revision_id}, member["id"])
                    elif pinned_changed:
                        pinned = db.execute('SELECT content_hash FROM issue_revisions WHERE id=?', (member['requirements_revision_id'],)).fetchone()
                        if content_approved is not True and content_hash != pinned['content_hash']:
                            if requirements_changed:
                                self.fence_member_in(db, command, member["id"], reason="issue_observation_changed")
                        else:
                            authority_changed = True
                            db.execute("UPDATE unit_issues SET requirements_revision_id = ? WHERE id = ?", (revision_id, member["id"]))
                            db.execute("UPDATE units SET membership_generation = membership_generation + 1 WHERE id = ?", (unit["id"],))
                            self._invalidate(db, command, unit, "requirements_changed", {"issue_id": issue_id, "revision_id": revision_id})
                if state == "closed" and old["github_state"] != "closed":
                    self.fence_member_in(db, command, member["id"], reason="issue_observation_changed")
                matching_hold = [g for g in _gates(db, unit["id"]) if g["code"] == "label_hold" and g["member_id"] == member["id"]]
                held = bool(set(hold_labels).intersection(payload["labels"]))
                if held and not matching_hold:
                    authority_changed = True
                    self.fence_member_in(db, command, member["id"], reason="issue_observation_changed")
                    self._open_gate(db, command, "label_hold", {"source": "github_labels", "issue_id": issue_id}, member["id"])
                elif not held:
                    for gate in matching_hold:
                        if json.loads(gate["details_json"]).get("source") == "github_labels":
                            authority_changed = True
                            self._resolve(db, command, gate["id"], {"observed_at": self.clock(), "labels": payload["labels"]})
                for code, blocked, scope in (
                        ('repository_setup', None if admission is None else not admission, None),
                        ('ownership_conflict', ownership_conflict, member['id'])):
                    if blocked is None:
                        continue
                    existing_gates = [g for g in _gates(db, unit['id'])
                                      if g['code'] == code and g['member_id'] == scope]
                    if blocked and not existing_gates:
                        authority_changed = True
                        if scope is None:
                            self._fence(db, command)
                        else:
                            self.fence_member_in(db, command, member["id"], reason="issue_observation_changed")
                        self._open_gate(db, command, code, {'source': 'controller_observation', 'issue_id': issue_id}, scope)
                    elif not blocked:
                        for gate in existing_gates:
                            if json.loads(gate['details_json']).get('source') == 'controller_observation':
                                authority_changed = True
                                self._resolve(db, command, gate['id'], {'verified': True, 'observed_at': self.clock()})
            if authority_changed:
                self._event(db, command, "issue_observed", {**payload, "planning_changed": True}, issue_id=issue_id)
            return {"issue_id": issue_id, "stale": False, "revision_id": revision_id, "unchanged": not authority_changed}
        return self._apply(command, "observe_issue", payload, change, version_changed=lambda: authority_changed,
                           journal_changed=lambda: authority_changed, prepare_payload=prepare_payload)

    def admit_known_issue(self, *, request_id, actor_id, unit_id, issue_id, expected_issue_version, base_branch="main"):
        """Admit a reopened or previously released issue without reusing its old unit."""
        command = Command(request_id, actor_id, unit_id, 1)
        payload = dict(issue_id=issue_id, expected_issue_version=expected_issue_version, base_branch=base_branch)
        with self._command_transaction(command, "admit_known_issue", payload) as db:
            previous = self._replay(db, command, "admit_known_issue", payload)
            if previous is not None:
                return previous
            issue = db.execute("SELECT i.*, r.enabled FROM issues i JOIN repositories r ON r.id = i.repository_id WHERE i.id = ?", (issue_id,)).fetchone()
            if issue is None or issue["github_state"] != "open" or not issue["enabled"]:
                raise TransitionError("admission requires an open issue in an enabled repository")
            if issue["version"] != expected_issue_version:
                raise VersionConflict("issue observation changed before admission")
            revision = db.execute('SELECT requirements_json FROM issue_revisions WHERE id=?', (issue['current_revision_id'],)).fetchone()
            requirements = json.loads(revision[0])
            if not complete_requirements(requirements):
                raise RequirementsStale('admission requires fresh complete issue requirements')
            if db.execute("SELECT 1 FROM unit_issues WHERE issue_id = ? AND left_at IS NULL", (issue_id,)).fetchone():
                raise TransitionError("issue already belongs to a live unit")
            member_id = self._create_unit(db, command, unit_id, issue, base_branch, issue["current_revision_id"])
            return self._save(db, command, "admit_known_issue", payload, {"member_id": member_id, "revision_id": issue["current_revision_id"]}, initial=True)

    def _create_unit(self, db, command, unit_id, issue, base_branch, revision_id):
        if not all(isinstance(v, str) and v for v in (command.request_id, command.actor_id, unit_id, base_branch)):
            raise TransitionError("admission identities must be nonempty")
        member_id = _id(command.request_id, f"member:{unit_id}")
        db.execute("INSERT INTO units(id,repository_id,base_branch,state,created_at,updated_at) VALUES(?,?,?,'building',?,?)", (unit_id, issue["repository_id"], base_branch, self.clock(), self.clock()))
        db.execute("INSERT INTO unit_issues(id,unit_id,issue_id,repository_id,base_branch,requirements_revision_id,state,joined_at) VALUES(?,?,?,?,?,?,'pending',?)", (member_id, unit_id, issue["id"], issue["repository_id"], base_branch, revision_id, self.clock()))
        child = Command(command.request_id, command.actor_id, unit_id, 1)
        self._event(db, child, "issue_admitted", {"member_id": member_id, "revision_id": revision_id}, issue_id=issue["id"])
        if any(label in {"hold", "blocked"} for label in json.loads(issue["labels_json"])):
            self._open_gate(db, child, "label_hold", {"source": "github_labels", "issue_id": issue["id"]}, member_id)
        return member_id

    def adopt_candidate(self, command, candidate, *, evidence):
        """Approve an existing PR for a quiescent singleton; review still must run."""
        if not isinstance(candidate, Candidate) or not isinstance(evidence, dict) or evidence.get("approved") is not True or evidence.get("verified") is not True:
            raise TransitionError("existing candidate needs operator approval and verified publication")
        def change(db, unit):
            self._live(unit)
            self._quiescent(db, unit["id"])
            member = self._singleton(db, unit)
            if unit["state"] not in {"building"} or unit["current_candidate_id"] is not None:
                raise TransitionError("existing PR adoption requires a candidate-less unit")
            if applicable_gates(db, unit, member["id"]):
                raise TransitionError("unit is blocked from adopting a candidate")
            revision = db.execute("SELECT requirements_hash FROM issue_revisions WHERE id = ?", (member["requirements_revision_id"],)).fetchone()
            if candidate.requirements_hash != revision[0] or any(evidence.get(k) != getattr(candidate, k) for k in ("pull_number", "head_oid", "base_oid", "policy_hash", "input_manifest_hash")):
                raise TransitionError("existing PR evidence does not match the approved candidate")
            candidate_id = _id(command.request_id, "candidate")
            db.execute("INSERT INTO candidates(id,unit_id,pull_number,branch,head_oid,base_oid,policy_hash,requirements_hash,membership_generation,input_manifest_hash,accepted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (candidate_id, unit["id"], candidate.pull_number, candidate.branch, candidate.head_oid, candidate.base_oid, candidate.policy_hash, candidate.requirements_hash, unit["membership_generation"], candidate.input_manifest_hash, self.clock()))
            db.execute("INSERT INTO candidate_evidence(id,unit_id,candidate_id,kind,verdict,actor_id,observed_at,details_json) VALUES(?,?,?,'publication','pass',?,?,?)", (_id(command.request_id, "evidence"), unit["id"], candidate_id, command.actor_id, self.clock(), _json(evidence)))
            db.execute("UPDATE units SET state = 'reviewing', current_candidate_id = ? WHERE id = ?", (candidate_id, unit["id"]))
            db.execute("UPDATE unit_issues SET state = 'built' WHERE id = ?", (member["id"],))
            self._event(db, command, "candidate_selected", {"candidate_id": candidate_id, "source": "operator_existing_pr"})
            return {"candidate_id": candidate_id}
        return self._apply(command, "adopt_candidate", {"candidate": asdict(candidate), "evidence": evidence}, change)

    @staticmethod
    def _prove_not_merged(db, unit, evidence):
        if unit["current_candidate_id"] is None:
            return
        candidate = db.execute("SELECT * FROM candidates WHERE id = ?", (unit["current_candidate_id"],)).fetchone()
        if (evidence.get("verified") is not True or evidence.get("merged") is not False
                or evidence.get("candidate_id") != candidate["id"]
                or evidence.get("pull_number") != candidate["pull_number"]
                or evidence.get("head_oid") != candidate["head_oid"]
                or not isinstance(evidence.get("observed_at"), (int, float))
                or evidence["observed_at"] < candidate["accepted_at"]):
            raise TransitionError("ending candidate work requires a fresh confirmed unmerged observation")

    def _settle(self, db, command, unit, target, *, close_code=None, merge_sha=None, exit_reason=None):
        ending = exit_reason or target
        for gate in _gates(db, unit["id"]):
            self._resolve(db, command, gate["id"], {"terminal_state": target}, resolution="superseded")
        self._fence(db, command)
        for member in self._members(db, unit["id"]):
            db.execute("UPDATE unit_issues SET state = ?, left_at = ?, exit_reason = ?, completion_issue_version = (SELECT version FROM issues WHERE id=unit_issues.issue_id) WHERE id = ?", ("done" if target == "merged" else "removed", self.clock(), ending, member["id"]))
            self._event(db, command, "membership_changed", {"member_id": member["id"], "exit_reason": ending}, issue_id=member["issue_id"])
        db.execute("UPDATE units SET state = ?, close_code = ?, merge_sha = ?, primary_gate_id = NULL WHERE id = ?", (target, close_code, merge_sha, unit["id"]))
        self._event(db, command, ending, {"close_code": close_code, "merge_sha": merge_sha})

    def confirm_merged(self, command, candidate_id, *, merge_sha, evidence):
        """Record a confirmed remote merge, including a human merge during review."""
        if not isinstance(merge_sha, str) or not merge_sha or not isinstance(evidence, dict) or evidence.get("verified") is not True:
            raise TransitionError("merge confirmation requires independently verified evidence")
        def change(db, unit):
            self._live(unit)
            # A preserved candidate may still be queued for rework when a human
            # merges it. Exact remote receipt and verified cleanup remain the
            # authority; the scheduler phase cannot erase an observed merge.
            if not candidate_id or candidate_id != unit["current_candidate_id"]:
                raise TransitionError("merge confirmation must identify the current candidate")
            self._quiescent(db, unit["id"], allow_confirmed_merge=True)
            identity = db.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
            if evidence.get("merge_sha") != merge_sha or evidence.get("merged") is not True or any(evidence.get(k) != identity[k] for k in ("pull_number", "head_oid", "base_oid")):
                raise TransitionError("merge receipt does not match the exact candidate")
            db.execute("INSERT INTO candidate_evidence(id,unit_id,candidate_id,kind,verdict,actor_id,observed_at,details_json) VALUES(?,?,?,'merge','pass',?,?,?)", (_id(command.request_id, "evidence"), unit["id"], candidate_id, command.actor_id, self.clock(), _json(evidence)))
            for request in db.execute("SELECT id,effect_id FROM merge_requests WHERE unit_id = ? AND candidate_id = ? AND status IN ('pending','uncertain')", (unit["id"], candidate_id)).fetchall():
                db.execute("UPDATE merge_requests SET status = 'merged', merge_oid = ? WHERE id = ?", (merge_sha, request["id"]))
                db.execute("UPDATE external_effects SET status = 'confirmed', receipt_json = ?, updated_at = ? WHERE id = ?", (_json(evidence), self.clock(), request["effect_id"]))
                self._event(db, command, 'external_effect_changed', {'effect_id': request['effect_id'],
                    'status': 'confirmed', 'candidate_id': candidate_id})
            # The receipt reconciles only this candidate's merge intent. Any
            # unrelated uncertain external effect still needs reconciliation.
            self._quiescent(db, unit["id"])
            if unit['state'] in {'building','publishing'}:
                # Normalize a preserved, already published candidate inside the
                # receipt transaction; no technical verdict or launch is implied.
                db.execute("UPDATE units SET state='reviewing' WHERE id=?", (unit['id'],))
            self._settle(db, command, unit, "merged", merge_sha=merge_sha)
            return {"merge_sha": merge_sha}
        return self._apply(command, "confirm_merged", dict(candidate_id=candidate_id, merge_sha=merge_sha, evidence=evidence), change)

    def close_unit(self, command, *, close_code, evidence):
        """End a singleton after cleanup, with a confirmed cause; stop uses pause."""
        if close_code not in codes.CLOSE_CODES or close_code in {"combined", "import_superseded"} or not isinstance(evidence, dict) or not evidence:
            raise TransitionError("unsupported singleton closure or missing evidence")
        def change(db, unit):
            self._live(unit)
            self._quiescent(db, unit["id"])
            self._prove_not_merged(db, unit, evidence)
            member = self._singleton(db, unit)
            issue = db.execute("SELECT * FROM issues WHERE id = ?", (member["issue_id"],)).fetchone()
            if close_code.startswith("github_"):
                reason = close_code.removeprefix("github_")
                if issue["github_state"] != "closed" or issue["state_reason"] != reason or evidence.get("issue_version") != issue["version"]:
                    raise TransitionError("closure must match the current authoritative issue observation")
            elif evidence.get("approved") is not True:
                raise TransitionError("operator closure requires explicit approval")
            self._settle(db, command, unit, "closed", close_code=close_code)
            return {"close_code": close_code}
        return self._apply(command, "close_unit", dict(close_code=close_code, evidence=evidence), change)

    def dissolve(self, command, *, evidence):
        """Explicitly release a quiescent batch into new singleton deliveries."""
        return self._apply(command,"dissolve",{"evidence":evidence},lambda db,unit:self.dissolve_in(db,command,unit,evidence=evidence))

    def dissolve_in(self,db,command,unit,*,evidence):
        if not isinstance(evidence,dict) or evidence.get('approved') is not True:
            raise TransitionError('dissolve requires explicit operator approval')
        self._live(unit)
        self._quiescent(db, unit["id"])
        from .recovery import pending_mandatory
        if pending_mandatory(db,unit['id']):
            raise TransitionError('mandatory investigations must finish before dissolve')
        self._prove_not_merged(db, unit, evidence)
        members = self._members(db, unit["id"])
        if len(members) < 2:
            raise TransitionError("only a batch can be dissolved")
        issues = [dict(db.execute("SELECT * FROM issues WHERE id = ?", (m["issue_id"],)).fetchone()) for m in members]
        if any(issue["github_state"] != "open" for issue in issues):
            raise TransitionError("closed members require an explicit membership decision before dissolve")
        retained_intent = [gate for gate in _gates(db, unit["id"]) if gate["code"] in {"operator_pause", "operator_backlog"}]
        from .forge_problems import member_faults_in, inherit_faults_in
        retained_faults = {member["id"]: member_faults_in(db,unit["id"],member["id"]) for member in members}
        from .worker_claims import member_claims_in, inherit_claims_in
        retained_claims = {member['id']: member_claims_in(db, unit['id'], member['id'], self.clock()) for member in members}
        # The approved graph remains a prerequisite after the work is split.
        # Revision-bound issue dependencies are the existing external authority.
        from .delivery_inputs import dependencies_in
        by_id={member['id']:member for member in members}
        for member in members:
            for prerequisite_id in dependencies_in(db,unit,member['id']):
                prerequisite=by_id[prerequisite_id]
                db.execute("INSERT OR IGNORE INTO issue_dependencies(id,issue_id,requirements_revision_id,prerequisite_issue_id,source,evidence,created_at) "
                    "VALUES(?,?,?,?,'operator',?,?)",(_id(command.request_id,'external:'+member['id']+':'+prerequisite_id),
                    member['issue_id'],member['requirements_revision_id'],prerequisite['issue_id'],'Approved dependency retained after dissolve',self.clock()))
        self._settle(db, command, unit, "closed", close_code="dissolved", exit_reason="dissolved")
        released = []
        for member, issue in zip(members, issues):
            child_id = _id(command.request_id, f"unit:{member['id']}")
            child_member = self._create_unit(db, command, child_id, issue, unit["base_branch"], member["requirements_revision_id"])
            child_command = Command(command.request_id, command.actor_id, child_id, 1)
            member_intent=[gate for gate in retained_intent if gate['member_id'] in {None,member['id']}]
            for code in sorted({gate['code'] for gate in member_intent}):
                self._open_gate(db,child_command,code,{'source_unit_id':unit['id'],'source_gate_ids':[gate['id'] for gate in member_intent if gate['code']==code]})
            inherit_faults_in(self,db,child_command,child_member,retained_faults[member["id"]])
            inherit_claims_in(self, db, child_command, child_member, retained_claims[member['id']])
            self._event(db, Command(command.request_id, command.actor_id, child_id, 1), "membership_changed", {"source_unit_id": unit["id"], "source_member_id": member["id"], "member_id": child_member}, issue_id=issue["id"])
            released.append({"unit_id": child_id, "member_id": child_member, "issue_id": issue["id"]})
        return {"released": released}
