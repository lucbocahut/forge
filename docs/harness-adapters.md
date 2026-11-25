# Coding harness adapter contract v1

An adapter is trusted Forge installation code under `forge/harnesses/<id>/`.
It is reviewed and baked into an immutable runner image. A repository or model
response cannot install an adapter or supply a Python module path. There is no
runtime plugin downloader.

Each module contains:

- `manifest.json`: contract version, stable ID, display name, pinned package
  version, package binary, official terminal login arguments, relative credential
  file location, private credential reference, and API-key environment variable.
- `adapter.py`: `run(note, directory, env, result_schema)` and
  `probe(note, directory)` functions.
- Any adapter-specific canary or managed sandbox policy. Policies are image-owned
  and cannot be weakened by repository configuration.

The build installer supports two pinned distributions: an npm package at an exact
version (`package`, `binary`), or a Linux binary per architecture
(`distribution: "binary"`, `binaries.x86_64` and `binaries.aarch64`, each with
`url` and `sha256`) downloaded at build time from `https://x.ai/cli/` and verified
against its manifest checksum before it is placed in the image. A build on an
architecture the manifest does not list fails with a message naming the harness.
Never run an unpinned remote installer in a worker.

`run` receives a supervisor-owned assignment, the prepared checkout, and only that
execution's credential environment. It invokes the harness with native isolation,
explicit model selection when present, and structured output matching the supplied
schema. It atomically publishes at most 64 KiB to `/work/result.json` and returns
an exit code. A partial output or zero exit alone never advances the issue.

Availability errors must emit a bounded JSON event with `type: "error"` and a
normalized message identifying authentication or quota failure. Never print raw
credentials or provider envelopes. Ordinary execution failures retain their
distinct failure/investigation path. Provider output is not trusted GitHub proof.

`probe` must verify a real provider response and native tool isolation, including
denial of provider files/environment and writes outside the checkout. Only then
publish `{"ready": true, "attempt": "<assignment-id>"}`. A missing sandbox fails
closed; there is no bypass-permissions fallback.

The shared runner owns assignment persistence, credential injection, heartbeat,
deadline, process-group termination, container identity, forced teardown, credential
refresh preservation and capacity release. The supervisor owns issue state,
publication reconciliation, failure accounting, and the durable provider/model
round-robin cursor. These behaviors must not be duplicated inside adapters.

Before enabling a new module, qualify:

1. Structured outputs for all four roles and malformed/oversized output rejection.
2. Native shell and file-tool isolation, including malicious repository settings.
3. Authentication/quota fallback without spending an issue failure attempt.
4. Timeout, crash, detached children, interrupted login, reset and clean reuse.
5. Refresh persistence, reconnect races and no credentials in logs or source.
6. Explicit model selection and deterministic rotation across controller restarts.
7. Assigned GitHub credentials remain available to repository tools while provider
   credentials stay isolated, and those tools can reach GitHub through the proxy.
   Claude's setup probe verifies this using a synthetic token and a public endpoint.

### Codex incomplete responses

Codex is pinned to 0.163.0. Its upstream fixes treat `response.incomplete`
(such as `max_output_tokens`) as a terminal provider response instead of a
network disconnect, and retain reported token usage. Earlier versions could
retry the same exhausted response and discard its usage. See
[terminal incomplete responses](https://github.com/openai/codex/pull/53451) and
[incomplete response accounting](https://github.com/openai/codex/pull/53670).

This update does not raise provider limits or guarantee that a previously failing
assignment will succeed. Forge continues to require a complete validated result.
When no model is configured, Codex selects from its account's available model
catalog; a pinned CLI version alone does not pin the model or reasoning default.
Image updates still need the native sandbox and authenticated setup checks before
work resumes. The upgrade retains device sign-in, file-backed credentials and
Forge's managed permission profile.

Grok Build (`forge/harnesses/grok`, pinned 1.0.34) ships with the image and appears
in setup like Codex and Claude Code. Its terminal login is `grok login --device-auth`;
the API-key path uses `XAI_API_KEY`. It is dispatched only once `setup check` passes
its authenticated sandbox probe, which fails closed when the strict sandbox, the
private-file denial or the structured result cannot be verified.

### Grok device sign-in

Run `bin/forge setup login grok --root <runtime-root>` as the Forge service account.
Forge runs the pinned Grok CLI inside its installed runner image. Bubblewrap is
already in that image; installing it on the host does not repair the login container.

Before browser authorization, Forge checks that the image contains Grok and
bubblewrap and can create the native sandbox under the installed runner seccomp
and AppArmor policies. The check has no network access or credentials. A failed
check gives the exact `bin/forge update --root ...` command to repair the image or
policies before retrying. API-key entry and importing a login file do not need this
device-login check; normal authenticated `setup check` verification still applies.

The sign-in container uses the same namespace policies and bubblewrap path as the
Grok worker, with a read-only root, no added capabilities, and only a temporary
private login directory mounted. The managed Grok deny rules remain enforced.
After sign-in, run `bin/forge setup check --root <runtime-root>` to verify the account
and its native tool isolation before Forge dispatches work to it.
