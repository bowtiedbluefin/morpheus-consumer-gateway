> Historical audit of the v0.1 baseline. See [v0.2 fixes and remaining qualifications](RECOVERY-AND-AUDIT-FIXES.md) for current status.

# Customer experience failure audit — September 13, 2026

**Assessment: hold unattended customer rollout pending a reliability pass and live-node acceptance.** The initial implementation covers the intended happy path, but routine failures can strand session creation, make healthy models unavailable, lose edits, or interrupt inference through a misleading restart control.

Audited gateway revision: [`5d226e5607326d713afb746127b1c194b22c979d`](https://github.com/bowtiedbluefin/morpheus-consumer-gateway/tree/5d226e5607326d713afb746127b1c194b22c979d). This is a second source review plus fault injection, not an independent third-party certification. The audit branch changes documentation and audit probes only; the product implementation is unchanged.

## Scope and evidence

Reviewed the browser flows, API authentication and request handling, session state transitions, SQLite persistence, native-node HTTP adapter, subprocess supervisor, Compose deployment, setup instructions, and existing test coverage. Cross-checked relevant native behavior against Morpheus node commit `99a8d86af1f9453d59797f9a208aa729ff3eb00d`, particularly opening by bid, transaction stages, response structures, stream framing and `/config` readiness.

- Existing backend suite: **33 passed** again, unchanged.
- New backend fault probes: **16 passed**, meaning each undesirable behavior was reproduced.
- New Chromium browser probes: **4 passed**, reproducing save races, expired login, hidden immediate restart, and refresh blocking.
- Native-chain transactions, actual provider failures, real-node crash/recovery, TLS provisioning, memory exhaustion and disk exhaustion were **not** executed. Findings requiring those conditions are labeled source-level risks or acceptance gaps.

The probes use a fake node or controlled HTTP/browser responses. They demonstrate gateway behavior under the stated input/failure, not the frequency of those failures on the live network. In particular, the simulated pre-submission error is corroborated by an actual pre-submission error path in the inspected node source.

This is a systematic inventory of the failure modes identified, not a guarantee that every possible runtime failure has been discovered.

**Priorities:** P1 = resolve before unattended customer use; P2 = significant reliability, usability or support issue; P3 = smaller usability issue. “Reproduced” means an included probe exercises it. “Source” means the path is established by code inspection, without a full runtime reproduction. “Gap” is a deliberately missing capability or unvalidated integration, rather than a newly discovered implementation defect.

## P1 findings

### F01. A temporary error can permanently block new sessions

**Evidence: reproduced; source corroborated.** [Opening error classification](../gateway/sessions.py#L205), [native error translation](../gateway/node.py#L30), [global uncertain-open guard](../gateway/sessions.py#L145).

**Customer trigger:** a native token-supply/budget lookup fails during opening, an allowance/open operation fails, or the opening request receives any error outside three narrow provider-rejection prefixes. The gateway marks the operation `open_unknown` without knowing whether a session exists. After the original problem is corrected, every new opening still returns `open_unknown`. The browser asks the customer to bind a session ID; if no session was created, there is nothing to bind. Existing healthy sessions may continue working until they expire, after which the outage becomes installation-wide.

The native [open-by-bid implementation](https://github.com/MorpheusAIs/Morpheus-Lumerin-Node/blob/99a8d86af1f9453d59797f9a208aa729ff3eb00d/proxy-router/internal/blockchainapi/service.go#L1273) can fail on supply/budget reads **before** `tryOpenSession`, which establishes that this is more than a hypothetical malformed response.

**Fix:** expose typed native outcomes that distinguish no submission, submitted/pending, mined success and mined failure. Persist transaction/operation identity and reconcile receipts/events. Add a resolution path for proven non-submission or confirmed failure. Preserve the conservative behavior for genuinely uncertain submissions; do not fix this by blindly retrying or adding an unrestricted “forget” button.

### F02. Opening one model can make an already-hot model fail

**Evidence: reproduced.** [Shared lock and queue](../gateway/sessions.py#L88), [network work inside acquisition](../gateway/sessions.py#L119), [timeouts](../gateway/config.py#L23).

A cold opening, a slow session lookup, a close, or a maintenance cycle owns the same global lock needed to borrow every existing session. A request for an idle, healthy session can therefore wait behind an unrelated model's transaction and return `pool_busy`. The one-second audit queue timed out while an unrelated cold operation held the lock, even though the requested hot session was idle.

The queue setting also does not bound a cold acquisition after it enters `_acquire`; multiple node reads and up to three opening attempts can extend it substantially. Inference has a separate, fixed 120-second deadline. Customers experience unpredictable first-token delays, timeouts from their clients, and rejection despite paying to keep sessions available.

**Fix:** separate per-model/session allocation from serialized wallet mutations. Keep a short state lock and use a dedicated mutation queue. Define separate queue, cold-start, first-token and generation deadlines, expose them clearly, and give clients useful retry/status information. Preserve mutation uncertainty when a deadline is exceeded.

### F03. One broken model or session stops unrelated maintenance

**Evidence: reproduced in two probes.** [Reconciliation loop](../gateway/sessions.py#L313), [maintenance loop](../gateway/sessions.py#L326).

One unreadable session makes reconciliation raise before idle closes or prewarming run. One maintained model with no eligible provider makes the model loop exit before later healthy models are considered. The next tick starts in the same order and can fail on the same object again. Customers see some models never become ready and sessions remain open longer than their settings imply. Acquisition also fails on the first unreadable reusable session instead of trying another healthy one.

**Fix:** isolate failures per session and per model; preserve their error states individually; continue unrelated work. Bound read durations and use backoff. Quarantine unverifiable sessions without deleting their ownership or authorizing duplicate chain mutations.

### F04. A failed provider stays selected for subsequent prompts

**Evidence: reproduced.** [Inference errors and cleanup](../gateway/app.py#L377), [session reuse](../gateway/sessions.py#L125).

The gateway surfaces an inference failure but records no health penalty, failure streak or quarantine on that session. If the session is still open on-chain, later requests keep using it. The probe sends three requests to a failing provider and all three use the same session. A busy customer can repeatedly refresh `last_used`, preventing ordinary idle cleanup from helping.

**Fix:** distinguish an uncertain in-flight request from eligibility for future requests. Do not replay partial output; mark the failing session unhealthy for subsequent requests, apply a cooldown, and select another permitted provider within policy and escrow limits. Expose why a provider was avoided.

### F05. Invalid requests can open sessions, then return misleading server errors

**Evidence: reproduced.** [Minimal body validation](../gateway/app.py#L304), [provider status translation](../gateway/app.py#L377).

`messages: [42]` passes gateway validation. The gateway opens a session before the native node rejects the malformed chat payload. Provider/native 400-series errors, apart from 429, are translated to 502. The customer sees a server failure instead of instructions to correct their request, and retrying clients may execute additional attempts. No idempotency key or completed-response ledger prevents duplicate prompt execution after a lost response.

**Fix:** validate the supported chat schema and basic option types before session acquisition. Preserve useful client-error categories through a sanitized error mapping. Document and implement an explicit retry/idempotency policy; do not replay completed or partially streamed work automatically. Validate model capability before opening as well (F22).

### F06. Applying ratings can unexpectedly interrupt active prompts

**Evidence: reproduced in Chromium.** [Shared `immediate` state](../web/src/main.tsx#L149), [restart request](../web/src/main.tsx#L224), [rating-screen button and description](../web/src/main.tsx#L928).

Check **Restart immediately** on Node & activity, navigate to Providers & rating, and click **Apply and restart node**. The request still contains `immediate: true`, while the rating screen says the restart waits for active requests. The customer can cut off other applications' prompts without seeing that choice on the current screen.

**Fix:** make immediate/graceful behavior an explicit property of each action. Default rating apply to graceful, or present the current immediate choice alongside that action with matching text. Clear one-shot immediate intent after use. Also label the lifecycle-lock wait accurately: “immediate” currently skips draining but cannot bypass an ongoing opening transaction.

### F07. Editing a field during Save silently loses the newer edit

**Evidence: reproduced in Chromium.** [Save implementation](../web/src/main.tsx#L215), [editable model fields](../web/src/main.tsx#L596).

Save one alias, then type a different alias before the save response returns. Inputs remain editable. The response replaces the whole policy with the older snapshot and clears `dirty`, so the later edit disappears without an unsaved warning. The same race applies to provider policy and other fields.

**Fix:** track the revision of the draft submitted by each save. Reconcile the returned server version with edits made afterward and keep them dirty, or temporarily disable the affected form while saving. Add route/reload protection for unsaved edits (F27).

### F08. Authentication controls create lockout and rotation surprises

**Evidence: reproduced.** [Global login bucket](../gateway/app.py#L140), [session authentication](../gateway/app.py#L116).

Ten incorrect login attempts consume a single installation-wide minute bucket. An unauthenticated caller who knows the public origin can repeatedly deny legitimate administrators access. Exact Origin checking does not prevent a non-browser caller from supplying that header. Successful attempts also consume the bucket.

Separately, replacing the administrator secret does not invalidate existing browser sessions: session validation checks the database record and expiry, not a credential epoch. A customer rotating a compromised secret can leave an old browser authorized for the remainder of its 12-hour lifetime.

**Fix:** use per-source and installation-wide abuse controls with a legitimate-admin recovery path, taking trusted-proxy configuration into account. Bind sessions to a credential epoch and offer revoke-all-sessions. Keep server-side expiry and CSRF checks.

### F09. Unbounded input/output paths can take down the shared endpoint

**Evidence: source-level resource risk, not an exhaustion test.** [Body buffering before authentication](../gateway/app.py#L91), [stream parsing](../gateway/app.py#L389), [non-stream buffering](../gateway/app.py#L416), [Compose resource configuration](../compose.yaml).

The 2 MiB request-size check does not impose an upload deadline or an installation-wide limit on unauthenticated connections. Slow uploads can accumulate before authorization and rate limiting. Provider responses are buffered in full for non-streaming requests. Streaming checks line size only after `aiter_lines()` has assembled a line; an unterminated oversized line can grow before that check. Deployment resource limits and response byte limits are absent.

**Customer outcome:** one problematic client/provider can consume memory or connection capacity and make unrelated applications and the dashboard fail.

**Fix:** enforce header/body deadlines and connection limits at ingress and application boundaries; enforce byte limits while reading provider chunks, before accumulating a whole line/body; isolate control traffic from inference pools; configure memory/log limits. Validate with bounded load and hostile-response tests.

### F10. Session limits do not bound the customer's wallet exposure

**Evidence: known implementation gap.** [Count-only checks and opening](../gateway/sessions.py#L144), [model policy](../gateway/models.py#L53), [native stake calculation](https://github.com/MorpheusAIs/Morpheus-Lumerin-Node/blob/99a8d86af1f9453d59797f9a208aa729ff3eb00d/proxy-router/internal/blockchainapi/service.go#L1490).

The customer can enable automatic replacements without a maximum stake, price-per-second policy, gas/headroom check or installation budget. One permitted session can require much more MOR than expected, and replacement sessions can tie up more MOR while prior amounts remain time-locked. Repeated opens/closes also require gas. MOR escrow is not the same thing as spending the full stake; this finding is about liquidity and transaction exposure, not a demonstrated loss of escrow.

**Fix:** show an estimate and its assumptions before manual opening, apply durable budget reservations for automation, add balance/headroom guards, and obtain a native enforceable upper bound on the actual transaction amount. A gateway-only quote is not a binding limit if the native open recalculates its amount.

### F11. An uncertain close can be submitted again from the UI

**Evidence: reproduced.** [Close implementation](../gateway/sessions.py#L251), [Close button](../web/src/main.tsx#L1067).

After a close times out, `close_pending` retains the session ID. The button remains enabled. Clicking again rechecks `ClosedAt`; if the first transaction is still pending, it sends another close. The probe records two submissions against the same session. Actual duplicate transaction costs/reverts depend on the node and chain and were not measured.

**Fix:** persist close transaction identity and make close an idempotent tracked operation. Pending closes should reconcile receipts instead of resubmitting. Show pending/confirmed/failed state and permit retry only after a definitive failed outcome.

## Other significant findings

| ID | Priority / evidence | Customer failure and trigger | Recommended change / code |
| --- | --- | --- | --- |
| F12 | P2, reproduced in Chromium | One hanging `/status` call delays application of already-completed session/key/operation results because `Promise.allSettled` is awaited as a group. Actions await the same refresh, keeping buttons busy. The five-second interval can start overlapping refreshes; late responses may overwrite newer state. | Apply independent results immediately, abort or serialize polls, use short control-plane deadlines, and show last-updated/stale indicators. [Refresh/action](../web/src/main.tsx#L153), [status's serial node calls](../gateway/app.py#L179). |
| F13 | P2, reproduced in backend and Chromium | After browser-session expiry, API calls return 401 but the UI stays signed in. Sign out also returns 401 before local state is cleared. The customer needs a full page reload to get the login form and may lose edits. | Centralize 401 handling; clear local auth even if logout fails; preserve a recoverable draft. [Fetch wrapper](../web/src/main.tsx#L83), [sign out](../web/src/main.tsx#L341), [logout authorization](../gateway/app.py#L172). |
| F14 | P2, source | Gateway startup waits for identity/reconciliation before the ASGI lifespan yields. A blackholed node or many slow session reads can delay the dashboard itself; this differs from the fast connection-refused case tested during the build. | Start the admin UI promptly in a clearly unready mode and recover asynchronously with bounded work; keep inference gated. [Lifespan](../gateway/app.py#L46), [recovery](../gateway/sessions.py#L42). |
| F15 | P2, partly reproduced | `helper_connected` means only that a socket path is configured; no helper request verifies it. `/healthz` always reports gateway liveness. Helper readiness accepts `/config` HTTP 200, which the native node can answer from wallet/config state without proving current RPC/provider serviceability. A hung, still-running node process is not restarted by the monitor. | Separate gateway, helper, node API, chain and model readiness; include health timestamps and degraded states. Avoid claiming helper connectivity or complete readiness from configuration alone. [Status](../gateway/app.py#L201), [helper readiness/monitor](../node_helper/app.py#L91), [native config handler](https://github.com/MorpheusAIs/Morpheus-Lumerin-Node/blob/99a8d86af1f9453d59797f9a208aa729ff3eb00d/proxy-router/internal/system/controller.go#L152). |
| F16 | P2, source | Recovery exposes only the latest 100 wallet sessions as raw JSON, with no pagination/search or transaction link. Binding checks wallet/model/provider/bid and a lower time bound, but cannot prove that the chosen session came from the uncertain submission; another matching same-wallet session may satisfy it. | Add paginated, legible recovery candidates and chain/receipt correlation. Keep unresolved cases explicit. [Wallet lookup](../gateway/node.py#L101), [binding](../gateway/sessions.py#L285), [recovery UI](../web/src/main.tsx#L1129). |
| F17 | P2, reproduced | Lowering global/per-model maximum sessions leaves the larger existing pool active and reusable. Two sessions can serve concurrently after both limits are changed to one. | Define whether this is an opening cap or target maximum. If a maximum, drain excess sessions without interrupting active work and show progress. [Reuse precedes cap check](../gateway/sessions.py#L125). |
| F18 | P2, partly reproduced / documented behavior | Closing a maintained session immediately leads to a replacement on the next tick. Expired maintenance deadlines still allow on-demand opens but cause them to close on the next idle tick. Near-expiry sessions are closed with a 135-second request buffer; replacements can leave an availability gap, especially with a one-session cap. Duration edits affect future opens only. | Offer “close this session” versus “stop maintaining this model,” show the effective schedule and replacement behavior, and explain next-open-only edits beside the controls. Consider planned overlap within wallet limits. [Maintenance](../gateway/sessions.py#L326), [expiry buffer](../gateway/sessions.py#L139), [UI controls](../web/src/main.tsx#L620). |
| F19 | P2, source / documented limitation | Saving an expanded allowlist does not undo a restrictive allowlist already loaded inside the node. A customer may see “saved” yet still have no candidates until restart. The “applied” indicator trusts the last gateway record and cannot detect manual file drift or a helper outcome lost during a restart. | Distinguish saved access policy from effective native ranking; show provider exclusion reasons and effective config hash read from the helper. Persist/reconcile apply jobs across lost responses. [Rating status](../gateway/app.py#L188), [candidate source](../gateway/sessions.py#L68), [apply completion](../gateway/sessions.py#L418). |
| F20 | P2, reproduced | Human percentages 10/20/30/30/10 show a 100% total but can fail exact floating-point validation. One invalid weight also prevents saving unrelated settings in the whole-policy form. | Use a representation/conversion tested against the pinned Go validator, validate inline with a useful correction, and avoid silently changing proportions. Consider isolated settings sections. [Exact validator](../gateway/models.py#L23), [UI total](../web/src/main.tsx#L909). |
| F21 | P2, source / UI gap | Keys have rate/concurrency settings in the API, but the browser hardcodes 60 requests/minute and two concurrent requests and provides no editor. A customer encountering 429 cannot adjust the limits using the promised web controls. Model-restricted keys can be stranded by changing/removing a model ID; key scope is not shown clearly in the table. | Add editable limits/scopes, show the current scope, and warn about affected keys when changing model IDs. [Key form](../web/src/main.tsx#L1166), [policy save](../gateway/app.py#L211). |
| F22 | P2, source | The catalog offers STT/TTS/embedding/unknown models as well as LLMs, but this gateway implements chat only. Manual IDs are checked for shape, not existence or capability. Names can generate invalid/duplicate aliases. A registered model is presented without verified provider availability. | Filter or disable unsupported capabilities, validate IDs and aliases with actionable errors, and distinguish registered/enabled/servable models. [Catalog mapping](../gateway/node.py#L67), [add-model UI](../web/src/main.tsx#L228), [catalog rendering](../web/src/main.tsx#L540). |
| F23 | P2, partly reproduced | Native JSON responses are largely unvalidated beyond parsing. Null/wrong-shaped bids or identity data can raise uncaught exceptions and produce generic 500 responses; invalid scores can crash the React render through `toFixed`. Conversely, `{}` or arbitrary dictionaries can be accepted as successful completion/chunk objects. | Validate native response schemas and supported completion shapes, translate errors consistently, and add a UI error boundary. Include malformed/partial response fixtures. [Node adapter](../gateway/node.py#L49), [candidates](../gateway/sessions.py#L72), [completion parsing](../gateway/app.py#L400), [score rendering](../web/src/main.tsx#L999). |
| F24 | P2, source-level operational risk | Session, key and operation records grow without pruning, while acquisition/status often deserialize full history. Synchronous SQLite writes/fsync run on the request loop. Disk-full/corruption/write failures lack an operator recovery mode; a write failure during cleanup can prevent later counters from being released. Docker logs also lack a configured rotation limit. | Index/query current records rather than all history, introduce retention/migrations, monitor disk, bound logs, separate optional audit writes from essential request cleanup, and test backup/restore plus write failure. [Store](../gateway/store.py#L38), [cleanup ordering](../gateway/app.py#L338), [Compose](../compose.yaml). |
| F25 | P2, source | Most node errors become “HTTP 500/502” without a safe explanation of gas, MOR, allowance, model or provider problems. `request_finished` does not record success/error, key, session or provider. Error responses omit the generated request ID. The Node & activity screen renders restart operations but discards the returned event list. | Add structured safe cause codes, request IDs on every response, outcome/route metadata without prompt text, and an actionable activity view. [Error mapping](../gateway/node.py#L48), [request event](../gateway/app.py#L350), [UI refresh](../web/src/main.tsx#L158). |
| F26 | P2, source | Prices under 0.0001 MOR/second display as `0.0000`; missing balances also display as zero. Failed pre-submission records are counted as “Open & pending sessions” because only `closed` is excluded. Customers can mistake unavailable data for an empty wallet or believe failed opens hold stake. | Use appropriate precision and explicit unknown/loading states, and count the actual live state set. [Amount formatter](../web/src/main.tsx#L105), [session count](../web/src/main.tsx#L315), [bid prices](../web/src/main.tsx#L1000). |
| F27 | P2, source | Refresh never reloads policy after initial load, even when this tab is clean. Another tab's saved changes remain invisible until a conflicting save forces a hard reload. Cached bid eligibility remains displayed after changing model selection or provider rules. No before-unload protection preserves drafts. | Refresh clean policy by revision, offer explicit conflict recovery preserving drafts, invalidate/reload ranking on its dependencies, and guard unsaved navigation/reload. [Load versus refresh](../web/src/main.tsx#L153), [selected model and cached bids](../web/src/main.tsx#L954). |
| F28 | P3, source | One-time key creation has no idempotency: a lost response leaves a live key the customer never received. Copy buttons depend on clipboard availability. Future scheduled dates are rendered using today's timezone offset, which can show a different time across a daylight-saving boundary. | Support retry-safe key creation or a clear revoke/recreate flow, provide select/copy fallback, and format the timestamp using its own timezone offset. [Key creation](../gateway/app.py#L240), [copy control](../web/src/main.tsx#L1220), [schedule conversion](../web/src/main.tsx#L675). |

## Deployment and integration acceptance gaps

These are not claims that the live system was observed failing. They are important customer journeys absent from the current evidence:

| Journey / dependency | Failure to validate before rollout |
| --- | --- |
| First installation | Secret ownership under Linux UID/GID 10001; invalid/missing RPC; wrong network; insufficient MOR/gas; invalid private key; no available provider; clear guidance without exposing secrets. |
| Hosted access | DNS/TLS issuance, browser origin mismatch, secure-cookie configuration, VM firewall, proxy timeouts, client compatibility, clipboard behavior and external reachability. |
| Native node integration | Actual released binary startup, auth file creation, real bid/session schema and provider handshake, allowance/open/close transactions, streaming and native session reconstruction. |
| Wallet concurrency | The gateway lock does not coordinate the native expiry worker or another wallet writer. Test actual nonce/replacement behavior; a dedicated wallet reduces external writers but does not eliminate native concurrency. |
| Restart under failure | Real node restart during opening, generation and stream disconnect; slow RPC; helper timeout; helper crash after file write but before gateway acknowledgement; process alive but hung; rollback after disk/write/readiness failure. |
| Availability over time | Warm-session replacement gaps, provider cooldowns, expired-session closure, insufficient balance after repeated replacements, gas spikes and claimable/time-locked stake. The dashboard currently has no stake-withdrawal action. |
| Recovery and durability | VM reboot, crash between submission and recording, disk full, database corruption, volume restore against newer chain state, secret rotation, copied deployments sharing a wallet, and upgrades requiring schema migration. |
| Sustained load | Concurrent models/keys, long generations, slow uploads/readers, large provider responses, browser polling and RPC rate limits, growing history and log retention. |

The existing supervisor test launches real OS subprocesses but replaces its readiness hook and does not launch the Morpheus binary. The existing browser test uses `FakeNode` and `FakeHelper`. Docker image builds establish packaging, not funded protocol correctness. These are useful tests with a narrower scope than live acceptance.

## Recommended repair sequence

1. **Session recovery and isolation:** F01–F04 and F11. Add durable typed operation outcomes and receipts, isolate per-model failures, remove hot-session reads from the wallet-mutation lock, and quarantine failing routes for future requests. Treat this as the first release gate.
2. **Customer controls and request correctness:** F05–F08, F12–F13 and F17–F23. Fix the reproduced save/restart/login defects, preserve drafts, validate requests before escrow, make limits/effective settings legible, and define deadline/retry semantics.
3. **Operational guardrails:** F09–F10, F14–F16 and F24–F28. Bound resources and wallet exposure, make health truthful, improve diagnostics, and provide safe operator recovery.
4. **Run live acceptance and publish sanitized evidence.** Exercise a dedicated test wallet/node/provider on the intended network, including induced failures. Do not use a passing characterization probe as a production acceptance test.

For each repair, convert the corresponding probe into a regression test that asserts the **desired** behavior. Keep financially consequential changes dependent on explicit transaction evidence, not guesses based on HTTP timeout or missing recent history.

## Reproduce the audit

From the repository root with dependencies installed:

```sh
.venv/bin/pytest -q
.venv/bin/python -m pytest -q audits/customer_experience/test_failure_modes.py
(cd web && npx playwright test --config audit/playwright.config.ts)
```

The first command tests the existing supported behavior. The second and third intentionally assert current defects; a pass means the failure reproduced. They are outside the default product test directories to avoid representing bad behavior as an acceptance contract.

See [backend probes](../audits/customer_experience/test_failure_modes.py) and [browser probes](../web/audit/customer-experience.spec.ts). No production fixes or live transactions were made as part of this audit.
