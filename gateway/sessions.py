import asyncio
import time
import uuid

from .config import Config
from .models import HASH, ModelPolicy
from .node import GatewayError, Helper, Node
from .store import Store

LIVE = {"opening", "open_unknown", "open", "quarantined", "orphaned", "draining", "closing", "close_pending"}


class Sessions:
    def __init__(self, store: Store, node: Node, helper: Helper, cfg: Config):
        self.store, self.node, self.helper, self.cfg = store, node, helper, cfg
        self.lock = asyncio.Lock()  # Wallet mutations only; borrowing hot sessions never takes it.
        self.active = set()
        self.waiting = 0
        self.maintenance = False
        self.identity = None
        self.ready = False
        self.last_error = None
        self.tasks = set()
        self.opening = {}
        self.closing = set()
        self.sweep_task = None
        self.last_sweep = 0

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)

        def finished(task):
            self.tasks.discard(task)
            if not task.cancelled():
                error = task.exception()
                if error:
                    self.last_error = (
                        error.message
                        if isinstance(error, GatewayError)
                        else "Background work failed; inspect activity"
                    )
                    self.store.event("background_error", code=getattr(error, "code", "internal_error"))

        task.add_done_callback(finished)
        return task

    async def verify_identity(self):
        identity = await self.node.identity()
        expected = self.store.get("settings", "installation")
        if expected and any(expected[k] != identity[k] for k in ("wallet", "chain")):
            self.ready = False
            raise GatewayError("node_identity_changed", "Node wallet/network differs from this installation")
        if identity["version"].removeprefix("v") != "7.11.0":
            self.ready = False
            raise GatewayError("unsupported_node", "This release requires Morpheus node v7.11.0")
        if self.cfg.require_guards and "stake-limit-v1" not in identity.get("capabilities", []):
            self.ready = False
            raise GatewayError(
                "node_guard_missing", "Use the bundled node with stake-limit and recovery support"
            )
        if not expected:
            self.store.put("settings", "installation", identity)
        self.identity = identity
        self.ready = True
        return identity

    async def recover(self):
        with self.store.db:
            self.store.db.execute(
                "UPDATE records SET data=json_set(data,'$.state','interrupted') WHERE kind='request' AND json_extract(data,'$.state')='started'"
            )
        for row in self.store.sessions():
            if row["state"] in ("opening", "closing"):
                row["state"] = "open_unknown" if row["state"] == "opening" else "close_pending"
                self.store.put("session", row["id"], row)
        for op in self.store.all("operation"):
            if op["kind"] != "withdraw" and op["state"] in (
                "queued",
                "draining",
                "restarting",
                "interrupted",
            ):
                try:
                    result = await self.helper.request(
                        "GET", "/operations/" + op["id"], timeout=self.cfg.control_timeout
                    )
                    if result.get("state") == "succeeded":
                        await self.verify_identity()
                        await self.node.balances()
                        op.update(state="succeeded", message="Restart completion recovered from local helper")
                        self.store.put("operation", op["id"], op)
                        continue
                except GatewayError:
                    pass
            if op["kind"] == "withdraw" and op["state"] in ("queued", "submitting"):
                op.update(state="uncertain", message="Checking an interrupted MOR withdrawal")
                self.store.put("operation", op["id"], op)
            elif op["kind"] != "withdraw" and op["state"] in ("queued", "draining", "restarting"):
                op.update(
                    state="interrupted",
                    message="Gateway restarted; checking node and effective configuration",
                )
                self.store.put("operation", op["id"], op)
        try:
            await self.verify_identity()
            await self.reconcile()
        except GatewayError as exc:
            self.last_error = exc.message

    def model(self, id) -> ModelPolicy:
        model = self.store.policy().resolve(id)
        if not model:
            raise GatewayError("model_not_found", "Model is not enabled", 404)
        return model

    def mark_error(self, row, exc):
        row["last_error"] = exc.message if isinstance(exc, GatewayError) else "Invalid node response"
        row["failures"] = row.get("failures", 0) + 1
        row["retry_at"] = time.time() + min(300, 2 ** min(row["failures"], 8))
        self.store.put("session", row["id"], row)
        self.store.event("session_error", session=row["id"], code=getattr(exc, "code", "node_protocol"))

    def cooldown(self, model, provider):
        data = self.store.get("provider", model + provider)
        return data and data["until"] > time.time()

    async def candidates(self, model_id):
        rated = await self.node.bids(model_id)
        policy = self.store.policy()
        results = []
        for entry in rated:
            if not isinstance(entry, dict) or not isinstance(entry.get("Bid"), dict):
                raise GatewayError("node_protocol", "Node returned an invalid bid", 502)
            bid = entry["Bid"]
            provider = str(bid.get("Provider", "")).lower()
            permitted = policy.providers.permits(provider)
            cooling = bool(self.cooldown(model_id, provider))
            price = int(bid.get("PricePerSecond", 0))
            affordable = 0 < price <= int(policy.budget.max_price_per_second_wei)
            results.append(
                {
                    "id": bid.get("Id"),
                    "provider": provider,
                    "score": entry.get("Score"),
                    "price_per_second_wei": str(price),
                    "eligible": permitted and not cooling and affordable,
                    "reason": "Excluded by provider policy"
                    if not permitted
                    else "Provider cooling down after a failure"
                    if cooling
                    else "Above configured price limit"
                    if not affordable
                    else "Allowed",
                }
            )
        return results

    def rebalance(self):
        policy = self.store.policy()
        counts, total = {}, 0
        rows = sorted(self.store.sessions(), key=lambda r: (r["id"] not in self.active, r["created_at"]))
        for row in rows:
            if row["state"] != "open":
                continue
            model = policy.resolve(row["model"])
            count = counts.get(row["model"], 0)
            if (
                not model
                or not policy.providers.permits(row["provider"])
                or total >= policy.max_sessions
                or count >= model.max_sessions
            ):
                row.update(state="draining", close_reason="Policy or pool limit changed")
                self.store.put("session", row["id"], row)
                self.schedule_close(row)
            else:
                total += 1
                counts[row["model"]] = count + 1

    def schedule_close(self, row):
        if (
            row["id"] in self.active
            or row["id"] in self.closing
            or row.get("retry_at", 0) > time.time()
            or self.maintenance
        ):
            return
        self.closing.add(row["id"])

        async def run():
            try:
                await self.close(row["id"])
            except GatewayError:
                pass  # Error and retry state are durable on the row.
            finally:
                self.closing.discard(row["id"])

        self.spawn(run())

    async def acquire(self, model_id, prewarm=False):
        if self.store.policy().paused or self.maintenance:
            raise GatewayError("paused", "Gateway is paused or draining for a node restart")
        if self.waiting >= 64:
            raise GatewayError("queue_full", "Request queue is full", 429)
        self.waiting += 1
        deadline = time.monotonic() + self.store.policy().queue_seconds
        try:
            while True:
                if self.maintenance or self.store.policy().paused:
                    raise GatewayError("paused", "Gateway is paused or draining")
                model = self.model(model_id)
                self.rebalance()
                for row in self.store.sessions():
                    if row["model"] != model.id or row["state"] != "open" or row["id"] in self.active:
                        continue
                    self.active.add(row["id"])  # Atomic reservation before any network await.
                    try:
                        await self.verify_identity()
                        truth = await self.node.session(row["chain_id"])
                        self._validate_truth(row, truth)
                        latest = self.store.get("session", row["id"])
                        if not latest or latest["state"] != "open":
                            self.active.discard(row["id"])
                            continue
                        row = latest
                        if int(truth["ClosedAt"]):
                            row["state"] = "closed"
                        elif int(truth["EndsAt"]) <= time.time() + self.cfg.request_timeout + 15:
                            row.update(state="draining", close_reason="Replacing session near expiry")
                        else:
                            current = self.store.policy()
                            if (
                                current.paused
                                or self.maintenance
                                or not current.resolve(model.id)
                                or not current.providers.permits(row["provider"])
                            ):
                                raise GatewayError("policy_changed", "Admission policy changed while waiting")
                            row.update(last_used=time.time(), ends_at=int(truth["EndsAt"]), last_error=None)
                            self.store.put("session", row["id"], row)
                            if prewarm:
                                self.active.discard(row["id"])
                            return row
                        self.store.put("session", row["id"], row)
                    except GatewayError as exc:
                        self.active.discard(row["id"])
                        if exc.code in (
                            "policy_changed",
                            "node_identity_changed",
                            "unsupported_node",
                            "node_guard_missing",
                        ):
                            raise
                        row["state"] = "quarantined"
                        self.mark_error(row, exc)
                        continue
                    except BaseException:
                        self.active.discard(row["id"])
                        raise
                    self.active.discard(row["id"])
                    if row["state"] == "draining":
                        self.schedule_close(row)
                rows = self.store.sessions()
                if any(r["state"] in ("opening", "open_unknown") and r["model"] != model.id for r in rows):
                    # Hot borrowing above is unaffected; unknown escrow cannot authorize another open.
                    if any(r["state"] == "open_unknown" for r in rows):
                        raise GatewayError(
                            "open_unknown", "Reconciling an uncertain opening before another wallet mutation"
                        )
                if any(r["state"] == "open_unknown" for r in rows):
                    raise GatewayError(
                        "open_unknown", "An opening outcome needs reconciliation before new sessions can open"
                    )
                task = self.opening.get(model.id)
                if (
                    not task
                    and len(rows) < self.store.policy().max_sessions
                    and sum(r["model"] == model.id for r in rows) < model.max_sessions
                ):
                    task = self.spawn(self._open(model.id))
                    self.opening[model.id] = task
                    task.add_done_callback(lambda t, mid=model.id: self.opening.pop(mid, None))
                if task:
                    try:
                        await asyncio.wait_for(asyncio.shield(task), self.cfg.acquisition_timeout)
                    except TimeoutError:
                        raise GatewayError(
                            "opening_pending",
                            "Opening is still running; inspect Sessions before retrying",
                            503,
                        ) from None
                    # The opener does not lease. Competing callers reserve through the hot path.
                    continue
                if time.monotonic() >= deadline:
                    raise GatewayError("pool_busy", "Session pool is busy; retry later", 429)
                await asyncio.sleep(0.05)
        finally:
            self.waiting -= 1

    async def quote(self, model_id):
        model = self.model(model_id)
        await self.verify_identity()
        await self.node.model(model.id)
        choices = [b for b in await self.candidates(model.id) if b["eligible"]]
        if not choices:
            raise GatewayError(
                "no_eligible_provider", "No provider meets access, cooldown and price policies"
            )
        bid = await self.node.bid(choices[0]["id"])
        estimate = await self.node.estimate(bid, model.duration_seconds)
        return {
            "bid": choices[0],
            "estimated_stake_wei": str(estimate),
            "duration_seconds": model.duration_seconds,
            "binding_limit_wei": self.store.policy().budget.max_session_stake_wei,
        }

    def require_clear_wallet(self, excluding=None):
        if any(
            r["id"] != excluding and r["state"] in ("opening", "open_unknown", "closing", "close_pending")
            for r in self.store.sessions()
        ):
            raise GatewayError(
                "wallet_pending", "Reconciling a prior wallet transaction before submitting another"
            )
        if any(
            op["kind"] == "withdraw" and op["state"] in ("uncertain", "pending", "submitting")
            for op in self.store.all("operation")
        ):
            raise GatewayError(
                "withdrawal_pending",
                "Reconciling a prior MOR withdrawal before submitting another transaction",
            )

    async def _open(self, model_id):
        async with self.lock:
            for attempt in range(3):
                policy = self.store.policy()
                if policy.paused or self.maintenance:
                    raise GatewayError("paused", "Gateway is paused or draining")
                model = self.model(model_id)
                await self.verify_identity()
                await self.node.model(model.id)
                rows = self.store.sessions()
                if any(r["state"] in ("opening", "open_unknown") for r in rows):
                    raise GatewayError("open_unknown", "An opening outcome needs reconciliation")
                if (
                    len(rows) >= policy.max_sessions
                    or sum(r["model"] == model.id for r in rows) >= model.max_sessions
                ):
                    return
                self.require_clear_wallet()
                options = [b for b in await self.candidates(model.id) if b["eligible"]]
                if not options:
                    raise GatewayError(
                        "no_eligible_provider",
                        "No rated provider is permitted, affordable and outside cooldown",
                    )
                selected = options[0]
                if not HASH.fullmatch(str(selected["id"])):
                    raise GatewayError("node_protocol", "Node returned an invalid bid ID")
                bid = await self.node.bid(selected["id"])
                provider = str(bid.get("Provider", "")).lower()
                if (
                    str(bid.get("ModelAgentId", "")).lower() != model.id
                    or int(bid.get("DeletedAt", 1))
                    or provider != selected["provider"]
                ):
                    raise GatewayError("bid_changed", "Selected bid changed; refresh and retry")
                balances = await self.node.balances()
                estimate = await self.node.estimate(bid, model.duration_seconds)
                stakes = await self.node.stakes()
                allocated = (
                    sum(int(r.get("stake_wei", 0)) for r in rows)
                    + int(stakes["hold"])
                    + int(stakes["available"])
                )
                remaining = int(policy.budget.max_total_stake_wei) - allocated
                limit = min(
                    int(policy.budget.max_session_stake_wei),
                    remaining,
                    int(balances["mor"]) - int(policy.budget.min_liquid_mor_wei),
                )
                if estimate <= 0 or estimate > limit or int(balances["eth"]) < int(policy.budget.min_eth_wei):
                    raise GatewayError(
                        "wallet_budget",
                        "Insufficient MOR/gas headroom or configured stake limit; check Wallet recovery",
                        outcome="not_submitted",
                    )
                current = self.store.policy()
                if (
                    current.revision != policy.revision
                    or not current.providers.permits(provider)
                    or self.maintenance
                    or current.paused
                ):
                    raise GatewayError("policy_changed", "Settings changed during opening preflight; retry")
                row = {
                    "id": uuid.uuid4().hex,
                    "chain_id": None,
                    "model": model.id,
                    "alias": model.alias,
                    "provider": provider,
                    "bid": selected["id"],
                    "state": "opening",
                    "created_at": time.time(),
                    "last_used": time.time(),
                    "ends_at": 0,
                    "stake_wei": str(limit),
                    "revision": policy.revision,
                    "estimated_stake_wei": str(estimate),
                    "transactions": [],
                }
                self.store.put("session", row["id"], row)
                self.store.event("session_open_started", session=row["id"], model=model.id, provider=provider)
                try:
                    result = await self.node.open(
                        row["bid"], model.duration_seconds, max_stake=limit, operation_id=row["id"]
                    )
                    sid = str(result.get("sessionID", ""))
                    if not HASH.fullmatch(sid) or int(sid, 16) == 0:
                        raise GatewayError("node_protocol", "Node did not return a session ID")
                    row["chain_id"] = sid.lower()
                    row["transactions"] = result.get("progress", {}).get("transactions", [])
                    self.store.put("session", row["id"], row)
                    truth = await self.node.session(sid)
                    self._validate_truth(row, truth)
                    row.update(
                        state="closed" if int(truth["ClosedAt"]) else "open",
                        ends_at=int(truth["EndsAt"]),
                        opened_at=int(truth.get("OpenedAt", 0)),
                        stake_wei=str(truth["Stake"]),
                    )
                    current = self.store.policy()
                    if (
                        current.paused
                        or not current.resolve(model.id)
                        or not current.providers.permits(provider)
                    ):
                        row["state"] = "draining"
                    self.store.put("session", row["id"], row)
                    if row["state"] == "draining":
                        raise GatewayError(
                            "policy_changed", "Opened session is being closed after a policy change"
                        )
                    return row
                except GatewayError as exc:
                    if row["state"] == "draining":
                        raise
                    row.update(
                        state="failed" if exc.outcome == "not_submitted" else "open_unknown",
                        transactions=exc.transactions or row.get("transactions", []),
                    )
                    self.mark_error(row, exc)
                    if exc.code == "provider_declined":
                        self._cool_provider(row, exc.code)
                        if attempt < 2:
                            continue
                    raise
                except BaseException:
                    row["state"] = "open_unknown"
                    self.store.put("session", row["id"], row)
                    raise

    def _validate_truth(self, row, truth):
        if (
            not isinstance(truth, dict)
            or not self.identity
            or any(
                str(truth.get(k, "")).lower() != str(v).lower()
                for k, v in (
                    ("User", self.identity["wallet"]),
                    ("ModelAgentId", row["model"]),
                    ("Provider", row["provider"]),
                    ("BidID", row["bid"]),
                    ("Id", row["chain_id"]),
                )
            )
        ):
            raise GatewayError("session_mismatch", "Session identity could not be verified")
        try:
            if any(int(truth[k]) < 0 for k in ("EndsAt", "ClosedAt", "Stake", "OpenedAt")):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise GatewayError("session_mismatch", "Session state is incomplete or invalid") from None

    def release(self, row):
        self.active.discard(row["id"])
        current = self.store.get("session", row["id"])
        if current:
            current["last_used"] = time.time()
            self.store.put("session", row["id"], current)
            if current["state"] == "draining":
                self.schedule_close(current)

    def _cool_provider(self, row, reason):
        self.store.put(
            "provider",
            row["model"] + row["provider"],
            {"until": time.time() + self.store.policy().recovery.provider_cooldown_seconds, "reason": reason},
        )

    def invalidate(self, row, reason):
        current = self.store.get("session", row["id"])
        if current and current["state"] not in ("closed", "closing", "close_pending"):
            current.update(state="draining", close_reason=reason)
            self.store.put("session", row["id"], current)
            self._cool_provider(current, reason)
            self.store.event(
                "session_invalidated", session=row["id"], provider=row["provider"], reason=reason
            )
            self.schedule_close(current)

    async def _close(self, row):
        if row["state"] in ("closed", "failed"):
            return row
        if row["state"] in ("closing", "close_pending"):
            return row  # Reconcile, never resubmit an uncertain close.
        if row["id"] in self.active:
            row["state"] = "draining"
            self.store.put("session", row["id"], row)
            return row
        if not row.get("chain_id"):
            raise GatewayError("open_unknown", "Reconciling the on-chain opening before closing")
        try:
            truth = await self.node.session(row["chain_id"])
            self._validate_truth(row, truth)
            if not int(truth["ClosedAt"]):
                self.require_clear_wallet(excluding=row["id"])
                row["state"] = "closing"
                row["close_operation_id"] = uuid.uuid4().hex
                self.store.put("session", row["id"], row)
                try:
                    result = await self.node.close_session(
                        row["chain_id"], operation_id=row["close_operation_id"]
                    )
                    row["close_tx"] = result.get("tx")
                    row["state"] = "close_pending"
                    self.store.put("session", row["id"], row)
                    truth = await self.node.session(row["chain_id"])
                    self._validate_truth(row, truth)
                    if not int(truth["ClosedAt"]):
                        return row
                except GatewayError as exc:
                    row.update(
                        state="draining" if exc.outcome == "not_submitted" else "close_pending",
                        close_transactions=exc.transactions,
                    )
                    self.mark_error(row, exc)
                    raise
                except BaseException:
                    row["state"] = "close_pending"
                    self.store.put("session", row["id"], row)
                    raise
            self.record_closed(row, truth)
            return row
        except GatewayError as exc:
            self.mark_error(row, exc)
            raise

    def record_closed(self, row, truth):
        row.update(
            state="closed", closed_at=int(truth["ClosedAt"]), stake_wei=str(truth["Stake"]), last_error=None
        )
        # Estimate only; authoritative available/held totals come from the contract.
        duration = max(1, int(truth["EndsAt"]) - int(truth["OpenedAt"]))
        lived = min(duration, max(0, int(truth["ClosedAt"]) - int(truth["OpenedAt"])))
        row["estimated_daylocked_wei"] = str(int(truth["Stake"]) * lived // duration)
        self.store.put("session", row["id"], row)
        self.store.event("session_closed", session=row["id"], tx=row.get("close_tx"))

    async def close(self, id, stop_maintaining=False):
        if stop_maintaining:
            row = self.store.get("session", id)
            policy = self.store.policy()
            model = policy.resolve(row["model"]) if row else None
            if model:
                model.retention = "on_demand"
                policy.revision += 1
                self.store.put("settings", "policy", policy.model_dump())
        async with self.lock:
            if self.maintenance:
                raise GatewayError("paused", "Cleanup waits until the node restart finishes")
            await self.verify_identity()
            row = self.store.get("session", id)
            if not row:
                raise GatewayError("not_found", "Session not found", 404)
            return await self._close(row)

    async def bind(self, id, chain_id):
        if not HASH.fullmatch(chain_id):
            raise GatewayError("invalid_id", "Invalid on-chain session ID", 400)
        await self.verify_identity()
        row = self.store.get("session", id)
        if not row or row["state"] != "open_unknown":
            raise GatewayError("not_uncertain", "This operation is not awaiting recovery", 409)
        if any(r["id"] != id and r.get("chain_id") == chain_id.lower() for r in self.store.sessions()):
            raise GatewayError("already_managed", "This session is already managed", 409)
        row["chain_id"] = chain_id.lower()
        truth = await self.node.session(chain_id)
        self._validate_truth(row, truth)
        if (
            not row["created_at"] - 5
            <= int(truth["OpenedAt"])
            <= row["created_at"] + self.cfg.open_timeout + 120
        ):
            raise GatewayError(
                "session_time_mismatch", "Session opening time does not match the operation", 409
            )
        row.update(
            state="open",
            ends_at=int(truth["EndsAt"]),
            stake_wei=str(truth["Stake"]),
            opened_at=int(truth["OpenedAt"]),
            last_error=None,
        )
        if int(truth["ClosedAt"]):
            self.record_closed(row, truth)
        else:
            self.store.put("session", id, row)
        self.store.event("session_reconciled", session=id)
        return row

    async def transaction_state(self, transactions):
        if not transactions:
            return "unknown"
        receipts = []
        for tx in transactions:
            txid = tx.get("hash") if isinstance(tx, dict) else tx
            if not HASH.fullmatch(str(txid)):
                return "unknown"
            receipt = await self.node.receipt(txid, self.identity["chain"])
            receipts.append(receipt)
        # Replacements can leave earlier hashes unmined; a mined successful receipt wins.
        if any(r and int(r["status"], 16) == 1 for r in receipts):
            return "succeeded"
        if all(r and int(r["status"], 16) == 0 for r in receipts):
            return "failed"
        return "pending"

    async def journal(self, operation_id):
        if not operation_id or not self.helper.configured:
            return None
        try:
            return await self.helper.request(
                "GET", "/journal/" + operation_id, timeout=self.cfg.control_timeout
            )
        except GatewayError:
            return None

    async def reconcile(self):
        # Native journal survives HTTP disconnects and gateway/node restarts.
        for row in self.store.sessions():
            if row["state"] not in ("open_unknown", "close_pending"):
                continue
            operation_id = row["id"] if row["state"] == "open_unknown" else row.get("close_operation_id")
            progress = await self.journal(operation_id)
            if self.store.get("session", row["id"]) != row:
                continue
            if not progress:
                continue
            if row["state"] == "open_unknown":
                row["transactions"] = progress.get("transactions", [])
                sid = str(progress.get("sessionID", ""))
                if HASH.fullmatch(sid) and int(sid, 16):
                    row["chain_id"] = sid.lower()
                elif progress.get("completed") and progress.get("stage") == "not_submitted":
                    row.update(
                        state="failed", last_error="Native journal confirms no transaction was submitted"
                    )
            else:
                row["close_transactions"] = progress.get("transactions", [])
                if progress.get("completed") and progress.get("stage") == "not_submitted":
                    row.update(state="draining", retry_at=time.time() + 30)
            self.store.put("session", row["id"], row)
        for row in self.store.sessions():
            if not row.get("chain_id") or row.get("retry_at", 0) > time.time():
                continue
            try:
                snapshot = dict(row)
                truth = await self.node.session(row["chain_id"])
                if self.store.get("session", row["id"]) != snapshot:
                    continue
                self._validate_truth(row, truth)
                if int(truth["ClosedAt"]):
                    self.record_closed(row, truth)
                    continue
                if row["state"] in ("open_unknown", "quarantined"):
                    row["state"] = "open"
                if row["state"] == "close_pending":
                    txs = row.get("close_transactions", []) or (
                        [row["close_tx"]] if row.get("close_tx") else []
                    )
                    if await self.transaction_state(txs) == "failed":
                        row.update(state="draining", retry_at=time.time() + 30)
                if self.store.get("session", row["id"]) != snapshot:
                    continue
                row.update(ends_at=int(truth["EndsAt"]), stake_wei=str(truth["Stake"]), last_error=None)
                self.store.put("session", row["id"], row)
            except (GatewayError, KeyError, TypeError, ValueError) as exc:
                if self.store.get("session", row["id"]) == snapshot:
                    self.mark_error(row, exc)
        for row in self.store.sessions():
            if row["state"] != "open_unknown" or row.get("chain_id"):
                continue
            try:
                txs = [tx for tx in row.get("transactions", []) if tx.get("kind") == "open"]
                if txs and await self.transaction_state(txs) == "failed":
                    row.update(state="failed", last_error="Opening transaction reverted; safe to retry")
                    self.store.put("session", row["id"], row)
            except GatewayError as exc:
                self.mark_error(row, exc)

    async def sweep_wallet(self):
        await self.verify_identity()
        settings = self.store.get("settings", "wallet_scan") or {"offset": 0}
        offset = settings["offset"]
        # A bounded page per cycle eventually scans the whole wallet; repeat from newest at EOF.
        data = await self.node.wallet_sessions(self.identity["wallet"], offset=offset)
        batch = data["sessions"]
        known = {r.get("chain_id") for r in self.store.sessions()}
        unknown = [r for r in self.store.sessions() if r["state"] == "open_unknown" and not r.get("chain_id")]
        for truth in batch:
            try:
                sid = str(truth.get("Id", "")).lower()
                if not HASH.fullmatch(sid) or str(truth.get("User", "")).lower() != self.identity["wallet"]:
                    continue
                if sid in known:
                    continue
                matches = [
                    r
                    for r in unknown
                    if r["bid"].lower() == str(truth.get("BidID", "")).lower()
                    and r["model"] == str(truth.get("ModelAgentId", "")).lower()
                    and r["provider"] == str(truth.get("Provider", "")).lower()
                    and r["created_at"] - 5
                    <= int(truth.get("OpenedAt", 0))
                    <= r["created_at"] + self.cfg.open_timeout + 120
                ]
                if matches:
                    # Don't choose between multiple candidates. Operator can inspect paginated history.
                    for row in matches:
                        candidates = row.get("recovery_candidates", [])
                        if sid not in candidates:
                            candidates.append(sid)
                        row["recovery_candidates"] = candidates
                        self.store.put("session", row["id"], row)
                    continue
                if int(truth.get("ClosedAt", 0)):
                    continue
                recovery = self.store.policy().recovery
                expired = int(truth.get("EndsAt", 0)) <= time.time()
                if not ((expired and recovery.cleanup_untracked_expired) or recovery.cleanup_untracked_live):
                    continue
                row = {
                    "id": "wallet-" + sid[2:],
                    "chain_id": sid,
                    "model": str(truth.get("ModelAgentId", "")).lower(),
                    "alias": "Recovered wallet session",
                    "bid": str(truth.get("BidID", "")).lower(),
                    "provider": str(truth.get("Provider", "")).lower(),
                    "state": "orphaned",
                    "created_at": time.time(),
                    "last_used": time.time(),
                    "ends_at": int(truth["EndsAt"]),
                    "stake_wei": str(truth["Stake"]),
                    "discovered": True,
                    "retry_at": 0 if expired else time.time() + recovery.orphan_grace_seconds,
                }
                self._validate_truth(row, truth)
                self.store.put("session", row["id"], row)
                self.store.event("wallet_session_discovered", session=row["id"], expired=expired)
            except (GatewayError, KeyError, TypeError, ValueError):
                self.store.event("wallet_session_invalid", session=str(truth.get("Id", "unknown")))
        self.store.put(
            "settings",
            "wallet_scan",
            {"offset": offset + len(batch) if len(batch) == 100 else 0, "updated_at": time.time()},
        )
        # Only resolve after a complete scan, and only one matching session/operation under this dedicated wallet owner.
        if len(batch) < 100:
            for row in self.store.sessions():
                if (
                    row["state"] == "open_unknown"
                    and not row.get("chain_id")
                    and len(row.get("recovery_candidates", [])) == 1
                ):
                    try:
                        await self.bind(row["id"], row["recovery_candidates"][0])
                    except GatewayError as exc:
                        self.mark_error(row, exc)

    async def treasury(self, manual=False):
        async with self.lock:
            if self.maintenance:
                return
            await self.verify_identity()
            recovery = self.store.policy().recovery
            for op in self.store.all("operation"):
                if op["kind"] != "withdraw" or op["state"] not in ("uncertain", "pending", "submitting"):
                    continue
                progress = await self.journal(op["id"])
                if progress:
                    op["transactions"] = progress.get("transactions", [])
                    if progress.get("completed") and progress.get("http_status") == 200:
                        op.update(
                            state="succeeded",
                            finished_at=time.time(),
                            message="Native journal confirms mined withdrawal",
                        )
                        self.store.put("operation", op["id"], op)
                        continue
                    if progress.get("completed") and progress.get("stage") == "not_submitted":
                        op.update(state="failed", message="Native journal confirms no withdrawal submission")
                        self.store.put("operation", op["id"], op)
                        continue
                    self.store.put("operation", op["id"], op)
                state = await self.transaction_state(op.get("transactions", []))
                if state in ("succeeded", "failed"):
                    op.update(
                        state=state,
                        message="Withdrawal confirmed"
                        if state == "succeeded"
                        else "Withdrawal reverted; will retry after backoff",
                        finished_at=time.time(),
                    )
                    self.store.put("operation", op["id"], op)
                else:
                    raise GatewayError(
                        "withdrawal_pending",
                        "A prior withdrawal is still being reconciled; no duplicate was sent",
                    )
            stakes, balances = await self.node.stakes(), await self.node.balances()
            self.store.put(
                "settings", "treasury", {**stakes, "balances": balances, "updated_at": time.time()}
            )
            if not manual and not recovery.auto_withdraw:
                return
            last = self.store.get("settings", "last_withdrawal") or {"time": 0}
            if not manual and time.time() - last["time"] < recovery.withdrawal_interval_seconds:
                return
            if int(stakes["available"]) < max(1, int(recovery.withdrawal_min_wei)):
                return {"state": "skipped", "message": "No eligible MOR above the configured threshold"}
            if int(balances["eth"]) < int(self.store.policy().budget.min_eth_wei):
                raise GatewayError("gas_headroom", "Add gas funds before withdrawing eligible MOR")
            if any(
                r["state"] in ("opening", "open_unknown", "closing", "close_pending")
                for r in self.store.sessions()
            ):
                raise GatewayError("wallet_pending", "Reconcile pending wallet operations before withdrawal")
            op = {
                "id": uuid.uuid4().hex,
                "kind": "withdraw",
                "state": "submitting",
                "created_at": time.time(),
                "eligible_before_wei": stakes["available"],
                "transactions": [],
                "message": "Withdrawing eligible MOR to this node's wallet",
            }
            self.store.put("operation", op["id"], op)
            self.store.put("settings", "last_withdrawal", {"time": time.time()})
            try:
                result = await self.node.withdraw(operation_id=op["id"])
                if not HASH.fullmatch(str(result.get("tx", ""))):
                    raise GatewayError(
                        "node_protocol", "Withdrawal response did not include a transaction ID"
                    )
                # Native endpoint returns only after successful mining; retain tx for support/reconciliation.
                op.update(
                    state="succeeded",
                    transactions=[{"kind": "withdraw", "hash": result["tx"]}],
                    message="MOR withdrawal confirmed",
                    finished_at=time.time(),
                )
                self.store.put("operation", op["id"], op)
                self.store.put(
                    "settings",
                    "treasury",
                    {
                        **(await self.node.stakes()),
                        "balances": await self.node.balances(),
                        "updated_at": time.time(),
                    },
                )
            except GatewayError as exc:
                if op["state"] != "succeeded":
                    op.update(
                        state="failed" if exc.outcome == "not_submitted" else "uncertain",
                        transactions=exc.transactions,
                        message=exc.message,
                    )
                    self.store.put("operation", op["id"], op)
                raise
            except BaseException:
                if op["state"] != "succeeded":
                    op["state"] = "uncertain"
                    self.store.put("operation", op["id"], op)
                raise
            self.store.event("mor_withdrawal", operation=op["id"], result=op["state"])
            return op

    async def sweep(self):
        errors = []
        for name, work in (("wallet_scan", self.sweep_wallet), ("withdrawal", self.treasury)):
            try:
                await work()
            except (GatewayError, KeyError, TypeError, ValueError) as exc:
                errors.append(getattr(exc, "message", "Invalid response during wallet recovery"))
                self.store.event("recovery_error", component=name, code=getattr(exc, "code", "node_protocol"))
        self.last_sweep = time.time()
        self.store.put("settings", "recovery_status", {"updated_at": time.time(), "errors": errors})
        self.store.prune()

    async def tick(self):
        if self.maintenance:
            return
        await self.verify_identity()
        await self.node.balances()
        await self.reconcile()
        self.rebalance()
        policy = self.store.policy()
        for row in self.store.sessions():
            if row["id"] in self.active or row.get("retry_at", 0) > time.time():
                continue
            model = policy.resolve(row["model"])
            expired = row["ends_at"] and row["ends_at"] <= time.time()
            idle = (
                model
                and model.retention == "on_demand"
                and time.time() - row["last_used"] >= model.idle_seconds
            )
            deadline = (
                model
                and model.retention == "maintain"
                and model.warm_until
                and time.time() >= model.warm_until
            )
            if row["state"] == "open" and (
                not model or not policy.providers.permits(row["provider"]) or idle or expired or deadline
            ):
                row.update(state="draining", close_reason="Expired, idle or disabled by policy")
                self.store.put("session", row["id"], row)
            if row["state"] in ("draining", "orphaned"):
                self.schedule_close(row)
        if not policy.paused:
            for model in policy.models:
                if (
                    model.enabled
                    and model.retention == "maintain"
                    and (not model.warm_until or time.time() < model.warm_until)
                    and model.id not in self.opening
                ):
                    # Never hold up another model's maintenance; per-model task deduplication.
                    if not any(
                        r["model"] == model.id and r["state"] == "open" and r["id"] not in self.active
                        for r in self.store.sessions()
                    ):
                        task = self.spawn(self._open(model.id))
                        self.opening[model.id] = task
                        task.add_done_callback(lambda t, mid=model.id: self.opening.pop(mid, None))
                    else:
                        for row in self.store.sessions():
                            if (
                                row["model"] == model.id
                                and row["state"] == "open"
                                and row["ends_at"] <= time.time() + self.cfg.request_timeout + 15
                            ):
                                row["state"] = "draining"
                                self.store.put("session", row["id"], row)
                                self.schedule_close(row)
        if (
            policy.recovery.enabled
            and time.time() - self.last_sweep >= self.cfg.recovery_interval
            and (not self.sweep_task or self.sweep_task.done())
        ):
            self.sweep_task = self.spawn(self.sweep())

    async def run(self):
        await self.recover()
        while True:
            try:
                await self.tick()
            except Exception as exc:
                self.ready = False
                self.last_error = (
                    exc.message
                    if isinstance(exc, GatewayError)
                    else "Maintenance needs attention; check storage and node health"
                )
            await asyncio.sleep(self.cfg.tick_seconds)

    def restart(self, immediate, apply_rating):
        if self.maintenance:
            raise GatewayError("restart_in_progress", "A restart is already in progress", 409)
        if not self.helper.configured:
            raise GatewayError("helper_missing", "Install/connect the local node management helper")
        self.maintenance = True
        op = {
            "id": uuid.uuid4().hex,
            "state": "queued",
            "kind": "apply_rating" if apply_rating else "restart",
            "created_at": time.time(),
            "message": "Waiting for active requests and wallet operations",
        }
        self.store.put("operation", op["id"], op)
        self.spawn(self._restart(op, immediate, apply_rating))
        return op

    async def _restart(self, op, immediate, apply_rating):
        policy = self.store.policy()
        try:
            op["state"] = "draining"
            self.store.put("operation", op["id"], op)
            async with asyncio.timeout(self.cfg.drain_timeout):
                while self.active and not immediate:
                    await asyncio.sleep(0.05)
                await self.lock.acquire()
            try:
                op.update(state="restarting", message="Restarting the local node")
                self.store.put("operation", op["id"], op)
                result = await self.helper.request(
                    "POST",
                    "/restart",
                    json={"rating": policy.rating() if apply_rating else None, "operation_id": op["id"]},
                )
                await self.verify_identity()
                await self.node.balances()  # Chain readiness, not just the static /config response.
                await self.reconcile()
                if apply_rating:
                    self.store.put(
                        "settings",
                        "applied_rating",
                        {
                            "revision": policy.revision,
                            "rating": policy.rating(),
                            "hash": result.get("rating_hash"),
                        },
                    )
                op.update(state="succeeded", message="Node and chain are ready; admission resumed")
            finally:
                self.lock.release()
        except TimeoutError:
            op.update(
                state="failed",
                message="Requests or wallet operations did not drain; no restart was performed",
            )
        except Exception as exc:
            op.update(
                state="failed",
                message=exc.message
                if isinstance(exc, GatewayError)
                else "Restart failed; inspect helper logs",
            )
        finally:
            self.store.put("operation", op["id"], op)
            self.store.event("node_restart", operation=op["id"], result=op["state"])
            self.maintenance = False
