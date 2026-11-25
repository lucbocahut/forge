# Operator sign-in

Operator sign-in is the authorization prerequisite for external agent connections
(#15, #122). It protects dashboard data and actions; external CLI connection
approval and scoped client tokens are described in [agent connections](agent-connections.md).

## Setup and identity

Start a fresh server with [`forge install`](cli-fresh-host.md). It registers one
Forge App, uses that App for GitHub sign-in fallback, and links the installing
operator's observed Tailscale identity to their authorized GitHub ID.

In Tailscale mode, the UI accepts identity only through its trusted local Serve
proxy: the loopback connection must be owned by root, as `tailscaled` is, so
another local account cannot present the identity header. The API checks the UI's upstream secret and the stored operator link;
client-supplied identity headers and tagged devices do not authorize an operator.
GitHub sign-in remains available when the Tailscale identity is absent or unlinked.
The service account and local proxy remain trusted administrative boundaries.

An older server with a separate identity App can continue using
[`forge server setup`](cli-server-setup.md). That command configures an existing
host; it is not the default fresh-install path. Sign-in is configured per factory,
not per repository. No personal GitHub administration token is uploaded to Forge.

## Sessions and recovery

GitHub sessions expire after eight hours and use Secure, HttpOnly, SameSite=Lax,
`__Host-` cookies. Sign-in state expires after ten minutes, is browser-bound and
consumed before code exchange. PKCE binds that exchange to the sign-in attempt.
GitHub identity tokens are used transiently for `/user`; Forge stores the
resulting identity and private session state, not that GitHub token.

Linked Tailscale sessions use eight-hour windows. Their derived tokens remain on
the server and cannot be presented as browser session cookies. Explicit sign-out
removes the active Tailscale link and revokes agent connections approved by that
operator; automatic browser-session expiry does not revoke those connections.
GitHub sign-in can establish the link again.

Identity, sessions, grants and audit history live in the selected SQLite store.
The sign-in client secret is a private, digest-checked credential file. Back up
both consistently; do not delete the database to recover access. The host account
is the administrative recovery boundary. The existing-server setup command
refuses to overwrite a different identity configuration.

Operational reads and browser mutations require an operator session. Complete
sign-in setup before accessing the dashboard or browser onboarding. Public
session metadata includes whether sign-in is enabled and the factory display
name. Invalid selected state fails closed.

## Request attribution

On the selected SQLite runtime, the API records mutation requests in append-only
`operator_audit` rows. A browser request has a durable `request_started` claim
before dispatch and one terminal `request_outcome`: **accepted**, **rejected**, or
**unknown** when interruption prevents a reliable result. A safely identified
operator's validation or CSRF rejection is attributed to the verified operator
and existing session row. Failed sign-in, invalid upstream requests and other
unverified requests do not acquire an operator identity. Requests refused by
Nitro or before the selected database can be opened are outside this audit.

Receipts contain the route, time, verified operator/session or client connection,
request identity, a validated target and expected revision where applicable,
and a coded reason. They exclude request/response bodies, free-form reasons,
cookies, OAuth codes, CSRF values, bearer tokens and exception text. Session
references are database row IDs, never raw session cookies.

Callers may send a UUID or 32-character lowercase hexadecimal
`X-Forge-Request-ID` and retain it for a retry. Existing body `request_id` fields
(and client operation `id`) take precedence. The response carries the caller
identity in the same header. Forge scopes it to the verified principal, client
connection and route; changing the body does not grant permission to reuse an
identity for another mutation. A repeated browser request receives HTTP 409 and
the prior receipt instead of rerunning the handler. A concurrent request can
report `in_progress`; an unfinished claim after interruption becomes `unknown`.
Reload the current state and reconcile any linked command before submitting a
new identity. A receipt's uniqueness does not promise exactly-once GitHub effects.

Client repository operations keep their existing durable operation ID and
immutable request binding. Repeating that operation returns its stored status;
its accepted request receipt is distinct from its eventual completion/failure
record. Lifecycle commands, independently reconciled GitHub effects and
configuration revisions retain their own durable identities. Audit command
links connect them to the initiating request. Configuration changes record only
before/after digests, domain and revision in the same transaction as the change;
background client operations retain the approved client attribution. Direct
local CLI/host configuration changes carry the actor `local:host` and have no
HTTP request receipt.

Reset preserves this history. One-way migration preserves legacy admission
records as `admitted`; it does not invent outcomes for them. The supported
runtime has no operational JSONL audit writer.

## Credential boundary

GitHub operator identity and approved Forge client credentials authorize actions
inside Forge. The external agent's local GitHub administrative identity stays in
its local CLI credential store. This change adds no endpoint accepting that
identity or its token. Workers receive source from the read-only Git mirror,
while the host holds GitHub publication authority. Provider authentication is a
separate boundary; see [Security](../SECURITY.md#runtime-trust-boundaries).

Protocol reference: [GitHub OAuth web application flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps#web-application-flow).

## Migrating an existing installation to the Forge App

After upgrading the installed release, use the CLI from your computer:

```sh
forge server identity plan --ssh HOST --source /absolute/forge/source --root /absolute/forge/runtime --service-user forge --json
```

The plan reports the installed App, permission and repository coverage blockers,
and a configuration revision. Accept any required permission changes on GitHub
before applying the App step with the returned revision:

```sh
forge server identity apps --ssh HOST --source /absolute/forge/source --root /absolute/forge/runtime --service-user forge --revision REVISION --json
```

This preserves retired App identities and runs a fresh repository protection
check. If the factory is paused or another refresh is running, the response shows
`protection_check: pending`; repeat the step once that condition clears. A
completed check can still report access or observation failures. It does not
require a Forge baseline or alter existing GitHub rules. Configure the
Forge App's sign-in callback, then run the `sign-in` step
with the same host options. It prompts privately for that App's client secret;
automation can use `--secret-stdin`. Secrets never belong in command arguments.
The `tailscale` step selects trusted Tailscale identity; `--mode github` selects
GitHub sign-in. Existing linked operators and trusted Serve ingress are required
for Tailscale access. Signing out revokes approved agent connections and removes
the automatic Tailscale link; an explicit GitHub sign-in restores it. The plan is read-only; each named mutation can be resumed
by repeating it. These commands do not edit GitHub permissions or uninstall old
Apps. Keep the old Apps until pending publications have been reconciled.
