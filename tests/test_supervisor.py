import json
import os
import sys

import pytest

from gateway.models import Policy
from node_helper.app import Supervisor, atomic_write


async def test_supervisor_restarts_real_child_and_rolls_back_invalid_config(tmp_path):
    # Actual OS subprocesses, without a wallet, Docker socket or blockchain transactions.
    script = tmp_path / "child.py"
    script.write_text("""
import json, sys, time
from pathlib import Path
cfg=json.loads(Path("rating-config.json").read_text())
if cfg.get("algorithm") == "broken": sys.exit(2)
while True: time.sleep(1)
""")
    supervisor = Supervisor(tmp_path, [sys.executable, str(script)], dict(os.environ), readiness_seconds=0.1)
    atomic_write(supervisor.rating_path, json.dumps(Policy().rating()).encode())

    async def ready():
        import asyncio

        await asyncio.sleep(0.05)
        if supervisor.process.returncode is not None:
            raise RuntimeError("child exited")

    supervisor.ready = ready
    await supervisor.start()
    first = supervisor.process
    try:
        result = await supervisor.restart(Policy().rating())
        assert result["ready"] and first.returncode is not None
        assert supervisor.process.pid != first.pid
        second = supervisor.process
        with pytest.raises(RuntimeError):
            await supervisor.restart({"algorithm": "broken"})
        assert second.returncode is not None
        assert supervisor.process.returncode is None
        assert json.loads(supervisor.rating_path.read_text())["algorithm"] == "default"
        assert not supervisor.journal.exists()
    finally:
        await supervisor.stop()


async def test_helper_restart_idempotency_and_journal_read(tmp_path):
    import httpx

    from node_helper.app import create_app

    supervisor = Supervisor(tmp_path, [], {})
    calls = []

    async def restart(rating):
        calls.append(rating)
        return {"ready": True, "rating_hash": "verified"}

    supervisor.restart = restart
    app = create_app(supervisor)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://helper") as client:
        body = {"operation_id": "a" * 32}
        assert (await client.post("/restart", json=body)).status_code == 200
        assert (await client.post("/restart", json=body)).status_code == 200
        assert len(calls) == 1
        assert (await client.get("/operations/" + "a" * 32)).json()["state"] == "succeeded"
        assert (await client.post("/restart", json={**body, "rating": Policy().rating()})).status_code == 409
        journal = tmp_path / "gateway-journal"
        journal.mkdir()
        atomic_write(journal / ("b" * 32 + ".json"), b'{"stage":"not_submitted","completed":true}')
        assert (await client.get("/journal/" + "b" * 32)).json()["completed"]
        assert (await client.get("/journal/invalid")).status_code == 400


@pytest.mark.parametrize("chain", ["8453", "84532"])
def test_packaged_node_has_network_defaults_and_existing_storage(tmp_path, monkeypatch, chain):
    from node_helper import app as helper_module

    captured = {}
    original = helper_module.Supervisor

    def capture(directory, command, environment, **kwargs):
        captured.update(environment)
        return original(directory, command, environment, **kwargs)

    monkeypatch.setattr(helper_module, "Supervisor", capture)
    monkeypatch.setenv("NODE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NODE_PASSWORD", "test-password-" + "x" * 40)
    monkeypatch.setenv("WALLET_PRIVATE_KEY", "1" * 64)
    monkeypatch.setenv("ETH_NODE_CHAIN_ID", chain)
    for name in (
        "NODE_PASSWORD_FILE",
        "WALLET_PRIVATE_KEY_FILE",
        "BLOCKSCOUT_API_URL",
        "DIAMOND_CONTRACT_ADDRESS",
        "MOR_TOKEN_ADDRESS",
    ):
        monkeypatch.delenv(name, raising=False)
    helper_module.create_app()
    assert (tmp_path / "storage").is_dir()
    # The pinned node uses BlockscoutApiV2Client, which appends /addresses/...
    network_host = "base" if chain == "8453" else "base-sepolia"
    assert captured["BLOCKSCOUT_API_URL"] == f"https://{network_host}.blockscout.com/api/v2"
    assert ("sepolia" in captured["BLOCKSCOUT_API_URL"]) == (chain == "84532")
    assert len(captured["DIAMOND_CONTRACT_ADDRESS"]) == 42
    assert len(captured["MOR_TOKEN_ADDRESS"]) == 42
