# Customer scenario coverage and release acceptance

> Historical campaign record. The eight identified findings have since been addressed; see the [remediation report](REMEDIATION-REPORT.md) for current results and remaining acceptance work.

Read this with the [actual results report](PRODUCTION-READINESS-REPORT.md). “Controlled” means an automated fixture or fault injection, not a funded production-network observation. Passing one layer does not imply another passed. The final release must be tested against the same application, node binary, database schema and deployment configuration that will ship.

## Implemented and exercised

| Customer action or failure | Evidence exercised | Current conclusion |
| --- | --- | --- |
| Use a wrong, blank, whitespace-only, unknown-ID or look-alike model | Controlled parameterized cases; funded HTTP negative requests | Rejected before opening escrow. |
| Use an alias, alias capitalization, or configured on-chain model ID | Controlled routing; real DeepSeek alias | Supported configured forms resolve correctly. |
| Request a disabled model or model outside the key's scope | Controlled API tests | Denied with distinct errors. |
| Send invalid messages, options, routing overrides, or oversized bodies | Controlled and selected real HTTP negatives | Common cases rejected; arbitrary non-finite extensions fail PR-03. |
| Send an extremely slow body or exceed streamed-body limit | Actual loopback TCP | Bounded 408/413 without wallet work. |
| Send JSON/Unicode multi-turn prompts through a standard SDK | Real provider, SDK retries disabled | Passed. |
| Request JSON output or tool calls | Real provider/SDK | Passed for tested DeepSeek provider. |
| Receive streaming output and usage | Real provider/SDK plus fragmented TCP fixtures | Passed normal operation. |
| Receive truncated, malformed, oversized or disconnected streams | Controlled framing faults and native-process kill | Structured errors; no false completion or prompt replay. |
| Receive invalid complete response or non-finite usage | Controlled provider fixtures | Failures preserved in PR-05. |
| Request while model is cold | Controlled cases plus real HTTP cold burst | Simple cases pass; completed-opener race can hang entire process, PR-01. |
| Send eight overlapping requests with capacity 1, 2 or 4 | Barrier-controlled inference and isolated markers | Capacity/lease invariants pass under that scheduling. Does not clear PR-01. |
| Send four real concurrent prompts | Single-provider and multi-provider runs | Single-provider 2/4 succeeded; multi-provider 4/4 HTTP 200 but one unretained content-marker failure. |
| Ask two different models concurrently | Controlled successful overlap and funded DeepSeek/Llama overlap | Routing overlap works; second live provider timed out. Full live acceptance incomplete. |
| Saturate one key while another customer sends a prompt | Controlled overlapping requests | Other key retains admission. |
| Exceed connection and queue limits | Actual TCP admission checks; 8–256 request bursts | Bounded rejection and released capacity on warm fixtures. |
| Wait for a busy healthy session after extra capacity fails | Controlled reproduction and live capacity shortage | Returns prematurely, PR-04. |
| Disconnect or cancel while waiting/opening/inference | Controlled cancellations and real streaming disconnect | Leases released; uncertain opening preserved rather than replayed. |
| Reuse an existing session | Controlled lifecycle; real repeated inference | Passed. |
| Change provider allow/deny policy during reuse or opening | Controlled races; browser provider controls | Denied provider is not dispatched; raced opening quarantined. |
| Stop maintaining a model or lower the pool limit | Controlled lifecycle | Excess capacity retires; configured stop is honored. |
| Keep a model hot to a finite deadline | Funded 301-second run with three sessions | Replaced and stopped correctly; replacement has an availability gap. |
| Select minimum five-minute session | Funded 300-second attempts and direct native diagnostic | Contract rejects derived duration; two approvals consumed gas, PR-02. |
| Close a session twice | Funded pending close plus later reconciliation | One close operation/transaction; eventually closed. |
| Lose HTTP response during opening/closing/withdrawal | Controlled journals/receipts; funded gateway SIGKILL during opening | Avoids unsafe duplicate wallet submission in tested paths. |
| Kill gateway during approval/open | Actual Docker SIGKILL with durable native journal | Recovered one escrow and served a later prompt. |
| Kill node during stream | Actual native-child SIGKILL | Stream fails explicitly; supervisor restarts; temporary wallet reconciliation delay. |
| Restart node with active inference and apply rating | Funded graceful restart; browser action checks | Existing request drains, admission pauses, rating applies, inference resumes. |
| Apply invalid node configuration or exhaust drain timeout | Controlled supervisor using real child processes | Rollback and bounded failure exercised. |
| Leave an expired session absent from gateway database | Funded native orphan plus automated wallet pagination | Live orphan protected; expired orphan discovered and closed. |
| Withdraw MOR after contract hold matures | Real midnight-UTC monitoring; controlled thresholds and reversed/unknown receipts | Passed automatically after midnight UTC; independent receipts and restored balances confirmed. |
| Lack MOR/ETH, exceed session stake or total allocation | Controlled guards; live expensive Qwen bid | Preflight budget guards exercised; no funded over-cap opening. |
| Temporarily lose SQLite writes | Real SQLite page exhaustion, read-only fault, HTTP injection | Financial work fails closed; readiness recovery is broken, PR-06. |
| Restart after killed database writer | Actual subprocess SIGKILL and WAL recovery | Acknowledged committed entry survives. |
| Start with corrupt database or restore older snapshot | Actual corrupt file and controlled newer chain truth | No silent reset; reconciliation exercised. Full VM backup restore outstanding. |
| Prune old history containing uncertain financial records | Controlled retention checks | Uncertain records preserved. |
| Use wrong API/admin credentials, CSRF, foreign origin | Controlled requests plus live unauthorized restart | Protected; no unauthorized wallet/restart operation in tested cases. |
| Revoke or rotate credentials while requests wait | Controlled races and campaign-key cleanup | Revocation honored; only test key revoked at cleanup. |
| Edit settings in two tabs or during a pending save | Controlled API revisions and three-browser UI tests | Saved revision protected; newer unsaved draft preserved. |
| Lose dashboard login or hang one refresh endpoint | Three-browser controlled regressions | Sign-in remains accessible; independent data/actions remain usable. |
| Administer on a phone with precise balances | Actual browser and deterministic 390 px fixture | Horizontal overflow to 656 px, PR-07. |
| Keep requesting for an hour | Real HTTP/SQLite, simulated 50 ms provider, eight preseeded sessions | Final totals in report. Excludes cold opening, chain and real-provider throughput. |
| Expose secrets/prompts in application events or logs | Controlled endpoint checks; actual container-log canary scan | No configured secret or selected prompt canary found in captured logs. |
| Run unprivileged containers without wallet key in gateway | Inspection of actual running containers | Confirmed; remote ingress and host protection still need testing. |
| Ship dependencies with known advisories | Python/npm scans; actual native binary and source govulncheck | Python/npm clean at scan time; native advisory gate fails, PR-08. |

## Remaining acceptance checks before release

These are explicit pending scenarios, not results from this campaign. Priority 0 blocks release; priority 1 is required operational acceptance; priority 2 expands support beyond the tested initial configuration.

| Priority | Scenario to run | Required evidence |
| --- | --- | --- |
| 0 | Repeat cold bursts, task-completion callback races, expiry/open races, and canceled cold callers after PR-01 fix | No process starvation; health/admin respond; every request meets its deadline; no duplicate opening. |
| 0 | Validate recursive JSON numbers, nesting, malformed/duplicate keys and supported provider response shapes | Reject invalid input before escrow; serialize/validate output before recording success; valid tool/reasoning extensions survive. |
| 0 | Retry minimum-duration estimation with exact boundary and changed contract inputs | Effective on-chain duration meets promise; deterministic failure backs off; mined approvals appear in history. |
| 0 | Retest busy-session waiting after capacity decline with cancellation, policy changes and key revocation | Eligible customers wait fairly within deadlines; no uncertain wallet retry or inference replay. |
| 0 | Resolve native advisories against a pinned rebuild | Each advisory remediated or supported by configuration-specific reachability evidence; rerun native/funded protocol tests on that binary. |
| 0 | Promote all repaired release gates into CI | Eight backend cases and mobile regression pass without expected-failure markers; cold HTTP test remains bounded. |
| 1 | Serve two independently healthy real models and all advertised features | Both requests succeed concurrently in separate sessions; JSON and SSE canaries per supported model/provider. |
| 1 | Run target Linux VM with real HTTPS ingress | Secure cookies/origin, authentication, certificate renewal, response limits and SSE flush timing work through actual proxy. |
| 1 | Reboot VM and restart Docker with pending wallet work | Durable journal/SQLite mounts return; one owner; no duplicate escrow; UI accurately reports recovery. |
| 1 | Perform paired-volume/secret backup restore on another VM | Restored identity/policy/keys, wallet reconciliation, closed/open/unknown states and exclusive ownership verified. |
| 1 | Recover writable storage after disk exhaustion and read-only filesystem | Safe refusal while broken; explicit recovery and readiness return after a verified write/reconciliation. |
| 1 | Run 24–48 hours with real providers, scheduled renewals and injected outages | Stable memory/disk; bounded retention/queues; full close/withdraw ledger; recorded model-specific latency/error targets. |
| 1 | Force RPC timeouts, rate limits, stale responses and provider disconnects at each submission stage | Durable distinction between not-submitted and unknown; no duplicate mutation; eventual recovery with accurate UI state. |
| 1 | Force pending, dropped, replaced and reverted transactions on controlled chain/testnet | Nonce ownership, receipt confirmation/reversal and gas reserve behavior verified without stranding mainnet funds. |
| 1 | Simulate chain reorganization and RPC disagreement | Previously observed receipt can be invalidated without unsafe duplicate submission or incorrect financial success. |
| 1 | Exercise deployed network faults between browser, gateway, helper, node, provider and RPC | Independent health diagnosis and bounded recovery for each failure; avoid blaming wallet shortage for transport failure. |
| 1 | Validate alerts and operator runbook | Detect stuck opening, stale recovery, depleted gas, persistent readiness failure and overdue withdrawal; explain corrective action. |
| 1 | Scan final container OS layers and review external exposure | Image advisory inventory, firewall/port checks and supported upgrade procedure for exact release artifacts. |
| 1 | Test mobile/tablet layout and accessibility with realistic long values | No clipping; visible controls, keyboard access, readable error states and exact-balance access. |
| 2 | Certify additional SDK versions and default retry behavior | Explicit retry/idempotency contract; no accidental extra paid work after an ambiguous client timeout. |
| 2 | Support more endpoints, model modalities or networks | Endpoint-specific contract tests and funded network-specific lifecycle tests before advertising support. |
| 2 | Validate upgrades and rollbacks between shipped schema/node versions | Preserved keys/policy/journals; no destructive downgrade; recovery of transactions begun on previous version. |

## Invariants that should gate every funded test

- Every funded session has a known wallet, model, provider, stake, opening transaction and closing outcome.
- An unknown submission is never treated as permission to submit a replacement blindly.
- A single session has at most one active inference lease; responses remain attached to their original request.
- Authentication, input validation and durable intent precede wallet mutation.
- A stream error or disconnect never becomes a successful completion in the response or activity history.
- Cleanup leaves zero campaign-owned live sessions, no unresolved submitted operations, a reconciled liquid/held MOR total, and an exact gas ledger.
- Restored user policy and keys are checked explicitly; campaign credentials are revoked.

No finite suite proves that every production failure is impossible. This matrix defines observable acceptance criteria and makes remaining uncertainty visible.
