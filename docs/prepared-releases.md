# Prepared release assets

New Forge releases distribute the built operator UI. The installer builds the
standard and full worker images on the operator's host from the exact release
source and pinned inputs. Worker binaries are not uploaded to GitHub. Provider
selection, credentials and host qualification keep their existing behavior.

Each release is named `forge-build-<full commit SHA>`. Manifest version 2 records:

- the exact source commit and Linux amd64 platform;
- pinned Node and Python base images;
- the UI archive's build fingerprint, size and SHA-256 checksum;
- `delivery: local-build` and the input fingerprint for each worker recipe.

The installer verifies these pins against the checked-out source before using
the UI or building workers. Build caches reuse matching local worker inputs.
Isolation and environment canaries still run before workers are enabled. A
missing UI download never silently starts a source build.

Version 1 manifests remain readable for existing published releases and pinned
recovery. Those releases contain worker archives; their checksums, platform and
image identities are still verified. Historical assets are not deleted by this
change.

## Publishing

The **Prepared UI release** workflow runs after **CI** succeeds for a push to this
repository's `main` branch. It checks out that exact commit, builds the UI with
the pinned Node container, builds both worker recipes and runs their isolation
and environment canaries. It then verifies that the UI archive can be consumed
through the installer's extraction path and contains dependency notices. Worker
images remain in the ephemeral CI job; no image export occurs.

The workflow uploads only `manifest.json` and `forge-ui-linux-amd64.tar.gz`, then
publishes the draft after both uploads succeed. Published releases are never
overwritten. A retry replaces an incomplete draft only when it belongs to
`github-actions[bot]` and names the exact tested commit. Human-owned drafts remain
untouched. The UI archive must fit GitHub's 2 GiB release asset limit.

### Manifest version

The current workflow rejects source revisions whose producer does not declare
manifest version 2. This stops an old CI completion event from using the new
workflow to publish version 1 artifacts.

### UI dependency notices

The UI archive includes `/third-party-notices.txt`. It records package names,
versions, license metadata, registry source URLs and integrity hashes, upstream
repositories, and the license/notice files from the exact installed build tree.
It conservatively includes build and test dependencies, since generated UI code
can contain assets from packages outside the runtime dependency set. Packages
without separate license text are listed explicitly with available README
information; a metadata label alone is not a substitute for a license notice.

Missing text for packages copied into the server output stops preparation.
Four small upstream license supplements cover Lucide, Embla, wheel-gestures and
vaul-vue, whose npm packages omit license files. Their package versions, upstream
commit URLs, evidence and file hashes are recorded under
`ui/third-party-licenses/`; changing a covered package version requires reviewing
that supplement. The Lucide text includes its retained Feather attribution.

ELK (`elkjs` 0.9.3, used by Mermaid) includes the EPL-2.0 license plus an explicit
source-availability statement. It links to the immutable elkjs source revision,
ELK0.9.1 source and additional PR955 patch identified in upstream's release
notes. No upstream source edits are made by Forge. DOMPurify includes its
MPL-2.0/Apache-2.0 choice and full license text.

The present built output contains Lucide icons and the elkjs server dependency;
no Embla component was found in the current generated chunks, so its supplements
also cover future use. Remaining packages with no standalone text are reported
as an installed-build superset, principally tooling. Absence of an identifiable
name in minified code alone is not proof of exclusion: the complete installed
notice inventory remains included, and package/version changes require renewed
bundle review. This records the actual evidence and preserved terms without
claiming comprehensive legal clearance from an inventory alone.

## Upgrading an older CLI

A version 1-only CLI expects all three prepared assets. Its existing
`forge self-update` can stay on an old release or fail to select a version 2
release; it cannot bootstrap support for the new manifest by itself.

First reinstall the CLI directly from an exact, published version 2-capable
Forge commit (replace `<FULL_COMMIT_SHA>` with that release's full commit):

```sh
uv tool install --force --refresh 'git+https://github.com/lucbocahut/forge.git@<FULL_COMMIT_SHA>'
```

Then use the upgraded `forge install` or update flow. Do this before removing
legacy release availability. The repository's fresh-install instructions already
use current source; this extra bootstrap is for existing older CLIs.

## Installation and private repository access

With no explicit revision, `forge install` and `forge self-update` choose the
latest complete published prepared release. An explicit full commit SHA remains
available for recovery and development. Wait for the Prepared UI release workflow
before installing a newly merged revision.

The CLI uses your local GitHub CLI login to read private release metadata and
obtain short-lived, read-only links for the UI. It sends those links over SSH;
your personal GitHub token stays local. Links stay in memory and are excluded
from journals, command arguments and normal output.

The host downloads verified files into a private cache at
`~/.cache/forge/prepared/<revision>`. Fresh installation stages this cache before
the Forge service account exists, then transfers verified files to that account.
Interrupted transfers do not become usable cache entries. Resume reuses valid
files and requests fresh links for unfinished transfers. Worker builds use the
existing pinned Docker recipes on the host and can reuse its local build cache.

Use the local CLI to install or update a private Forge release. A direct host
update requires an already staged cache or an explicitly selected source build;
it cannot fetch private releases using your personal credentials.

## Development builds

Maintainers can deliberately build all assets from a source checkout by setting
`FORGE_BUILD_FROM_SOURCE=1` for the host-side setup/update command. This remains
an explicit development choice, not a fallback for missing release assets.
ARM64 Linux hosts take this path automatically, because prepared releases are amd64-only.

To prepare release files on a clean Linux amd64 checkout with Docker, sudo and
AppArmor available:

```sh
python3 tools/build_release_artifacts.py --output /tmp/forge-prepared
sudo python3 tools/qualify_release_roundtrip.py --assets /tmp/forge-prepared
```

The destination must be empty. These commands build and verify files locally;
they do not publish a release or change GitHub permissions. The roundtrip tool
also retains legacy version 1 image-store qualification for maintainers examining
an existing release.
