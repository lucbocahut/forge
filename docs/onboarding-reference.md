# Set up Forge — reference

Detailed setup and repository protection reference. For the everyday walkthrough, see [Getting set up](onboarding.md).

Use the [guided installer](onboarding.md) for a new factory. It opens the web
installation page after bootstrap. Existing manual installations can continue
at `/onboarding`, with three steps: GitHub, Repository and Start.

1. **GitHub.** Register and install one Forge App through the guided flow.
   Select the repositories it may access, then refresh access in Forge.
2. **Repository.** Choose a repository and prepare it. Forge verifies App access
   and reads existing GitHub rules without changing them. Choose Forge's merge
   policy and whether Review and Audit cover all PRs or Forge-created PRs.
3. **Start.** Connect a coding account and follow the installation page's checks.
   Provider verification makes small model requests that may incur charges.
   Recent account and worker evidence can be reused when credentials and runtime
   settings have not changed. The page shows the smoke issue's progress through
   Build, Review, Audit and merge. Activation explicitly enables eligible work.

You own the App. Forge does not request a personal administrator token. GitHub
requires a human to approve each App registration and installation. App private
keys arrive through GitHub's manifest exchange and are stored as private files in
credentials/, with references in configuration. Installation IDs supplied by a
browser are never accepted as proof of access.

Forge polls GitHub rather than receiving webhooks. Its App manifest includes the
webhook URL required by GitHub's schema, with delivery disabled and no subscribed
events. If registration fails, return to /onboarding and press Connect again;
reopening the GitHub registration URL alone does not resend the manifest form.

## Maintain GitHub access

Open GitHub access from onboarding or settings (/settings/github). This operator
view shows the Forge App connection and a list of repositories. Each
repository is Not enabled, Enabled, or Needs attention. Choose Enable to open its
setup checklist, or Resolve to repair an enabled repository. Add repository opens
the same wizard with a searchable repository picker.

The wizard keeps repository access, Forge merge policy and final activation
together. Manage GitHub access opens the App installation settings. Refresh
access after granting another repository. New repositories reuse the existing App.

Only incomplete steps ask for action. Enable automation confirms activation;
checking access or leaving the page never enables a repository. Activation
requires current repository identity, App access and readable rule observations.
GitHub's plan restriction on private repository rules does not block setup.

Checks expire after five minutes or when the relevant App connection or
repository identity changes. Forge refreshes these observations before starting
work. Resolving an access or observation failure releases its setup hold while
preserving explicit holds, failure budgets and human approval waits.

## GitHub repository protection

Forge's supervisor governs merges performed by Forge. It requires current
independent Review and Audit evidence, CI, any required human approval and the
exact candidate head and base. A changed candidate invalidates earlier evidence.
Forge does not require a GitHub ruleset, a native approval count, stale-approval
dismissal or a bypass grant as a setup prerequisite. Private repositories can be
used without a GitHub Pro subscription.

The repository's `human` merge policy requires an eligible operator approval;
`auto` allows the supervisor to merge after the other gates pass. Review and
Audit cover all PRs by default, with a Forge-created PRs option. That scope
controls Forge's review work; it does not prevent people from merging on GitHub.

Existing owner and organization protections remain in force. Forge reads them
without changing them, and GitHub makes the final decision on each merge request.
If GitHub rejects a merge, Forge shows the reason on the affected PR. The owner
can satisfy the rule or change their policy independently of Forge setup.

Setup does not generate or import rulesets, add bypass actors, or repair owner
rules. Upgrading also does not delete rulesets created by an older Forge version;
removing those is an explicit owner decision.

## Deployment profile

Once a repository is enabled, Forge reads it once to propose a deployment
profile: how it deploys, and how to tell a running commit apart from the
source. The Repository page shows this as a "Deployment" card — the strategy
and deploy workflow Forge found, a domain box prefilled with its candidate
when it found one, and where the running commit is exposed (an existing
health field, a response header, or not exposed). The domain box is the only
thing you type.

Confirm accepts the profile. Confirming is the only moment it is written to
repository settings and observation begins; nothing before that point reads
the domain or files anything. Reject discards the proposal with a reason;
Forge does not read the repository again until you press Re-read deployment
or its deploy workflows change. Re-read deployment forces a fresh read
regardless.

When Forge finds no way to expose the running commit — no health field, no
response header, but the application does serve HTTP — confirming also files
an issue on the repository, labelled `forge:deployment`. That issue does not
get the ready label: Forge never queues it for Build. Add the ready label
yourself when you want it worked. A profile with no domain still observes
GitHub deployment statuses for the deploy environment; it just cannot
cross-check what is actually running.

## Host installation

`bin/forge setup` installs the host: runner image, isolation policies, egress
network, operator UI and services. `bin/forge update` applies newer releases. See
[installation](installation.md); there are no files to copy by hand.

Guided setup allows up to four Build, two Review, one Audit and one Recovery worker.
These are ceilings: how many actually run depends on available memory (see
[factory runtime](factory-runtime.md)), so a small host needs no tuning.

Connect or reconnect Codex in **Settings → Connections → Coding accounts**.
Choose Connect, open the OpenAI sign-in page and enter the displayed code. Forge
finishes sign-in and verifies Codex automatically. Closing the dialog does not
stop the login; return to the same page to resume it or request a fresh code
after expiry.

Other providers and API-key/token modes currently use the CLI. Connect from the VM terminal with
`bin/forge setup login codex --root <runtime>` or
`bin/forge setup login claude --root <runtime>`, then `bin/forge setup check` with
the same root. The UI labels those modes as CLI instructions before you open them.
Forge preserves refreshed installation-owned logins across runner cleanup; an
older runner cannot overwrite a newer login. External imported files remain untouched.
See [installation](installation.md) for API keys, model rotation and fresh VM setup.

## First work

After Start, the factory shows current supervisor activity and the next action.
A missing heartbeat is Waiting for the Forge service, not an empty queue. When
checks pass and no issues are queued, open the linked repository and add the
configured ready label to an issue you want Forge to handle. Forge does not
create a demonstration issue automatically.

After editing an issue's title or description, remove and reapply the ready
label to approve the new text. For an issue still in the backlog, you can also
review the current description and choose **Queue for Forge**.

The Issues page lists open work in enabled repositories, including issues that
have never been queued for Forge. Search or filter by repository and state; use
Completed to find closed work. Opening an issue shows its description, blocker,
prerequisite issue links, pull requests and recent activity inside Forge.

Choose Queue for Forge to request work. Return to backlog removes its ready label;
Pause adds an explicit hold. Release hold requires an operator reason and checks
current prerequisites before continuing. Setup holds lead to repository setup or
Recheck setup once verification passes. These actions preserve the failure budget.
Retry attempt is reserved for failed attempts and explicitly grants another budget.
Stop attempt warns that unfinished workspace changes will be discarded; published
pull requests remain. Active worker output stays available under Activity.

Viewing issues never queues them. Forge verifies each decision against current
GitHub state, access, rules, explicit local dependencies and existing work. Human
assignees, work-in-progress labels and linked external pull requests require review
before starting another attempt. Unavailable or incomplete observations are labeled;
stale rows cannot authorize decisions. Inventory refresh uses the Controller App's
existing access, without repository administration writes.

Earlier pre-first-build review-settings holds are recognized only by their exact
history, with no prior attempts or failures. They can recover after a fresh check;
explicit holds and existing human work remain paused. Completed issues use GitHub's
confirmed merge or closed state. Worker exit alone never counts as completion.

## Reset and restart

Settings → Reset Forge opens the single confirmation screen. Stop and reset Forge
fences scheduling first. The supervisor pauses open work, stops each recorded
container and verifies its removal, waits for pending GitHub effects, then retires
the active configuration, disables repositories, revokes agent connections and
clears Forge-owned credential copies. Lifecycle history stays in the SQLite store.
It preserves GitHub issues, commits, branches, PRs and App registrations, shared
external credentials, and unrelated host work.

The reset journal survives service restarts; an unverified teardown keeps reset
pending and scheduling closed. The dashboard remains available throughout. Old
reset intents cannot target a newer setup. Retained history does not feed the new
queue or failure budget. Remove old App registrations on GitHub separately.

Reset stops only Forge's own runner containers and discussion jobs. It does not
look for or stop tmux sessions or other processes, and it does not touch
checkouts that Forge did not provision as disposable runner workspaces.

## Configuration and recovery

setup/factory.json and setup/runtime.json are draft configuration domains in the
SQLite store. factory.json and runtime.json become active only when the supervisor
applies Start. Settings for repositories and worker capacity are available in
/settings. The store's setup instance holds the setup and reset journal. Do not
delete ownership records to bypass an unsuccessful cleanup.

If App registration succeeds but its one-use key exchange is lost, remove that
unused App on GitHub and reconnect the role. A stale callback cannot install an
App into a later setup. If access changes or checks expire, refresh and check
again. Setup checks expire after five minutes; Start independently rechecks
repository access, protection and runner prerequisites.

The production onboarding screen is part of the operator UI, a separate
`forge-ui` service on the tailnet address; `forge-status` serves only the JSON
API on localhost. See docs/ui-deployment.md.
