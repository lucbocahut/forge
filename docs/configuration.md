# Configuration reference: environment variables and credentials

`bin/forge setup` and `bin/forge update` write every variable a standard install needs into the service units. Set one by hand only to override a default, and expect `bin/forge update` to flag drop-ins that override the managed ones (see `forge/host_update.py`).

## Credentials

GitHub App keys and coding-provider logins live in **the credential store** at `<root>/credentials/` (mode 700). Onboarding and Settings write to it. Role App keys (`FORGE_SETUP_<ROLE>_KEY`) and terminal logins (`FORGE_SETUP_*`) are read from the store only; the process environment never supplies them (`Credentials.environment()` in `forge/credentials.py`; asserted by `tests/test_credentials.py`). A stored provider API key (each harness manifest's `api_key_env` / `token_env`) takes precedence over the same name in the process environment.

Configuration refers to stored credentials by name (`private_key_env`, `provider_auth_file_env`, `seccomp_profile_env`). The values themselves never appear in `factory.json`.

## Operator-facing variables

| Variable | Read by | Default | Purpose |
|---|---|---|---|
| `FORGE_ROOT` | `forge/status_api.py`, UI help route | `~/.local/share/forge` for the API unless set by setup, `/app` in the UI container | Runtime root: state, configuration, credentials |
| `FORGE_BIND_ADDRESS` | `forge/status_api.py` | none; must be `127.0.0.1` | Status API listen address. The API refuses to start otherwise |
| `FORGE_PORT` | `forge/status_api.py` | `8787` in code; setup writes `8788` | Status API port (the UI takes 8787) |
| `FORGE_UPSTREAM_SECRET_FILE` | `forge/status_api.py` | `<root>/state/ui-upstream.secret` | Secret proving requests came through the UI proxy |
| `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_API_KEY`, `XAI_API_KEY` | harness adapters, via the credential store | unset | Coding-provider credentials (store wins; see above) |
| `SAFE_BUILD_TIMEOUT_SECONDS` | `forge/factory/build_environment.py` | `1200` (max `2400`) | Bounded safe-build step timeout |

## Operator UI (Nuxt) variables

Set by `bin/forge update` on the `forge-ui` container.

| Variable | Default | Purpose |
|---|---|---|
| `NITRO_FORGE_API` | `http://127.0.0.1:8788` | Status API the UI proxies to |
| `NITRO_FORGE_UPSTREAM_SECRET_FILE` | `/run/forge/upstream.secret` | Same secret as `FORGE_UPSTREAM_SECRET_FILE`, mounted read-only |
| `NITRO_FORGE_PUBLIC_ORIGIN`, `NITRO_ALLOWED_HOSTS` | unset | Public HTTPS origin for operator sign-in and App manifests |
| `NITRO_HELP_DOC` | `../docs/onboarding.md` | Help page source |
| `NUXT_PUBLIC_POLL_INTERVAL` | `5000` | Dashboard poll interval (ms) |

## Internal variables

Forge sets these itself. Do not set them by hand.

| Variable | Set by | Purpose |
|---|---|---|
| `FORGE_CREDENTIAL_ROOT` | `Credentials.environment()` | Lets the runner locate the store and `runner-seccomp.json` |
| `FORGE_RUNNER_SECCOMP` | `forge/factory/config.py` default `seccomp_profile_env` | Seccomp profile path; falls back to `<root>/runner-seccomp.json` |
| `FORGE_EGRESS_IMAGE`, `FORGE_EGRESS_USER` | `forge/host_update.py` | Egress proxy build inputs |
| `FORGE_ASSIGNMENT`, `FORGE_RESULT`, `FORGE_HARNESS_AUTH`, `FORGE_AUTH_DIGEST`, `FORGE_PROBE_SECRET`, `GH_TOKEN` | runner → worker container | Per-job assignment, result path, provider login and scoped GitHub token |
| `FORGE_REQUIRED_SERVICES` | runner (full tier) | Services `forge-services` starts for a job; see `docs/job-environments.md` |
