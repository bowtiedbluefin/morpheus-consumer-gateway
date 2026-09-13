# Recovery and customer-experience fixes — v0.2

Implemented September 13, 2026. This extends the original customer-experience audit; it is not a claim of funded live-network acceptance. The executable regression suites are `tests/test_recovery.py`, `tests/test_gateway.py`, `tests/test_node_contract.py`, `tests/test_supervisor.py` and `web/e2e/`.

## What customers now get

A failed provider stops receiving new requests. Its session drains and closes in the background, and that provider enters a configurable cooldown. An application receives the failure; its prompt is never automatically replayed after inference starts. Other healthy sessions remain available while cleanup or a cold opening is running.

The recovery worker scans the consumer wallet in pages of 100. It recognizes already managed sessions, correlates uncertain openings, and discovers expired sessions absent from the gateway database. Expired untracked sessions are eligible for automatic cleanup by default. Closing live untracked sessions is a separate opt-in with a discovery grace period: another application may still be using them. Use a dedicated wallet.

After closure, some MOR can remain locked by the contract. The worker reads the contract's `available` and `hold` totals. It withdraws only `available`, subject to a threshold, interval, sufficient gas balance and no unresolved wallet mutation. The native endpoint withdraws up to 255 eligible entries per transaction; later cycles collect additional batches. It never attempts to bypass the contract lock. The dashboard separates liquid, withdrawable and still-locked MOR, and exposes manual recovery and withdrawal checks.

Recovery defaults are enabled, automatic withdrawal enabled, expired-untracked cleanup enabled, live-untracked cleanup disabled, 600-second live-orphan grace, 300-second withdrawal interval, 0.001 MOR withdrawal minimum and 120-second provider cooldown. Wallet defaults are 100 MOR per session, 400 MOR managed plus contract-held stake, 1 MOR/second provider price ceiling, zero minimum liquid MOR reserve and 0.0001 ETH minimum gas balance. These are configurable starting values, not estimates of an appropriate market price or guaranteed gas cost.

## What was reused and what had to change

Source was read from local Git checkouts of the actual hosted API and native node (including Electron). The API's `session_routing_service.py` contains invalidation, asynchronous closing, expired-session cleanup, low-water MOR admission and day-lock accounting. Those patterns informed the lightweight worker. The reviewed API snapshots did **not** contain an explicit automatic stake-withdrawal worker; withdrawal here is implemented against the inspected native routes and contract behavior, not claimed as copied API code.

Inspected API revisions: `c2180d952bc9c4ec3117d37a10fc542d56ff2da3` (default HEAD) and `c5415ccc6adcb3fc05ce84d1a0b6bddfa59350b6` (main). Native/Electron revision: `99a8d86af1f9453d59797f9a208aa729ff3eb00d`, v7.11.0. See [the original source review](DESIGN.md) for repository references.

The node still performs provider discovery/ranking, health checks, handshake, signing, allowance/open/close transactions, inference and stake withdrawals. Its source is now built with a small, reviewable patch in `deploy/patch_node.py` and `deploy/native/`. The archive commit and SHA-256 are pinned in the Dockerfile; patch substitutions fail if the expected source changes. The patch:

- Adds `maxStakeWei` to open-by-bid and checks the **actual calculated stake** before negotiation/submission. An estimate by the gateway alone could not enforce this bound.
- Distinguishes a failed native transaction builder (before broadcast) from an uncertain submitted transaction, so a local estimation failure does not permanently poison the pool.
- Adds an operation ID, durable submission-stage/transaction-hash/session-ID journal and progress responses. Intent is fsynced before a transaction attempt; duplicate operation IDs cannot execute again. A post-mining local-registration error preserves its session ID.
- Serializes approval-plus-open, close and withdrawal inside the node, including work whose HTTP client disconnects.
- Retains native cache rehydration but delegates expiry submission to the gateway when the bundled journal environment is configured. This avoids two independent cleanup workers issuing competing transactions.

The gateway requires the bundled node capability by default. Replacing it with the stock v7.11.0 binary is not an equivalent deployment. `REQUIRE_NODE_GUARDS=false` is an unsupported debugging escape hatch and removes the enforced capability check.

## Transaction recovery rules

| Situation | Action |
| --- | --- |
| Proven pre-submission failure | Release the pending reservation. Provider-specific failures may walk up to three eligible providers. |
| Client disconnects during opening | Keep the shared opening task running and journal its result. A later request can reuse the resulting session. |
| Gateway/node restarts during a mutation | Recover durable intent and native journal; query session truth and transaction receipts through the configured RPC. |
| Successful opening but response lost | Recover session ID from native journal, or correlate wallet/bid/model/provider/opening time after a full paginated scan. Ambiguous matches require verified operator binding. |
| Close submitted but outcome unknown | Keep `close_pending`; repeated close clicks never send the same close again. Read chain/journal until confirmed or proven reverted. |
| Withdrawal submitted but response lost | Block duplicate withdrawal and new wallet submissions. A completed successful native journal or successful receipt confirms withdrawal. |
| No reliable evidence of outcome | Preserve uncertainty. Existing verified hot sessions may serve requests; new wallet transactions wait. Do not erase the journal to force progress. |

One wallet lock governs gateway mutations; a separate native lock covers the native sequence. Neither lock controls another machine holding the private key or arbitrary external administrative transactions. Pending or replacement transactions for which receipt evidence remains incomplete can still require operator investigation. A confirmed receipt is not a promise against a later chain reorganization.

The total stake admission ceiling includes tracked live/pending reservations and authoritative contract-held/withdrawable MOR. It is not a wallet-wide accounting system for undiscovered live sessions created elsewhere. The per-session bound is enforced by the patched node; gas reserve checks are admission thresholds, not exact fee caps.

## Audit disposition

| Audit finding | Implemented change / remaining qualification |
| --- | --- |
| F01 Unknown-open blockade after preflight failure | Proven preflight failures release reservations; native durable progress and wallet/receipt reconciliation recover lost responses. Genuinely unknown outcomes remain blocked. |
| F02 Cold opening blocks hot use | Hot leases do not acquire the wallet mutation lock; control and inference have separate HTTP connection pools. |
| F03 One failure stops maintenance | Per-session reconciliation and per-model tasks isolate failures; wallet scan and withdrawal errors are reported independently. |
| F04 Reuse of failed provider | Immediate invalidation, background close, provider cooldown; no prompt replay. |
| F05 Invalid prompt opens escrow / wrong errors | Validate common chat fields before opening; preserve provider 4xx; optional request Idempotency-Key rejects repeats without storing responses. Provider-specific options still require provider validation. |
| F06 Hidden immediate restart | Rating apply always drains; immediate selection applies only to the explicit restart action. |
| F07 Save overwrites new typing | Draft revision tracking preserves edits made during a pending save. |
| F08 Login lockout / secret rotation | Per-client failure buckets; a valid secret is not locked out by invalid guesses. Admin sessions are bound to secret/generation; logout-all endpoint added. |
| F09 Resource limits | Upload deadline/size, response/SSE bounds, control deadlines, separate pools, deployed server concurrency and keepalive limits. Load/soak testing remains a live acceptance task. |
| F10 Financial limits | Exact native per-session stake guard, quote endpoint/UI, price/total/reserve admission checks. No hard maximum gas fee or external-wallet accounting. |
| F11 Duplicate pending close | Pending close is reconciled, not blindly resubmitted. |
| F12 Refresh hangs / overlapping polls | Each result updates independently, polling deduplicates, requests time out, completed actions do not wait for refresh. |
| F13 Expired auth/logout | 401 returns to sign-in; sign-out clears local state even if server session expired. Unsaved draft remains in the open tab on reauthentication. |
| F14 Blocking startup | Recovery starts as background work; dashboard starts independently. |
| F15 False helper/readiness status | Helper connectivity and readiness are observed; chain balance read is part of readiness. `/healthz` and `/readyz` are separate. |
| F16 Wallet history and correlation | Wallet and local history pagination, bounded background pages, native operation identity, strict binding checks. |
| F17 Lowered session caps | Excess managed sessions drain and close. |
| F18 Maintained session close | Separate stop-maintaining-and-close action; near-expiry replacement and deadline cleanup retained. With one slot there can be a replacement gap. |
| F19 Effective config and interrupted applies | Status reads effective rating file, helper persists restart operation results, identical operation IDs do not repeat restart, startup reconciles recorded results. Manual file edits are not a supported concurrent workflow. |
| F20 Floating-point weights | Accept human percentages and normalize only representation-level error for the native exact-sum check. |
| F21 Key scopes/limits | Creation controls and existing-key editing; scopes displayed. UI selects all models or one model; API accepts multiple scopes. |
| F22 Model capability validation | Chat-only catalog and registry validation before escrow. Alias/duplicate errors remain explicit policy validation errors. |
| F23 Malformed responses | Node/control/chat response validation and React recovery boundary. This is not full schema coverage for every provider extension. |
| F24 Historical storage | Indexed live-session queries, paginated history, terminal history retention, bounded event log, rotated container logs, optional logging failure cannot prevent lease release. Native recovery journals are deliberately retained; disk monitoring/backups remain required. |
| F25 Diagnostics | Request IDs, result/error/session/provider events, last recovery/error and operation status in UI. Metrics export and automatic support bundles remain future work. |
| F26 Precision/unknown totals | Exact decimal display, unavailable balances shown as unknown, active counts exclude failed rows. |
| F27 Stale policy/rankings/drafts | Independent refresh, revision conflict notice/reload, unsaved-edit protection and cache invalidation. Drafts are not persisted across closing the browser tab. |
| F28 Key response loss/copy/DST | Idempotent creation attempts, explicit replacement attempt, visible secret/manual-copy guidance, target-date timezone offset. Lost plaintext keys cannot be retrieved; revoke and replace them. |

The original audit's characterization probes intentionally assert broken behavior and belong to the audited baseline. They are skipped on this branch; regression assertions now live in the normal test suites. The original report remains historical evidence, not the implementation status of v0.2.

## Verification and release boundary

Automated tests use simulated node/RPC responses and real local subprocesses. Container builds compile the patched Go binary and run native stake/journal/serialization tests. Browser tests exercise the UI through HTTP. See [VALIDATION.md](VALIDATION.md) for the current counts and the remaining funded acceptance procedure.

No real MOR was moved to validate this implementation. Before unattended use, exercise opening, expiry closure, day-lock maturity, repeated withdrawal batches, loss of HTTP responses, process kills during submission, node restart and restored backups with a dedicated test wallet. Review the native patch along with the Python worker: the recovery guarantees depend on both.
