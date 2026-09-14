# v0.2 implementation status

The gateway is a separate hosted package with FastAPI, React, SQLite, a local supervisor and a pinned, patched Morpheus node. The [recovery and audit-fix report](RECOVERY-AND-AUDIT-FIXES.md) is the detailed implementation record, including the disposition of all 28 audit findings and the source reuse decisions.

Implemented: model selection/aliases, session duration and retention, maintained sessions, allow/blocklists and native rating configuration, application keys/scopes/rates, streaming chat, graceful/immediate node restart, background invalidation and closure, wallet-history recovery, durable transaction correlation, mature MOR withdrawal, exact native stake bounds, reserve checks and web diagnostics.

The bundled node patch is required. Electron is not packaged. The hosted API patterns were adapted without its billing, Redis or Postgres dependencies. The actual API and node/Electron Git checkouts remain in the parent research workspace; pinned source references are in the report and original DESIGN.md.

Remaining work before unattended funded deployment is live acceptance: actual provider negotiation, chain transactions and day-lock maturity, receipt/restart fault injection, gas behavior, load and backup/restore. Native journals are retained and need disk monitoring. Unknown transaction outcomes without sufficient evidence still require operator investigation. Exact gas-fee caps, multi-wallet/tenant isolation, persistent browser drafts, metrics export, systemd integration and additional inference APIs are outside this update.

See [validation evidence](VALIDATION.md), [operations](OPERATIONS.md) and [the original design](DESIGN.md). The original design/audit describe their historical baselines; the recovery report supersedes their implementation-status statements.
