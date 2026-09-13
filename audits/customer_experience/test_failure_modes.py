"""Characterize current customer-visible failures; passing means the defect reproduced.

Run explicitly: .venv/bin/python -m pytest -q audits/customer_experience/test_failure_modes.py
These are audit probes, not assertions that the current behavior is desirable.
"""

import asyncio
import time

import httpx
import pytest

pytestmark = pytest.mark.skip(reason="Historical v0.1 characterization probes; current regressions are in tests/test_recovery.py")

from devtools.fakes import MODEL
from gateway.models import ModelPolicy
from gateway.node import GatewayError
from tests.conftest import env as env
from tests.conftest import key, login

BODY = {"model": "demo", "messages": [{"role": "user", "content": "Hello"}]}
SECOND = "0x" + "9" * 64


async def test_pre_submission_failure_permanently_poisoned_pool(env):
    app, c, node, *_ = env
    headers, _ = await key(c)
    original = node.open

    async def failed(*args):
        raise GatewayError("node_rejected", "Consumer node returned HTTP 500", 502)

    node.open = failed  # e.g. native token-supply read fails BEFORE any transaction.
    assert (await c.post("/v1/chat/completions", json=BODY, headers=headers)).status_code == 502
    node.open = original  # RPC/funds/provider issue has now been corrected.
    response = await c.post("/v1/chat/completions", json=BODY, headers=headers)
    assert response.json()["error"]["code"] == "open_unknown"
    assert not node.sessions and node.opens == 0  # Nothing exists to bind in the recovery UI.


async def test_first_failing_model_prevents_other_model_maintenance(env):
    app, _, node, *_ = env
    policy = app.state.store.policy()
    policy.models[0].retention = "maintain"
    policy.models.insert(0, ModelPolicy(id=SECOND, alias="unavailable", retention="maintain"))
    app.state.store.put("settings", "policy", policy.model_dump())
    original = node.bids
    queried = []

    async def bids(id):
        queried.append(id)
        return [] if id == SECOND else await original(id)

    node.bids = bids
    with pytest.raises(GatewayError, match="No rated provider"):
        await app.state.sessions.tick()
    assert queried == [SECOND] and node.opens == 0


async def test_unreadable_session_prevents_healthy_session_reuse(env):
    app, _, node, *_ = env
    policy = app.state.store.policy()
    policy.models[0].max_sessions = 2
    app.state.store.put("settings", "policy", policy.model_dump())
    manager = app.state.sessions
    healthy = await manager.acquire(MODEL)
    broken = await manager.acquire(MODEL)
    manager.release(healthy)
    manager.release(broken)
    del node.sessions[broken["chain_id"]]
    with pytest.raises(GatewayError, match="Session not found"):
        await manager.acquire(MODEL)
    assert healthy["chain_id"] in node.sessions
    with pytest.raises(GatewayError, match="Session not found"):
        await manager.tick()


async def test_cold_open_of_other_model_blocks_idle_hot_model(env):
    app, _, node, *_ = env
    manager = app.state.sessions
    hot = await manager.acquire(MODEL)
    manager.release(hot)
    policy = app.state.store.policy()
    policy.models.append(ModelPolicy(id=SECOND, alias="cold"))
    policy.queue_seconds = 1
    app.state.store.put("settings", "policy", policy.model_dump())
    original_bid = node.bid

    async def bid(id):
        return {**(await original_bid(id)), "ModelAgentId": SECOND}

    node.bid = bid
    node.open_delay = 10
    node.open_started.clear()
    cold = asyncio.create_task(manager.acquire(SECOND))
    await node.open_started.wait()
    try:
        with pytest.raises(GatewayError, match="pool is busy"):
            await manager.acquire(MODEL)
        assert hot["chain_id"] in node.sessions and not manager.active
    finally:
        cold.cancel()
        await asyncio.gather(cold, return_exceptions=True)


async def test_queue_setting_does_not_bound_cold_acquisition(env):
    app, _, node, *_ = env
    node.open_delay = 1.15
    started = time.monotonic()
    row = await app.state.sessions.acquire(MODEL)
    assert time.monotonic() - started > app.state.store.policy().queue_seconds
    app.state.sessions.release(row)


async def test_provider_inference_failure_reuses_same_session_forever(env):
    app, c, node, *_ = env
    headers, _ = await key(c)
    seen = []

    async def completion(sid, *args):
        seen.append(sid)
        return httpx.Response(503, json={"error": "provider offline"})

    node.completion = completion
    for _ in range(3):
        assert (await c.post("/v1/chat/completions", json=BODY, headers=headers)).status_code == 502
    assert len(set(seen)) == 1 and node.opens == 1
    assert app.state.store.all("session")[0]["state"] == "open"


async def test_malformed_prompt_opens_session_then_returns_retryable_gateway_error(env):
    app, c, node, *_ = env
    headers, _ = await key(c)

    async def reject(*args):
        return httpx.Response(400, json={"error": "invalid messages"})

    node.completion = reject
    response = await c.post("/v1/chat/completions", json={**BODY, "messages": [42]}, headers=headers)
    assert node.opens == 1
    assert response.status_code == 502  # Actual customer request error is translated to server failure.


async def test_expired_admin_cannot_even_sign_out(env):
    app, c, *_ = env
    admin_headers = await login(c)
    row = app.state.store.all("admin")[0]
    row["expires"] = 1
    app.state.store.put("admin", row["id"], row)
    assert (await c.post("/admin/api/logout", headers=admin_headers)).status_code == 401


async def test_login_attempts_from_one_caller_block_other_admins(env):
    app, c, *_ = env
    from tests.conftest import ORIGIN, TOKEN

    for _ in range(10):
        assert (
            await c.post("/admin/api/login", json={"token": "wrong"}, headers={"origin": ORIGIN})
        ).status_code == 401
    assert (
        await c.post("/admin/api/login", json={"token": TOKEN}, headers={"origin": ORIGIN})
    ).status_code == 429


async def test_admin_secret_rotation_leaves_existing_session_authorized(env):
    app, c, _, _, cfg = env
    await login(c)
    cfg.admin_token = "a-new-secret-" + "z" * 40
    assert (await c.get("/admin/api/policy")).status_code == 200


async def test_helper_status_reports_connected_when_helper_is_unreachable(env):
    app, c, _, helper, _ = env
    await login(c)
    helper.fail = True
    response = await c.get("/admin/api/status")
    assert response.json()["helper_connected"] is True
    assert response.json()["node_error"] is None


async def test_close_maintained_session_opens_replacement(env):
    app, _, node, *_ = env
    policy = app.state.store.policy()
    policy.models[0].retention = "maintain"
    app.state.store.put("settings", "policy", policy.model_dump())
    manager = app.state.sessions
    await manager.tick()
    await manager.close(app.state.store.all("session")[0]["id"])
    await manager.tick()
    assert node.opens == 2 and node.closes == 1


async def test_lowering_caps_does_not_reduce_existing_pool(env):
    app, _, node, *_ = env
    policy = app.state.store.policy()
    policy.models[0].max_sessions = 2
    app.state.store.put("settings", "policy", policy.model_dump())
    manager = app.state.sessions
    rows = [await manager.acquire(MODEL), await manager.acquire(MODEL)]
    for row in rows:
        manager.release(row)
    policy.models[0].max_sessions = policy.max_sessions = 1
    app.state.store.put("settings", "policy", policy.model_dump())
    await manager.tick()
    first, second = await manager.acquire(MODEL), await manager.acquire(MODEL)
    assert len(manager.active) == 2 and node.closes == 0
    manager.release(first)
    manager.release(second)


async def test_close_pending_can_resubmit_same_close(env):
    app, _, node, *_ = env
    manager = app.state.sessions
    row = await manager.acquire(MODEL)
    manager.release(row)
    submitted = []

    async def close(sid):
        submitted.append(sid)  # Assume transaction submitted, no confirmation available yet.
        raise GatewayError("node_unreachable", "lost response")

    node.close_session = close
    for _ in range(2):
        with pytest.raises(GatewayError):
            await manager.close(row["id"])
    assert submitted == [row["chain_id"], row["chain_id"]]


async def test_malformed_success_response_escapes_standard_error_handling(env):
    app, _, node, *_ = env

    async def invalid_bids(id):
        return [None]

    node.bids = invalid_bids
    with pytest.raises(AttributeError):
        await app.state.sessions.acquire(MODEL)


def test_valid_human_percentages_can_be_rejected_as_not_exactly_one():
    from gateway.models import Weights

    with pytest.raises(ValueError, match="exactly"):
        Weights(tps=0.1, ttft=0.2, duration=0.3, success=0.3, stake=0.1)
