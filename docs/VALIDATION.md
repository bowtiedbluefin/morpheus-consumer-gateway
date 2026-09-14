# Validation record

**Superseded by the [September 13–14 production-readiness campaign](PRODUCTION-READINESS-REPORT.md).** The latest decision is NO-GO despite passing regression tests. The record below describes the earlier implementation checkpoint; it does not include the subsequent funded crash/cleanup/load results or newly discovered release blockers. See the [current scenario matrix](PRODUCTION-TEST-MATRIX.md) for remaining acceptance work.

Recovery update v0.2 verified September 13, 2026. The automated checks below use simulated wallets/providers. A separately authorized live opening and inference test also passed; see the live-test record below.

## Completed locally

| Check | Result and evidence |
| --- | --- |
| Python backend tests | 61 passing tests in `tests/`; FastAPI/SQLite behavior with a simulated node and an HTTPX transport contract fixture |
| Real subprocess supervision | Supervisor test starts real child processes, confirms a different PID after restart, confirms old processes exit, and restores the previous config after a deliberately broken child startup. Its readiness hook is simulated; it is not a real-node readiness test. |
| Browser acceptance | Six Playwright Chromium cases: four audit regressions (save race, expired login, restart intent, stalled refresh), wallet recovery controls, and sign in, choose catalog model, edit duration/retention, save policy, create an API key, send JSON and SSE chat requests, observe session reuse, block provider, reject subsequent inference, apply rating/restart, inspect success, desktop/mobile layouts without page errors |
| Frontend | TypeScript check and Vite production build pass; Prettier passes |
| Backend static checks | Ruff lint and format checks pass |
| Packaging | Both Compose images build, including compilation of the pinned, patched v7.11.0 proxy-router binary, five native stake/journal/serialization tests and its license notice |
| Packaged gateway smoke | Built container serves `/healthz` and production dashboard assets as UID 10001 while its node address is unavailable; temporary container removed after test |

Backend coverage includes authentication and CSRF, actual request byte limits, policy revisions, provider exclusion during slow opening and reuse, cold-open concurrency, session reuse, idle/expiry/maintain modes, bounded pre-submission provider walking, uncertain openings, cancellation, binding rejection for a different wallet, pending-close recovery, key revocation while waiting, rate limiting, restart draining/timeout/immediate/failure reporting, inference deadlines, stream errors without gateway replay, prompt-free event records and exclusive process ownership.

Recovery coverage includes contract-held versus available MOR, withdrawal thresholds/gas/backoff, unknown withdrawal deduplication, journal-confirmed outcomes, failed receipts, uncertain opening recovery, scans beyond 100 wallet sessions, opt-in live-orphan cleanup, pending-close idempotence, stake budgets, healthy-model maintenance isolation, hot reuse during wallet work, stale reconciliation races, provider cooldown, invalid prompts, POST idempotency, credential rotation and pool-cap reduction.

The checked-in dashboard image was captured from the automated browser flow and is visibly labeled **DEMO**. Its balances, provider, and response are simulated.

## Authorized local live test — September 13, 2026

Started the bundled node and gateway against Base mainnet using operator-provided, locally ignored credentials. Verified live wallet/network identity and rated bids for `deepseek-v4-flash`. Opened one 1,800-second session through the gateway with a 10 MOR per-session ceiling. Two providers failed before submission; the third opened successfully. Actual escrow was approximately 2.678757 MOR. Approval and opening receipts both had status 1. A model-scoped application key sent a small prompt through `/v1/chat/completions`; HTTP 200 returned the requested text, using the same session.

The test exposed and fixed missing explorer/network defaults, a missing native storage directory, and a nonexistent single-model lookup route. The adapter now uses the native paginated catalog. Regression tests cover packaged startup defaults for both supported networks and the actual model lookup route.

Private credentials and detailed session evidence remain in ignored local `secrets/` and `data/` files. This test does not validate expiry closure, day-lock maturity, withdrawal, crash recovery, load or backup restoration. Those cases remain below.

## Still required: live acceptance

Use a dedicated, appropriately funded test wallet and an eligible provider on the selected network. This procedure is for the installation operator; the build agent did not execute it.

1. Follow the README on a Linux VM. Check secret file permissions, actual image version, chain, wallet address, RPC connectivity, model discovery and rated bids. Verify the wallet/network against the intended environment before enabling a model.
2. Start with one permitted provider, one model, a short session duration and a one-session cap. Check wallet balances and native allowance/stake behavior. Send one cold prompt and record the real session ID and transaction outcome. Send another and verify reuse without another open.
3. Send streaming inference and confirm standard SSE chunks and termination. Disconnect a client midstream and verify lease release and the native node's actual behavior. Try two concurrent requests to confirm pool/queue semantics.
4. Block the provider and verify no new dispatch reaches it; confirm the idle session closes. Apply rating changes, verify the actual rating file and node restart, then verify post-restart identity, session reconstruction and inference.
5. Restart gracefully during a slow response, then separately test explicit immediate restart. Verify dashboard availability and accurate status. Inject node startup failure and verify the previous rating file is restored.
6. With test-only fault injection, interrupt the response to a successful opening and restart the gateway. Verify that it records uncertainty rather than opening a duplicate. Verify automatic recovery from the native durable journal, then separately test wallet-history correlation and verified binding. Close the recovered session. Check balances and stake release with the node/chain.
7. Verify gateway-owned expiration cleanup (native cache rehydration remains enabled), maintained-session replacement, serialization inside the patched node, insufficient MOR/gas, provider unavailability, and failure after partial output. Close enough test sessions to exercise withdrawal batching; wait for contract lock maturity and confirm eligible MOR returns to the wallet while still-locked MOR remains held. Interrupt a withdrawal response and restart both services; confirm no duplicate is sent. Record where behavior differs from static review.
8. Validate HTTPS certificate provisioning, request size/timeouts, backup/restore, secret rotation, and log redaction. Confirm that only intended ports are reachable from outside the VM.

Do not treat simulated tests or a successful image build as evidence that these live acceptance cases passed. Publish sanitized outcomes, node image digest and network/provider identifiers before describing the release as validated for unattended operation.
