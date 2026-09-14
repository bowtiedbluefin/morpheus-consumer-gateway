# Production acceptance campaign

Read [the report](../../docs/PRODUCTION-READINESS-REPORT.md) before using these tools. Passing the regular regression suite does **not** clear the separately reproduced release blockers.

## Reproducible wallet-free checks

From the repository root:

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest audits/production_readiness/test_release_gates.py -q
.venv/bin/python -m devtools.soak --seconds 120 --output data/cold-soak.json
.venv/bin/python -m devtools.soak --warm --seconds 3600 --rps 60 --output data/warm-soak.json
```

The second command intentionally fails while the documented defects remain. It is excluded from the normal green regression suite; it must pass before declaring those release gates fixed. The cold soak currently reproduces an acquisition hang; the runner bounds client waits and terminates its disposable service. `--warm` seeds eight fake sessions and measures steady-state behavior only. It must not be used to claim that cold starts passed. `SOAK_DEBUG=1` adds a diagnostic policy-read limit that changes failure behavior; diagnostic runs are not performance measurements.

Browser regressions use a fresh demo database for each invocation:

```sh
cd web
TEST_BROWSER=chromium npm test
TEST_BROWSER=firefox npm test
TEST_BROWSER=webkit npm test
npx playwright test -c playwright.production-audit.config.ts
```

Install the corresponding browser binaries with `npx playwright install chromium firefox webkit`. The last command reproduces the mobile overflow and currently fails. Failure traces contain dummy credentials only; do not enable traces on a funded deployment.

## Funded campaign reproductions

The `live/` scripts preserve the campaign scenarios. They are **not** a parallel or unattended run-all suite. Run them sequentially against a dedicated local Compose installation, with `core.py` first to create the restricted test key. They read the existing `secrets/admin-token` and `secrets/production-test-api-key` files, write ignored local evidence, and change saved policy. Some stop containers or kill the native child process. Keep an initial policy snapshot and restore the desired settings after a campaign. Revoke the campaign key afterward.

Every script requires `--allow-wallet-transactions`; interruption scenarios additionally require `--allow-service-interruption`. This is an explicit CLI safeguard for future executions. This campaign's user had already authorized funded sessions and autonomous testing.

| Script | Scenario / prerequisite |
| --- | --- |
| `core.py` | Invalid requests, authorization, four cold concurrent requests, streaming, request idempotency; prepares test key and bounded campaign policy. |
| `concurrency.py` | Four long requests with all providers allowed; expects `core.py` preparation. Content markers alone are not a reliable routing oracle for a reasoning model with a token limit. |
| `lifecycle.py` | Stream disconnect, graceful restart, rating application, post-restart inference, repeated close. A `close_pending` response requires later chain reconciliation. |
| `gateway_crash.py` | Kills the gateway after a journaled approval and before the opening completes; verifies recovery of one escrow and duplicate-request rejection. |
| `node_crash.py` | Kills the native node during a stream; checks structured failure and supervisor recovery. Immediate subsequent inference may correctly return `wallet_pending` until cleanup confirms. |
| `multimodel.py` | DeepSeek and Llama concurrently. Provider availability, latency, and stake estimates are live variables; this campaign did not get two successful model responses. |
| `maintain.py` | Uses 301-second sessions with a finite seven-minute warm deadline. The original 300-second run failed with `SessionTooShort`; 301 is an experimental boundary check, not the UI default. |
| `orphan.py` | Requires paused admission and no managed live sessions. Stops the gateway, opens one native session, restarts the gateway, verifies live-orphan protection and expired-orphan cleanup. |
| `sdk.py` | Requires the `openai` Python package. Tests listing, Unicode conversation history, JSON output, tool calls, streaming usage, and typed wrong-model errors with client retries disabled. |

Example, **only after preparing a funded test installation**:

```sh
.venv/bin/python audits/production_readiness/live/gateway_crash.py \
  --allow-wallet-transactions --allow-service-interruption
```

Do not clear unknown operations, delete wallet journals, or delete funded volumes to make a test pass. A failed request can still have a mined opening. Read the session record, native journal, contract state, and receipt before deciding whether a new wallet operation is safe.

## Evidence

Raw campaign artifacts are in ignored `data/production-readiness/`: JUnit XML, Playwright JSON, live session/operation snapshots, transaction journals, receipts, SDK outputs, soak samples, dependency audits, and the wallet-monitor timeline. The committed report and evidence summary contain sanitized results. Local screenshots include public wallet identifiers and balances; no admin/API/private-key values are included in the report.
