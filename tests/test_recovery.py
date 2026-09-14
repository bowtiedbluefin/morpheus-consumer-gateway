"""Recovery invariants: never duplicate uncertain mutations; do reclaim eligible funds."""

import asyncio
import time

import httpx
import pytest

from devtools.fakes import MODEL
from gateway.models import ModelPolicy, Weights
from gateway.node import GatewayError
from tests.conftest import ORIGIN, TOKEN, key, login

BODY = {"model": "demo", "messages": [{"role": "user", "content": "hello"}]}


async def settle(manager):
    while manager.tasks:
        await asyncio.gather(*list(manager.tasks), return_exceptions=True)


async def hot(env):
    manager = env[0].state.sessions
    row = await manager.acquire(MODEL)
    manager.release(row)
    return manager, row


async def test_mature_mor_withdraws_and_held_mor_waits(env):
    app, _, node, *_ = env
    node.available, node.hold = 2 * 10**18, 3 * 10**18
    op = await app.state.sessions.treasury()
    assert op["state"] == "succeeded" and node.withdrawals == 1
    assert node.available == 0 and node.hold == 3 * 10**18
    await app.state.sessions.treasury(manual=True)
    assert node.withdrawals == 1
    assert app.state.store.get("settings", "treasury")["hold"] == str(3 * 10**18)


async def test_threshold_gas_and_interval_prevent_waste(env):
    app, _, node, *_ = env
    manager = app.state.sessions
    node.available = 1
    await manager.treasury(manual=True)
    assert node.withdrawals == 0
    node.available = 10**18
    original = node.balances

    async def low_gas():
        return {"mor": str(10**20), "eth": "0"}

    node.balances = low_gas
    with pytest.raises(GatewayError, match="gas"):
        await manager.treasury(manual=True)
    assert node.withdrawals == 0
    node.balances = original
    await manager.treasury()
    node.available = 10**18
    await manager.treasury()
    assert node.withdrawals == 1


async def test_unknown_withdrawal_never_repeats_and_hot_session_still_works(env):
    manager, row = await hot(env)
    app, _, node, helper, _ = env
    node.available = 10**18

    async def lost(**kwargs):
        node.withdrawals += 1
        raise GatewayError("node_unreachable", "Withdrawal response lost")

    node.withdraw = lost
    with pytest.raises(GatewayError):
        await manager.treasury(manual=True)
    with pytest.raises(GatewayError, match="prior withdrawal"):
        await manager.treasury(manual=True)
    reused = await manager.acquire(MODEL)
    assert reused["id"] == row["id"] and node.withdrawals == 1
    manager.release(reused)
    with pytest.raises(GatewayError, match="prior MOR withdrawal"):
        await manager.close(row["id"])
    op = app.state.store.all("operation")[0]
    helper.journals[op["id"]] = {
        "completed": True,
        "http_status": 200,
        "stage": "submitting_withdraw",
        "transactions": [],
    }
    node.available = 0
    await manager.treasury(manual=True)
    assert app.state.store.get("operation", op["id"])["state"] == "succeeded"
    assert node.withdrawals == 1


async def test_withdrawal_reverted_receipt_permits_later_retry(env):
    app, _, node, *_ = env
    tx = "0x" + "f" * 64
    app.state.store.put(
        "operation",
        "old",
        {
            "id": "old",
            "kind": "withdraw",
            "state": "uncertain",
            "created_at": time.time(),
            "transactions": [{"kind": "withdraw", "hash": tx}],
        },
    )
    node.receipts[tx] = {"status": "0x0"}
    node.available = 10**18
    await app.state.sessions.treasury(manual=True)
    assert app.state.store.get("operation", "old")["state"] == "failed"
    assert node.withdrawals == 1


async def test_lost_open_recovers_from_native_journal_without_second_open(env):
    app, _, node, helper, _ = env
    node.lose_open_response = True
    with pytest.raises(GatewayError):
        await app.state.sessions.acquire(MODEL)
    row = app.state.store.sessions()[0]
    sid = next(iter(node.sessions))
    helper.journals[row["id"]] = {"sessionID": sid, "completed": True, "http_status": 500, "transactions": []}
    row["retry_at"] = 0
    app.state.store.put("session", row["id"], row)
    await app.state.sessions.recover()
    recovered = await app.state.sessions.acquire(MODEL)
    assert recovered["chain_id"] == sid and node.opens == 1
    app.state.sessions.release(recovered)


async def test_journal_proven_no_submission_unblocks_pool(env):
    app, _, node, helper, _ = env
    original = node.open

    async def unavailable(*args, **kwargs):
        raise GatewayError("node_unreachable", "Lost preflight response")

    node.open = unavailable
    with pytest.raises(GatewayError):
        await app.state.sessions.acquire(MODEL)
    row = app.state.store.sessions()[0]
    helper.journals[row["id"]] = {"stage": "not_submitted", "completed": True, "http_status": 500}
    await app.state.sessions.reconcile()
    node.open = original
    acquired = await app.state.sessions.acquire(MODEL)
    assert node.opens == 1
    app.state.sessions.release(acquired)


async def test_scan_recovers_unknown_open_and_paginates_expired_orphans(env):
    app, _, node, *_ = env
    node.lose_open_response = True
    with pytest.raises(GatewayError):
        await app.state.sessions.acquire(MODEL)
    manager = app.state.sessions
    sid = next(iter(node.sessions))
    prototype = dict(node.sessions[sid])
    for i in range(200, 301):
        other = "0x" + f"{i:064x}"
        node.sessions[other] = {
            **prototype,
            "Id": other,
            "OpenedAt": int(time.time()) - 3600,
            "EndsAt": int(time.time()) - 10,
        }
    await manager.sweep_wallet()
    assert app.state.store.get("settings", "wallet_scan")["offset"] == 100
    await manager.sweep_wallet()
    assert app.state.store.get("settings", "wallet_scan")["offset"] == 0
    assert len([r for r in app.state.store.sessions() if r["state"] == "orphaned"]) == 101
    assert any(r["state"] == "open" and r["chain_id"] == sid for r in app.state.store.sessions())
    # Imported expired sessions are cleaned even when the inference pool is full.
    await manager.tick()
    await settle(manager)
    assert node.closes == 101


async def test_live_untracked_sessions_require_explicit_opt_in_and_grace(env):
    app, _, node, *_ = env
    await node.open("bid", 600)
    await app.state.sessions.sweep_wallet()
    assert not app.state.store.sessions()
    policy = app.state.store.policy()
    policy.recovery.cleanup_untracked_live = True
    app.state.store.put("settings", "policy", policy.model_dump())
    await app.state.sessions.sweep_wallet()
    row = app.state.store.sessions()[0]
    assert row["state"] == "orphaned" and row["retry_at"] > time.time() + 100
    await app.state.sessions.tick()
    await settle(app.state.sessions)
    assert node.closes == 0


async def test_close_lost_response_does_not_duplicate_then_reconciles(env):
    manager, row = await hot(env)
    app, _, node, *_ = env

    async def uncertain(sid, **kwargs):
        node.closes += 1
        raise GatewayError("node_unreachable", "Close result lost")

    node.close_session = uncertain
    with pytest.raises(GatewayError):
        await manager.close(row["id"])
    await manager.close(row["id"])
    assert node.closes == 1
    node.sessions[row["chain_id"]]["ClosedAt"] = int(time.time())
    row = app.state.store.get("session", row["id"])
    row["retry_at"] = 0
    app.state.store.put("session", row["id"], row)
    await manager.reconcile()
    assert app.state.store.get("session", row["id"])["state"] == "closed"


async def test_session_budget_includes_contract_hold_and_native_binding_limit(env):
    app, _, node, *_ = env
    p = app.state.store.policy()
    p.budget.max_total_stake_wei = str(6 * 10**18)
    app.state.store.put("settings", "policy", p.model_dump())
    node.hold = 2 * 10**18
    with pytest.raises(GatewayError, match="stake limit"):
        await app.state.sessions.acquire(MODEL)
    assert node.opens == 0
    node.hold = 0
    original = node.open

    async def guarded(*args, **kwargs):
        assert kwargs["max_stake"] == 6 * 10**18
        assert len(kwargs["operation_id"]) == 32
        return await original(*args, **kwargs)

    node.open = guarded
    row = await app.state.sessions.acquire(MODEL)
    app.state.sessions.release(row)


async def test_one_failed_maintained_model_does_not_starve_healthy_model(env):
    app, _, node, *_ = env
    second = "0x" + "9" * 64
    p = app.state.store.policy()
    p.models[0].retention = "maintain"
    p.models.insert(0, ModelPolicy(id=second, alias="offline", retention="maintain"))
    app.state.store.put("settings", "policy", p.model_dump())
    original = node.bids

    async def bids(mid):
        return [] if mid == second else await original(mid)

    node.bids = bids
    await app.state.sessions.tick()
    await settle(app.state.sessions)
    assert node.opens == 1 and app.state.store.sessions()[0]["model"] == MODEL


async def test_unreadable_session_does_not_hide_healthy_one(env):
    app, _, node, *_ = env
    p = app.state.store.policy()
    p.models[0].max_sessions = 2
    app.state.store.put("settings", "policy", p.model_dump())
    manager = app.state.sessions
    healthy = await manager.acquire(MODEL)
    broken = await manager.acquire(MODEL)
    manager.release(healthy)
    manager.release(broken)
    del node.sessions[broken["chain_id"]]
    reused = await manager.acquire(MODEL)
    assert reused["id"] == healthy["id"]
    manager.release(reused)
    await manager.tick()
    await settle(manager)


async def test_hot_borrow_does_not_wait_for_wallet_lock(env):
    manager, row = await hot(env)
    async with manager.lock:
        reused = await asyncio.wait_for(manager.acquire(MODEL), 0.2)
    assert reused["id"] == row["id"]
    manager.release(reused)


async def test_reconcile_does_not_resurrect_concurrently_invalidated_session(env):
    manager, row = await hot(env)
    app, _, node, *_ = env
    manager.active.add(row["id"])
    original = node.session

    async def lookup(sid):
        manager.invalidate(row, "provider_error")
        return await original(sid)

    node.session = lookup
    await manager.reconcile()
    assert app.state.store.get("session", row["id"])["state"] == "draining"
    node.session = original
    manager.release(row)
    await settle(manager)


async def test_provider_failure_quarantines_without_replaying_prompt(env):
    app, c, node, *_ = env
    headers, _ = await key(c)

    async def offline(*args):
        return httpx.Response(503, json={"error": "offline"})

    node.completion = offline
    assert (await c.post("/v1/chat/completions", headers=headers, json=BODY)).status_code == 502
    await settle(app.state.sessions)
    res = await c.post("/v1/chat/completions", headers=headers, json=BODY)
    assert res.json()["error"]["code"] == "no_eligible_provider" and node.opens == 1
    assert node.closes == 1


@pytest.mark.parametrize("messages", [[42], [{"role": "wrong", "content": "hello"}], [{"role": "user"}]])
async def test_invalid_prompt_never_opens_escrow(env, messages):
    _, c, node, *_ = env
    headers, _ = await key(c)
    assert (
        await c.post("/v1/chat/completions", headers=headers, json={**BODY, "messages": messages})
    ).status_code == 400
    assert node.opens == 0


async def test_client_error_and_idempotency_are_preserved(env):
    app, c, node, *_ = env
    headers, _ = await key(c)

    async def reject(*args):
        return httpx.Response(400, json={"error": "unsupported option"})

    node.completion = reject
    headers["Idempotency-Key"] = "unique-request"
    assert (await c.post("/v1/chat/completions", headers=headers, json=BODY)).status_code == 400
    assert (await c.post("/v1/chat/completions", headers=headers, json=BODY)).status_code == 409
    assert node.opens == 1 and app.state.store.sessions()[0]["state"] == "open"


async def test_login_noise_cannot_lock_out_valid_admin_and_rotation_revokes(env):
    _, c, _, _, cfg = env
    for _ in range(12):
        await c.post("/admin/api/login", json={"token": "wrong"}, headers={"origin": ORIGIN})
    await login(c)
    cfg.admin_token = "changed-" + TOKEN
    assert (await c.get("/admin/api/policy")).status_code == 401
    assert (await c.post("/admin/api/logout", headers={"origin": ORIGIN})).status_code == 200


async def test_helper_connectivity_is_observed(env):
    _, c, _, helper, _ = env
    await login(c)
    helper.fail = True
    assert (await c.get("/admin/api/status")).json()["helper_connected"] is False


async def test_stop_maintaining_close_and_lowered_pool_caps(env):
    manager, row = await hot(env)
    app, _, node, *_ = env
    p = app.state.store.policy()
    p.models[0].retention = "maintain"
    app.state.store.put("settings", "policy", p.model_dump())
    await manager.close(row["id"], stop_maintaining=True)
    await manager.tick()
    await settle(manager)
    assert node.opens == 1 and node.closes == 1
    p = app.state.store.policy()
    p.models[0].max_sessions = 2
    app.state.store.put("settings", "policy", p.model_dump())
    rows = [await manager.acquire(MODEL), await manager.acquire(MODEL)]
    for row in rows:
        manager.release(row)
    p.models[0].max_sessions = 1
    app.state.store.put("settings", "policy", p.model_dump())
    await manager.tick()
    await settle(manager)
    assert len(app.state.store.sessions()) == 1


def test_human_percentages_normalize_to_native_exact_sum():
    w = Weights(tps=0.1, ttft=0.2, duration=0.3, success=0.3, stake=0.1)
    assert sum(w.model_dump().values()) == 1.0
