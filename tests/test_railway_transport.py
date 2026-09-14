import asyncio
import json
import os
import socket
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi.responses import RedirectResponse, Response

from gateway.models import Policy
from gateway.node import GatewayError, Helper
from gateway.runtime import tcp_listener
from node_helper.app import Supervisor, atomic_write, create_app
from node_helper.run import transport
from tests.test_http_boundaries import serve

TOKEN = "test-helper-" + "x" * 48


@pytest.mark.parametrize("token", ["", "short", "x" * 32 + "\n", "é" * 32])
def test_http_helper_refuses_missing_or_invalid_credentials(monkeypatch, token):
    monkeypatch.setenv("HELPER_TRANSPORT", "http")
    monkeypatch.setenv("HELPER_TOKEN", token)
    monkeypatch.delenv("HELPER_TOKEN_FILE", raising=False)
    with pytest.raises(RuntimeError, match="HELPER_TOKEN"):
        transport()
    with pytest.raises(RuntimeError, match="HELPER_TOKEN"):
        Helper(url="http://node:8083", token=token)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://node:8083",
        "http://user:password@node:8083",
        "http://node/path",
        "http://node?key=x",
        "http://node#fragment",
    ],
)
def test_helper_rejects_ambiguous_endpoint_configuration(url):
    with pytest.raises(RuntimeError, match="HELPER_URL"):
        Helper(url=url, token=TOKEN)


def test_helper_rejects_two_transports():
    with pytest.raises(RuntimeError, match="only one"):
        Helper("/control/helper.sock", url="http://node:8083", token=TOKEN)


async def test_private_http_auth_restart_idempotency_and_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("HELPER_TRANSPORT", "http")
    monkeypatch.setenv("HELPER_TOKEN", TOKEN)
    monkeypatch.delenv("HELPER_TOKEN_FILE", raising=False)
    supervisor = Supervisor(tmp_path, [sys.executable, "-c", "import time; time.sleep(60)"], dict(os.environ))
    atomic_write(supervisor.rating_path, json.dumps(Policy().rating()).encode())

    async def ready():
        await asyncio.sleep(0.02)
        assert supervisor.process.returncode is None
        supervisor.health = {"ready": True}

    supervisor.ready = ready
    await supervisor.start()
    initial_pid = supervisor.process.pid
    app = create_app(supervisor)
    journal = tmp_path / "gateway-journal"
    journal.mkdir()
    atomic_write(journal / ("b" * 32 + ".json"), b'{"completed":true,"stage":"not_submitted"}')
    try:
        async with serve(app, lifespan="off") as (url, _):
            helper = Helper(url=url, token=TOKEN)
            try:
                async with httpx.AsyncClient(base_url=url, trust_env=False) as outsider:
                    for method, path in [
                        ("GET", "/status"),
                        ("POST", "/restart"),
                        ("GET", "/journal/" + "b" * 32),
                        ("GET", "/operations/" + "a" * 32),
                    ]:
                        r = await outsider.request(method, path)
                        assert r.status_code == 401 and TOKEN not in r.text
                    assert (await outsider.get("/healthz")).json() == {"running": True}
                    bad = await outsider.get("/status", headers={"Authorization": "Bearer " + "z" * 48})
                    assert bad.status_code == 401
                assert supervisor.process.pid == initial_pid
                assert (await helper.request("GET", "/journal/" + "b" * 32))["completed"]
                body = {"operation_id": "a" * 32, "rating": Policy().rating()}
                assert (await helper.request("POST", "/restart", json=body))["ready"]
                new_pid = supervisor.process.pid
                assert new_pid != initial_pid
                assert (await helper.request("POST", "/restart", json=body))["ready"]
                assert supervisor.process.pid == new_pid
                assert (await helper.request("GET", "/operations/" + "a" * 32))["state"] == "succeeded"
                rejected = await helper.client.post(
                    "/restart", headers={"Origin": "https://attacker.example"}, json=body
                )
                assert rejected.status_code == 403 and supervisor.process.pid == new_pid
                oversized = await helper.client.post("/restart", content=b"x" * (128 * 1024 + 1))
                assert oversized.status_code == 413 and supervisor.process.pid == new_pid
            finally:
                await helper.aclose()
    finally:
        await supervisor.stop()


async def test_helper_wrong_token_is_actionable_and_redirect_is_not_followed(tmp_path, monkeypatch):
    monkeypatch.setenv("HELPER_TRANSPORT", "http")
    monkeypatch.setenv("HELPER_TOKEN", TOKEN)
    supervisor = Supervisor(tmp_path, [], {})
    supervisor.process = SimpleNamespace(returncode=None)
    app = create_app(supervisor)
    followed = []

    @app.get("/redirect")
    async def redirect():
        return RedirectResponse("/credential-target")

    @app.get("/credential-target")
    async def credential_target():
        followed.append(True)
        return {"oops": True}

    @app.get("/huge")
    async def huge():
        return Response(b"x" * (2 * 1024 * 1024 + 1))

    async with serve(app, lifespan="off") as (url, _):
        wrong = Helper(url=url, token="wrong-" + "x" * 48)
        right = Helper(url=url, token=TOKEN)
        try:
            with pytest.raises(GatewayError) as denied:
                await wrong.request("GET", "/status")
            assert denied.value.code == "helper_auth"
            with pytest.raises(GatewayError) as moved:
                await right.request("GET", "/redirect")
            assert moved.value.code == "helper_rejected" and not followed
            with pytest.raises(GatewayError) as big:
                await right.request("GET", "/huge")
            assert big.value.code == "helper_protocol"
        finally:
            await wrong.aclose()
            await right.aclose()


def test_tcp_listener_accepts_ipv4_and_ipv6():
    with tcp_listener("::", 0) as listener:
        port = listener.getsockname()[1]
        listener.settimeout(1)
        for address in ("127.0.0.1", "::1"):
            with socket.create_connection((address, port), timeout=1):
                connection, _ = listener.accept()
                connection.close()


async def test_railway_readiness_requires_authenticated_management(env):
    app, client, _, helper, cfg = env
    cfg.helper_url = "http://node:8083"
    original = helper.request

    async def rejected(*args, **kwargs):
        raise GatewayError("helper_auth", "Node helper authentication failed; check HELPER_TOKEN")

    helper.request = rejected
    assert (await client.get("/readyz")).status_code == 503
    helper.request = original
    await app.state.sessions.verify_identity()
    assert (await client.get("/readyz")).status_code == 200
