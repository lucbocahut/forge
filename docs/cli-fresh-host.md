# Install Forge from your computer

The supported complete flow is `forge install`. It saves an interview, checks the
host and repositories, registers one Forge GitHub App, connects a coding account,
and runs a smoke issue through Triage, Build, Review, Audit and Merge. Only a
GitHub-confirmed merge allows the final repository handover.

## Prerequisites

- A dedicated x86-64 Ubuntu 24.04 VM with AppArmor, systemd and `/usr/bin/python3`.
- SSH access from your computer as root or an account with noninteractive sudo.
  Configure its SSH key through your VM provider. Forge handles remote commands
  and file transfers through the CLI.
- Local Python 3.10+, Git, OpenSSH, authenticated GitHub CLI (`gh`) and an unlocked
  macOS Keychain or Linux Secret Service.
- Your computer joined to the intended Tailscale network. Enable MagicDNS and
  HTTPS certificates in that network.
- Authority to register a GitHub App under the chosen owner and administer the
  selected repositories. GitHub Pro is not required. Forge respects existing
  repository rules and reports any merge blockers.

## Install the CLI and start

```sh
uv tool install 'git+https://github.com/lucbocahut/forge.git'
forge self-update
forge install --ssh anvil
```

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first if needed.
If your shell cannot find `forge`, run `uv tool update-shell` and open a new terminal.
`anvil` is an example of an SSH alias; a `user@hostname` destination also works.

The default interview asks for the factory name, GitHub owner, repositories and
coding account, plus SSH host identity. Choose repositories and providers by
number; you can also type a repository name if it is not listed. All selected
repositories must belong to the chosen GitHub owner.

Review one editable summary before preflight. Defaults are your GitHub account
as operator, Codex browser sign-in, approval before project merges, workflow
edits off, and a private repository for the first-merge test. **Advanced** offers
additional operators, network hostname, merge policies, explicit workflow-write
permission, other login methods and a different test repository. No passwords
or tokens belong in this interview.

For an unfamiliar SSH host, verify its ED25519 fingerprint with your VM provider
before entering it. Leaving it blank uses your existing trusted SSH keys; it does
not trust a newly discovered host. Preflight checks the plan before the final
Apply confirmation. The deterministic intent-file interface is unchanged.

The default smoke target creates two private repositories under the selected
owner: one for the smoke issue, one for signed-publication qualification. Their
names and ownership markers are bound to this installation. They remain available
for diagnosis after handover, with automation disabled. Choosing a real repository
instead creates a small documentation smoke issue there; its merge policy and
any independent operator holds still apply.

After preflight passes, the CLI prepares these GitHub targets, installs the host,
and configures private HTTPS, the App and operator identity. It then opens
**Installation progress** in Forge and hands the approved plan to the server.
Only after the CLI says **You can close this terminal** is the handoff confirmed.
The server prepares the worker environment while
checking up to three repositories in parallel and waiting for Codex sign-in.
Publication checks and the first-merge test start after their prerequisites pass.

### Browser steps

The CLI prints the exact browser actions as they become available. Host and
network preparation still run before GitHub App consent; this is not an
all-consent-before-installation flow:

1. Approve the VM joining Tailscale if it needs authorization.
2. Open the local GitHub handoff, create the single Forge App, and install it on
   the selected repositories, including the disposable repositories.
3. In Forge's Installation progress page, choose **Connect Codex**. Open the
   displayed sign-in URL and enter its one-time code. Forge detects completion
   automatically; an expired attempt offers a new sign-in. Other providers and
   API-key or token logins use the CLI's existing private input flow.

Keep the CLI running while using its local GitHub callback page. GitHub may ask
for a passkey. If GitHub explicitly reports a missing manifest and no App was
created, return to the same local handoff and use its confirmation to resend that
same manifest. Do not start another installation. An unknown App conversion
outcome remains pending for reconciliation.

Manifest conversion happens locally. Only the App's own credentials are sent to
Forge through SSH stdin. Personal GitHub administration credentials remain local;
no secret belongs in an intent file, command argument or chat message. The same
App provides GitHub sign-in fallback. The installing operator's observed Tailscale
login is linked to their immutable GitHub ID, then verified through HTTPS.

## Resume and inspect

```sh
forge install --ssh anvil --status
forge install --ssh anvil --status --refresh
forge install --ssh anvil --resume
```

`--status` reads saved local progress without contacting the host. Add `--refresh`
to read server preparation and completion, reconcile the journal and produce the
handover report. Resume keeps the original installation and first-merge timer.
After handoff, it observes server work without repeating host bootstrap. If the
approved plan allows local GitHub administration, resume repairs only repositories
the server identifies as needing it. Personal GitHub credentials stay local.

Human CLI output returns the web destination after handoff, including when
`--wait` was supplied. Agents can use `--json --wait SECONDS` to follow the server
through the first merge. Neither closing the terminal nor reaching that timeout
cancels server preparation. The web page reports blockers and offers explicit
Continue or Retry actions after you resolve them.

If a Codex device login expires, explicitly start a replacement:

```sh
forge account login codex --ssh anvil --retry --json
forge install --ssh anvil --resume --wait 3600
```

For other providers, the existing `forge account login PROVIDER --ssh HOST` guided
login remains available. API keys and long-lived tokens use a hidden prompt or
`--secret-stdin`; the intent contains only the selected provider and login kind.

## Deterministic agent operations

Use the same flow with a non-secret JSON intent:

```sh
forge install --ssh anvil --intent-file install.json --preflight --json
forge install --ssh anvil --apply --wait 3600 --json
forge install --ssh anvil --resume --wait 3600 --json
forge install --ssh anvil --status --json
forge commands --json
```

JSON mode never opens a browser or prompts. Browser actions and progress events
are JSON records on stderr; the final structured result is on stdout. The local
GitHub receiver stays available during its bounded consent wait. The agent can
present its URL while the command is running. Only findings explicitly marked
waivable accept `--waive FINDING_ID`.

Example intent:

```json
{
  "version": 1,
  "ssh": "anvil",
  "name": "Anvil",
  "revision": null,
  "host_key": null,
  "hostname": "anvil",
  "owner": "YOUR_GITHUB_LOGIN",
  "operators": ["YOUR_GITHUB_LOGIN"],
  "repositories": [
    {"name": "OWNER/REPO", "merge_policy": "human", "allow_workflow_edits": false}
  ],
  "providers": [{"id": "codex", "kind": "browser"}],
  "smoke_target": "scratch"
}
```

`revision: null` selects the latest complete published prepared release once. `host_key: null` uses existing SSH trust.
An optional positive `smoke_issue` selects an existing issue in a selected real
smoke repository; otherwise Forge creates its own smoke issue. Workflow-edit
permission is enforced per repository even when another selected repository
requires the App's workflow permission.

## What completion proves

`installed` means the host is ready. `installation_complete` requires all of:

- Current App access and branch-rule checks, including preservation of signing
  requirements and stronger existing protections.
- A qualified database worker image when repository scans require database services.
- A signed-publication probe under the selected App and current target rules.
- Only the smoke issue admitted for execution while installation is pending.
- Real Triage, Build, Review and Audit evidence, owned-worker cleanup, a confirmed
  merge effect, and an independent GitHub read of the exact PR head, merge and
  closed issue.
- An atomic handover applying selected merge/workflow policies and enabling the
  selected repositories. Independent human approval holds remain authoritative.

The CLI prints a handover summary and saves a separate private JSON report with
stage timings, App and installation IDs, repository policies and observed rules,
provider status, the exact smoke merge evidence and time to first merge. It also
prints the Forge URL and `forge connect` command. The original installation
journal remains available; JSON output keeps `report_path` for that journal and
adds `handover_report` and `handover_report_path` for the summary.

The report records the client and server revisions observed after handover
separately from the requested installation revision, so an installation repaired
with a newer release does not appear to have run entirely on its original pin.
Missing version, worker image, harness or historical rule-change evidence is
explicit. `forge install --status` reads local evidence without contacting the
server. Resuming an older completed installation creates its report once without
repeating setup. A failed final version read leaves the server revision unknown.

Stage durations and time to first merge include interruptions. Completion does
not certify a clean installation, a human-touch count or early consent; those
still require measurement in an actual run.

## Clean reinstall on a test VM

A reset is a separate, explicit CLI operation. Read its plan before applying:

```sh
forge install --ssh anvil --reset --json
forge install --ssh anvil --reset --apply --reset-revision PLAN_DIGEST --json
```

Repeat the same apply command while `reset_pending` is returned. It fences and
drains owned work and waits for uncertain GitHub effects. It then stops verified
Forge services, removes the dedicated service account and archives its source,
runtime, history and remaining credentials in root-only directories. It archives
matching local setup and consent journals after remote completion is confirmed.

SSH, Tailscale, Ubuntu packages, Docker images and GitHub objects remain. This is
a clean **Forge reinstall on the existing OS**. Provisioning or rebuilding the VM
itself remains a provider operation. After `reset_complete`, run `forge install`
again. Old GitHub App registrations remain visible and can be retired separately.

Setup does not create or repair GitHub rulesets. The supervisor enforces Forge's
merge requirements, and GitHub enforces any rules chosen by the repository owner.
Saved installation intents from earlier versions can still resume; historical
protection and rules-repair choices no longer authorize rule changes. Existing
Forge-created rulesets require an explicit owner cleanup decision and are never
deleted automatically during an upgrade or reinstall.

## Existing installations and individual stages

Use [the upgrade guide](store-migration.md) for an installed factory. Existing
`forge setup plan/apply/status/resume`, repository, account and qualification
commands remain available as deterministic building blocks. An interactive
`forge setup new` without explicit stage options enters the complete interview.
The older identity/role-App commands remain for existing installations; new
installations use the single-App flow above.
