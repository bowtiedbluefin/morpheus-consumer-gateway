"""Regression coverage for the production acceptance findings."""

import asyncio
import json

import httpx
import pytest

from gateway.node import GatewayError
from tests.conftest import key

BODY = {"model": "demo", "messages": [{"role": "user", "content": "hello"}]}


async def test_nonfinite_extension_rejected_before_any_escrow(env):
    app, client, node, *_ = env
    headers, _ = await key(client)

    async def encode_as_real_node(sid, mid, body, rid):
        # HTTPX's real JSON serializer is stricter than Python json.loads.
        httpx.Request("POST", "http://node/chat", json=body)

    node.completion = encode_as_real_node
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver"
    ) as wire:
        r = await wire.post(
            "/v1/chat/completions",
            headers={**headers, "Content-Type": "application/json"},
            content=json.dumps({**BODY, "vendor_extension": float("nan")}),
        )
    assert (r.status_code, node.opens) == (400, 0), (r.status_code, node.opens, r.text)


@pytest.mark.parametrize(
    "message", [{}, {"content": 42}, {"role": "assistant", "content": {"unexpected": "object"}}]
)
async def test_invalid_provider_message_is_not_success(env, message):
    app, client, node, *_ = env
    headers, _ = await key(client)

    async def invalid(*args):
        return httpx.Response(
            200, json={"id": "x", "object": "chat.completion", "choices": [{"message": message}]}
        )

    node.completion = invalid
    r = await client.post("/v1/chat/completions", headers=headers, json=BODY)
    assert r.status_code == 502, r.text


async def test_provider_nonfinite_usage_is_error_not_success(env):
    app, client, node, *_ = env
    headers, _ = await key(client)

    async def invalid(*args):
        value = {
            "id": "x",
            "object": "chat.completion",
            "choices": [{"message": {"role": "assistant", "content": "answer"}}],
            "usage": {"total_tokens": float("nan")},
        }
        return httpx.Response(200, content=json.dumps(value))

    node.completion = invalid
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver"
    ) as wire:
        r = await wire.post("/v1/chat/completions", headers=headers, json=BODY)
    assert r.status_code == 502, (r.status_code, r.text)


async def test_waiter_can_use_busy_session_if_extra_provider_capacity_declines(env):
    app, client, node, *_ = env
    policy = app.state.store.policy()
    policy.models[0].max_sessions = 2
    policy.queue_seconds = 2
    app.state.store.put("settings", "policy", policy.model_dump())
    headers, _ = await key(client, concurrency=2)
    original = node.completion
    started = asyncio.Event()
    release = asyncio.Event()

    async def held(*args):
        started.set()
        await release.wait()
        return await original(*args)

    node.completion = held
    first = asyncio.create_task(client.post("/v1/chat/completions", headers=headers, json=BODY))
    await started.wait()

    async def no_capacity(*args, **kwargs):
        raise GatewayError("provider_declined", "No additional slots", outcome="not_submitted")

    node.open = no_capacity
    second = asyncio.create_task(client.post("/v1/chat/completions", headers=headers, json=BODY))
    await asyncio.sleep(0.05)
    release.set()
    a, b = await asyncio.gather(first, second)
    assert [a.status_code, b.status_code] == [200, 200], b.text


async def test_completed_opener_does_not_starve_event_loop(env):
    app, client, node, *_ = env
    headers, _ = await key(client)
    mid = app.state.store.policy().models[0].id
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    # A new HTTP task can see a completed opener before its done callback runs.
    app.state.sessions.opening[mid] = task
    asyncio.get_running_loop().call_soon(app.state.sessions.opening.pop, mid, None)
    original = app.state.store.policy
    reads = 0

    def bounded_policy():
        nonlocal reads
        reads += 1
        if reads > 1000:
            raise RuntimeError("Acquisition starved the event loop; pending callback never ran")
        return original()

    app.state.store.policy = bounded_policy
    try:
        row = await app.state.sessions.acquire(mid)
        assert row["state"] == "open"
        app.state.sessions.release(row)
    finally:
        app.state.store.policy = original


async def test_readiness_recovers_after_transient_storage_failure(env):
    app, client, node, *_ = env
    headers, _ = await key(client)
    app.state.store.db.execute("PRAGMA query_only=ON")
    try:
        failed = await client.post("/v1/chat/completions", headers=headers, json=BODY)
        assert failed.status_code == 503
    finally:
        app.state.store.db.execute("PRAGMA query_only=OFF")
    assert (await client.post("/v1/chat/completions", headers=headers, json=BODY)).status_code == 200
    await app.state.sessions.tick()
    assert (await client.get("/readyz")).status_code == 200
