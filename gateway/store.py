import fcntl
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from .models import Policy


class Store:
    """Single-owner SQLite journal. Never holds a DB transaction across network I/O."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = (directory / "owner.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("Only one gateway process/worker may own this data directory") from None
        self.db = sqlite3.connect(directory / "gateway.sqlite", check_same_thread=False)
        os.chmod(directory / "gateway.sqlite", 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, data TEXT, PRIMARY KEY(kind,id))"
        )
        self.db.commit()
        if self.get("settings", "policy") is None:
            self.put("settings", "policy", Policy().model_dump())

    def get(self, kind: str, id: str):
        row = self.db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, id)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, kind: str) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT data FROM records WHERE kind=? ORDER BY rowid DESC", (kind,))
        ]

    def put(self, kind: str, id: str, value: dict):
        with self.db:
            self.db.execute(
                "INSERT INTO records VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
                (kind, id, json.dumps(value, allow_nan=False)),
            )

    def delete(self, kind: str, id: str):
        with self.db:
            self.db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, id))

    def policy(self) -> Policy:
        return Policy.model_validate(self.get("settings", "policy"))

    def event(self, action: str, **fields):
        id = uuid.uuid4().hex
        self.put("event", id, {"id": id, "time": int(time.time()), "action": action, **fields})
        with self.db:
            self.db.execute(
                "DELETE FROM records WHERE kind='event' AND rowid NOT IN "
                "(SELECT rowid FROM records WHERE kind='event' ORDER BY rowid DESC LIMIT 500)"
            )

    def close(self):
        self.db.close()
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()
