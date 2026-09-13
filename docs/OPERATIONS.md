# Operations and recovery

## Files and process ownership

Compose maintains `gateway-data` (SQLite and process lock), `node-data` (native node storage, cookie/auth files, rating JSON, backup and apply journal), and a shared `control` volume holding a Unix socket. The node helper owns one fixed node subprocess. It cannot accept arbitrary commands or paths, and its HTTP service is never published as a TCP endpoint.

The gateway itself does not receive the wallet private-key file. The node helper loads the wallet into its node process environment, as required by the upstream node. The gateway has the native node admin password and therefore must remain a trusted component. Anyone who can control the VM or container runtime can access these secrets.

Do not run Electron, another gateway, or other wallet-writing automation against this same dedicated wallet/node during normal operation. The gateway coordinates its own requests, not all possible users of a private key or the node's native expiry worker.

## Settings and restart semantics

Saving provider restrictions changes gateway eligibility immediately. In-flight inference is allowed to finish. Eligible selection is checked again after slow acquisition, immediately before dispatch. Rating weights and the node's own allow/deny filters change only after **Apply rating and restart** succeeds.

The pending indicator compares saved rating settings with the last successful apply recorded by the gateway. It does not detect arbitrary manual edits to the node's file. Treat this dashboard as the owner of that file; do not concurrently edit it by hand. The helper removes inherited legacy allowlist and inline rating environment values to avoid precedence surprises.

A restart closes admission, drains active work for up to 150 seconds, then waits for any current lifecycle operation before restarting the process. A normal drain timeout reports failure without stopping the node. Immediate restart skips the active-request drain but still waits for the lifecycle lock. It may terminate streams. The helper allows up to 90 seconds for native `/config` readiness; the gateway then checks identity and sessions. A failed operation never shows as successfully applied.

If the node exits unexpectedly, the helper attempts to restart it every five seconds. This process-monitor restart is different from an operator-requested graceful drain; requests can fail during a crash. The dashboard remains served by its separate container.

## Session errors

| State/error | Meaning and action |
| --- | --- |
| `pool_busy` / 429 | All permitted slots are in use or the queue deadline elapsed. Retry later; consider higher pool limits if the wallet supports them. |
| `no_eligible_provider` | Current rated bids and your access policy produce no candidate. Check the provider addresses and apply pending native rating changes. A catalog entry is not a capacity guarantee. |
| `open_unknown` with a known chain ID | The gateway records an opening but could not verify its result. Background reconciliation reads the node until its state is known. |
| `open_unknown` without an ID | A submitted operation may have succeeded even though the response was lost. Inspect wallet sessions and chain history; bind the correct ID in the Sessions screen. Wallet, model, provider, bid, ID, and opening time are verified before adoption. |
| `close_pending` | A close response or confirmation was uncertain. Background reads can confirm closure. Do not repeatedly click close until you have checked the prior transaction outcome. |
| `node_identity_changed` | The node now uses another wallet or network. Restore the intended node environment. Do not delete the database just to silence this check. |
| `unsupported_node` | This release requires v7.11.0. Restore the pinned version or validate/update the adapter. |
| `stream_interrupted` | The provider stream failed or sent malformed/control data. The gateway emits a structured SSE error and does not replay the prompt. |

There is deliberately no “forget uncertain open” button. If no matching session appears, the current UI cannot prove that the transaction never happened. Reconcile it against complete on-chain transaction history before engineering an administrative state correction. Closing, rather than a nonexistent recovery RPC, is the upstream mechanism for releasing a session's unused stake. Claiming matured, day-locked stake is a separate native operation and is not implemented by this dashboard.

## Backups

Pause admission, wait for active requests and lifecycle operations to finish, and stop the two application containers before taking a consistent copy of their persistent volumes and secret files. Keep the same wallet, node storage and gateway database together. Encrypt backup storage and restrict access; never commit a backup to this repository. Do not use `docker compose down -v` unless you intentionally want to destroy local state.

Restoring local files does not roll back blockchain state. Start the node and gateway and let reconciliation verify the current session truth. The process lock prevents two workers from sharing one data directory but cannot stop another installation with copied files or the same private key from running elsewhere.

## Credential changes

Revoke application keys in the browser; keys are returned once at creation and only hashes are stored. Existing admitted requests can finish. Dashboard sessions expire after 12 hours and can be logged out.

Admin-token rotation requires replacing its host secret and recreating the gateway; existing browser sessions remain valid until logout/expiry. Emergency global browser-session invalidation currently requires an operator database change while the gateway is stopped; automatic token-bound session invalidation is future work.

Native node-password rotation is not a simple environment change: the upstream node seeds `.cookie` on first start and then reuses it. Coordinate changes to the persisted cookie/auth configuration and both services while stopped. Do not overwrite the wallet secret or switch chains on an existing installation without handling its live sessions and recorded identity first.

## Privacy and exposure

The gateway event log stores event types, IDs, model/provider identifiers and timing; it does not store prompt or response text. Uvicorn access logging is disabled in the packaged commands. The node is configured with chat-context storage/forwarding off and reduced log levels. Independent providers still process prompts, and native logging behavior should be verified in live acceptance. This is not a promise of end-to-end prompt confidentiality or TEE coverage beyond the selected node/provider behavior.

Do not expose port 8082 or the helper socket publicly. Application keys authorize inference, not administration. The dashboard uses an HttpOnly session cookie, strict same-site policy, a CSRF token and exact-origin checks. Put the public endpoint behind HTTPS and any network access controls appropriate to your own deployment.
