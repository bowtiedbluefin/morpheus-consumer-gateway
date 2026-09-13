"""Real SQLite durability checks using disposable files, without a wallet."""

import sqlite3
import subprocess
import sys
import time

import pytest

from gateway.app import create_app
from gateway.store import Store
from tests.conftest import key


def test_full_sqlite_page_budget_preserves_committed_settings(tmp_path):
    store = Store(tmp_path)
    try:
        original = store.policy().model_dump()
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        pages = store.db.execute("PRAGMA page_count").fetchone()[0]
        store.db.execute(f"PRAGMA max_page_count={pages}")
        with pytest.raises(sqlite3.OperationalError, match="full"):
            store.put("request", "oversized", {"payload": "x" * 1024 * 1024})
        assert store.get("request", "oversized") is None
        assert store.policy().model_dump() == original
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        store.close()


def test_corrupt_database_fails_startup_without_resetting_data(tmp_path):
    path = tmp_path / "gateway.sqlite"
    content = b"not a SQLite database; must not be overwritten"
    path.write_bytes(content)
    with pytest.raises(sqlite3.DatabaseError):
        Store(tmp_path)
    assert path.read_bytes() == content


def test_process_kill_preserves_acknowledged_wal_commit(tmp_path):
    script = """
import sys, time
from pathlib import Path
from gateway.store import Store
store=Store(Path(sys.argv[1]))
store.put('request','committed',{'state':'started','created_at':time.time()})
print('committed',flush=True)
while True: time.sleep(1)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert process.stdout.readline().strip() == "committed"
        process.kill()
        process.wait(timeout=5)
        store = Store(tmp_path)
        try:
            assert store.get("request", "committed")["state"] == "started"
            assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            store.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


async def test_restored_snapshot_reconciles_closed_chain_session(env, tmp_path):
    app, client, node, helper, cfg = env
    headers, _ = await key(client)
    response = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "demo", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    row = app.state.store.sessions()[0]
    restored_dir = tmp_path / "restored"
    restored_dir.mkdir()
    with sqlite3.connect(restored_dir / "gateway.sqlite") as backup:
        app.state.store.db.backup(backup)
    node.sessions[row["chain_id"]]["ClosedAt"] = int(time.time())
    from dataclasses import replace

    restored = create_app(replace(cfg, data_dir=restored_dir), node=node, helper=helper)
    try:
        await restored.state.sessions.recover()
        assert restored.state.store.get("session", row["id"])["state"] == "closed"
        assert node.opens == 1 and node.closes == 0
    finally:
        restored.state.store.close()


def test_retention_keeps_uncertain_financial_operations(tmp_path):
    store = Store(tmp_path)
    try:
        old = time.time() - 40 * 86400
        for state in ("open_unknown", "close_pending", "closed", "failed"):
            store.put("session", state, {"id": state, "state": state, "created_at": old})
        for state in ("uncertain", "pending", "succeeded", "failed"):
            store.put("operation", state, {"id": state, "state": state, "created_at": old})
        store.prune()
        assert {r["state"] for r in store.all("session")} == {"open_unknown", "close_pending"}
        assert {r["state"] for r in store.all("operation")} == {"uncertain", "pending"}
    finally:
        store.close()
