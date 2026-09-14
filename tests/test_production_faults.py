"""Fault injection against the real gateway and SQLite, with no funded wallet."""

import asyncio
import json
import sqlite3

import httpx
import pytest

from gateway.node import GatewayError
from gateway.transport import chat_events
from tests.conftest import key, login

BODY = {"model": "demo", "messages": [{"role": "user", "content": "private-prompt-canary"}]}


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422, 429, 500, 502, 503])
async def test_provider_status_is_classified_without_replay(env, status):
    app, client, node, *_ = env
    headers, _ = await key(client)
    calls = []

    async def reject(*args):
        calls.append(args)
        return httpx.Response(status, json={"error": "upstream-secret-canary"})

    node.completion = reject
    response = await client.post("/v1/chat/completions", headers=headers, json=BODY)
    assert response.status_code == (status if status < 500 else 502)
    assert "upstream-secret-canary" not in response.text
    assert len(calls) == 1 and not app.state.sessions.active
    row = app.state.store.all("session")[0]
    if status >= 500 or status == 429:
        assert row["state"] in ("draining", "closing", "close_pending", "closed")
        assert app.state.sessions.cooldown(row["model"], row["provider"])
    else:
        assert row["state"] == "open"


@pytest.mark.parametrize("failure", [httpx.ReadError, httpx.ReadTimeout, httpx.ConnectError])
async def test_transport_failure_releases_lease_and_never_replays(env, failure):
    app, client, node, *_ = env
    headers, _ = await key(client, concurrency=1)
    calls = []

    async def broken(*args):
        calls.append(args)
        raise failure("transport-secret-canary")

    node.completion = broken
    response = await client.post("/v1/chat/completions", headers=headers, json=BODY)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "inference_transport"
    assert "transport-secret-canary" not in response.text
    assert len(calls) == 1 and not app.state.sessions.active and app.state.sessions.waiting == 0


@pytest.mark.parametrize("payload", [b"not-json", b"[]", b"{}", b'{"error":"secret"}', b"\xff"])
async def test_malformed_nonstream_response_quarantines_provider(env, payload):
    app, client, node, *_ = env
    headers, _ = await key(client)

    async def broken(*args):
        return httpx.Response(200, content=payload)

    node.completion = broken
    response = await client.post("/v1/chat/completions", headers=headers, json=BODY)
    assert response.status_code == 502 and response.json()["error"]["code"] == "provider_protocol"
    assert not app.state.sessions.active
    row = app.state.store.all("session")[0]
    assert row["state"] in ("draining", "closing", "close_pending", "closed")
    assert app.state.sessions.cooldown(row["model"], row["provider"])


async def test_response_size_limit_and_next_key_admission(env):
    app, client, node, _, cfg = env
    cfg.response_bytes = 256
    headers, _ = await key(client, concurrency=1)

    async def huge(*args):
        return httpx.Response(200, content=b"x" * 257)

    node.completion = huge
    response = await client.post("/v1/chat/completions", headers=headers, json=BODY)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_response_too_large"
    assert not app.state.sessions.active
    response = await client.post("/v1/chat/completions", headers=headers, json=BODY)
    assert response.status_code != 429  # Failure must not leak the key's sole concurrency slot.


async def test_readonly_database_rejects_before_open_and_recovers(env):
    app, client, node, *_ = env
    headers, _ = await key(client, concurrency=1)
    app.state.store.db.execute("PRAGMA query_only=ON")
    try:
        response = await client.post("/v1/chat/completions", headers=headers, json=BODY)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "storage_unavailable"
        assert node.opens == 0 and not node.completions
        assert not app.state.sessions.active and app.state.sessions.waiting == 0
    finally:
        app.state.store.db.execute("PRAGMA query_only=OFF")
    response = await client.post("/v1/chat/completions", headers=headers, json=BODY)
    assert response.status_code == 200


async def test_disk_full_before_escrow_fails_closed(env, monkeypatch):
    app, client, node, *_ = env
    headers, _ = await key(client)
    original = app.state.store.put

    def disk_full(kind, id, value):
        if kind == "session":
            raise sqlite3.OperationalError("database or disk is full")
        return original(kind, id, value)

    monkeypatch.setattr(app.state.store, "put", disk_full)
    response = await client.post("/v1/chat/completions", headers=headers, json=BODY)
    assert response.status_code == 503
    assert node.opens == 0 and not app.state.sessions.active


async def test_cancellation_during_nonstream_inference_releases_key_and_session(env):
    app, client, node, *_ = env
    headers, _ = await key(client, concurrency=1)
    started = asyncio.Event()
    original = node.completion

    async def wait_forever(*args):
        started.set()
        await asyncio.Event().wait()

    node.completion = wait_forever
    request = asyncio.create_task(client.post("/v1/chat/completions", headers=headers, json=BODY))
    await asyncio.wait_for(started.wait(), 2)
    request.cancel()
    await asyncio.gather(request, return_exceptions=True)
    assert not app.state.sessions.active and app.state.sessions.waiting == 0
    node.completion = original
    assert (await client.post("/v1/chat/completions", headers=headers, json=BODY)).status_code == 200


async def test_race_between_two_tabs_preserves_first_saved_policy(env):
    app, client, *_ = env
    headers = await login(client)
    original = (await client.get("/admin/api/policy")).json()
    first = {**original, "queue_seconds": 10}
    second = {**original, "queue_seconds": 20}
    responses = await asyncio.gather(
        *[client.put("/admin/api/policy", headers=headers, json=body) for body in (first, second)]
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    saved = (await client.get("/admin/api/policy")).json()
    winner = next(r.json() for r in responses if r.status_code == 200)
    assert saved == winner and saved["revision"] == original["revision"] + 1


class Fragments(httpx.AsyncByteStream):
    def __init__(self, pieces):
        self.pieces = pieces

    async def __aiter__(self):
        for piece in self.pieces:
            yield piece


@pytest.mark.parametrize("width", [1, 2, 7, 10000])
async def test_sse_chunk_boundaries_comments_crlf_and_unicode(width):
    event = {"id": "x", "object": "chat.completion.chunk", "choices": [{"delta": {"content": "héllo 🐟"}}]}
    payload = (
        b": keepalive\r\n\r\n"
        + ("data: " + json.dumps(event, ensure_ascii=False) + "\r\n\r\ndata: [DONE]\r\n\r\n").encode()
    )
    response = httpx.Response(
        200, stream=Fragments([payload[i : i + width] for i in range(0, len(payload), width)])
    )
    frames = [frame async for frame in chat_events(response, 10000)]
    assert len(frames) == 2 and frames[-1] == b"data: [DONE]\n\n"
    assert json.loads(frames[0][6:])["choices"][0]["delta"]["content"] == "héllo 🐟"


@pytest.mark.parametrize("payload", [b"data: {bad}\n\n", b"data: {}\n\n", b"", b"data: [DONE]"])
async def test_invalid_or_incomplete_sse_is_not_success(payload):
    response = httpx.Response(200, stream=Fragments([payload]))
    with pytest.raises(GatewayError):
        _ = [frame async for frame in chat_events(response, 1000)]


async def test_admin_and_event_endpoints_never_return_secrets_or_prompts(env):
    app, client, node, *_ = env
    headers, _ = await key(client)
    secret = headers["authorization"].removeprefix("Bearer ")
    await client.post("/v1/chat/completions", headers=headers, json=BODY)
    for path in ("/keys", "/events", "/sessions", "/operations", "/status"):
        response = await client.get("/admin/api" + path)
        assert response.status_code == 200
        assert secret not in response.text and "private-prompt-canary" not in response.text
