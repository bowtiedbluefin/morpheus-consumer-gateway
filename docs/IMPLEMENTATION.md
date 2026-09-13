# v0.1 implementation and remaining work

This document is the implementation status. [DESIGN.md](DESIGN.md) preserves the full source review and target plan; recommendations there are not a claim that every proposed feature ships in v0.1.

## Implemented

The new standalone package contains a working FastAPI service, React web dashboard, SQLite session/policy/key persistence, a typed adapter for node v7.11.0, a local node supervisor, Docker Compose deployment, and automated backend/browser checks.

The browser exposes model aliases, enabled models, duration, idle retention, expiry retention, continuously maintained sessions, an optional maintenance end time, per-model/global pool limits, queue timeout, strict allowlist and blocklist, the five native rating weights, keys, session inspection, verified binding of uncertain opens, close, pause, and drain/immediate/apply-rating restarts. Configuration edits use revision checks so a stale browser cannot silently overwrite newer settings.

The runtime builds on the node's existing high-level open-by-bid flow. It adds gateway ownership and serialized lifecycle operations around that flow. Gateway state is written before chain-mutating calls. Ambiguous opens block further opens; a known session ID is reconciled automatically, while an unknown ID requires explicit, verified operator binding. Existing verified sessions can remain reusable during an unrelated uncertain open. Inference uses a separate generated chat ID for each request and does not forward client routing or node-auth headers.

Warm maintenance targets an available session, not a reserved GPU or a fixed pool size. Normal expiration is handled by the node first; the gateway checks state and repairs overdue closures. A session with less than the request timeout plus a 15-second buffer is closed/replaced rather than assigned another request. Replacement may cause a gap, particularly at a one-session cap. Changing duration affects subsequent opens. Disabling a model or blocking its provider prevents new dispatches and closes managed sessions once active work finishes.

For provider selection, the gateway consumes the node's currently rated bids and independently filters them. At most three distinct providers are tried on exact, pinned pre-submission rejection codes. All other open failures remain uncertain; inference failures are surfaced without gateway replay.

## Source reuse decisions

- **Node:** reuse its released proxy-router binary without patching its protocol, wallet, handshake or transaction code. Implement a small adapter against its inspected routes and schemas.
- **Hosted API:** reuse the design patterns—model resolution, session reuse, durable ownership, key hashing, stream cleanup—through a new lightweight implementation. No hosted billing/auth/Redis/Postgres stack and no wholesale API source copy.
- **Electron:** reuse domain knowledge and configuration/session semantics. Build a browser-native UI because Electron's IPC and local-process assumptions cannot run in a hosted webpage.

The reviewed source commits and specific file references are in DESIGN.md. The API and node (including Electron) were actually cloned and inspected in the parent research workspace. This separate repository intentionally does not vendor those large checkouts.

## Next engineering work, in order

1. **Live acceptance on a dedicated test wallet.** Exercise the unmodified v7.11.0 binary, real RPC, eligible provider, MOR allowance/open/close, SSE, actual restart, and state recovery. Capture sanitized evidence. Resolve any source-versus-runtime differences before unattended production use.
2. **Economic guardrails.** Show an estimated stake and gas requirement; add balance/headroom policy and pending-reservation accounting. A hard bound on escrow requires extending the node's high-level opening contract or an upstream change. Label estimates accurately until then.
3. **Recovery completion.** Discover ambiguous operations through transaction receipts/events and wallet history with durable submission identity. The current 100-session inspection window and verified manual binding are conservative, but an operation with no session found cannot simply be forgotten: absence from that window does not prove no transaction occurred. Build a documented operator resolution path with chain evidence.
4. **Integration coverage.** Fault-inject actual node restarts during opening/streaming; verify cancellation over real sockets; test rate/concurrency under load; test native nonce coordination; test HTTPS certificate provisioning on a VM; record memory, disk and cold/warm latency.
5. **Operational hardening.** Add metrics/readiness, bounded request time including slow HTTP body upload, quotas on persisted historical records, credential rotation tooling, backups/restore automation, and schema migrations. Add a service-scoped native auth account only after testing its allowance-agent semantics.
6. **Capability expansion.** Add provider health cooldowns and configurable bounded failure policy; add new inference endpoints only with contract tests. Consider separately supported existing-node/systemd deployment. Multi-wallet or multi-tenant hosting is a separate architectural step.

## Decisions still requiring product input

| Question | Current choice | Tradeoff |
| --- | --- | --- |
| Who hosts it? | One consumer/team on their VM | Simple ownership; no SaaS account or billing |
| Node deployment? | Bundled, pinned container plus local supervisor | Reliable process control; existing systemd installations need another adapter |
| Session “hot” behavior? | Maintain availability through replacements, optionally until a deadline | Extra transactions and escrow; provider capacity is still external |
| Wallet budget? | Operator-funded wallet with count limits | Usable MVP; not a hard monetary budget guarantee |
| Provider policy scope? | Installation-wide | Clear control; per-model exceptions would need additional UI and enforcement |
| API key privileges? | Optional model restrictions and rate/concurrency limits | Shared wallet/session pool, no accounting isolation |
| Request failures? | No inference replay; narrow pre-open provider retry | Less transparent recovery, avoids guessing after uncertain mutations/output |
| Public distribution? | Initial repository created private | Owner can choose visibility after review and live acceptance |
| Future node versions? | Explicitly rejected until adapter validation | Prevents silent contract drift; upgrades require maintenance |

The API's convenience is implemented here. Live network acceptance and the remaining operational guardrails determine when it is appropriate to run unattended with a funded wallet.
