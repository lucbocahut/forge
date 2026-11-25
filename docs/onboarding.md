# Getting set up

Start a new factory from your computer with `forge install`. The CLI handles
remote installation, saves progress and prints each required browser action.
You do not need to SSH into the VM or copy configuration files yourself.

## Host installation

```sh
uv tool install 'git+https://github.com/lucbocahut/forge.git'
forge install --ssh anvil
```

`anvil` is an example SSH alias. See the [complete installation guide](cli-fresh-host.md)
for prerequisites, CLI installation, intent files and agent operations.

```mermaid
flowchart TD
  I[Interview and preflight] --> H[Prepare host and private HTTPS]
  H --> A[Register and install one Forge GitHub App]
  A --> C[Connect coding account and qualify repositories]
  C --> S[Run the selected smoke issue]
  S --> M[Verify Triage, Build, Review, Audit and Merge]
  M --> E[Enable selected repositories]
```

![Build, Review, Audit and Controller roles bring a repository to the Forge workbench.](assets/setup-hero.svg)

## What the CLI asks you to do

The interview records the GitHub owner, operators, repositories, merge policies,
coding accounts and smoke target. Review the preflight results and confirm Apply.
The CLI then guides the browser steps:

1. Approve the host joining your Tailscale network when required.
2. Create the Forge App from the supplied GitHub manifest and install it on the
   named repositories. GitHub may require your passkey.
3. Sign in to the selected coding provider using the displayed instructions.

Personal GitHub administrator credentials stay on your computer. The host stores
the Forge App's own credentials. The same App provides GitHub sign-in fallback;
the installing operator's observed Tailscale identity is linked to their GitHub ID.
Provider login is separate from operator sign-in and GitHub repository access.

Keep the CLI running during its local GitHub handoff. To inspect or resume:

```sh
forge install --ssh anvil --status
forge install --ssh anvil --resume --wait 3600
```

The saved installation and first-merge timer survive resume. Browser actions and
blockers are reported explicitly; resuming does not create a second installation.

## One App, independent workers

New installations register **one Forge App**. Build, Review, Audit and Controller
remain separate responsibilities; Review and Audit are produced by independent
workers. Their verdicts appear as `forge/review` and `forge/audit` checks, and
`forge/merge-policy` records the merge decision.

## GitHub repository protection

The default repository scope runs Forge Review and Audit for all pull
requests, including human-authored ones. A repository can instead select
Forge-created PRs only. Forge enforces its gates when its supervisor merges.
Setup does not require GitHub rulesets or a paid plan for private repositories.
Existing CI, signing rules and stronger protections remain
in force. Workflow-file edits are a separate repository permission, disabled by
default in the installation interview.

The CLI checks App access, branch rules, coding accounts, worker isolation and
signed publication. It qualifies the full database environment when repository
requirements need it. Provider verification makes real model calls and can consume
provider usage. See [GitHub setup](github-settings.md) for permissions and rules.

When memory is unavailable, verification shows a waiting state and resumes
automatically. Before starting a delayed check, Forge cleans up its unstarted
reservation and creates a fresh one with a full execution deadline. It preserves
the connected account. A check that already started keeps its original deadline;
an execution timeout is reported explicitly.

## The first merge and handover

```mermaid
flowchart TD
  T[Triage proposal] --> K{Unchanged singleton keep?}
  K -->|Yes| A[Accept through the normal API]
  K -->|No| O[Operator reviews the disposition]
  O --> D{Runnable disposition?}
  D -->|Yes| A
  D -->|No| W[Wait for resolution]
  W --> T
  A --> E[Confirm required effects]
  E --> B[Build the smoke issue]
  B --> R[Independent Review and Audit]
  R --> M[Verify exact GitHub merge and completed issue]
  M --> H[Hand over selected repositories]
```

While installation is pending, execution is limited to the exact smoke issue.
The default creates a private smoke repository and a separate publication-probe
repository. You can choose a real repository and optionally an existing issue.
These choices and their merge policies are part of the saved intent.

Triage must produce an accepted runnable disposition. An unchanged singleton
`keep` can be accepted under the installation consent through the normal
acceptance API. Questions, parking, closure and broader proposals require an
operator decision. Human merge approval and independent holds remain effective.

Completion requires real Build, Review and Audit evidence, worker cleanup, a
confirmed merge effect and an independent GitHub read of the exact merged PR and
completed issue. Only then does handover apply the selected repository policies
and enable the selected repositories. Disposable repositories remain available
for diagnosis with automation disabled.

A prepared host or passing automated tests do not establish that this live
installation acceptance has succeeded. The CLI reports the recorded proof and
time to first merge when the run completes.

## Configuration and recovery

For an active factory, use **Settings** to manage repository access, accounts and
configuration. Codex browser sign-in and reconnect are available under
**Settings → Connections → Coding accounts**. Other providers and API-key/token
modes currently use the CLI and are labeled accordingly.

**Settings → Repositories → Add repository** checks access and
rules before **Enable automation**. Checking or discovering a repository alone
does not enable it. Refresh expired checks when prompted.

![Repository setup checks access and rules before the operator enables automation.](assets/settings-screen.svg)

Older factories may retain four role Apps and a separate sign-in App. Their
existing setup commands remain available; creating a new installation uses the
single-App flow above. See the [GitHub reference](github-settings.md) and
[host operations reference](installation.md) for existing installations.

**Reset Forge** in Settings fences work and returns the existing installation to
setup while retaining its SQLite history. A clean Forge reinstall is a separate
CLI operation with an explicit resource plan:

```sh
forge install --ssh anvil --reset --json
forge install --ssh anvil --reset --apply --reset-revision PLAN_DIGEST --json
```

The CLI archives the owned Forge installation after cleanup. It preserves SSH,
Tailscale, the operating system and GitHub objects. Read the
[reset procedure](cli-fresh-host.md#clean-reinstall-on-a-test-vm) before applying.

Continue with [How an issue moves](issue-state-machine.md).
