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
