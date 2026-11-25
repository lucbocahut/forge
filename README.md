# Forge

Forge turns GitHub issues into pull requests, with independent code review and
security audit before merge. Use the web app to choose repositories, approve
work and follow its progress.

**Experimental.** Start with a test repository. This repository contains the
server, workers, web app and CLI.

## Get started

You need:

- An x86-64 Ubuntu 24.04 server with SSH access and administrator privileges.
- A Tailscale account for private access to the factory.
- Permission to create a GitHub App and install it on your repositories.
- A supported coding account, such as Codex or Claude Code.
- On your computer: Python 3.10+, Git, authenticated GitHub CLI (`gh`), and
  [uv](https://docs.astral.sh/uv/getting-started/installation/).

Your server and coding provider may have costs. GitHub Pro is not required.
Mac hosting is not supported by this installer yet.

Install the CLI on your computer, then start setup:

```sh
uv tool install 'git+https://github.com/XRPL-Commons/forge.git'
forge self-update
forge install --ssh USER@HOST
```

Replace `USER@HOST` with your server's SSH address or alias. The CLI asks for the
factory name and GitHub owner, then lists repositories and coding accounts to
choose from. Review the summary, edit anything you need, and confirm installation.
Forge handles SSH and file transfers. Your approval is required before project
merges by default; customization is under **Advanced**.

Approve the Tailscale and GitHub browser prompts. Keep the terminal open until
setup says **You can close this terminal**. From there, **Installation progress** shows the
current step, anything waiting for you, and the first merge test. Codex sign-in
can continue in the web app; other provider setup may require the CLI.

Forge completes a test issue before enabling your chosen repositories. You can
close the web page and return while it runs.

If interrupted, resume the saved installation:

```sh
forge install --ssh USER@HOST --resume --wait 3600
```

If your shell cannot find `forge`, run `uv tool update-shell` and open a new
terminal. See [installation details and prerequisites](docs/cli-fresh-host.md)
for supported hosts, credential storage and troubleshooting.

## Use your factory

- **Activity:** see running, queued and blocked work, including pull request reviews.
- **Issues:** follow issues and their results.
- **Settings → Repositories:** add repositories and choose whether Forge needs
  your approval before merging. The default is to require approval.
- **Settings → Connections:** manage GitHub access, coding accounts and external agents.
- **Settings → General → Updates:** check published GitHub releases and open update instructions.

Label an issue `ready` to make it eligible for work. If you edit its title or
description, remove and reapply `ready` to approve the edit. To group related
issues, open **Plan repository work**, request triage and approve the proposed group.
Forge builds one pull request for the group and reviews it as a whole.

Forge enforces its own merge checks and respects existing GitHub rules. It does
not create branch protections or require a paid GitHub plan.

## Guides

- [Fresh installation](docs/cli-fresh-host.md)
- [Updates](docs/store-migration.md)
- [Connect an external agent](docs/agent-connections.md) — agents use the same CLI,
  with structured input and JSON output.
- [Repository planning](docs/repository-planning.md) and [merge policy](docs/factory-runtime.md#what-permits-a-merge)
- [Operations](docs/factory-runtime.md), [manual server installation](docs/installation.md)
  and [documentation index](docs/README.md)

## Development

The Python runtime uses the standard library. Run:

```sh
python3 -m unittest discover -s tests -v
```

The operator UI has its own unit and end-to-end suites, in `ui/`:

```sh
cd ui && npm ci && npm test && npm run test:e2e
```

`npm run test:e2e` drives a real Chromium through `playwright-core`; install
it first with `npx playwright-core install chromium`, or set
`FORGE_BROWSER_PATH` to an existing executable. Installing Chromium's system
dependencies may require administrator access. See
[`ui/README.md`](ui/README.md) for the harness and its environment variables.
GitHub Actions runs the Python suite and the `ui` job on pushes and pull
requests.

These checks use synthetic local fixtures and do not mutate GitHub. Optional systemd integration tests are documented separately and
must be enabled explicitly.

## Source layout

| Directory | Purpose |
| --- | --- |
| `forge/` | Setup/update, settings, the setup supervisor, and the durable factory (`forge/factory`) |
| `forge_client/` | Local CLI for installation, host maintenance and scoped agent connections |
| `ui/` | Operator UI (Nuxt) |
| `deploy/` | Runner image, isolation policies and deployment utilities |
| `docs/` | Operator and reference documentation ([index](docs/README.md)) |
| `tests/` | Deterministic backend and browser regression checks |

Read [CONTRIBUTING.md](CONTRIBUTING.md) before contributing and
[SECURITY.md](SECURITY.md) for security reporting and operational boundaries.

## License

MIT. See [LICENSE](LICENSE).
