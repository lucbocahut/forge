# Installation and host operations

## New installations: start from your computer

Use the complete CLI flow on a dedicated Ubuntu 24.04 VM:

```sh
uv tool install 'git+https://github.com/XRPL-Commons/forge.git'
forge self-update
forge install --ssh anvil
```

The [installation guide](cli-fresh-host.md) lists prerequisites and walks through
preflight, private HTTPS, one Forge GitHub App, operator identity, coding-account
login and repository qualification. The CLI performs remote commands and file
transfers. The same App supports GitHub sign-in fallback. Database requirements
trigger qualification of the [full job environment](job-environments.md).

Choose the GitHub App owner to match your repositories: personal repositories
need an App owned by your personal account. The wizard creates a private App,
which can only be installed on its owner's account. Private repositories on
GitHub Free can use Forge without enabling branch protection. The supervisor
enforces Forge's Review, Audit, CI and approval checks before its own merges.

Preflight reports progress while checking each repository and the host. If it
finds a prerequisite to fix, it stops before installation starts. To change the
saved choices or select a newer Forge release at this point, run
`forge install --ssh anvil --replace-plan`. Once an installation stage has
started, continue it with `--resume`.

Once signed in, open **Installation progress** from the setup banner in Forge.
The page shows completed steps, the current task, elapsed time, the last progress
update, and any action needed. It links to the first test issue and its pull
request. Repository setup links to this page when installation still blocks
activation, instead of offering an Enable button that cannot succeed.

After bootstrap and operator sign-in setup, the CLI hands the approved preparation
plan to the server and opens this page. The server prepares the environment while
checking repositories in parallel. Choose **Connect Codex** to request a one-time
code in the web page; Forge detects completed sign-in automatically. You can close
the terminal and browser, then return to see progress. The server retains each
step and reports what needs your attention. A failed planning attempt offers an
explicit retry. A proposal that needs a decision remains blocked until resolved
in Issues. The server verifies the exact merged test and selected repository
policies before enabling automation. The web controls cannot add repositories or
expand the GitHub permissions approved during bootstrap.

Host installation, private networking and the initial privileged GitHub setup
still start through the CLI. The web page does not replace those bootstrap steps.
The CLI remains available for unattended use and recovery. After handoff, it
observes the saved server operation without repeating bootstrap. Setup observes
existing GitHub rules without changing them; personal administrator credentials
stay local. Agents can follow automated work through the
first merge using `--json --wait SECONDS`.

The first smoke issue must complete Triage, Build, Review, Audit and a verified
merge before Forge enables the selected repositories. A successful host build
alone is not installation completion. Resume with:

```sh
forge install --ssh anvil --resume
forge install --ssh anvil --status --refresh
```

When smoke planning stops with `smoke_triage_failed`, fix the reported provider,
runner or planning problem first. Then use the exact failed proposal ID from the
result to request another planning cycle:

```sh
forge install --ssh anvil --resume --retry-smoke FAILED_PROPOSAL_ID --wait 3600 --json
```

This command keeps the same installation, issue and first-merge timer. It requires
the current failed proposal and completed worker cleanup. Repeating the command
reuses its successor proposal; it does not reset that successor's attempt limit.
Ordinary `--resume` never retries an exhausted proposal automatically. Agents can
read the structured recovery command in the pending stage's `next_action.command`.

For a clean Forge reinstall, use the CLI's explicit
[reset plan and apply operation](cli-fresh-host.md#clean-reinstall-on-a-test-vm).
It archives the owned installation and preserves SSH, Tailscale, the OS and
GitHub registrations. This does not rebuild the VM's operating system.

## Existing or manually managed hosts

The remaining commands are the low-level host operations used for maintenance
or manually managed installations. They run on the host as indicated; they are
not extra steps required after `forge install`.

Use an AppArmor-enabled Linux VM (Ubuntu 24.04 is the tested host) with Python
3.10+, Git, Docker Engine with the Compose plugin, and systemd. Run Forge as a dedicated non-root service account with Docker
access. Follow [Docker's installation instructions](https://docs.docker.com/engine/install/)
for your distribution. Docker access is administrative authority; never give it
to a worker.

As an administrator, create the account, give it Docker access, and enable
lingering so its user services start at boot and survive logout. Setup refuses
to run without that user service manager. Then install the runner's AppArmor
profile from the reviewed source, since the service account normally has no sudo:

```sh
cat /sys/module/apparmor/parameters/enabled   # must print Y
sudo useradd --create-home --shell /bin/bash forge
sudo usermod -aG docker forge
sudo loginctl enable-linger forge
sudo -iu forge git clone https://github.com/XRPL-Commons/forge.git
sudo install -m 0644 ~forge/forge/deploy/runner/forge-runner-private-proc.apparmor /etc/apparmor.d/forge-runner-private-proc
sudo apparmor_parser -r /etc/apparmor.d/forge-runner-private-proc
```

If the account may use sudo, you can skip the last two commands: setup installs
the profile itself and prompts for the password. Otherwise, when a later update
changes the profile, setup stops and prints the same two commands to run.

Then, as the service account (`sudo -iu forge` is fine):

```sh
cd ~/forge
bin/forge setup initialize
bin/forge setup
```

`bin/forge setup initialize` creates the private SQLite store every Forge service
runs on; the services refuse to start without it.

`bin/forge setup install` prepares the host without offering coding-account logins.
Use a published prepared release: setup downloads its checksum-verified worker
image and UI, then qualifies the worker on this host. See
[prepared releases](prepared-releases.md) for the publication and development-build paths.

Keep this checkout in place: the installed services run its reviewed source.
Setup loads the pinned runner image containing Codex, Claude Code and Grok Build, installs
the runner's isolation policy and private egress network, stages the prepared operator UI,
and starts the UI, the status API and the supervisor as user
services. The host needs no Node toolchain. It does not start issue processing or
alter GitHub permissions. To run `systemctl --user` or `journalctl --user` yourself
after `sudo -iu forge`, first `export XDG_RUNTIME_DIR=/run/user/$(id -u)`.

The image also owns the build environment: Node 22, npm, Corepack (with pnpm
10.23.0 preloaded), Python 3.12, a C/C++ compiler and Chromium. You do not install
project tools on the host. Before declaring installation successful, setup runs
an offline canary in the native tool sandbox: real Node memory readings, package
manager execution, a bounded build, a browser page, a Git commit, and protection
of private credentials and agent configuration. A failure stops installation
before services or the success marker are written.

Each checkout has disposable disk-backed temporary and package-cache directories.
Public package archives are also reused across jobs through the host-owned
[read-only package cache](package-cache.md); registry metadata remains live.
Workers install the repository's locked dependencies inside their native sandbox;
Forge does not execute repository installation scripts in the credential-bearing
parent process. Corepack honors a project's declared package-manager version;
versions not preloaded require allowed registry access. Browser tests can use
`PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` or `CHROME_BIN` to select image Chromium.

Inside a worker, `safe-build COMMAND` serializes builds, limits Node's heap to
60% of the container's finite memory budget, and kills the process group after
the deadline (20 minutes by default, at most 40). The container cgroup remains
the hard resource limit; the host admission check reserves memory before launch.
Projects
needing another runtime or more memory stop with an environment blocker; Forge
does not bypass isolation or treat missing infrastructure as a code defect.

## Update an existing host

```sh
bin/forge update
```

Update fast-forwards this checkout to its upstream branch, then converges the host
to it. It refuses a checkout with local changes or diverged history; use
`bin/forge update --no-pull` to apply the checkout exactly as it is (after your own
`git pull`, or to test a branch).

Each part is compared with what the source wants, and only changed parts are replaced
and restarted:

- **Runner image.** Selected from the release manifest by its image digest, so an
  image already present is reused. A new image must pass the offline canary described above before
  any runtime configuration points at it. The switch happens between supervisor
  cycles. When coding accounts are connected, update then runs `setup check`
  against the new image, and new work waits for that check. Workers already
  running finish on the image they started with.
- **Isolation policies.** The seccomp and AppArmor policies are replaced only when
  their sources change. Installing AppArmor uses sudo; without it, setup prints the commands for an administrator (see above).
- **Operator UI.** Downloaded into a content-addressed release under
  `<runtime-root>/ui/releases/`. `ui/current` switches atomically, and the previous
  release is kept.
- **Services.** `forge-status`, `forge-ui` and `forge-supervisor` are rewritten
  when their definition changes, and restarted when their unit, the source revision
  or the UI release changes. Update then waits for the API and the UI to answer.

Drop-ins (`<service>.service.d/*.conf`) for settings such as
`NITRO_ALLOWED_HOSTS` or resource limits are kept. A drop-in that replaces what a
service runs (`ExecStart`, `WorkingDirectory`, or `PYTHONPATH`, `FORGE_ROOT`,
`HOST`, `PORT` and the other addresses) would silently undo an update. Update shows
it and, once you confirm, moves it to `<runtime-root>/backups/`.
A unit or AppArmor policy edited outside Forge is never replaced silently. Update
shows the difference and asks, or stops unless you pass `--yes`. A failed build or
canary leaves everything in use unchanged. An interrupted update converges when
rerun.

Use a compatible forward update. The installation keeps its history and update
records in SQLite; switching back to an older incompatible source is unsupported. Schema changes use [`forge migrate upgrade`](store-migration.md).
`bin/forge setup install` on an installed host performs the same reconciliation
without fetching.

An environment blocker preserves the published PR, stage and code-failure budget.
Deploying a different image or resource configuration permits a new readiness
check. After repairing access or connectivity without changing the image, use
**Recheck readiness** on the affected issue. A repeated blocker in the same
unchanged environment stays paused. Genuine test failures remain code failures.

The controller supplies workers with bounded CI check summaries and annotations
for the exact candidate head and base. Worker credentials remain narrowly scoped;
they do not gain Actions-log or check-publication permissions. Missing CI evidence
is explicit, and snapshots never replace GitHub's current merge requirements.

The legacy host setup wizard offers each coding account in turn. Each login runs the installed
harness's official login command in a temporary container, without any repository
or GitHub credentials. Follow the link/code instructions in your terminal using
your browser. Skip any account if desired. Forge privately stores the resulting
login under the service account's runtime directory; no secret belongs in Git,
command arguments, or browser onboarding.

Setup then runs a small real model request for each configured model, verifies
native tool isolation, and checks termination and runner reuse. Provider charges
may apply. An unavailable account is excluded while other verified accounts remain usable.
If none pass, the factory stays stopped. Cleanup intent is always retained.

For a manually managed installation, configure [operator sign-in](operator-sign-in.md)
before opening browser onboarding. The local [`forge server setup` command](cli-server-setup.md)
configures sign-in and private HTTPS on an existing Tailscale host. Sign in at
the configured HTTPS origin and open `/onboarding` to configure GitHub access,
choose repositories, verify repository protection, and start Forge.

Port 8787 is served by the Nuxt/Nitro operator UI (`ui/`). It proxies the Python
status API, which listens only on `127.0.0.1:8788`. Update keeps the `--bind`
address you chose at setup; pass `--bind` to `bin/forge update` to change it. See
[the operator UI deployment notes](ui-deployment.md).

## Resume or reconnect

The default private runtime is `~/.local/share/forge`. Use `--root` consistently
if choosing a different directory. It must be owned by the service account and
have mode 0700. Existing host settings and credentials are preserved when setup
is repeated. The UI reset returns the existing installation to setup and retains
its history. The clean CLI reinstall procedure above is a separate operation.

```sh
bin/forge setup initialize              # Create the SQLite store (first install only)
bin/forge setup install                 # Prepare or converge the host without interactive login
bin/forge setup login codex             # Device-code login
bin/forge setup login claude            # Claude Code's browser/code login
bin/forge setup check                   # Verify all configured harnesses
bin/forge setup status                  # No credential values printed
```

Both login commands support `--api-key` to prompt privately instead of using an
account login, and `--import-login /absolute/path` for an existing login file.
Claude Code also supports `--token`: run `claude setup-token` on your own machine
(one-year token on your subscription, model requests only) and paste it at the
private prompt. A browser login is a rotating credential with a single writer,
so its jobs run one at a time; a token or API key never rotates, so workers share
it and as many run as the role's slots allow.
Import copies it; Forge never writes back to the source. Reconnect through Forge
after import so another application cannot rotate the same credential separately.

Sign-in can run while the factory is active. Forge saves the new login privately,
then waits up to two minutes for the current supervisor cycle before applying it.
If the supervisor stays busy or the terminal closes during this handoff, rerun
the same login command: Forge resumes the saved sign-in without opening another
browser login. A factory reset discards pending sign-ins from the old setup.

Repeat `--model <model-id>` on login to configure multiple models. Without it,
the harness chooses its default model. Provider/model entries have equal weight
in one durable round-robin sequence across all roles. Auth or quota failures
temporarily skip that harness and fall back to another without consuming the
issue's failure budget. When all entries are unavailable, issues wait.

After an active factory reconnects, that harness remains excluded from scheduling
until `setup check` succeeds. Verification drains running jobs before probing.
The `/settings/harnesses` page shows configured models, verification status, and
the appropriate terminal reconnect command.

`setup check` reports its current phase and elapsed time. On an active factory it
waits for existing workers to finish before using shared account credentials;
new dispatch pauses during verification. With the supervisor running, Ctrl+C
only stops watching: the check continues on the server. Run the same command to
reattach, or `setup status` to see what it is waiting for. The terminal stops
watching after 15 minutes without cancelling the saved operation.

Refreshable account logins have one active writer per credential reference. Forge
saves refreshed credentials before removing the runner and refuses stale writes
after a newer login. API-key and long-lived-token jobs can use the full configured
parallel capacity, and so can Codex jobs on an existing ChatGPT subscription: the
controller owns refresh, and workers get access-only snapshots. See the
[runtime guide](factory-runtime.md) for lifecycle and recovery details.
Losing a refresh during a crash can still require another login; cleanup and other
available harnesses continue independently.

For host requirements, isolation qualification, capacity and upgrades, see
[the runtime guide](factory-runtime.md). To add a harness, see
[the adapter contract](harness-adapters.md).

Official login references: [Codex authentication](https://learn.chatgpt.com/docs/auth)
and [Claude Code CLI](https://code.claude.com/docs/en/cli-reference).

## Factory name

Forge remains the main product name. The header and sign-in screen show the
deployment name chosen during `forge install` as a smaller secondary label,
for example **Anvil**. Browser titles include both: **Settings · Forge · Anvil**. Older factories without a saved display name show **Forge**.

After updating an existing Anvil installation using the standard service paths,
copy its original installation name into the runtime once:

```sh
ssh anvil 'sudo -u forge /home/forge/forge/bin/forge setup initialize --root /home/forge/.local/share/forge --name Anvil'
```

Use the original name from the approved installation, not an inferred hostname.
This preserves existing factory data. Repeating the same name has no effect;
a different name is refused. Reload the web page afterward.

## Check for updates

Open **Settings → General → Updates** to see the installed revision and the
latest complete published build from `XRPL-Commons/forge`. **Check for updates**
refreshes the result; release notes and the supported upgrade instructions are
linked there. Checking does not install anything or interrupt workers.

Checks use GitHub’s public API without credentials. The server caches results
for an hour, with a one-minute minimum between manual checks. Private repository
access, rate limits and network failures are reported as unavailable. Forge
compares commit ancestry before offering an update, so a development build is
not mistaken for an older release.
