import asyncio
import time
import uuid

from .config import Config
from .models import HASH, ModelPolicy
from .node import GatewayError, Helper, Node
from .store import Store

LIVE = {"opening", "open_unknown", "open", "draining", "closing", "close_pending"}


class Sessions:
    def __init__(self, store: Store, node: Node, helper: Helper, cfg: Config):
        self.store, self.node, self.helper, self.cfg = store, node, helper, cfg
        self.lock = asyncio.Lock()  # One owner serializes gateway-originated wallet operations.
        self.active: set[str] = set()
        self.waiting = 0
        self.maintenance = False
        self.identity = None
        self.last_error = None
        self.tasks: set[asyncio.Task] = set()

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def verify_identity(self):
        identity = await self.node.identity()
        expected = self.store.get("settings", "installation")
        if expected and any(expected[k] != identity[k] for k in ("wallet", "chain")):
            raise GatewayError("node_identity_changed", "Node wallet/network differs from this installation")
        if identity["version"].removeprefix("v") != "7.11.0":
            raise GatewayError("unsupported_node", "This release requires the pinned Morpheus node v7.11.0")
        if not expected:
            self.store.put("settings", "installation", identity)
        self.identity = identity
        return identity

    async def recover(self):
        # A process may die between submit and recording a response. Never infer failure from age.
        for row in self.store.all("session"):
            if row["state"] == "opening":
                row["state"] = "open_unknown"
                self.store.put("session", row["id"], row)
            elif row["state"] == "closing":
                row["state"] = "close_pending"
                self.store.put("session", row["id"], row)
        for op in self.store.all("operation"):
            if op["state"] in ("queued", "draining", "restarting"):
                op.update(state="interrupted", message="Gateway restarted; check node status before retrying")
                self.store.put("operation", op["id"], op)
        # Node can be offline at boot; UI still starts and maintenance retries read-only recovery.
        try:
            await self.verify_identity()
            await self.reconcile()
        except GatewayError as exc:
            self.last_error = exc.message

    def model(self, id: str) -> ModelPolicy:
        model = self.store.policy().resolve(id)
        if not model:
            raise GatewayError("model_not_found", "Model is not enabled", 404)
        return model

    async def candidates(self, model_id):
        rated = await self.node.bids(model_id)
        policy = self.store.policy()
        results = []
        for entry in rated:
            bid = entry.get("Bid", {})
            provider = str(bid.get("Provider", "")).lower()
            results.append(
                {
                    "id": bid.get("Id"),
                    "provider": provider,
                    "score": entry.get("Score"),
                    "price_per_second_wei": str(bid.get("PricePerSecond", "0")),
                    "eligible": policy.providers.permits(provider),
                    "reason": "Allowed"
                    if policy.providers.permits(provider)
                    else "Excluded by provider policy",
                }
            )
        return results

    async def acquire(self, model_id: str, prewarm=False):
        policy = self.store.policy()
        if policy.paused or self.maintenance:
            raise GatewayError("paused", "Gateway is paused or draining for a node restart")
        if self.waiting >= 64:
            raise GatewayError("queue_full", "Request queue is full", 429)
        self.waiting += 1
        deadline = time.monotonic() + policy.queue_seconds
        try:
            while True:
                try:
                    await asyncio.wait_for(self.lock.acquire(), max(0.01, deadline - time.monotonic()))
                except TimeoutError:
                    raise GatewayError("pool_busy", "Session pool is busy; retry later", 429) from None
                try:
                    if self.maintenance or self.store.policy().paused:
                        raise GatewayError("paused", "Gateway is paused or draining")
                    result = await self._acquire(model_id)
                    if result:
                        if not prewarm:
                            self.active.add(result["id"])
                        return result
                finally:
                    self.lock.release()
                if time.monotonic() >= deadline:
                    raise GatewayError("pool_busy", "Session pool is busy; retry later", 429)
                await asyncio.sleep(0.1)
        finally:
            self.waiting -= 1

    async def _acquire(self, model_id, omitted=None, attempts=0):
        omitted = omitted or set()
        policy = self.store.policy()
        model = self.model(model_id)
        await self.verify_identity()
        all_rows = self.store.all("session")
        for row in all_rows:
            if row["model"] != model_id or row["state"] != "open" or row["id"] in self.active:
                continue
            if not policy.providers.permits(row["provider"]):
                await self._close(row)
                continue
            truth = await self.node.session(row["chain_id"])
            self._validate_truth(row, truth)
            if int(truth["ClosedAt"]) != 0:
                row["state"] = "closed"
                self.store.put("session", row["id"], row)
                continue
            row["ends_at"] = int(truth["EndsAt"])
            if row["ends_at"] <= time.time() + self.cfg.request_timeout + 15:
                await self._close(row)
                continue
            row["last_used"] = time.time()
            self.store.put("session", row["id"], row)
            return row
        live = [r for r in self.store.all("session") if r["state"] in LIVE]
        if any(r["state"] in ("opening", "open_unknown") for r in live):
            raise GatewayError(
                "open_unknown", "An opening outcome needs reconciliation before new sessions can open"
            )
        if (
            len(live) >= policy.max_sessions
            or sum(r["model"] == model_id for r in live) >= model.max_sessions
        ):
            return None
        candidates = [
            b for b in await self.candidates(model_id) if b["eligible"] and b["provider"] not in omitted
        ]
        if not candidates:
            raise GatewayError("no_eligible_provider", "No rated provider is permitted by your policy")
        selected = candidates[0]
        if not HASH.fullmatch(str(selected["id"])):
            raise GatewayError("node_protocol", "Node returned an invalid bid ID")
        bid = await self.node.bid(selected["id"])
        provider = str(bid.get("Provider", "")).lower()
        if (
            str(bid.get("ModelAgentId", "")).lower() != model_id
            or int(bid.get("DeletedAt", 1)) != 0
            or provider != selected["provider"]
            or not self.store.policy().providers.permits(provider)
        ):
            raise GatewayError("bid_changed", "Selected bid is no longer eligible; refresh and retry")
        current = self.store.policy()
        if current.paused or self.maintenance or current.revision != policy.revision:
            raise GatewayError("policy_changed", "Admission policy changed before opening; retry later")
        row = {
            "id": uuid.uuid4().hex,
            "chain_id": None,
            "model": model_id,
            "alias": model.alias,
            "provider": provider,
            "bid": selected["id"],
            "state": "opening",
            "created_at": time.time(),
            "last_used": time.time(),
            "ends_at": 0,
            "stake_wei": "0",
            "revision": policy.revision,
        }
        self.store.put("session", row["id"], row)  # Durable BEFORE the external mutation.
        self.store.event("session_open_started", session=row["id"], model=model_id, provider=provider)
        try:
            opened = await self.node.open(selected["id"], model.duration_seconds)
            sid = str(opened.get("sessionID", ""))
            if not HASH.fullmatch(sid):
                raise GatewayError("node_protocol", "Node did not return a valid session ID")
            row["chain_id"] = sid.lower()
            self.store.put("session", row["id"], row)
            truth = await self.node.session(sid)
            self._validate_truth(row, truth)
            if int(truth["ClosedAt"]) or int(truth["EndsAt"]) <= time.time() + self.cfg.request_timeout + 15:
                raise GatewayError(
                    "session_not_usable", "Opened session is closed or has insufficient time remaining"
                )
            row.update(ends_at=int(truth["EndsAt"]), stake_wei=str(truth["Stake"]), state="open")
            self.store.put("session", row["id"], row)
        except GatewayError as exc:
            row["state"] = "failed" if exc.code == "provider_declined" else "open_unknown"
            self.store.put("session", row["id"], row)
            self.store.event("session_open_failed", session=row["id"], code=exc.code)
            if exc.code == "provider_declined" and attempts < 2:
                return await self._acquire(model_id, omitted | {provider}, attempts + 1)
            raise
        except BaseException:
            row["state"] = "open_unknown"
            self.store.put("session", row["id"], row)
            self.store.event("session_open_uncertain", session=row["id"])
            raise
        # Policies can change while the slow opening call is in flight.
        current = self.store.policy()
        if not current.providers.permits(provider) or not current.resolve(model_id) or current.paused:
            row["state"] = "draining"
            self.store.put("session", row["id"], row)
            raise GatewayError("policy_changed", "Session opened during a policy change and will be closed")
        return row

    def _validate_truth(self, row, truth):
        identity = self.identity
        if not identity or any(
            str(truth.get(k, "")).lower() != str(v).lower()
            for k, v in (
                ("User", identity["wallet"]),
                ("ModelAgentId", row["model"]),
                ("Provider", row["provider"]),
                ("BidID", row["bid"]),
                ("Id", row["chain_id"]),
            )
        ):
            raise GatewayError("session_mismatch", "Session identity could not be verified")
        try:
            if any(int(truth[k]) < 0 for k in ("EndsAt", "ClosedAt", "Stake")):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise GatewayError("session_mismatch", "Session state is incomplete or invalid") from None

    def release(self, row):
        self.active.discard(row["id"])
        current = self.store.get("session", row["id"])
        if current:
            current["last_used"] = time.time()
            self.store.put("session", row["id"], current)

    async def _close(self, row):
        if row["id"] in self.active:
            row["state"] = "draining"
            self.store.put("session", row["id"], row)
            return
        if not row.get("chain_id"):
            raise GatewayError("open_unknown", "Bind the verified on-chain session before closing")
        truth = await self.node.session(row["chain_id"])
        self._validate_truth(row, truth)
        if int(truth["ClosedAt"]) == 0:
            row["state"] = "closing"
            self.store.put("session", row["id"], row)
            try:
                await self.node.close_session(row["chain_id"])
                truth = await self.node.session(row["chain_id"])
                if int(truth.get("ClosedAt", 0)) == 0:
                    raise GatewayError("close_pending", "Close is awaiting confirmation")
            except BaseException:
                row["state"] = "close_pending"
                self.store.put("session", row["id"], row)
                raise
        row["state"] = "closed"
        self.store.put("session", row["id"], row)
        self.store.event("session_closed", session=row["id"])

    async def close(self, id):
        async with self.lock:
            await self.verify_identity()
            row = self.store.get("session", id)
            if not row:
                raise GatewayError("not_found", "Session not found", 404)
            await self._close(row)
            return row

    async def bind(self, id, chain_id):
        if not HASH.fullmatch(chain_id):
            raise GatewayError("invalid_id", "Invalid on-chain session ID", 400)
        async with self.lock:
            await self.verify_identity()
            row = self.store.get("session", id)
            if not row or row["state"] != "open_unknown":
                raise GatewayError("not_uncertain", "This operation is not awaiting recovery", 409)
            if any(
                r["id"] != id and r.get("chain_id") == chain_id.lower() for r in self.store.all("session")
            ):
                raise GatewayError(
                    "already_managed", "That session already belongs to another operation", 409
                )
            row["chain_id"] = chain_id.lower()
            truth = await self.node.session(chain_id)
            self._validate_truth(row, truth)
            if int(truth.get("OpenedAt", 0)) < row["created_at"] - 120:
                raise GatewayError("session_too_old", "This session predates the opening operation", 409)
            row.update(
                state="closed" if int(truth["ClosedAt"]) else "open",
                ends_at=int(truth["EndsAt"]),
                stake_wei=str(truth["Stake"]),
            )
            self.store.put("session", id, row)
            self.store.event("session_reconciled", session=id)
            return row

    async def reconcile(self):
        for row in self.store.all("session"):
            if row["state"] not in LIVE or not row.get("chain_id"):
                continue
            truth = await self.node.session(row["chain_id"])
            self._validate_truth(row, truth)
            if int(truth["ClosedAt"]) != 0:
                row["state"] = "closed"
            elif row["state"] == "open_unknown":
                row["state"] = "open"
            row.update(ends_at=int(truth["EndsAt"]), stake_wei=str(truth["Stake"]))
            self.store.put("session", row["id"], row)

    async def tick(self):
        if self.maintenance or self.lock.locked():
            return
        async with self.lock:
            await self.verify_identity()
            await self.reconcile()
            policy = self.store.policy()
            for row in self.store.all("session"):
                if row["state"] not in ("open", "draining") or row["id"] in self.active:
                    continue
                model = policy.resolve(row["model"])
                idle = (
                    model
                    and model.retention == "on_demand"
                    and time.time() - row["last_used"] > model.idle_seconds
                )
                stopped_warm = (
                    model
                    and model.retention == "maintain"
                    and model.warm_until
                    and time.time() >= model.warm_until
                )
                expired = row["ends_at"] <= time.time()
                if (
                    row["state"] == "draining"
                    or not model
                    or not policy.providers.permits(row["provider"])
                    or idle
                    or stopped_warm
                ):
                    await self._close(row)
                elif expired:
                    # Native node auto-close gets the first minute; gateway repairs after two.
                    if time.time() > row["ends_at"] + 120:
                        await self._close(row)
            if not policy.paused:
                for model in policy.models:
                    if (
                        model.enabled
                        and model.retention == "maintain"
                        and (not model.warm_until or time.time() < model.warm_until)
                    ):
                        await self._acquire(model.id)
        self.last_error = None

    async def run(self):
        while True:
            await asyncio.sleep(self.cfg.tick_seconds)
            try:
                await self.tick()
            except Exception as exc:
                self.last_error = (
                    exc.message if isinstance(exc, GatewayError) else "Session maintenance needs attention"
                )

    def restart(self, immediate: bool, apply_rating: bool):
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
            "message": "Waiting for active requests",
        }
        self.store.put("operation", op["id"], op)
        self.spawn(self._restart(op, immediate, apply_rating))
        return op

    async def _restart(self, op, immediate, apply_rating):
        policy = self.store.policy()
        try:
            op["state"] = "draining"
            self.store.put("operation", op["id"], op)
            deadline = time.monotonic() + self.cfg.drain_timeout
            while self.active and not immediate:
                if time.monotonic() >= deadline:
                    raise GatewayError(
                        "drain_timeout", "Active requests did not drain; no restart was performed"
                    )
                await asyncio.sleep(0.1)
            async with self.lock:
                op.update(state="restarting", message="Restarting the local node")
                self.store.put("operation", op["id"], op)
                result = await self.helper.request(
                    "POST", "/restart", json={"rating": policy.rating() if apply_rating else None}
                )
                await self.verify_identity()
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
                op.update(state="succeeded", message="Node is ready; requests resumed")
                self.last_error = None
        except Exception as exc:
            op.update(
                state="failed",
                message=exc.message
                if isinstance(exc, GatewayError)
                else "Restart failed; inspect local helper logs",
            )
            self.last_error = op["message"]
        finally:
            self.store.put("operation", op["id"], op)
            self.store.event("node_restart", operation=op["id"], result=op["state"])
            self.maintenance = False
