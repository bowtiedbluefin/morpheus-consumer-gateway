"""First production-contract gaps, tested without a live node or wallet.

Overlapping requests deliberately remain in progress behind a barrier. An
instantaneous mock response cannot demonstrate concurrent session ownership.
"""

import asyncio

import httpx
import pytest

from devtools.fakes import BID, MODEL
from gateway.models import ModelPolicy
from tests.conftest import key

BODY = {"model": "demo", "messages": [{"role": "user", "content": "hello"}]}


@pytest.mark.parametrize(
    ("model", "status", "code"),
    [
        ("deepseek-v4-flahs", 404, "model_not_found"),
        (" demo", 404, "model_not_found"),
        ("demo ", 404, "model_not_found"),
        ("demο", 404, "model_not_found"),  # Greek omicron, not an ASCII o.
        ("0x" + "a" * 64, 404, "model_not_found"),
        ("", 400, "invalid_request"),
        ("x" * 101, 400, "invalid_request"),
        (None, 400, "invalid_request"),
        (123, 400, "invalid_request"),
        ([], 400, "invalid_request"),
        ({}, 400, "invalid_request"),
    ],
)
async def test_invalid_model_fails_before_provider_or_wallet_work(env, model, status, code):
    app, client, node, *_ = env
    headers, _ = await key(client)

    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid model triggered provider discovery or wallet work")

    node.bids = node.balances = node.model = forbidden
    response = await client.post("/v1/chat/completions", json={**BODY, "model": model}, headers=headers)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.headers.get("X-Request-ID")
    assert node.opens == 0 and not node.completions
    assert not app.state.store.sessions()


@pytest.mark.parametrize("model", ["demo", "DEMO", MODEL])
async def test_supported_alias_case_and_onchain_id(env, model):
    app, client, node, *_ = env
    headers, _ = await key(client)
    response = await client.post("/v1/chat/completions", json={**BODY, "model": model}, headers=headers)
    assert response.status_code == 200
    assert node.completions[0][1] == MODEL
    assert not app.state.sessions.active


async def test_disabled_model_and_key_scope_are_distinct(env):
    app, client, node, *_ = env
    second = "0x" + "9" * 64
    policy = app.state.store.policy()
    policy.models.append(ModelPolicy(id=second, alias="other"))
    app.state.store.put("settings", "policy", policy.model_dump())
    headers, _ = await key(client, models=[MODEL])
    listed = (await client.get("/v1/models", headers=headers)).json()["data"]
    assert [row["id"] for row in listed] == ["demo"]
    denied = await client.post("/v1/chat/completions", json={**BODY, "model": "other"}, headers=headers)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "model_forbidden"
    policy.models[0].enabled = False
    app.state.store.put("settings", "policy", policy.model_dump())
    disabled = await client.post("/v1/chat/completions", json=BODY, headers=headers)
    assert disabled.status_code == 404
    assert (await client.get("/v1/models", headers=headers)).json()["data"] == []
    assert node.opens == 0


class HeldProvider:
    def __init__(self, target):
        self.target = target
        self.ready = asyncio.Event()
        self.release = asyncio.Event()
        self.inflight = set()
        self.seen = []
        self.peak = 0
        self.duplicate_lease = False

    async def completion(self, sid, model, body, request_id):
        if sid in self.inflight:
            self.duplicate_lease = True
        self.inflight.add(sid)
        self.seen.append((sid, model, request_id))
        self.peak = max(self.peak, len(self.inflight))
        if len(self.inflight) >= self.target:
            self.ready.set()
        try:
            await self.release.wait()
            return httpx.Response(
                200,
                json={
                    "id": request_id,
                    "object": "chat.completion",
                    "model": body["model"],
                    "choices": [
                        {"message": {"role": "assistant", "content": body["messages"][0]["content"]}}
                    ],
                },
            )
        finally:
            self.inflight.discard(sid)


@pytest.mark.parametrize("capacity", [1, 2, 4])
async def test_eight_overlapping_requests_obey_session_capacity_and_keep_responses_isolated(env, capacity):
    app, client, node, *_ = env
    policy = app.state.store.policy()
    policy.max_sessions = policy.models[0].max_sessions = capacity
    policy.queue_seconds = 3
    app.state.store.put("settings", "policy", policy.model_dump())
    headers, _ = await key(client, concurrency=8)
    provider = HeldProvider(capacity)
    node.completion = provider.completion
    requests = [
        asyncio.create_task(
            client.post(
                "/v1/chat/completions",
                headers=headers,
                json={**BODY, "messages": [{"role": "user", "content": f"request-{i}"}]},
            )
        )
        for i in range(8)
    ]
    try:
        await asyncio.wait_for(provider.ready.wait(), 2)
        assert node.opens == capacity
        assert len(app.state.sessions.active) == capacity
        assert not any(task.done() for task in requests)
        provider.release.set()
        responses = await asyncio.wait_for(asyncio.gather(*requests), 4)
        for i, response in enumerate(responses):
            assert response.status_code == 200, response.text
            assert response.json()["choices"][0]["message"]["content"] == f"request-{i}"
        assert provider.peak == capacity and not provider.duplicate_lease
        assert len({r.headers["X-Request-ID"] for r in responses}) == 8
        assert node.opens == capacity
        assert not app.state.sessions.active and app.state.sessions.waiting == 0
    finally:
        provider.release.set()
        for task in requests:
            if not task.done():
                task.cancel()
        await asyncio.gather(*requests, return_exceptions=True)


async def test_two_models_can_serve_simultaneously_in_distinct_sessions(env):
    app, client, node, *_ = env
    second, second_bid = "0x" + "9" * 64, "0x" + "8" * 64
    policy = app.state.store.policy()
    policy.models.append(ModelPolicy(id=second, alias="other"))
    policy.max_sessions = 2
    app.state.store.put("settings", "policy", policy.model_dump())
    original_bid, original_open = node.bid, node.open

    async def bid(bid_id):
        result = await original_bid(bid_id)
        if bid_id == second_bid:
            result.update(Id=second_bid, ModelAgentId=second)
        return result

    async def bids(mid):
        return [{"Bid": await bid(second_bid if mid == second else BID), "Score": 10}]

    async def open_session(bid_id, duration, **kwargs):
        result = await original_open(bid_id, duration, **kwargs)
        if bid_id == second_bid:
            node.sessions[result["sessionID"]].update(ModelAgentId=second, BidID=second_bid)
        return result

    node.bid, node.bids, node.open = bid, bids, open_session
    provider = HeldProvider(2)
    node.completion = provider.completion
    headers, _ = await key(client, concurrency=2)
    tasks = [
        asyncio.create_task(client.post("/v1/chat/completions", headers=headers, json={**BODY, "model": mid}))
        for mid in ("demo", "other")
    ]
    try:
        await asyncio.wait_for(provider.ready.wait(), 2)
        assert {mid for _, mid, _ in provider.seen} == {MODEL, second}
        assert len(provider.inflight) == 2 and node.opens == 2
        provider.release.set()
        responses = await asyncio.gather(*tasks)
        assert all(r.status_code == 200 for r in responses)
        assert not provider.duplicate_lease and not app.state.sessions.active
    finally:
        provider.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_saturated_key_does_not_block_another_key(env):
    app, client, node, *_ = env
    policy = app.state.store.policy()
    policy.models[0].max_sessions = 2
    app.state.store.put("settings", "policy", policy.model_dump())
    first, _ = await key(client, concurrency=1)
    second, _ = await key(client, concurrency=1)
    provider = HeldProvider(1)
    node.completion = provider.completion
    task = asyncio.create_task(client.post("/v1/chat/completions", headers=first, json=BODY))
    other = None
    try:
        await asyncio.wait_for(provider.ready.wait(), 2)
        blocked = await client.post("/v1/chat/completions", headers=first, json=BODY)
        assert blocked.status_code == 429 and blocked.headers["Retry-After"] == "5"
        provider.target = 2
        provider.ready.clear()
        other = asyncio.create_task(client.post("/v1/chat/completions", headers=second, json=BODY))
        await asyncio.wait_for(provider.ready.wait(), 2)
        assert node.opens == 2 and len(provider.inflight) == 2
        provider.release.set()
        assert (await task).status_code == 200 and (await other).status_code == 200
        assert not app.state.sessions.active
    finally:
        provider.release.set()
        await asyncio.gather(*[t for t in (task, other) if t], return_exceptions=True)
