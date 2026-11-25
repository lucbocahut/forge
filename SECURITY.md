# Security

## Report a vulnerability

Report vulnerabilities privately through GitHub: open the repository's
**Security** tab and choose **Report a vulnerability**, or go directly to
[the private report form](https://github.com/lucbocahut/forge/security/advisories/new).
Do not open a public issue for a vulnerability.

Do not publish exploit details, credentials, private source or raw worker output.
A private report should identify the affected Forge revision, deployment type,
impact and minimal reproduction. Use synthetic data and redact credentials.

## Supported boundary

Forge is experimental operator software. The supported installer targets a
dedicated Linux host. The service account, Docker daemon, host kernel and
administrators are trusted. Docker workers share the host kernel: this is **not
VM isolation** and is not a claim that arbitrary hostile workloads cannot escape.
Do not share this host with workloads that require a separate trust boundary.

Fresh installations register one Forge GitHub App. Its private key and GitHub
installation tokens stay on the host. Jobs receive source through a read-only
mirror and return artifacts; the host publishes changes and merges approved
candidates. “No GitHub credentials in jobs” does **not** mean “no credentials of
any kind in jobs”: provider authentication depends on the selected account mode.

## Runtime trust boundaries

| Boundary | Enforcement and limits |
| --- | --- |
| Operator → web app | Operational reads and browser mutations require an authorized operator session, so operator sign-in must be configured first. Identity comes from GitHub or a linked Tailscale user through the trusted Serve/UI proxy. HTTPS, host validation, session expiry and action tokens protect browser actions. The factory name is public sign-in metadata. |
| UI → Python API | Loopback API, private upstream secret, restricted forwarded headers and routes. App keys and provider credentials are not mounted into the UI. The host service account can cross this boundary. |
| External agent → Forge | Explicit operator approval, repository/capability scope, expiring credentials and revocation. Personal GitHub administration credentials stay in the agent's local CLI. These grants do not give direct host or worker control. |
| Installation CLI → host | Explicit administrative SSH destination, verified host identity and saved installation intent. App credentials are transferred privately; personal GitHub administration tokens remain local. This authority is broader than an external agent connection. |
| Controller → worker | Disposable workspace, unprivileged UID, read-only root, dropped capabilities, no-new-privileges, namespaces, seccomp/AppArmor and resource limits. A read-only job-input directory is mounted; the Docker socket, controller state and App credentials are not. Trusted frontends bridge narrowly scoped host services. |
| Worker → Git mirror | Job-bound authority, pinned source revisions, current installation access and a repository deny list. Declared dependencies and permitted organization repositories can be fetched on demand; a deny entry overrides those allowances. Requests and outcomes are associated with the job. The mirror does not accept pushes. |
| Worker → publication | Workers return retained artifacts, not GitHub writes. The host validates assignment, target ownership, expected revision, publication permissions and signing qualification before publishing. Workflow edits require the repository's explicit permission and App grant. Uncertain writes are reconciled rather than assumed successful. |
| Worker → coding provider | Claude API-key mode uses a host proxy: the worker gets a job capability restricted by model, route, account generation, execution ownership and deadline. Other modes, including browser logins, still provide authentication to the harness inside the container and depend on the qualified native tool sandbox and credential scrubbing. Do not assume the proxy covers every provider or login mode. |
| Worker → network | Internal per-job network and controlled frontends/egress. The egress proxy restricts HTTPS destinations and rejects private/non-global addresses. An allowed service can still receive data sent by a worker; an allowlist is not a data-loss prevention system. |
| Controller → GitHub merge | The supervisor requires current Review/Audit evidence and applicable operator approval before its own merges. It observes existing GitHub protections and checks the expected head. Forge does not create a mandatory GitHub ruleset or govern merges performed outside Forge. GitHub App grants remain broader than an individual issue or branch. |

Older configurations may retain separate role Apps or a separate sign-in App.
Those compatibility paths do not change the job credential boundary.

Relevant implementation: [worker boundary](forge/factory/worker_boundary.py),
[runner](forge/factory/runner.py), [mirror policy](forge/mirror/policy.py),
[publisher](forge/factory/publisher.py), [provider mode selection](forge/provider_runtime.py),
[UI proxy](ui/server/utils/proxy.ts), [API admission](forge/status_api.py)
and [operator identity](docs/operator-sign-in.md).

## Isolation status

The mechanisms above are implemented contracts, not a claim that every host,
provider or repository has passed live qualification. Setup checks the installed
runner and provider; publication requires evidence for its current target and
App context. Synthetic tests and sandbox canaries do not prove a complete
production issue-to-merge journey.

Resource admission, timeouts and memory limits reduce resource exhaustion. They
do not isolate credentials or establish authorization. Do not weaken ownership,
current-revision, human-approval or cleanup checks to increase throughput.

## Storage, retention and recovery

Persistent state uses one selected SQLite database with WAL and
`synchronous=FULL`, private directories/files and a marker binding its location
and instance. Managed launchers qualify the SQLite runtime and reject missing
or incompatible selected state. There is no JSON runtime fallback. Long-lived
App/provider credentials remain private referenced files outside the database;
session hashes, CSRF values and pending OAuth verifiers are private database state.

Back up the database using a consistent SQLite backup, together with its private
instance metadata and referenced credential files. Copying only the main file
while WAL writers run is not a coherent backup. Disposable workspaces and VM
snapshots are not substitutes for those backups. See [upgrades and recovery](docs/store-migration.md).

Events and structured results are retained. Eligible raw logs expire after
30 days; active work, current evidence and pinned recovery/cleanup references
are retained. Reset preserves lifecycle and audit history. Artifacts remain
sensitive after best-effort redaction. Never replace the selected store with an
empty one while workers or GitHub publications remain unreconciled.
