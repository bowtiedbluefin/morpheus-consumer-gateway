# Production audit remediation

**The eight findings from the September 13–14 audit have been addressed.** The fixes are implemented, regression tested and deployed to the local Compose installation. This clears the identified defects; it does not certify an untested public VM deployment or every marketplace provider. Native security finding PR-08 is resolved through dependency upgrades, removal of unused features, and a documented non-runtime residual advisory.

This report follows the [original audit](PRODUCTION-READINESS-REPORT.md), which remains an unchanged historical record apart from navigation updates. The implementation is in `fix/recovery-and-customer-reliability`, [PR #1](https://github.com/bowtiedbluefin/morpheus-consumer-gateway/pull/1). Local verification ran September 14, 2026 UTC. Sanitized measurements and artifact hashes are in [REMEDIATION-EVIDENCE.json](REMEDIATION-EVIDENCE.json).

## What changed for customers

| Finding | Fix and verification |
| --- | --- |
| PR-01: simultaneous first requests could hang the service | Completed opening tasks are removed by identity; an old callback cannot remove a replacement. Waiting yields to the event loop and opening waits share a bounded deadline. The regression and a 35,968-request cold-start HTTP run pass. |
| PR-02: a five-minute session became 299 seconds and failed | The native node refreshes supply/budget before calculating collateral, rounds upward, and includes 0.01% headroom. The existing stake cap applies to the final amount. Two funded sessions requested at 300 seconds both measured exactly 300 seconds on-chain. Deterministic pre-submission failures now back off, persist across restarts, and reset when policy changes; mined approval history is retained. |
| PR-03: malformed request extensions could lock MOR before failing | Strict JSON validation covers the entire request, including extensions: non-finite numbers, invalid Unicode, duplicate members, excessive nesting and oversized integers fail before opening. Boundary tests and live NaN/wrong-model checks confirm no escrow is opened. |
| PR-04: a declined extra provider slot failed callers despite an existing busy session | If an additional open definitively was not submitted, callers can wait within the queue limit for an eligible existing session. Uncertain wallet operations still block further opening. Controlled concurrent tests confirm both requests finish successfully using the healthy session. |
| PR-05: invalid provider replies were recorded as successful | Validate assistant messages, tool/function calls, usage and streamed events. Invalid responses become structured provider errors. Serialization succeeds before success is recorded. Valid reasoning text, null content with tool calls, and usage-only stream events remain supported. |
| PR-06: a temporary storage failure left readiness permanently unhealthy | Recovery performs a durable write/read probe and successful maintenance before clearing the storage error. Tests cover a recovered disk and a disk that remains unwritable. |
| PR-07: long balances broke phone layouts | Show compact balances with an expandable exact amount. Grid cells and activity details wrap safely. All seven screens pass at 390px with precise balances, long identifiers and long errors, across Chromium, Firefox and WebKit. |
| PR-08: native dependencies had known advisories | Upgrade Go and the native dependency lock; remove unused IPFS and Docker host-management implementations from the bundled consumer build. Preserve wallet, remote inference and TEE verification. Source scanning reports zero called or imported-package vulnerabilities. The remaining binary OpenPGP advisory is assessed below and guarded against at build time. |

The funded cleanup also exposed an additional failure: a node-control timeout could occur before a manual close request was saved. Close intent is now persisted as draining before that network check, so maintenance retries it after recovery. A regression verifies this behavior. The funded harness also retries close by the same session ID and revokes its temporary key even if cleanup raises an error.

The session calculation is a bounded estimate, not a guaranteed price reservation. Supply movement beyond the headroom, governance changes, provider capacity and RPC failures can still prevent opening. These produce classified errors/backoff; they must not trigger an unbounded stream of wallet transactions. No prompt is replayed automatically after inference begins.

## Tests on the repaired build

| Check | Result and scope |
| --- | --- |
| Backend suite | **153 passed** on macOS; **153 passed** in a Linux container constrained to 2 CPUs/1 GiB, UID 10001, no external network, dropped capabilities and no privilege escalation. Includes the eight formerly separate failing gates. |
| Browser suite | **21 passed:** seven each in Chromium, Firefox and WebKit. Includes all-screen mobile navigation with exact balances expanded and long error/activity payloads. |
| Deployed dashboard | Authenticated read-only checks against the real local deployment; all seven screens fit a 390px viewport, no browser application errors, exact balance expansion fits. No credential-bearing traces retained. |
| Cold-start HTTP load | **35,968 requests in 600.52 seconds**, with an initially empty simulated pool. 28,772 expected 200s, 5,397 expected wrong-model 404s, 1,799 expected authentication 401s; **zero incorrect outcomes**. Eight sessions opened, peak eight active, zero duplicate leases and zero active/queued requests at completion. Final sample p95 228 ms, p99 267 ms. Uses a 50 ms simulated provider, not mainnet throughput. |
| Funded DeepSeek concurrency | Four simultaneous cold requests, two funded provider sessions, four HTTP 200 replies with the correct markers; peak two active, approximately 12.6–21.9 seconds per request. |
| Minimum duration | Both funded sessions measured **exactly 300 seconds** on-chain. |
| Real streaming | HTTP 200, a completed `[DONE]` stream and no error event. |
| Funded cleanup/accounting | Both sessions closed. Six successful receipts: two approvals, two opens, two closes. ETH fees reconcile exactly to the balance change. All MOR accounted for. |
| Native tests | Seven gateway-specific native tests, plus wallet, proxy API and internal library suites passed. Self-contained attestation suite: **77 passed, 6 upstream skips**. Ten additional attestation tests require fixtures missing from the pinned upstream archive and were explicitly excluded; those tests are not claimed as passing. |
| Build and static checks | Python lint/format, TypeScript, frontend production build, both Docker images and the native source security stage passed locally. CI also runs the promoted regression gates and native source security stage. |

The first funded cleanup command returned a transient 503. That result remains in the raw log. Closing the same session ID subsequently succeeded; the other session had already closed through policy reconciliation. The durable-close regression was then added and the final code deployed. The report does not present the first harness execution as an uninterrupted pass.

The original campaign's real mid-opening crash, native crash, orphan cleanup, SDK and midnight withdrawal results remain useful historical evidence. They were **not all repeated on the upgraded native dependency graph**. The ten-minute cold run above is separate from the original one-hour preseeded warm run; neither is a 24-hour real-provider soak.

## Native dependency security assessment

The bundled node remains pinned to upstream v7.11.0, commit `99a8d86af1f9453d59797f9a208aa729ff3eb00d`, with reviewed local patches. The builder is **Go 1.26.8**. `deploy/native/go.mod` and `go.sum` pin the full resolved dependency graph, including updated `x/crypto`, `x/net`, `x/text`, Sigstore, Rekor and timestamp verification dependencies. Wallet derivation now uses the maintained `btcd/btcutil` package.

The consumer build removes the unused IPFS file-management and Docker host-administration routes and implementations. This eliminates Kubo, Boxo, libp2p, QUIC and the Docker daemon dependency graph from the runtime package closure. It intentionally changes the bundled node's host-management API surface; it is not a replacement distribution for users who need those upstream features. The gateway's documented remote inference, wallet and node-control features remain available. TEE verification was retained and its dependencies upgraded.

`govulncheck` v1.8.0 source analysis with the Docker build tag reports **zero vulnerabilities in called code and zero in imported packages**, with one advisory in a required module. The scan of the **actual deployed binary** reports **one advisory**, down from 53; do not interpret this as a literally clean binary scan.

The residual is **GO-2026-5932**, the unmaintained `golang.org/x/crypto/openpgp` package. Its wildcard symbol advisory is conservatively reported by binary scanning. The matching source package closure contains **no OpenPGP package**. `go mod why golang.org/x/crypto/openpgp` traces its module presence through a Rekor **test** importing Rekor's end-to-end test utility. The application still requires `x/crypto` for other cryptography. The source scan reports the residual only at module level.

This is a specific non-runtime disposition, not a blanket advisory suppression. The Docker build fails if OpenPGP or the removed host/P2P packages enter `go list -tags docker -deps ./cmd`; CI runs the unsuppressed source vulnerability scan. Future dependency or feature changes must repeat this assessment. See the committed [new binary scan](NATIVE-REMEDIATED-SCAN.txt), [source scan](NATIVE-REMEDIATED-SOURCE-SCAN.txt) and [dependency path](NATIVE-OPENPGP-REACHABILITY.txt). Original scans remain available for comparison.

## Funded test ledger and current local state

| Session | Requested / actual | Outcome |
| --- | --- | --- |
| `0x0bd6f44edd3ff307bf8e74c60d116c6b752a96ae7aab1b41235d7494c574ea76` | 300 / 300 seconds | Closed; 0.446609540066177399 MOR collateral. |
| `0x3c2aa13a4365bd18847a508a3c203a92525801eeedb680a6bd54562977ecf56b` | 300 / 300 seconds | Closed; 0.446609540066177399 MOR collateral. |

At the verified post-cleanup snapshot, **20.240992326560259772 MOR** was liquid and **0.259007673439740228 MOR** remained contract-held: **20.5 MOR total**. The held portion is not withdrawable until the next contract window, expected September 15, 2026 at 00:00 UTC. Automatic recovery/withdrawal remains enabled. This new held amount has **not yet been observed withdrawing**; the prior campaign's eligible midnight withdrawal was observed and verified separately.

Gas for these six transactions was **0.000011293715774009 ETH**. The final ETH balance was **0.002422915720724343 ETH**. The receipt sum includes Base L1 fees and exactly matches the initial-to-final balance difference.

The local UI is **http://localhost:8000**. Original policy was restored: DeepSeek alias, 1,800-second sessions, until-expiry retention and one-session model/global caps, original provider/rating and budget settings. Test credentials were revoked; existing user keys remain. No funded sessions, active requests or queued requests remain. Health and readiness returned 200. The next valid prompt may open a new session under the restored settings.

## Remaining production acceptance work

These are validation limits, not claims that the eight known defects remain unfixed:

- Repeat the full funded crash, orphan, expiry and SDK campaign on the final upgraded native build; obtain successful simultaneous requests to two distinct real models and capability checks for every advertised model.
- Test the intended public Linux VM and domain: HTTPS provisioning/renewal, browser cookies/origin, proxy streaming, firewall, reboot and paired-volume/secret restoration. Local Docker checks do not certify that deployment.
- Complete a 24–48-hour operational soak with real providers, renewal and withdrawal windows, and agreed latency/error alerts.
- Force stuck/replaced transactions, RPC outages and chain reorganizations on a controlled chain/testnet. Existing controlled tests and historical mainnet crashes do not cover every chain failure.
- Restore the missing upstream attestation fixtures before claiming full fixture-backed TEE acceptance, and observe the new held-MOR withdrawal after its window opens.

The PR remains a draft until broader production acceptance is complete. There is no justified blanket “production ready” claim yet.
