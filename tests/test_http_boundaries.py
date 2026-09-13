"""Exercise the real Node HTTP client over TCP against a fault-injectable node.

This is a protocol fixture, not a Morpheus network/provider test. No wallet keys
or external connections are used.
"""

import asyncio
import base64
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from devtools.fakes import MODEL, WALLET, FakeNode
from gateway.node import Node
from tests.conftest import key

BODY = {"model": "demo", "messages": [{"role": "user", "content": "wire-marker"}]}


@asynccontextmanager
async def serve(app, lifespan="auto"):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False, lifespan=lifespan))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}", server
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 3)
        finally:
            sock.close()


@pytest_asyncio.fixture
async def wire(env):
    gateway, client, _, helper, cfg = env
    fake = FakeNode()
    upstream = FastAPI()
    state = {"fault": None, "calls": [], "completions": 0, "bad_auth": 0}
    expected = "Basic " + base64.b64encode(b"admin:wire-node-password").decode()

    @upstream.api_route("/{path:path}", methods=["GET", "POST"])
    async def route(path: str, request: Request):
        state["calls"].append((request.method, path))
        if request.headers.get("authorization") != expected:
            state["bad_auth"] += 1
            return Response(status_code=401)
        fault = state["fault"]
        if path == "config":
            if fault == "control_503":
                return JSONResponse({"error": "rpc-secret-canary"}, status_code=503)
            if fault == "control_timeout":
                await asyncio.sleep(0.2)
            if fault == "malformed_identity":
                return Response("not-json")
            return {
                "DerivedConfig": {"WalletAddress": WALLET, "ChainID": "84532"},
                "Version": "v7.11.0",
                "GatewayCapabilities": ["stake-limit-v1", "operation-journal-v1"],
            }
        if path == "blockchain/models":
            return {"models": await fake.catalog()}
        if path.endswith("/bids/rated"):
            return {"bids": await fake.bids(MODEL)}
        if path == "blockchain/balance":
            return await fake.balances()
        if path == "blockchain/token/supply":
            return {"supply": "10000000000000000000000000"}
        if path == "blockchain/sessions/budget":
            return {"budget": "12000000000000000000"}
        if path == "blockchain/stakes/onhold":
            return await fake.stakes()
        if path.startswith("blockchain/bids/") and path.endswith("/session"):
            body = await request.json()
            result = await fake.open(path.split("/")[2], body["sessionDuration"])
            if fault == "lost_open":
                # Native operation succeeds but its HTTP response exceeds the caller deadline.
                helper.journals[request.headers["x-gateway-operation"]] = {
                    "sessionID": result["sessionID"],
                    "completed": True,
                    "http_status": 200,
                }
                await asyncio.sleep(0.2)
            return result
        if path.startswith("blockchain/bids/"):
            return {"bid": await fake.bid(path.split("/")[2])}
        if path == "blockchain/sessions/user":
            return await fake.wallet_sessions(WALLET)
        if path.startswith("blockchain/sessions/") and path.endswith("/close"):
            return await fake.close_session(path.split("/")[2])
        if path.startswith("blockchain/sessions/"):
            return {"session": await fake.session(path.split("/")[2])}
        if path == "v1/chat/completions":
            state["completions"] += 1
            assert request.headers["model_id"] == MODEL
            assert request.headers["session_id"] in fake.sessions
            assert request.headers["chat_id"].startswith("0x")
            body = await request.json()
            if fault == "inference_timeout":
                await asyncio.sleep(0.2)
            if body.get("stream"):

                async def events():
                    yield b'data: {"id":"wire","object":"chat.completion.chunk","choices":[{"delta":{"content":"wire-marker"}}]}\n\n'
                    await asyncio.sleep(0.02)
                    if fault != "truncated_stream":
                        yield b"data: [DONE]\n\n"

                return StreamingResponse(events(), media_type="text/event-stream")
            return {
                "id": "wire",
                "object": "chat.completion",
                "choices": [{"message": {"role": "assistant", "content": body["messages"][0]["content"]}}],
            }
        return JSONResponse({"error": "unknown route"}, status_code=404)

    async with serve(upstream) as (url, _):
        cfg.node_url, cfg.node_password = url, "wire-node-password"
        node = Node(cfg)
        gateway.state.sessions.node = node
        # Endpoints captured the original adapter in create_app. Update its inference
        # method to the real TCP client; session control uses the manager above.
        original_fake = env[2]
        original_fake.completion = node.completion
        try:
            yield gateway, client, fake, state, node, cfg
        finally:
            await node.aclose()


async def test_real_tcp_node_protocol_opens_once_reuses_and_authenticates(wire):
    app, client, fake, state, *_ = wire
    headers, _ = await key(client)
    for _ in range(2):
        response = await client.post("/v1/chat/completions", headers=headers, json=BODY)
        assert response.status_code == 200, response.text
        assert response.json()["choices"][0]["message"]["content"] == "wire-marker"
    assert fake.opens == 1 and state["completions"] == 2 and state["bad_auth"] == 0
    assert not app.state.sessions.active


@pytest.mark.parametrize("fault", ["control_503", "control_timeout", "malformed_identity"])
async def test_real_tcp_control_fault_never_opens_escrow(wire, fault):
    app, client, fake, state, node, cfg = wire
    cfg.control_timeout = 0.05
    headers, _ = await key(client)
    state["fault"] = fault
    response = await client.post("/v1/chat/completions", headers=headers, json=BODY)
    assert response.status_code in (502, 503)
    assert "rpc-secret-canary" not in response.text
    assert fake.opens == 0 and state["completions"] == 0
    state["fault"] = None
    assert (await client.post("/v1/chat/completions", headers=headers, json=BODY)).status_code == 200


async def test_real_tcp_lost_open_response_reconciles_without_second_post(wire):
    app, client, fake, state, node, cfg = wire
    cfg.open_timeout = 0.05
    headers, _ = await key(client)
    state["fault"] = "lost_open"
    response = await client.post("/v1/chat/completions", headers=headers, json=BODY)
    assert response.status_code == 503
    assert fake.opens == 1
    state["fault"] = None
    async with asyncio.timeout(3):
        while app.state.store.sessions()[0]["state"] != "open":
            await app.state.sessions.recover()
            await asyncio.sleep(0.05)
    response = await client.post("/v1/chat/completions", headers=headers, json=BODY)
    assert response.status_code == 200, response.text
    assert fake.opens == 1
    assert sum(method == "POST" and path.endswith("/session") for method, path in state["calls"]) == 1


async def test_real_tcp_truncated_stream_emits_error_and_never_replays(wire):
    app, client, fake, state, *_ = wire
    headers, _ = await key(client)
    state["fault"] = "truncated_stream"
    response = await client.post("/v1/chat/completions", headers=headers, json={**BODY, "stream": True})
    assert response.status_code == 200
    assert "wire-marker" in response.text and "stream_interrupted" in response.text
    assert "[DONE]" not in response.text
    assert state["completions"] == 1 and not app.state.sessions.active


async def test_real_tcp_inference_deadline_bounds_header_wait(wire):
    app, client, fake, state, node, cfg = wire
    cfg.request_timeout = 0.05
    headers, _ = await key(client)
    state["fault"] = "inference_timeout"
    response = await client.post("/v1/chat/completions", headers=headers, json=BODY)
    assert response.status_code == 504
    assert state["completions"] == 1 and not app.state.sessions.active


async def test_slow_upload_over_tcp_times_out_without_wallet_work(env):
    app, client, node, _, cfg = env
    cfg.body_timeout = 0.08
    async with serve(app, lifespan="off") as (url, _):
        reader, writer = await asyncio.open_connection("127.0.0.1", int(url.rsplit(":", 1)[1]))
        try:
            writer.write(
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\n{"
            )
            await writer.drain()
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1)
            assert b"408 Request Timeout" in headers
            assert node.opens == 0
        finally:
            writer.close()
            await writer.wait_closed()
        async with httpx.AsyncClient(base_url=url) as wire_client:
            assert (await wire_client.get("/healthz")).status_code == 200


async def test_streamed_upload_size_limit_over_tcp(env):
    app, client, node, *_ = env
    headers, _ = await key(client)

    async def oversized():
        for _ in range(33):
            yield b"x" * 65536
            await asyncio.sleep(0)

    async with serve(app, lifespan="off") as (url, _):
        async with httpx.AsyncClient(base_url=url) as wire_client:
            response = await wire_client.post("/v1/chat/completions", headers=headers, content=oversized())
            assert response.status_code == 413
            assert response.json()["error"]["code"] == "payload_too_large"
            assert node.opens == 0


async def test_connection_limit_is_bounded_and_releases_after_disconnect(env):
    app, client, node, _, cfg = env
    cfg.body_timeout = 0.5
    cfg.max_connections = 2
    async with serve(app, lifespan="off") as (url, _):
        writers = []
        try:
            for _ in range(2):
                _, writer = await asyncio.open_connection("127.0.0.1", int(url.rsplit(":", 1)[1]))
                writers.append(writer)
                writer.write(
                    b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\n{"
                )
                await writer.drain()
            await asyncio.sleep(0.02)
            async with httpx.AsyncClient(base_url=url) as wire_client:
                response = await wire_client.get("/healthz")
                assert response.status_code == 429
                assert response.json()["error"]["code"] == "connection_limit"
        finally:
            for writer in writers:
                writer.close()
                await writer.wait_closed()
        await asyncio.sleep(0.05)
        async with httpx.AsyncClient(base_url=url) as wire_client:
            assert (await wire_client.get("/healthz")).status_code == 200
        assert node.opens == 0
