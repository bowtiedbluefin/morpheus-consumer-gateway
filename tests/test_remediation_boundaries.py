import asyncio
import json

import httpx
import pytest

from devtools.fakes import MODEL
from gateway.node import GatewayError, Node
from gateway.transport import chat_events, validate_completion
from tests.conftest import key

BODY = {"model": "demo", "messages": [{"role": "user", "content": "hello"}]}


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps({**BODY, "extension": {"values": [float("nan")]}}),
        json.dumps({**BODY, "tools": [{"parameters": {"default": float("inf")}}]}),
        json.dumps({**BODY, "extension": "\ud800"}),
        '{"model":"demo","model":"other","messages":[{"role":"user","content":"x"}]}',
        json.dumps(BODY)[:-1] + ',"x":1e999}',
        json.dumps(BODY)[:-1] + ',"x":' + "[" * 70 + "0" + "]" * 70 + "}",
        json.dumps(BODY)[:-1] + ',"x":' + "9" * 257 + "}",
    ],
)
async def test_invalid_extended_json_never_acquires_wallet(env, raw):
    app, client, node, *_ = env
    headers, _ = await key(client)
    r = await client.post(
        "/v1/chat/completions", headers={**headers, "content-type": "application/json"}, content=raw
    )
    assert r.status_code == 400
    assert r.headers["x-request-id"]
    assert node.opens == 0
    assert not app.state.sessions.active


@pytest.mark.parametrize(
    "message",
    [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "weather", "arguments": "{}"}}
            ],
        },
        {"role": "assistant", "content": "", "reasoning_content": "thinking", "vendor": {"version": 1}},
        {"role": "assistant", "content": None, "refusal": "Cannot answer"},
    ],
)
def test_supported_assistant_extensions_remain_valid(message):
    response = {"id": "x", "object": "chat.completion", "choices": [{"message": message}]}
    assert validate_completion(response) is response


async def test_nonfinite_stream_event_is_never_forwarded():
    payload = {
        "id": "x",
        "object": "chat.completion.chunk",
        "choices": [],
        "usage": {"total_tokens": float("nan")},
    }
    response = httpx.Response(200, content=("data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n").encode())
    with pytest.raises(GatewayError, match="invalid event"):
        async for _ in chat_events(response, 10000):
            pytest.fail("Invalid event was forwarded")


async def test_bad_response_is_recorded_failed_not_success(env):
    app, client, node, *_ = env
    headers, _ = await key(client)

    async def malformed(*args):
        return httpx.Response(
            200,
            content='{"id":"x","object":"chat.completion","choices":[{"message":{"role":"assistant","content":"ok"}}],"usage":{"total_tokens":NaN}}',
        )

    node.completion = malformed
    r = await client.post("/v1/chat/completions", headers=headers, json=BODY)
    assert r.status_code == 502
    events = [e for e in app.state.store.all("event") if e["action"] == "request_finished"]
    assert events[0]["outcome"] == "failed"
    assert not app.state.sessions.active


async def test_completed_old_callback_cannot_remove_new_opener(env):
    app, *_ = env
    manager = app.state.sessions
    old = manager.start_open(MODEL)
    replacement = asyncio.create_task(asyncio.sleep(0.1))
    manager.opening[MODEL] = replacement
    await old
    await asyncio.sleep(0)
    assert manager.opening[MODEL] is replacement
    await replacement
    manager.opening.pop(MODEL)


async def test_deterministic_duration_failure_backs_off_until_policy_changes(env):
    app, client, node, *_ = env
    headers, _ = await key(client)
    calls = 0

    async def invalid(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise GatewayError(
            "session_duration_invalid",
            "Contract duration minimum",
            400,
            outcome="not_submitted",
            transactions=[{"kind": "approval", "hash": "0x" + "a" * 64}],
        )

    node.open = invalid
    for _ in range(4):
        r = await client.post("/v1/chat/completions", headers=headers, json=BODY)
        assert r.status_code == 400
    assert calls == 1
    row = app.state.store.all("session")[0]
    assert row["state"] == "failed" and row["transactions"][0]["kind"] == "approval"
    policy = app.state.store.policy()
    policy.revision += 1
    app.state.store.put("settings", "policy", policy.model_dump())
    await client.post("/v1/chat/completions", headers=headers, json=BODY)
    assert calls == 2


async def test_storage_remains_unready_until_write_probe_succeeds(env):
    app, client, *_ = env
    app.state.store.error = "storage failed"
    app.state.store.db.execute("PRAGMA query_only=ON")
    try:
        with pytest.raises(Exception, match="readonly"):
            await app.state.sessions.tick()
        assert (await client.get("/readyz")).status_code == 503
    finally:
        app.state.store.db.execute("PRAGMA query_only=OFF")
    await app.state.sessions.tick()
    assert (await client.get("/readyz")).status_code == 200


async def test_native_duration_error_preserves_approval_receipt(env):
    *_, cfg = env
    node = Node(cfg)

    async def response(request):
        return httpx.Response(
            500,
            json={
                "error": "execution reverted: SessionTooShort()",
                "progress": {
                    "stage": "not_submitted",
                    "transactions": [{"kind": "approval", "hash": "0x" + "a" * 64}],
                },
            },
        )

    await node.client.aclose()
    node.client = httpx.AsyncClient(transport=httpx.MockTransport(response), base_url="http://node")
    try:
        with pytest.raises(GatewayError) as raised:
            await node.open("0x" + "b" * 64, 300, operation_id="test")
        assert raised.value.code == "session_duration_invalid"
        assert raised.value.outcome == "not_submitted"
        assert raised.value.transactions[0]["kind"] == "approval"
    finally:
        await node.aclose()


async def test_close_intent_survives_control_outage(env):
    app, client, node, *_ = env
    headers, _ = await key(client)
    assert (await client.post("/v1/chat/completions", headers=headers, json=BODY)).status_code == 200
    manager = app.state.sessions
    row = app.state.store.sessions()[0]
    original = node.identity

    async def unavailable():
        raise GatewayError("node_unreachable", "Temporary control timeout")

    node.identity = unavailable
    with pytest.raises(GatewayError):
        await manager.close(row["id"])
    stored = app.state.store.get("session", row["id"])
    assert stored["state"] == "draining"
    node.identity = original
    stored["retry_at"] = 0
    app.state.store.put("session", row["id"], stored)
    await manager.tick()
    for _ in range(20):
        if app.state.store.get("session", row["id"])["state"] == "closed":
            break
        await asyncio.sleep(0.01)
    assert app.state.store.get("session", row["id"])["state"] == "closed"
    assert node.opens == 1
