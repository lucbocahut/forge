"""Plain wording for lifecycle codes, in one place.

The supervisor, API, UI and CLI show `describe(code, **params)`; none of them
compares or parses this text. Each entry is a sentence that needs no parameters,
plus an optional richer sentence used when its parameters are supplied.
"""
from __future__ import annotations

import json

from . import codes

WORDING: dict[str, tuple[str, str | None]] = {
    'requirements_stale': ("Couldn't start this issue: its requirements need a fresh read", None),
    'remote_timestamp_invalid': ("Couldn't refresh this issue: its GitHub observation has an invalid timestamp", None),
    'admission_conflict': ("Couldn't start this issue: its state changed; Forge will check again", None),
    'result_pending': ('Checking the completed worker result', None),
    # Unit states
    'building': ('Building', 'Building {parts}'),
    'publishing': ('Publishing the completed change', None),
    'reviewing': ('In review', 'Reviewing pull request #{pull}'),
    'auditing': ('In audit', 'Auditing pull request #{pull}'),
    'merging': ('Ready to merge', 'Merging pull request #{pull}'),
    'merged': ('Merged', 'Merged in pull request #{pull}'),
    'closed': ('Closed without merging', None),
    'dissolved': ('Dissolved; its issues continue separately', None),
    # Close codes
    'github_completed': ('Closed on GitHub as completed', None),
    'github_not_planned': ('Closed on GitHub as not planned', None),
    'github_duplicate': ('Closed on GitHub as a duplicate', 'Closed on GitHub as a duplicate of #{duplicate_of}'),
    'operator_closed': ('Stopped by the operator', None),
    'combined': ('Combined into another unit', 'Combined into unit {into}'),
    'pull_request_closed': ('Review ended because the pull request closed', None),
    'pull_retargeted': ('Review continues against the new target branch', None),
    'import_superseded': ('Imported delivery retired; the issue can start again as a new delivery', None),
    # Gates
    'operator_pause': ('Paused by the operator', None),
    'operator_backlog': ('Moved to the backlog by the operator', None),
    'label_hold': ('On hold by a GitHub label; removing it clears this hold', 'On hold by the "{label}" label; removing it clears this hold'),
    'membership_decision': ('Waiting for a decision about which issues stay in this unit', None),
    'operator_sync': ("Applying the operator's decision on GitHub", None),
    'triage_sync': ('Applying the approved triage decision on GitHub', None),
    'external_change': ('Paused after a change made outside Forge', None),
    'review_source': ('Waiting for the pull request author or current review scope', None),
    'ownership_conflict': ('Someone else is working on this issue', '{login} is working on this issue'),
    'budget_exhausted': ('Stopped after three failed attempts; choose how to continue', None),
    'approach_required': ('The same failure keeps recurring; a new approach is needed', None),
    'decision_required': ('Waiting for an operator decision', 'Waiting for an operator decision: {question}'),
    'forge_problem': ('Forge failed before the agent started; waiting for a qualified runner update. No issue attempt was charged', None),
    'repository_setup': ('Waiting for repository setup; work resumes once it is ready', None),
    'environment': ('Waiting for the build environment to be repaired', None),
    'observation_unavailable': ('GitHub could not be read; waiting to confirm the current state', None),
    'legacy_reconciliation': ('Imported from the old state; needs an operator review', None),
    'dependency': ('Waiting for other issues to close', 'Waiting for {issues} to close'),
    'recovery_pending': ('Investigating a failed attempt', None),
    'cleanup_pending': ('Cleaning up a finished attempt', None),
    'provider_unavailable': ('Waiting for an available agent account', 'Waiting for an agent account until {until}'),
    'retry_delay': ('Waiting before retrying', 'Retrying after {until}'),
    'verification_human': ('Waiting for a person to verify the change', None),
    'verification_ci': ('Waiting for CI to verify the change', None),
    'approval_required': ('Waiting for approval to merge', None),
    'merge_checks': ('Waiting for required checks to pass', None),
}


REVIEW_CAUSES = {
    'dependency_access': 'Dependency downloads are unavailable.',
    'missing_service': 'A required test service is unavailable.',
    'environment': 'The review environment needs attention.',
    'resources': 'The worker needs additional resources.',
    'forge_problem': 'Forge could not prepare or run this review.',
    'provider_login_expired': 'The coding account needs sign-in.',
    'provider_usage_limit': 'The coding account has reached its usage limit.',
    'approval_required': 'Waiting for human approval.',
    'verification_human': 'Waiting for human verification.',
    'verification_ci': 'Waiting for required verification checks.',
    'label_hold': 'A hold label pauses this pull request.',
    'pull_draft': 'This pull request is a draft.',
    'repository_disabled': 'Automation is disabled for this repository.',
    'operator_pause': 'An operator paused this work.',
    'technical_rejection': 'Review or Audit requested changes.',
    'verdict_failed': 'Review or Audit requested changes.',
}


def describe(code: str, **params) -> str:
    """The plain sentence for a code. Unknown codes are a programming error."""
    try:
        plain, detailed = WORDING[code]
    except KeyError:
        raise KeyError(f'No wording for code {code!r}') from None
    if detailed and params:
        try:
            return detailed.format_map(params)
        except (KeyError, IndexError, ValueError):
            pass
    return plain


def repository_setup_reason(reason):
    """Explain typed readiness evidence without exposing its internal code."""
    return {
        'protection_needs_setup': 'GitHub repository rules could not be verified. Open Repository setup and check again.',
        'protection_unchecked': 'Repository rules need to be checked for the current setup.',
        'protection_branch_changed': 'Repository rules need to be checked for this delivery’s target branch.',
        'protection_identity_changed': 'The saved rules check belongs to a different repository. Check this repository’s setup again.',
        'protection_unavailable': 'Forge could not verify the repository rules on GitHub. Open Repository setup to check the connection and try again.',
        'github_access_unverified': 'Forge has not verified its GitHub Apps’ access to this repository. Open Repository setup to check their access.',
        'repository_not_enabled': 'This repository is not enabled for Forge. Open Repository setup to enable it.',
        'repository_disabled': 'This repository is not enabled for Forge. Open Repository setup to enable it.',
    }.get(reason, 'Open Repository setup to check the repository’s access and rules.')


def github_rule(details):
    """A push GitHub refused by repository rule, worded for the operator, or None.

    Older environment gates carry only the worker's report, so its text counts too."""
    from ..factory.contracts import github_rule_kind
    details = details if isinstance(details, dict) else {}
    text = ' '.join(str(details.get(key) or '') for key in ('summary', 'detail'))
    rule = github_rule_kind(text)
    if details.get('reason') != 'github_permission' and rule is None:
        return None
    if rule == 'workflow_permission':
        return {'code': 'github_permission', 'headline': "GitHub won't let Forge change workflow files in this repository",
                'plain': 'GitHub reports that the Forge app lacks the Workflows permission needed for this change. '
                         'Review that permission with a repository administrator, or remove the workflow change from the issue. '
                         'Recheck the issue after the decision.'}
    if rule == 'signed_commits':
        return {'code': 'github_permission', 'headline': "GitHub requires signed commits for this change",
                'plain': 'GitHub rejected commits that do not satisfy the repository signing rule. '
                         'Review the signing requirement and how Forge publishes commits with a repository administrator, then recheck the issue.'}
    return {'code': 'github_permission', 'headline': "GitHub rejected the change under this repository's rules",
            'plain': 'GitHub refused the change because a repository rule or App permission was not satisfied. '
                     'Read the reported rejection with a repository administrator to identify the requirement, then recheck the issue.'}


def describe_gate(gate):
    """Common wording for a gate, including its typed dependency distinction."""
    details = json.loads(gate['details_json'])
    if gate['code'] == 'review_source' and details.get('reason') == 'review_unavailable':
        role = 'Audit' if details.get('role') == 'audit' else 'Review'
        cause = REVIEW_CAUSES.get(details.get('cause'), 'The worker could not finish this revision.')
        retry = ('Forge will retry this revision.' if details.get('retry_at') else
                 'Retries are exhausted. Update the pull request or ask the operator to investigate.')
        return f'{role} is deferred. {cause} {retry}'
    if gate['code'] == 'forge_problem' and details.get('source') == 'execution':
        return 'Forge could not complete the worker execution; waiting for a qualified runner update. No issue attempt was charged'
    if gate['code'] == 'forge_problem' and details.get('source') == 'scheduling':
        if details.get('reason') == 'mergeability_unknown':
            return 'GitHub has not established whether this pull request can merge; Forge is checking again'
        return ('Forge has no scheduled next step; recheck to resume scheduling. No issue attempt was charged'
                if details.get('reason') == 'stalled' else
                'Forge is reconciling an uncertain GitHub action; no further sends are scheduled until its outcome is known')
    if gate['code'] == 'dependency' and details.get('reason') == 'prerequisite_output_pending':
        return 'Waiting for accepted prerequisite outputs'
    if gate['code'] == 'membership_decision' and details.get('reason') == 'requirements_revision_changed':
        return 'Waiting for approval of the updated issue requirements'
    if gate['code'] == 'environment' and (rule := github_rule(details.get('value', details))):
        return rule['headline']
    return describe(gate['code'])
