"""Production construction of the row controllers for a selected installation."""
import time
import uuid

from ..credentials import Credentials
from ..settings import Settings
from ..store.executions import Executions, ExecutionQueries
from ..store.models import Command
from ..store.runtime import required
from ..store.transitions import manifest_hash
from .config import load, policy, validate
from .github import GitHub
from .runner import ContainerRunner
from .dispatch_controller import DispatchController, GitHubPrepare
from .execution_controller import ExecutionController
from .observation_controller import ObservationController
from .result_controller import ResultController
from .steward_controller import StewardController
from .verification_controller import VerificationController
from .requirements_controller import RuntimeRequirementsController
from .merge_controller import MergeAdmission, MergeController
from .effect_controller import EffectController
from .sqlite_supervisor import SqliteSupervisor


def components(root, *, config=None, runner=None, github=None, settings=None, clock=time.time, dispatch=True):
    store = required(root)
    settings = settings or Settings(root, clock=clock)
    config = validate(config if config is not None else load(root))
    if not dispatch or settings.paused():
        config = {**config, 'enabled': False, 'merge_enabled': False}
    runner = runner or ContainerRunner(config, environ=Credentials(root).environment())
    github = github or GitHub(settings, config, clock=clock)
    from .source_preparation import SourcePreparation
    sources = SourcePreparation(root, store, github=github, settings=settings, clock=clock)
    if isinstance(runner, ContainerRunner):
        runner.source_preparation = sources

    from ..install_scope import execution_allowed

    def admission(item):
        if not execution_allowed(store, item):
            return False
        repo = item.get('repo') or (item.get('issue') or {}).get('repository')
        return bool(repo and settings.admission(repo)['allowed'])

    from .repository_readiness import RepositoryReadiness
    readiness = RepositoryReadiness(store, settings, clock=clock)

    def execution_admission(item):
        if not execution_allowed(store, item):
            return False
        if item.get('review_only'):
            from .config import merge_check_scope
            return merge_check_scope(current_config(), item['repo_id']) == 'all' and admission(item)
        return admission(item) if item['role'] in {'recovery','steward'} else readiness.allowed(item)

    def current_config():
        current = load(root)
        if not dispatch or settings.paused():
            current = {**current, 'enabled': False, 'merge_enabled': False}
        return current

    requirements = RuntimeRequirementsController(store, github, config=config, runner=runner, clock=clock)
    verification = VerificationController(store, github, profiles=lambda repo: current_config()['repository_verification'].get(repo, []),
        policy=lambda item: policy(current_config(),item['repo_id'],repository_name=item['repo']), clock=clock, hold_labels=config['hold_labels'])
    merge_admission = MergeAdmission(store, github, config=current_config,
                                     repository_ready=lambda repo: readiness.allowed({'repo': repo}),
                                     review_repository_ready=lambda repo: settings.admission(repo)['allowed'], clock=clock)
    from ..store.scheduling import Scheduling
    reliability = config['reliability']
    scheduling = Scheduling(store, clock=clock, retry_seconds=reliability['retry_seconds'],
                            stall_seconds=reliability['stall_seconds'])
    effects = EffectController(store, github, admission=merge_admission, clock=clock, reliability=reliability)
    from .publisher_runtime import PublicationRuntime, SigningCapabilities
    publisher = PublicationRuntime(store, github, sources,
        signing_capability=SigningCapabilities(store, github),
        admission=lambda job: current_config()['enabled'] and execution_admission(job),
        clock=clock, reliability=reliability)
    parts = dict(store=store, settings=settings, config=config, runner=runner, github=github, publisher=publisher,
        requirements=requirements, verification=verification, effects=effects, admission=admission, scheduling=scheduling,
        execution=ExecutionController(store, runner, github, admission=execution_admission, clock=clock, enabled=lambda: current_config()['enabled']),
        observations=ObservationController(store, github, admission=admission, readiness=readiness, policy=lambda item: manifest_hash(policy(current_config(),item['repo_id'],repository_name=item['repo'])),
                                            hold_labels=config['hold_labels'], ready_label=config['ready_label'], clock=clock),
        results=ResultController(store, github, policy=lambda item: policy(current_config(),item['repo_id'],repository_name=item['repo']), verdicts=effects,
            requirements=requirements, clock=clock, hold_labels=config['hold_labels'],
            availability_retry_seconds=config['availability_retry_seconds'], scheduling=scheduling, publisher=publisher),
        recovery=StewardController(store, runner, github, config=config, admission=admission, clock=clock),
        merges=MergeController(store, admission=merge_admission, clock=clock, scheduling=scheduling),
        dispatch=DispatchController(store, runner, config=config, prepare=GitHubPrepare(github, config, requirements=requirements, readiness=readiness),
                                      admission=lambda item: execution_allowed(store, item) and (admission(item) if item.get('review_only') else readiness.allowed(item)),
                                      readiness=readiness, clock=clock))

    from .repository_planner import RepositoryPlanner
    from .triage_effects import TriageEffects
    from .steward_effect_controller import StewardEffectController
    def planner_runner(worker):
        # The setup service supplies a real runner configured for the default
        # provider. Each owned planner must use its immutable selected config.
        return ContainerRunner(worker, environ=runner.environ, source=runner.source_preparation) if isinstance(runner, ContainerRunner) else runner

    parts['planning'] = RepositoryPlanner(store, github, config=config, settings=settings,
        observations=parts['observations'], runner=planner_runner, clock=clock)
    parts['triage_effects'] = TriageEffects(store, github, config=config, clock=clock)
    parts['steward_effects'] = StewardEffectController(store, github, config=current_config, admission=admission, clock=clock)
    from .merge_checks import ReviewPullsController
    parts['review_pulls'] = ReviewPullsController(store, github, config=config, settings=settings, clock=clock)
    return parts

def operator_actions(root, actor_id, **kwargs):
    from .store_operator import OperatorActions
    parts = components(root, **kwargs)
    return OperatorActions(parts['store'], parts['github'], actor_id=actor_id, config=lambda: load(root),
                           requirements=parts['requirements'], verification=parts['verification'], clock=kwargs.get('clock', time.time))


def cycle(root, *, config=None, runner=None, github=None, settings=None, clock=time.time, dispatch=True):
    from .store_operator import OperatorActions
    from .store_inventory import DiscoveryController
    parts = components(root, config=config, runner=runner, github=github, settings=settings, clock=clock, dispatch=dispatch)
    Credentials(root).publish_login_generations(parts['store'], parts['config'])
    from .provider_logins import check_logins
    check_logins(root, parts['store'], parts['config'], clock=clock)
    actions = OperatorActions(parts['store'], parts['github'], config=lambda: load(root),
                              requirements=parts['requirements'], verification=parts['verification'], clock=clock)
    discovery = DiscoveryController(parts['store'], parts['settings'], parts['github'], config=parts['config'], clock=clock)
    class DeploymentObserver:
        def cycle(self, *, after=None, limit=50):
            from ..deployments import Deployments
            from .live import read_commit
            adapter = Deployments(root, runner=None, github=parts['github'], settings=parts['settings'], clock=clock)
            errors = adapter.observe(parts['github'], read_commit)
            return {'items': [{'repo': row['repo'], 'error': 'deployment_observation_unavailable'} for row in errors], 'next_cursor': None}
    supervisor = SqliteSupervisor(parts['store'], **{key: parts[key] for key in
        ('execution', 'observations', 'results', 'recovery', 'verification', 'merges', 'effects', 'dispatch', 'planning', 'triage_effects', 'steward_effects', 'review_pulls')},
        requirements=parts['requirements'], operators=actions, discovery=discovery, deployments=DeploymentObserver(),
        scheduling=parts['scheduling'], clock=clock, cycle_budget_seconds=parts['config']['cycle_budget_seconds'])
    return supervisor.cycle()


def cleanup(root, *, runner=None, github=None, clock=time.time):
    parts = components(root, runner=runner, github=github, clock=clock, dispatch=False)
    store = parts['store']
    with store.read_snapshot() as db:
        ids = [row[0] for row in db.execute('SELECT id FROM jobs WHERE cleanup=0 ORDER BY created_at,id LIMIT 100')]
    jobs, commands = ExecutionQueries(store), Executions(store, clock=clock)
    for identifier in ids:
        job = jobs.job(identifier)
        if not job['fenced']:
            commands.fence_execution(Command(str(uuid.uuid4()), 'setup:cleanup', job['unit_id'], job['unit_version']),
                                     identifier, execution_fence=job['execution_fence'], reason='setup_cleanup')
        parts['execution'].step(identifier)
    # Reconcile existing effects; no new merge intents or merge authorization.
    parts['effects'].cycle(limit=50)
    triage_pending = parts['planning'].cleanup()
    with store.read_snapshot() as db:
        return {'pending': db.execute('SELECT COUNT(*) FROM jobs WHERE cleanup=0').fetchone()[0] + triage_pending}
