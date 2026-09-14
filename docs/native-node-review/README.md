# Morpheus consumer-node changes for upstream review

This folder contains a normal Git patch showing the native node changes used by the Morpheus Consumer Gateway. Start with [the patch](node-v7.11.0.patch) and [file-change summary](diffstat.txt). The patch changes 18 files under `proxy-router`; most deletions remove host-management features or their dependency graph from our consumer-only build.

## Where the code lives

The maintained implementation is in `bowtiedbluefin/morpheus-consumer-gateway`, branch `fix/recovery-and-customer-reliability`. It is stored as a reproducible source patch recipe, not as a separate maintained fork of the entire upstream repository:

| File | Purpose |
| --- | --- |
| [deploy/node.Dockerfile](../../deploy/node.Dockerfile) | Downloads the exact upstream archive, checks its hash, applies both patch scripts, installs dependency locks, formats/tests Go, and builds the native binary. |
| [deploy/patch_node.py](../../deploy/patch_node.py) | Applies transaction-progress, operation-journal, collateral, wallet-locking and cleanup changes. Fails if expected upstream text does not match. |
| [deploy/native](../../deploy/native) | Added Go code/tests and the replacement `go.mod` / `go.sum`. |
| [deploy/consumer_node.py](../../deploy/consumer_node.py) | Removes IPFS and Docker host-management implementations/routes for the bundled consumer build. |
| [node_helper](../../node_helper) | Separate Python supervisor for rating-file changes, native restarts, readiness and authenticated management. It is outside the native Go patch. |

The local checkout at `research/Morpheus-Lumerin-Node` remains the upstream reference checkout. The modified Go source is assembled during Docker build. This exported patch makes that assembled source reviewable without reverse-engineering the Python replacement scripts.

## Exact baseline and release relationship

- Upstream repository: [MorpheusAIs/Morpheus-Lumerin-Node](https://github.com/MorpheusAIs/Morpheus-Lumerin-Node).
- Baseline: **v7.11.0**, commit **`99a8d86af1f9453d59797f9a208aa729ff3eb00d`**.
- Recipe snapshot: gateway commit **`e678eea`**; [manifest.json](manifest.json) records the full commit and patch hash.
- The native recipe is the one used by `bowtiedbluefin/morpheus-consumer-node:railway-20260914.1`. Later gateway/helper documentation and Blockscout-default corrections do not change the Go source in this patch.
- Native build uses Go **1.26.8**, `CGO_ENABLED=0`, and the Docker build tag through the upstream build script. The Dockerfile's dependency and runtime-package checks are part of the build recipe, not additions to upstream Go source.
- No Solidity contracts are modified by this patch.

## What changed and why

| Area | Native changes | Motivation / scope |
| --- | --- | --- |
| Session collateral | Refresh MOR supply and daily budget before the negotiated duration-based open; round collateral upward with 0.01% headroom. Add `maxStakeWei` to duration-open requests and enforce it against the calculated amount. | A requested 300-second session could otherwise round down to 299 seconds. The headroom is a bounded estimate, not a reservation against future supply/governance changes. |
| Durable operation tracking | Accept `X-Gateway-Operation`; create an exclusive operation journal under `GATEWAY_JOURNAL_PATH`; track stages, transaction hashes, session ID and HTTP completion for open/close/withdraw. Add progress fields to responses. | Help the gateway reconcile requests whose HTTP response is lost, and reject reuse of the same journaled operation ID. This does not establish unconditional exactly-once blockchain execution. |
| Wallet serialization | Add a process-local lock spanning wallet mutations, including approval plus opening, when gateway-managed mode is enabled. | Prevent this native process's managed operations from competing for the wallet. It does not coordinate independent processes/containers sharing the wallet. |
| Cleanup ownership | Keep startup chain rehydration, but disable the native expiry loop when `GATEWAY_JOURNAL_PATH` selects gateway-managed mode. | The gateway then owns durable expiry cleanup/reconciliation. Starting managed mode without its gateway would leave that responsibility unserved. |
| Capability discovery | Add `GatewayCapabilities` to configuration responses with `stake-limit-v1`, `operation-journal-v1` and `transaction-progress-v1`. | Let the gateway detect support rather than silently assume request fields are enforced. |
| Consumer-only packaging | Remove IPFS file-management and Docker host-administration routes/implementations. | Reduce unused runtime dependencies in this distribution. These removals are not proposed as a general feature deletion from upstream. |
| Dependencies | Replace module locks, update cryptography/attestation-related dependencies, and migrate wallet HD derivation import to maintained `btcd/btcutil`. | Address the audit's dependency findings while retaining wallet, remote inference and TEE verification. |

The Python gateway owns model policies, provider allow/block lists, request queues, dead-session cleanup, reconciliation and eligible MOR withdrawal scheduling. The Python helper owns native process restart/configuration. Those systems are not contained in this Go patch; review their implementations in the gateway repository for the full behavior.

## Apply to a disposable upstream checkout

Save `node-v7.11.0.patch` beside the new checkout below, then run:

```sh
git clone https://github.com/MorpheusAIs/Morpheus-Lumerin-Node.git morpheus-node-review
cd morpheus-node-review
git switch --detach 99a8d86af1f9453d59797f9a208aa729ff3eb00d
git switch -c review/consumer-gateway
git apply --check ../node-v7.11.0.patch
git apply ../node-v7.11.0.patch
git diff --stat
git diff -- proxy-router/internal
```

The patch includes newly added files as well as edits/deletions. After applying it, use `git status --short` to see the added files too; ordinary `git diff` omits untracked new files. To include them in the review diff, run `git add -N proxy-router`, then `git diff`.

Use a disposable checkout rather than the operational node directory containing a wallet `.env`. Applying this patch does not start the node, transfer funds or configure a wallet.

For the native tests used by the image build, from the patched checkout with Go 1.26.8:

```sh
cd proxy-router
go test ./internal/lib -run '^TestGateway' -count=1
go test ./internal/repositories/wallet ./internal/proxyapi -count=1
```

The complete packaged build and source vulnerability gate remain in `deploy/node.Dockerfile` in the gateway repository. The patch by itself does not package the Python management helper or reproduce that container's entrypoint.

## Evidence and review boundaries

This export was checked by applying it to a fresh archive of the exact upstream commit, then comparing every resulting `proxy-router` file's SHA-256 to the patched/formatted recipe output. The resulting trees matched. Exporting this review artifact did not rerun funded tests or rebuild the images.

Prior validation and its limits are documented in the [remediation report](../REMEDIATION-REPORT.md) and [Railway setup guide](../../railway_setup.md). They include native regression tests, funded session-duration checks, local crash/recovery history and the source/binary vulnerability distinction. The residual OpenPGP advisory and missing upstream attestation fixtures are disclosed there; this is not a claim of complete production acceptance.

For an upstream contribution, review the functional changes separately from the consumer-only feature removals and dependency reductions. In particular, agree on the public request/response/header contracts, cleanup ownership, behavior on journal-write failures after transaction submission, ambiguous RPC/broadcast outcomes, cancellation/locking semantics and collateral headroom. The current code is coupled to the gateway's reconciliation protocol; the entire distribution patch should not be treated as a ready-to-merge upstream PR.

## Sharing with a Morpheus developer

Send this folder's README, patch, diffstat and manifest. The gateway GitHub repository is private, so a link requires existing repository access; alternatively download and share these four review files. They contain source changes and public identifiers, not deployment `.env` files or wallet credentials. No upstream PR or developer message has been sent as part of creating this artifact.

Suggested message:

> We built a self-hosted consumer gateway against node v7.11.0 (`99a8d86`). This patch shows the native changes: collateral rounding/caps, operation journals and transaction progress, managed wallet serialization, and expiry-cleanup coordination. It also includes consumer-only dependency and host-management reductions, which should be reviewed separately from upstream functional changes. The README explains the build, tests and remaining limits. We'd like your feedback on which capabilities belong upstream so the gateway can eventually use an official node build.
