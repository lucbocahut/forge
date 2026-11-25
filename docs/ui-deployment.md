# Operator UI deployment

`bin/forge setup` and `bin/forge update` install and update the operator UI.
There is no manual build or switch-over step. See
[Update Forge](installation.md#update-forge).

## Layout

| Service | Listens on | Runs |
|---|---|---|
| `forge-ui` | `<bind>:8787` | The Nitro build of `ui/`, in the pinned Node image (read-only, non-root, host network) |
| `forge-status` | `127.0.0.1:8788` | `forge.status_api`, the JSON API and the GitHub App callback |
| `forge-supervisor` | — | `bin/forge service` |

The status API accepts only requests carrying the secret in
`<runtime-root>/state/ui-upstream.secret` (0600, created by setup). The UI container
mounts it read-only and sends it on every proxied request, so another local process
or account cannot read the control token or act as the operator. The API refuses to
bind anything but loopback.

The UI proxies `/api/**` and `/onboarding/github/callback` to the status API (see
`ui/README.md`). It forwards the host name the browser used, after checking it, so
that GitHub App manifests send their callbacks back through the UI.

Releases live in `<runtime-root>/ui/releases/<digest>/`. Each contains the
self-contained `.output` bundle and the Help guides (the top-level `docs/*.md`,
read through `FORGE_ROOT=/app`).
`<runtime-root>/ui/current` points at the release in use. Icons are bundled
(`icon: { serverBundle: 'local' }`), so the UI fetches nothing at runtime.

Nitro answers a proxied request whose `Host` is neither `$HOST:$PORT` nor loopback
with 403 `Untrusted host`, as a DNS-rebinding defence. Reaching the UI under
another name, such as a MagicDNS name or a reverse proxy, needs
`NITRO_ALLOWED_HOSTS` (comma-separated `host:port` values). Add it with a systemd
drop-in (`systemctl --user edit forge-ui`), which update never overwrites.

## Moving a hand-built host to managed services

Hosts that followed the earlier manual runbook have their own `forge-ui.service`
and a `forge-status.service` edited for port 8788. Run `bin/forge update` once.
It shows each difference against the managed definition and asks before replacing
it. After that, update owns these units.

## Status polling and freshness

The status service caches `/api/factory` and `/api/issues` views in memory.
Conditional requests compare a validator derived from runtime-file modification
times and sizes, query arguments, the operator's action token, and a short clock
bucket. An unchanged request returns 304 before rebuilding or serializing the
view. Authentication and reset checks still run on every request. A service
restart invalidates validators; time-derived fields refresh within ten seconds.

Concurrent polls share view builds. Inventory refresh starts at most once every
30 seconds per process unless an operator explicitly requests a refresh. Status
reads do not rewrite operator-request or issue-action stores. The UI proxy uses
gzip for large status JSON when the client accepts it, and sets
`Vary: Accept-Encoding`. Hidden tabs suspend automatic and Check now polling;
returning to the tab triggers a fresh read.
