# Validation record

Initial build verified September 13, 2026. No consumer wallet secret was used, no real provider inference was requested, and no blockchain transactions were submitted during this build.

## Completed locally

| Check | Result and evidence |
| --- | --- |
| Python backend tests | 33 passing tests in `tests/`; FastAPI/SQLite behavior with a simulated node and an HTTPX transport contract fixture |
| Real subprocess supervision | Supervisor test starts real child processes, confirms a different PID after restart, confirms old processes exit, and restores the previous config after a deliberately broken child startup. Its readiness hook is simulated; it is not a real-node readiness test. |
| Browser acceptance | Playwright Chromium: sign in, choose catalog model, edit duration/retention, save policy, create an API key, send JSON and SSE chat requests, observe session reuse, block provider, reject subsequent inference, apply rating/restart, inspect success, desktop/mobile layouts without page errors |
| Frontend | TypeScript check and Vite production build pass; Prettier passes |
| Backend static checks | Ruff lint and format checks pass |
| Packaging | Both Compose images build, including the pinned upstream v7.11.0 proxy-router binary and its license notice |
| Packaged gateway smoke | Built container serves `/healthz` and production dashboard assets as UID 10001 while its node address is unavailable; temporary container removed after test |

Backend coverage includes authentication and CSRF, actual request byte limits, policy revisions, provider exclusion during slow opening and reuse, cold-open concurrency, session reuse, idle/expiry/maintain modes, bounded pre-submission provider walking, uncertain openings, cancellation, binding rejection for a different wallet, pending-close recovery, key revocation while waiting, rate limiting, restart draining/timeout/immediate/failure reporting, inference deadlines, stream errors without gateway replay, prompt-free event records and exclusive process ownership.

The checked-in dashboard image was captured from the automated browser flow and is visibly labeled **DEMO**. Its balances, provider, and response are simulated.

## Still required: live acceptance

Use a dedicated, appropriately funded test wallet and an eligible provider on the selected network. This procedure is for the installation operator; the build agent did not execute it.

1. Follow the README on a Linux VM. Check secret file permissions, actual image version, chain, wallet address, RPC connectivity, model discovery and rated bids. Verify the wallet/network against the intended environment before enabling a model.
2. Start with one permitted provider, one model, a short session duration and a one-session cap. Check wallet balances and native allowance/stake behavior. Send one cold prompt and record the real session ID and transaction outcome. Send another and verify reuse without another open.
3. Send streaming inference and confirm standard SSE chunks and termination. Disconnect a client midstream and verify lease release and the native node's actual behavior. Try two concurrent requests to confirm pool/queue semantics.
4. Block the provider and verify no new dispatch reaches it; confirm the idle session closes. Apply rating changes, verify the actual rating file and node restart, then verify post-restart identity, session reconstruction and inference.
5. Restart gracefully during a slow response, then separately test explicit immediate restart. Verify dashboard availability and accurate status. Inject node startup failure and verify the previous rating file is restored.
6. With test-only fault injection, interrupt the response to a successful opening and restart the gateway. Verify that it records uncertainty rather than opening a duplicate. Recover the actual session through verified binding and close it. Check balances and stake release with the node/chain.
7. Verify native expiration cleanup, maintained-session replacement, wallet transaction behavior under concurrent native cleanup, insufficient MOR/gas, provider unavailability, and failure after partial output. Record where behavior differs from the static source review.
8. Validate HTTPS certificate provisioning, request size/timeouts, backup/restore, secret rotation, and log redaction. Confirm that only intended ports are reachable from outside the VM.

Do not treat simulated tests or a successful image build as evidence that these live acceptance cases passed. Publish sanitized outcomes, node image digest and network/provider identifiers before describing the release as validated for unattended operation.
