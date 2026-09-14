# Production-readiness test report

> Historical campaign record. The eight identified findings have since been addressed; see the [remediation report](REMEDIATION-REPORT.md) for current results and remaining acceptance work.

**Decision: NO-GO for production.** Real inference and important recovery paths work, but the test campaign found an availability hang and several customer-facing failures that the previous tests missed. The green regression results below do not override those failures.

Companion artifacts: [scenario coverage and remaining acceptance checks](PRODUCTION-TEST-MATRIX.md), [funded session ledger](PRODUCTION-SESSION-LEDGER.md), [native dependency triage](NATIVE-DEPENDENCY-TRIAGE.md), and [machine-readable evidence](PRODUCTION-TEST-EVIDENCE.json).

This report covers the autonomous campaign from approximately September 13, 2026, 22:30 UTC through 2026-09-14 00:00:13 UTC. The gateway application under test is commit `cbab1a0`, on `fix/recovery-and-customer-reliability`; the bundled node is v7.11.0, pinned to upstream `99a8d86af1f9453d59797f9a208aa729ff3eb00d` with the checked-in gateway patches. Application fixes were **not** applied during this campaign. New tests, reproductions, and this report were added so the failures can be addressed and retested explicitly.

## What the results mean in plain English

- A customer can use the gateway with a normal chat client. Real DeepSeek prompts, streaming, Unicode conversations, JSON output, and tool calls worked.
- Wrong model names and common malformed requests were rejected without opening a session.
- The gateway recovered a real opening after being killed halfway through the operation, without creating a second escrow. It also found and closed an expired session missing from its own database.
- Provider availability is inconsistent. A model appearing in the catalog does not guarantee a working provider. Two-model routing overlapped, but the second provider timed out; a successful two-model live acceptance test remains outstanding.
- A cold burst can hang the gateway. Exactly five minutes can fail at the contract boundary. Some malformed responses are accepted as successes, and realistic wallet balances break the mobile layout. These need fixes before customers rely on the service.

## Test environment and scope

The funded deployment ran in Docker Desktop on macOS ARM64, against Base chain ID 8453, using the user's dedicated wallet. The real gateway and patched native-node images were exercised. The frontend was also opened against this deployment in Chromium.

The complete backend regression suite additionally ran in a disposable Linux container as UID 10001, with two CPUs, 1 GiB RAM, all capabilities dropped, and external networking disabled. Its protocol tests used loopback TCP fixtures. This establishes Linux functional compatibility under those constraints; it is not a throughput certification for a cloud VM.

The sustained load test used the real HTTP gateway and SQLite with a simulated provider taking 50 ms per request. Eight sessions were explicitly preseeded after the cold-start failure was reproduced. It used 32-request waves targeting 60 requests/second, with 80% valid prompts, 15% wrong models, and 5% unauthorized requests. Provider speed, chain performance, and cold openings are excluded from that measurement.

Versions: Python 3.13.1 on the host, FastAPI 0.141.1, Starlette 1.6.0, Pydantic 2.13.5, HTTPX 0.28.1, Uvicorn 0.52.4. The production dependency lockfile was used. Browser runs used Playwright's installed Chromium, Firefox, and WebKit engines. Precise image identifiers and machine-readable results are retained locally.

## Results by suite

| Suite | Result | What it establishes |
| --- | --- | --- |
| Backend regression suite | **128 passed on macOS and Linux** | Authentication, model policy, exclusive session leases, true overlapping requests, wallet budgets, state recovery, deadlines, storage and TCP faults. |
| Native Go guard/journal tests | **5 passed** | Stake ceiling, durable intent, duplicate-operation rejection, wallet-lock cancellation, journal disk failure, conservative treatment of failed transaction building. |
| Browser regression suite | **18 passed: 6 each in Chromium, Firefox, WebKit** | Dashboard setup, key creation, chat/SSE, provider blocking, restart, wallet controls, save races, expired login and hanging status requests against demo/controlled data. |
| Dedicated backend release gates | **8 failed on macOS and Linux** | Deliberately preserved reproductions of unresolved defects, listed below. These are not hidden with expected-failure markers. |
| Dedicated mobile release gate | **1 failed** | A 390 px viewport expands to 656 px with realistic wallet balances. |
| Actual deployment browser checks | **7 passed, 1 failed** | All six administration screens and no uncaught JavaScript errors; mobile width failed. |
| Actual client SDK acceptance | **6 passed** | Model list, Unicode multi-turn chat, JSON response format, tool calling, streaming usage, typed 404 for a wrong model. Client retries disabled. |
| Burst overload on warm simulated pool | **5 stages passed** | 8, 32, 64, 128 and 256 concurrent requests; bounded rejection, response isolation, no leaked leases, health recovers after each burst. |
| One-hour warm-pool soak | **Passed: 215,392 requests** | Real HTTP/SQLite with eight preseeded sessions and a simulated provider; exact results below. |
| Funded sessions and recovery | Mixed; detailed below | Actual chain transactions, provider handshakes, streaming, restarts, orphan cleanup and wallet accounting. |
| Python/frontend dependency advisory scans | **No known advisories reported** | npm dependency graph and the pinned production Python requirements. Container-OS packages were not scanned; native Go results are separate below. |

### Overload results

| Concurrent requests | HTTP 200 | HTTP 429 | HTTP 503 | Maximum observed latency | Healthy afterward |
| ---: | ---: | ---: | ---: | ---: | --- |
| 8 | 8 | 0 | 0 | 0.057 s | Yes |
| 32 | 32 | 0 | 0 | 0.235 s | Yes |
| 64 | 64 | 0 | 0 | 0.490 s | Yes |
| 128 | 80 | 48 | 0 | 0.598 s | Yes |
| 256 | 88 | 158 | 10 | 0.696 s | Yes |

The larger bursts exceeded configured queue/server admission limits. Those 429/503 responses were expected overload rejection, not successful inference. Every 200 response contained the correct per-request marker. There were zero duplicate session leases, zero active leases left over, and zero queued requests left over. The simulated pool stayed at eight sessions.

### One-hour sustained run

The run completed in **3600.48 seconds** with **215,392 requests** (59.82/second observed): **172,312 HTTP 200**, **32,310 expected HTTP 404**, and **10,770 expected HTTP 401**. There were **zero incorrect outcomes**, zero duplicate leases, and zero active or queued requests left at completion. Fifteen warm-up requests preceded the measured run.

Across the recorded one-minute windows, mixed-traffic p95 latency ranged from **227.1 to 231.2 ms**, and the worst window's p99 was **282.4 ms**. These are per-window percentiles, not a computed overall percentile, and include fast negative requests. Sampled process RSS ranged from **60.19 to 61.23 MiB**; the largest sampled SQLite/WAL footprint was **4.27 MiB**. Event retention ended at 500 records. These measurements show bounded behavior during this run, not proof that no memory leak or long-term growth is possible.

The workload used a 50 ms simulated provider with eight preseeded sessions. Cold-start runs failed separately; this passing run does not clear PR-01 or establish real-provider throughput.

## Required fixes

### PR-01 — Cold acquisition can monopolize the event loop — critical

**Observed:** A fresh HTTP service receiving a burst of requests stopped returning valid prompts and health checks. CPU usage rose and client requests timed out. The failure also reproduced with a burst of 32 valid requests, independent of the mixed workload runner.

**Cause:** `Sessions.acquire()` can observe a completed task still present in `self.opening`, before its completion callback removes it. Awaiting an already-completed task does not necessarily yield. The loop continues, re-reads policy, and awaits that same completed task indefinitely. Its queue deadline is below the task branch and is never checked in this path. The callback that would clear the condition cannot run.

**Customer effect:** The API, dashboard refresh, and background recovery can all stop progressing. Docker health checks can report unhealthy, but the current restart policy does not automatically restart a merely unhealthy, still-running process.

**Fix/acceptance:** Remove completed openers safely, guard callback cleanup by task identity, guarantee scheduler fairness and a deadline on every acquisition path. Retest cold bursts and reopen/expiry races over real HTTP on Linux, with responsive health/admin endpoints. See `test_completed_opener_does_not_starve_event_loop` and `devtools/soak.py` without `--warm`.

### PR-02 — The advertised five-minute boundary fails on-chain — high

**Observed:** The UI/API accepted 300-second sessions, but a controlled native request returned `SessionTooShort()`. Twelve keep-hot attempts failed before the campaign paused the invalid setting. No session was created, but the receipt audit found two successful token-approval transactions from these failed attempts; they consumed gas. The failed gateway rows did not display those approvals, which were recovered from the native journals. A 301-second native request succeeded and produced a 300-second session.

**Cause:** The contract minimum is five minutes. The node's stake calculation rounds down; the contract converts stake back into duration with integer division. Requested duration and effective on-chain duration can differ by a second. Other funded records also showed this one-second difference.

**Customer effect:** Selecting the minimum accepted duration produces a generic error and repeated ineffective maintenance attempts. The current dashboard does not expose the useful native reason.

**Fix/acceptance:** Make stake rounding and the duration minimum agree, including changes in supply/budget between estimation and submission. Surface a safe, useful validation error, preserve mined approval transactions in failed-operation history, and apply backoff for deterministic preflight failures. Test the exact minimum, one second below/above, and all supported network configurations. Do not silently treat 301 seconds as a complete production fix.

Source evidence: upstream `SessionStorage.sol` defines a five-minute minimum; `SessionRouter.sol` checks derived duration and uses integer division in `getSessionEnd()`/`stakeToStipend()`. Native response retained in `diagnostic-open-300.json`.

### PR-03 — Invalid numeric extensions can open escrow before failing — high

**Observed:** A request containing `NaN` in an extension field returned plain HTTP 500 after one session was opened. It should have been rejected before wallet work. Standard temperature/top-p validation does not cover arbitrary extension fields.

**Cause:** Python JSON parsing accepts non-finite values and the request model permits extra fields. HTTPX later rejects the value while serializing the provider request, after session acquisition.

**Fix/acceptance:** Reject non-finite numbers recursively before acquisition; bound nesting and numeric parsing; preserve a structured 400 with a request ID. Retest extensions, message/tool payloads, and streaming options. Reproduction: `test_nonfinite_extension_rejected_before_any_escrow`.

### PR-04 — Extra-capacity failure abandons customers who could wait — high

**Observed:** With one healthy session busy and room in the configured pool, a second request attempted an additional opening. The provider declined extra capacity. The second request immediately returned 503 even though the healthy session became free within its queue deadline. In the funded single-provider four-request run, two requests succeeded and two failed with `no_eligible_provider`.

**Fix/acceptance:** Preserve safe waiting for already-owned healthy sessions after a proven pre-submission capacity failure, within the configured deadline. Do not retry uncertain wallet submissions or replay a prompt. Test cancellation, key revocation, provider-policy changes, and queue fairness while waiting. Reproduction: `test_waiter_can_use_busy_session_if_extra_provider_capacity_declines`.

### PR-05 — Provider response validation is too shallow — high

**Observed:** Three malformed assistant-message shapes were forwarded as HTTP 200, including an empty object and numeric/object content. Non-finite usage returned HTTP 500 after the gateway had already recorded the request as successful.

**Customer effect:** Client code can fail on a supposedly valid response, while the activity log gives misleading success information.

**Fix/acceptance:** Validate the supported response schema, including choice/message/delta fields and finite numeric values, while preserving legitimate tool calls, reasoning fields, usage-only chunks and provider extensions. Finish validation/serialization before recording success. Retest both complete JSON and SSE. Reproductions: `test_invalid_provider_message_is_not_success` and `test_provider_nonfinite_usage_is_error_not_success`.

### PR-06 — Readiness stays failed after storage recovers — medium

**Observed:** A temporary read-only database produced the expected safe 503. After writes were restored, inference succeeded and a maintenance tick completed, but `/readyz` continued to return 503 because the stored error flag was never cleared.

**Customer effect:** A deployment using readiness to route traffic can remain unavailable after the underlying problem is resolved. Restarting only the node does not clear a gateway-memory error flag.

**Fix/acceptance:** Clear the failure only after a verified successful storage health check/reconciliation, or provide an explicit recovery action with accurate guidance. Retest actual full-disk/read-only recovery and preserve fail-closed behavior while writes are unsafe. Reproduction: `test_readiness_recovers_after_transient_storage_failure`.

### PR-07 — Realistic balances break the mobile dashboard — medium

**Observed:** On the actual deployment and a deterministic browser reproduction, a 390 px viewport grew to 656 px. Long MOR/ETH values force the two-column statistics grid beyond the screen. The existing demo screenshot test had short values and passed.

**Fix/acceptance:** Use a compact human-readable display with exact values available separately, and constrain/wrap the grid cells. Check all screens at phone/tablet widths with large values, long aliases, addresses and errors. Reproduction: `web/e2e/mobile.spec.ts`.

### PR-08 — Native dependency advisories require remediation and reachability review — high

The actual deployed Go 1.25.14 binary was scanned with `govulncheck` v1.8.0. The binary-symbol scan reported **53 advisories across 18 modules**, plus 19 additional module-level advisories without matching called symbols. This is a failed security acceptance gate, not proof of 53 remotely exploitable paths. Binary scanning can include unreachable functions and cannot provide source call stacks; those limitations are described by the [Go tool's documentation](https://pkg.go.dev/golang.org/x/vuln/cmd/govulncheck).

Examples include `golang.org/x/text` v0.35.0, for which [GO-2026-5970](https://pkg.go.dev/vuln/GO-2026-5970) describes an invalid-UTF-8 infinite loop fixed in v0.39.0, and vulnerable-symbol matches in x/net, x/crypto, Pion, QUIC, compression, Docker libraries and other transitive dependencies. Some compiled features may be disabled in this consumer deployment. Updating or removing affected dependencies and documenting actual reachability is necessary; a Python/npm scan cannot cover this native binary.

See the [full binary scan](NATIVE-VULNERABILITY-SCAN.txt). The completed source call-graph scan, with the Docker build tag and matching Linux/ARM64 target, reported **32 advisory matches in 19 modules**. Its conservative paths are not proof of deployment exploitability; source and binary counts are not additive. The [triage inventory](NATIVE-DEPENDENCY-TRIAGE.md) lists affected/fixed versions and the differences between scans. Do not broadly upgrade the native dependency graph and deploy it without rebuilding and rerunning the funded protocol/recovery tests.

## Funded end-to-end findings

| Scenario | Observed result |
| --- | --- |
| Wrong/blank/misspelled/unknown-ID/whitespace/homoglyph model | Correct 400/404; no session changes. |
| Common invalid messages/options and internal routing-field injection | Correct 400 before escrow. |
| Missing/wrong API key; API key used for admin access; foreign-origin restart | Correct 401/403; no unauthorized restart. |
| Four requests restricted to one provider | Peak two active; two 200 and two 503. Extra provider capacity was unavailable; PR-04 worsens the customer outcome. |
| Four requests with multiple providers allowed | Peak four active; four 200 responses in approximately 53–75 seconds. One content marker failed. The original harness did not retain that response body, so its cause is unresolved. |
| Separate DeepSeek empty-content response | Retained payload showed `finish_reason: length`, all 2,048 completion tokens used for reasoning, and no final text. This explains that particular content-check failure; it does not prove the cause of the earlier four-way marker failure. |
| Streaming | Actual SSE, about 1.69 seconds to first event in the initial run, ten chunks and `[DONE]`. |
| Client disconnect during streaming | Lease released; zero active requests afterward. |
| Graceful node restart during inference, applying rating | Existing request completed; new admission returned 503; health remained available; operation succeeded in approximately 27.5 seconds; subsequent inference worked. |
| Repeated early close | Same close operation/transaction returned while pending, then chain reconciliation marked it closed. The original immediate-closed assertion was too strict; `close_pending` is valid until mined. |
| Gateway SIGKILL during journaled approval/open | Recovered from `open_unknown` to one open session; duplicate application request returned 409; subsequent inference worked. |
| Native child SIGKILL mid-stream | Structured stream error, no false `[DONE]`, health still 200, supervisor launched a new native process in approximately 11 seconds. Immediate next inference returned `wallet_pending` while cleanup was being reconciled; later prompts worked. |
| DeepSeek and Llama simultaneously | Distinct model sessions overlapped. DeepSeek succeeded; Llama reached the 120-second inference deadline and returned 504. This is not a passed two-model service acceptance test. |
| Qwen with expensive bid | 503 `wallet_budget`; the estimated 6.8357 MOR stake exceeded the 3 MOR per-session cap. No opening was submitted. This is the budget guard working. |
| Low-stake Qwen provider | Provider connection timed out before submission; no funded Qwen session. |
| Exactly 300-second keep-hot sessions | Failed as described in PR-02; paused after twelve ineffective attempts. No session openings, but two approval transactions consumed gas. |
| 301-second finite keep-hot run | **Passed:** three sessions opened and closed automatically; peak managed live count one; no replacement after the fixed deadline. Replacement had a visible gap while the old session closed and the next one opened; this is not uninterrupted availability. |
| Wallet session missing from gateway DB | Opened with the gateway stopped. On restart, live-orphan protection left it alone. After expiry, wallet scanning discovered and closed it. Confirmed closed approximately 26 seconds after its on-chain end time. |
| Automatic matured-MOR withdrawal | **Passed:** 7.392395593543746086 MOR withdrawn automatically after the UTC-day boundary; receipt confirmed and liquid balance restored to 20.5 MOR. |

The real provider network remains a production dependency. A node catalog, a rated bid and a successful handshake do not establish that a model will produce a timely, usable answer. Acceptance needs successful JSON and SSE canaries for each model/provider combination actually offered to customers.

## Recovery and security coverage

The expanded tests cover uncertain opens and closes, lost native responses, transaction receipt reconciliation, reversed withdrawals, held-versus-available MOR, minimum withdrawal thresholds, gas headroom, provider cooldowns, expired/untracked wallet sessions, pagination beyond 100 entries, and preservation of uncertain financial records during history pruning.

Storage tests used real SQLite page exhaustion, an actual killed writer with a committed WAL entry, a corrupt database that must not be silently replaced, and restoration of an older snapshot followed by reconciliation against newer session truth. Injected storage faults confirmed that escrow is not opened when durable state cannot be written. These are not a physical host power-loss or complete encrypted-backup restore drill.

Transport tests used actual loopback TCP for the node adapter, including Basic authentication, control failures/timeouts, malformed identity, a lost opening response, truncated SSE, inference header deadlines, slow uploads, streamed body-size limits, and connection admission/release. Unit tests separately cover byte-by-byte SSE framing, CRLF, Unicode, provider HTTP errors, response limits and request cancellation.

Deployment inspection confirmed both services run as UID 10001, drop all capabilities, and set `no-new-privileges`. The node publishes no host TCP port. The gateway publishes port 8000 on loopback only. Neither service mounts the Docker socket, and the gateway does not mount the wallet key. The packaged access logs are disabled; test checks did not expose API secrets or prompt contents through admin/event endpoints. This does not establish provider-side confidentiality or a full penetration-test result.

## Wallet ledger and final installation state

The campaign started with one previously authorized 30-minute DeepSeek session already open. The original wallet contained 20.5 MOR before that session. The campaign's additional wallet operations were bounded by a 3 MOR per-session ceiling, 15 MOR total configured allocation ceiling, and an ETH reserve; one direct orphan test used the same per-session ceiling while the gateway was stopped.

Session stakes are collateral, not a statement that the same amount of MOR was spent. Early closure returns the unused portion; the remainder can stay locked until the contract's day boundary. Gas expenditure is separate. The final ledger reconciles actual session IDs and receipts; it does not count prebroadcast failed rows as funded sessions.

**All 11 funded sessions are closed: the original session plus ten new campaign sessions. Automatic withdrawal passed.** The worker submitted its withdrawal at 2026-09-14 00:00:02 UTC and reported confirmation at 2026-09-14 00:00:03 UTC. Independent RPC receipt and balance checks then completed at 2026-09-14 00:00:13 UTC.

| Final accounting item | Verified result |
| --- | --- |
| Original MOR before the first session | 20.5 MOR |
| Liquid MOR after automatic withdrawal | **20.5 MOR** |
| Still held / available to withdraw | **0 / 0 MOR** |
| Automatically withdrawn at maturity | 7.392395593543746086 MOR |
| Funded sessions / final state | 11 / all closed |
| Active requests / queued requests | 0 / 0 |
| Confirmed transactions | 35: 12 approvals, 11 opens, 11 closes, 1 withdrawal; every receipt status successful |
| Aggregate collateral across all sessions | 10.197051532538223736 MOR; not expenditure or peak allocation |
| ETH before original session | 0.002493364109255375 ETH |
| Final ETH balance | 0.002434209436498352 ETH |
| Total recorded transaction fees, including original opening | 0.000059154672757023 ETH |
| Incremental campaign transaction fees | 0.000056239813383007 ETH; excludes original approval/open, includes its cleanup |

Withdrawal transaction: `0x84ae5ecc5cb6a6d9be9ad8ef6aaeeede620649550b00b5f7113f140f9eceb47d`. All 43 native operation journals are complete. Nineteen failed gateway rows have no funded session ID; the two mined approvals from failed 300-second attempts are included in the 35-transaction ledger. Gas used times effective gas price plus recorded Base L1 fees reconciles exactly to the change in the ETH balance. The MOR balance is back to its original total; no MOR is left held by these test sessions.

The installation remains available at **http://localhost:8000** with the original DeepSeek alias, 1,800-second session duration, until-expiry retention, and one-session model/global caps. Original provider/rating settings and 10 MOR per-session / 20 MOR total limits were restored. Recovery and automatic withdrawal remain enabled; live-untracked cleanup remains disabled. The campaign key was revoked and its local secret removed; original user keys were preserved. Both health and readiness returned 200 after cleanup. No session is being kept hot; the next valid prompt can initiate a new session under the restored policy.

The [sanitized evidence JSON](PRODUCTION-TEST-EVIDENCE.json) records test cases, image IDs, session/transaction IDs, balances, fees and artifact hashes. The [session ledger](PRODUCTION-SESSION-LEDGER.md) lists every funded session. Raw native journals, receipts, balance snapshots and monitor samples remain in ignored `data/production-readiness/` for inspection. No private key, administrator secret, node password or application-key value is included in the committed evidence.

## What still must happen before production

1. Address native dependency advisories in PR-08 with upgrades/removal or documented reachability evidence. Fix PR-01 through PR-04, the provider protocol/success-recording defects, and the operational readiness problem. Resolve mobile usability before supporting mobile administration. Promote the failing release gates into the normal suite after they pass.
2. Repeat cold/warm/concurrent/expiry/crash acceptance on the fixed application and pinned node build. The present soak cannot validate future fixes or the cold-start path it intentionally bypassed.
3. Obtain two successful simultaneous real model responses and a supported-feature canary for every advertised model. Test provider failover before submission, busy healthy-provider waiting, and bounded recovery after provider timeouts.
4. Run the intended Linux VM, HTTPS ingress and actual network path. Validate certificate provisioning/renewal, remote browser origin/cookies, SSE flushing through the proxy, firewall rules, restart after VM reboot, and a paired-volume/secret backup restore. No deployment domain or target VM was supplied for those checks.
5. Run a longer soak with real providers and scheduled session renewals; establish customer-facing latency/error targets and alerts. A one-hour simulated warm-pool run does not replace a 24–48-hour operational soak.
6. Exercise controlled pending/stuck/replaced transactions and chain/RPC outages on a local chain or funded testnet. Tests here simulate several receipt outcomes and perform a real mid-opening crash, but do not force a Base reorganization or intentionally strand a mainnet transaction.
7. Document supported API endpoints and client retry/idempotency behavior. The live SDK tests cover chat completions with retries disabled; they do not certify Responses, embeddings, audio, image inputs, every SDK, or automatic client replay behavior.

## Reproduction and evidence map

See [campaign instructions](../audits/production_readiness/README.md), [regression tests](../tests/), [failing release gates](../tests/test_release_regressions.py), [browser failure](../web/e2e/mobile.spec.ts), and [soak runner](../devtools/soak.py).

The regression/test commit `209d806fcde80d2763a407f3d8e2162e7b9c3232` passed [GitHub CI](https://github.com/bowtiedbluefin/morpheus-consumer-gateway/actions/runs/34789698044): backend tests, lint/format, frontend build, Chromium regressions, and both Docker image builds. CI currently excludes the separate failing acceptance gates; Firefox/WebKit, funded tests, and the sustained load test were run locally.

The chosen browser setup follows Playwright's [browser/project support](https://playwright.dev/docs/test-projects) and [failure tracing](https://playwright.dev/docs/trace-viewer). Load acceptance separates explicit thresholds from raw request volume, consistent with [k6's threshold model](https://grafana.com/docs/k6/latest/using-k6/thresholds/); this campaign used a custom HTTPX runner, not k6. Network faults were injected in local TCP fixtures; Toxiproxy is a suitable future tool for a full deployed network-fault matrix, but it was not run in this campaign.

A green CI run on the existing regression job is not a production sign-off. The unresolved release-gate failures and unperformed deployment checks above remain authoritative.
