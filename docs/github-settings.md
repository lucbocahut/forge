# GitHub setup and repository access

New factories use `forge install` from the operator's computer. It registers
**one Forge GitHub App**, installs it on the named repositories and configures
operator identity using the same App for GitHub sign-in fallback. Follow the
[installation guide](cli-fresh-host.md) for the complete flow.

This page explains the resulting access configuration and existing factories.
Older installations may still have four role Apps and a separate sign-in App;
those connections remain distinct until migrated.

Forge separates three decisions: GitHub grants repository access, the settings
sync discovers it, and `factory.json` records which repositories the operator
wants to automate. Discovery never enables a repository automatically.

The `/settings` page provides App connection forms, a repository access matrix,
activation controls, sync timing, and a JSON editor. UI edits and agent edits use
the same SQLite-backed configuration documents. Saves validate the complete
configuration and reject stale revisions rather than overwriting another editor's changes.

## Start disabled

The settings component requires Python 3.10+, OpenSSL, and outbound HTTPS to
`api.github.com`. It uses the Python standard library. Repository discovery
requires no personal administrator token or additional Python package. The complete installation also stores the App's own client
secret for GitHub sign-in fallback; personal administration credentials stay local.

A new installation has no connections or enabled repositories. The default sync interval
is five minutes; access observations expire after fifteen minutes. A settings GET
reads local data only. Refresh requests run in a background thread. Each installation sync
has a 90-second deadline, with up to 15 seconds for its final in-flight HTTP call;
large or inconsistent paginated responses fail rather than becoming empty lists.

The settings UI is the operator UI's `/settings` page. Host setup installs the
UI and the loopback status API it reads, with `FORGE_ROOT` set to the private
runtime root and its selected SQLite store; see [UI deployment](ui-deployment.md). Keep
the UI on a trusted private network. Settings reads and mutations require an
[operator session](operator-sign-in.md). Mutations also require same-origin JSON
and the session's CSRF token.

## Create and install the Forge App

Use the manifest handoff printed by `forge install`. It supplies the App's name,
permissions, private visibility and callback URLs. Approve it in GitHub and install
it on the repositories listed by the CLI, including the smoke and publication
qualification repositories. GitHub may ask for a passkey. The CLI converts the
manifest locally and sends only the App's credentials to Forge through SSH stdin.

The one App has these repository grants; Metadata read is implicit:

| Permission | Forge App |
| --- | --- |
| Contents | Read & write |
| Issues | Read & write |
| Pull requests | Read & write |
| Checks | Read & write |
| Administration | Read |
| Workflows | Write only when selected repository policy requires it |

Administration write is not requested. Repository rules are observed read-only.
The CLI keeps personal GitHub credentials local for approved App access changes.
An App installation grants access; it does not enable repository automation.

Build, Review, Audit and Controller remain separate roles using this shared
connection. Independent workers produce Review and Audit verdicts. The controller
publishes their checks and performs publication and merge under the verified
policy. Keep App keys and personal credentials outside worker sandboxes.

Workflow edits also require explicit per-repository permission. New installation
intents default `allow_workflow_edits` to false. Granting the App Workflows write
for one selected repository does not authorize edits in another repository whose
policy denies them.

## Repository rules and first merge

The default merge-check scope is **all pull requests**. Human-authored PRs receive
Forge's independent Review and Audit too. A repository can instead select
Forge-created PRs only. The supervisor enforces technical checks, CI and any
required human approval before Forge requests a merge of the exact candidate.

Setup does not create rulesets, require GitHub approvals or grant a bypass.
Private repositories do not need a paid plan for Forge's own merge policy.
Existing owner and organization rules still apply at GitHub's merge endpoint;
Forge reports rejected merges without modifying those rules.

The CLI verifies signed publication with a separate private probe repository.
Installation work stays limited to the smoke issue until accepted triage, Build,
Review, Audit, cleanup and the exact GitHub-confirmed merge are recorded. Handover
then applies selected policies and enables the selected repositories. Creating an
App or passing a host check alone does not establish this live acceptance.

## Existing four-App installations

Existing factories can retain Builder, Reviewer, Auditor and Controller Apps.
Their role grants are:

| Permission | Builder | Reviewer | Auditor | Controller |
| --- | --- | --- | --- | --- |
| Contents | Read & write | Read | Read | Read & write |
| Issues | Read & write | Read | Read | Read & write |
| Pull requests | Read & write | Read & write | Read & write | Read & write |
| Checks | None | Read & write | Read & write | Read & write |
| Administration | None | None | None | Read |

For such a factory, grant each configured App access to the same repositories,
then refresh access in Forge. The separate sign-in App identifies operators.
Do not remove old App registrations merely because a newer CLI is installed;
follow the migration procedure for the existing factory. Fresh installations use
the single App above.

## Private key references

The setup flow stores the App's private key in the installation's credential store
(`<root>/credentials/`, mode 0700, each key 0600) under the reference
`FORGE_SETUP_APP_KEY` for a fresh single-App installation. Legacy role Apps use
`FORGE_SETUP_<ROLE>_KEY`. Configuration holds only the reference name. Forge reads
App keys from the credential store only, never from the process environment.
Runners do not inherit App private keys or the controller's environment. Reconnecting
an App in Settings replaces its stored key; the next sync uses it.

The fresh installer writes the shared connection for every role. For an existing
installation, Settings accepts App ID, installation ID and the corresponding
credential reference for each configured connection. Save connections, then click
**Refresh access**. The sync authenticates each App, verifies its installation identity and grants, and
creates a metadata-only installation token to list accessible repositories. Tokens
and JWTs exist only in memory; error responses omit GitHub response bodies and
private key paths. No repository, App installation, label, or protection is changed.

An example connection entry, using placeholder identifiers:

```json
"build": {
  "app_id": 123456,
  "installation_id": 234567,
  "private_key_env": "FORGE_SETUP_APP_KEY"
}
```

## Enable, reconcile, dispatch

Each discovered repository initially shows Enable. All four roles must have a
fresh, successful observation, the required grants, and access to the same numeric
repository identity. Archived or disabled repositories are ineligible. The Enable
dialog explains that eligible issues may be processed after execution setup and
initial reconciliation. Enabling stores the repository ID, current name, and
`enabled: true` in `factory.json`.

Repository renames or transfers require updating the configured name after checking
the identity and access. A newly created repository with the old name must not
inherit activation from a different numeric ID.

The supervisor's snapshot and capacity/assignment gates consult this access state.
An enabled repository with missing, expired, suspended, or unverified access cannot
start new work. Failed syncs preserve intent and the last known inventory but do
not authorize dispatch from stale facts. Existing issue history is retained.
The `factory.json` configuration document is authoritative for repository
activation. App keys remain on the host; publication and access mediation stay
outside worker sandboxes. See [implementation status](implementation.md).

## Sync without the dashboard

These maintenance commands run on an existing host:

```sh
bin/forge settings sync --root ./local
bin/forge settings watch --root ./local
```

`watch` syncs at startup and on the configured interval. The `forge-status` API
also runs this loop. A shared file lock prevents concurrent syncs from duplicating API work.
The one-shot `sync` command exits nonzero when configured access cannot be verified;
exit code 2 means another sync already owns the lock.
A crash leaves an interrupted indicator; restarting or clicking Refresh retries
the observation. Configuration changes invalidate observations for affected App
connections immediately. Failed refreshes never remove configured repositories.

## References

- [Register a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app)
- [Install your GitHub App](https://docs.github.com/en/apps/using-github-apps/installing-your-own-github-app)
- [Manage private keys](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps)
- [List installation repositories](https://docs.github.com/en/rest/apps/installations#list-repositories-accessible-to-the-app-installation)

## Durable factory integration

The controller and resettable runners are implemented in `forge/factory`; see
[factory runtime](factory-runtime.md) for image/native-sandbox provisioning, the
three-failure budget and approval enforcement. Controller
App permissions now also include **Administration: read** for branch-protection
verification. This does not grant administration writes.

For repositories across accounts, configure the Forge App for installation on the
relevant accounts and install it on each account. A legacy factory must install
each of its configured role Apps. Enter comma-separated
installation IDs in Settings, or use `installation_ids: [101, 202]` instead of
`installation_id` in a connection. Forge maps repository IDs to their installation
before minting a scoped token. Changing that mapping requires a fresh sync. A failed
installation observation conservatively pauses the role until access is known.

`runtime.json` starts dispatch and merge disabled and is editable in Settings.
Authorized human logins and the pilot repository still need real operator values;
examples deliberately contain no private identities, keys or enabled repositories.
