import asyncio
import json
import time

import pytest

from devtools.fakes import MODEL, PROVIDER
from gateway.models import Providers, Weights
from gateway.node import GatewayError
from gateway.store import Store
from tests.conftest import key, login

BODY = {"model": "demo", "messages": [{"role": "user", "content": "Hello"}]}


async def test_auth_and_csrf_never_open_sessions(env):
    app, c, node, *_ = env
    assert (await c.post("/v1/chat/completions", json=BODY)).status_code == 401
    assert (await c.get("/admin/api/policy")).status_code == 401
    headers = await login(c)
    policy = (await c.get("/admin/api/policy")).json()
    assert (await c.put("/admin/api/policy", json=policy)).status_code == 403
    bad = {**headers, "origin": "https://evil.example"}
    assert (await c.put("/admin/api/policy", json=policy, headers=bad)).status_code == 403
    assert node.opens == 0


async def test_cold_open_then_reuse_and_close(env):
    app, c, node, *_ = env
    headers, _ = await key(c)
    for _ in range(2):
        res = await c.post("/v1/chat/completions", json=BODY, headers=headers)
        assert res.status_code == 200, res.text
        assert res.json()["choices"][0]["message"]["role"] == "assistant"
    assert node.opens == 1
    assert not app.state.sessions.active
    session = app.state.store.all("session")[0]
    admin = await login(c)
    assert (await c.post(f"/admin/api/sessions/{session['id']}/close", headers=admin)).status_code == 200
    assert node.closes == 1
    assert app.state.store.all("session")[0]["state"] == "closed"


async def test_concurrent_cold_requests_share_one_session(env):
    app, c, node, *_ = env
    headers, _ = await key(c)
    node.open_delay = 0.1
    responses = await asyncio.gather(
        *[c.post("/v1/chat/completions", json=BODY, headers=headers) for _ in range(2)]
    )
    assert [r.status_code for r in responses] == [200, 200]
    assert node.opens == 1


async def test_stream_preserved_and_lease_released(env):
    app, c, node, *_ = env
    headers, _ = await key(c)
    res = await c.post("/v1/chat/completions", json={**BODY, "stream": True}, headers=headers)
    assert res.status_code == 200
    assert '"chat.completion.chunk"' in res.text and "data: [DONE]" in res.text
    assert not app.state.sessions.active
    assert node.opens == 1


async def test_malformed_stream_is_error_and_not_replayed(env):
    app, c, node, *_ = env
    headers, _ = await key(c)
    node.stream_lines = 'data: {"choices":[]}\n\ndata: new session opened: internal-control\n\n'
    res = await c.post("/v1/chat/completions", json={**BODY, "stream": True}, headers=headers)
    assert "stream_interrupted" in res.text
    assert "internal-control" not in res.text
    assert node.opens == 1 and len(node.completions) == 1
    assert not app.state.sessions.active


async def test_allowlist_empty_and_deny_precedence(env):
    app, c, node, *_ = env
    headers, _ = await key(c)
    policy = app.state.store.policy()
    policy.providers = Providers(mode="allowlist")
    app.state.store.put("settings", "policy", policy.model_dump())
    res = await c.post("/v1/chat/completions", json=BODY, headers=headers)
    assert res.json()["error"]["code"] == "no_eligible_provider"
    policy.providers = Providers(mode="allowlist", allow=[PROVIDER], deny=[PROVIDER])
    app.state.store.put("settings", "policy", policy.model_dump())
    assert (await c.post("/v1/chat/completions", json=BODY, headers=headers)).status_code == 503
    assert node.opens == 0


async def test_denied_provider_not_reused(env):
    app, c, node, *_ = env
    headers, _ = await key(c)
    assert (await c.post("/v1/chat/completions", json=BODY, headers=headers)).status_code == 200
    p = app.state.store.policy()
    p.providers.deny = [PROVIDER]
    app.state.store.put("settings", "policy", p.model_dump())
    assert (await c.post("/v1/chat/completions", json=BODY, headers=headers)).status_code == 503
    assert len(node.completions) == 1
    await app.state.sessions.tick()
    await asyncio.gather(*app.state.sessions.tasks)
    assert node.closes == 1


async def test_policy_changes_during_open_quarantine_session(env):
    app, c, node, *_ = env
    headers, _ = await key(c)
    node.open_delay = 0.1
    task = asyncio.create_task(c.post("/v1/chat/completions", json=BODY, headers=headers))
    await node.open_started.wait()
    p = app.state.store.policy()
    p.providers.deny = [PROVIDER]
    app.state.store.put("settings", "policy", p.model_dump())
    res = await task
    assert res.json()["error"]["code"] == "policy_changed"
    assert not node.completions
    assert app.state.store.all("session")[0]["state"] == "draining"


async def test_ambiguous_open_blocks_duplicate_and_can_recover(env):
    app, c, node, *_ = env
    headers, _ = await key(c)
    node.lose_open_response = True
    assert (await c.post("/v1/chat/completions", json=BODY, headers=headers)).status_code == 503
    assert app.state.store.all("session")[0]["state"] == "open_unknown"
    res = await c.post("/v1/chat/completions", json=BODY, headers=headers)
    assert res.json()["error"]["code"] == "open_unknown"
    assert node.opens == 1
    admin = await login(c)
    row = app.state.store.all("session")[0]
    res = await c.post(
        f"/admin/api/sessions/{row['id']}/bind", json={"session_id": next(iter(node.sessions))}, headers=admin
    )
    assert res.status_code == 200, res.text
    assert (await c.post("/v1/chat/completions", json=BODY, headers=headers)).status_code == 200
    assert node.opens == 1


async def test_invalid_bind_cannot_adopt_someone_elses_session(env):
    app, c, node, *_ = env
    headers, _ = await key(c)
    node.lose_open_response = True
    await c.post("/v1/chat/completions", json=BODY, headers=headers)
    row = app.state.store.all("session")[0]
    sid = next(iter(node.sessions))
    node.sessions[sid]["User"] = "0x" + "f" * 40
    with pytest.raises(GatewayError, match="identity"):
        await app.state.sessions.bind(row["id"], sid)
    assert app.state.store.all("session")[0]["state"] == "open_unknown"


async def test_revocation_while_waiting_blocks_inference(env):
    app, c, node, *_ = env
    headers, record = await key(c)
    admin = await login(c)
    node.open_delay = 0.1
    task = asyncio.create_task(c.post("/v1/chat/completions", json=BODY, headers=headers))
    await node.open_started.wait()
    assert (await c.delete("/admin/api/keys/" + record["id"], headers=admin)).status_code == 200
    assert (await task).status_code == 401
    assert not node.completions and not app.state.sessions.active


async def test_key_scopes_and_rate_limit(env):
    app, c, node, *_ = env
    headers, _ = await key(c, requests_per_minute=1)
    assert (await c.post("/v1/chat/completions", json=BODY, headers=headers)).status_code == 200
    assert (await c.post("/v1/chat/completions", json=BODY, headers=headers)).status_code == 429
    assert len(node.completions) == 1
    assert (await c.get("/admin/api/policy", headers=headers)).status_code == 200  # browser cookie exists
    c.cookies.clear()
    assert (await c.get("/admin/api/policy", headers=headers)).status_code == 401


async def test_revision_conflict_and_bad_weights_do_not_replace_policy(env):
    app, c, node, *_ = env
    admin = await login(c)
    p = app.state.store.policy().model_dump()
    assert (await c.put("/admin/api/policy", json=p, headers=admin)).status_code == 200
    assert (await c.put("/admin/api/policy", json=p, headers=admin)).status_code == 409
    p["revision"] = 1
    p["weights"]["tps"] = -1
    assert (await c.put("/admin/api/policy", json=p, headers=admin)).status_code == 422
    assert app.state.store.policy().weights.tps == 0.24


async def test_restart_drains_and_dashboard_remains_live(env):
    app, c, node, helper, cfg = env
    manager = app.state.sessions
    row = await manager.acquire(MODEL)
    op = manager.restart(False, True)
    await asyncio.sleep(0.03)
    assert not helper.restarts
    assert (await c.get("/healthz")).status_code == 200
    with pytest.raises(GatewayError, match="paused"):
        await manager.acquire(MODEL)
    manager.release(row)
    await asyncio.gather(*manager.tasks)
    assert helper.restarts[0]["json"]["rating"]["algorithm"] == "default"
    assert app.state.store.get("operation", op["id"])["state"] == "succeeded"
    assert app.state.store.get("settings", "applied_rating")


async def test_drain_timeout_does_not_kill_busy_node(env):
    app, _, _, helper, cfg = env
    cfg.drain_timeout = 0.01
    row = await app.state.sessions.acquire(MODEL)
    op = app.state.sessions.restart(False, False)
    await asyncio.gather(*app.state.sessions.tasks)
    assert not helper.restarts
    assert app.state.store.get("operation", op["id"])["state"] == "failed"
    app.state.sessions.release(row)


async def test_immediate_restart_and_failure_reporting(env):
    app, _, _, helper, _ = env
    row = await app.state.sessions.acquire(MODEL)
    helper.fail = True
    op = app.state.sessions.restart(True, True)
    await asyncio.gather(*app.state.sessions.tasks)
    assert len(helper.restarts) == 1
    assert app.state.store.get("operation", op["id"])["state"] == "failed"
    assert app.state.store.get("settings", "applied_rating") is None
    app.state.sessions.release(row)


async def test_idle_and_until_expiry_retention(env):
    app, _, node, *_ = env
    row = await app.state.sessions.acquire(MODEL)
    app.state.sessions.release(row)
    row = app.state.store.get("session", row["id"])
    row["last_used"] = time.time() - 600
    app.state.store.put("session", row["id"], row)
    p = app.state.store.policy()
    p.models[0].retention = "until_expiry"
    app.state.store.put("settings", "policy", p.model_dump())
    await app.state.sessions.tick()
    assert node.closes == 0
    p.models[0].retention = "on_demand"
    app.state.store.put("settings", "policy", p.model_dump())
    await app.state.sessions.tick()
    await asyncio.gather(*app.state.sessions.tasks)
    assert node.closes == 1


async def test_maintain_warms_then_replaces_near_expiry(env):
    app, _, node, *_ = env
    p = app.state.store.policy()
    p.models[0].retention = "maintain"
    app.state.store.put("settings", "policy", p.model_dump())
    await app.state.sessions.tick()
    await asyncio.gather(*app.state.sessions.tasks)
    assert node.opens == 1 and not app.state.sessions.active
    sid = next(iter(node.sessions))
    node.sessions[sid]["EndsAt"] = int(time.time()) + 10
    await app.state.sessions.tick()
    await asyncio.gather(*app.state.sessions.tasks)
    await app.state.sessions.tick()
    await asyncio.gather(*app.state.sessions.tasks)
    assert node.opens == 2 and node.closes == 1


async def test_routing_injection_and_large_payload_rejected(env):
    app, c, node, *_ = env
    headers, _ = await key(c)
    assert (
        await c.post("/v1/chat/completions", headers=headers, json={**BODY, "session_id": "evil"})
    ).status_code == 400
    assert (
        await c.post("/v1/chat/completions", headers=headers, content=b"x" * (2 * 1024 * 1024 + 1))
    ).status_code == 413
    assert node.opens == 0


async def test_events_do_not_store_prompt_content(env):
    app, c, node, *_ = env
    headers, _ = await key(c)
    body = {**BODY, "messages": [{"role": "user", "content": "PRIVATE-PROMPT-MARKER"}]}
    await c.post("/v1/chat/completions", json=body, headers=headers)
    assert "PRIVATE-PROMPT-MARKER" not in json.dumps(app.state.store.all("event"))


def test_single_owner_and_policy_validation(tmp_path):
    store = Store(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="one gateway"):
            Store(tmp_path)
    finally:
        store.close()
    with pytest.raises(ValueError):
        Weights(tps=2)
    with pytest.raises(ValueError):
        Providers(deny=["garbage"])


async def test_provider_decline_tries_another_provider_before_chain_submission(env):
    app, c, node, *_ = env
    headers, _ = await key(c)
    original_bid, original_open = node.bid, node.open
    first_id = "0x" + "a" * 64
    first_provider = "0x" + "b" * 40
    attempts = []

    async def bid(id):
        result = await original_bid(id)
        if id == first_id:
            result.update(Id=first_id, Provider=first_provider)
        return result

    async def bids(model):
        from devtools.fakes import BID

        return [{"Bid": await bid(id), "Score": score} for id, score in ((first_id, 20), (BID, 10))]

    async def open_session(id, duration, **kwargs):
        attempts.append(id)
        if id == first_id:
            raise GatewayError(
                "provider_declined", "Provider declined before submission", outcome="not_submitted"
            )
        return await original_open(id, duration)

    node.bid, node.bids, node.open = bid, bids, open_session
    response = await c.post("/v1/chat/completions", json=BODY, headers=headers)
    assert response.status_code == 200, response.text
    assert len(attempts) == 2 and node.opens == 1
    assert sorted(r["state"] for r in app.state.store.all("session")) == ["failed", "open"]


async def test_blocking_provider_during_reuse_lookup_prevents_dispatch(env):
    app, c, node, *_ = env
    headers, _ = await key(c)
    assert (await c.post("/v1/chat/completions", json=BODY, headers=headers)).status_code == 200
    original = node.session

    async def lookup(id):
        policy = app.state.store.policy()
        policy.providers.deny = [PROVIDER]
        app.state.store.put("settings", "policy", policy.model_dump())
        return await original(id)

    node.session = lookup
    response = await c.post("/v1/chat/completions", json=BODY, headers=headers)
    assert response.json()["error"]["code"] == "policy_changed"
    assert len(node.completions) == 1 and not app.state.sessions.active


async def test_cancelled_caller_does_not_cancel_or_duplicate_native_open(env):
    app, _, node, *_ = env
    node.open_delay = 0.1
    task = asyncio.create_task(app.state.sessions.acquire(MODEL))
    await node.open_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.gather(*app.state.sessions.tasks)
    row = await app.state.sessions.acquire(MODEL)
    assert row["state"] == "open"
    app.state.sessions.release(row)
    assert node.opens == 1 and not app.state.sessions.lock.locked()


async def test_recovery_preserves_uncertain_operations(env):
    app, _, node, *_ = env
    row = await app.state.sessions.acquire(MODEL)
    app.state.sessions.release(row)
    row["state"] = "closing"
    app.state.store.put("session", row["id"], row)
    await app.state.sessions.recover()
    assert app.state.store.get("session", row["id"])["state"] == "close_pending"
    assert node.closes == 0
    node.sessions[row["chain_id"]]["ClosedAt"] = int(time.time())
    await app.state.sessions.reconcile()
    assert app.state.store.get("session", row["id"])["state"] == "closed"


async def test_inference_deadline_includes_wait_for_response_headers(env):
    app, c, node, _, cfg = env
    headers, _ = await key(c)
    cfg.request_timeout = 0.01

    async def slow_completion(*args):
        await asyncio.sleep(10)

    node.completion = slow_completion
    response = await c.post("/v1/chat/completions", json=BODY, headers=headers)
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "inference_timeout"
    assert not app.state.sessions.active and node.opens == 1
