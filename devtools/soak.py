"""Wallet-free HTTP soak with an explicit simulated provider.

Run: .venv/bin/python -m devtools.soak --seconds 3600 --output data/soak.json
Starts a disposable loopback service; never connects to the funded node.
"""

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import httpx

from devtools.fakes import BID, MODEL, PROVIDER, WALLET, FakeHelper, FakeNode
from gateway.app import create_app as gateway_app
from gateway.config import Config
from gateway.models import ModelPolicy, Policy, RecoveryPolicy


def create_app():
    if os.getenv("SOAK_DEBUG"):
        import faulthandler

        faulthandler.dump_traceback_later(5, repeat=True)

    class SlowNode(FakeNode):
        def __init__(self):
            super().__init__()
            self.inflight = set()
            self.peak = 0
            self.duplicate = 0
            self.calls = 0

        async def completion(self, session_id, model_id, body, request_id):
            self.calls += 1
            self.duplicate += int(session_id in self.inflight)
            self.inflight.add(session_id)
            self.peak = max(self.peak, len(self.inflight))
            try:
                await asyncio.sleep(0.05)
                return httpx.Response(
                    200,
                    json={
                        "id": request_id,
                        "object": "chat.completion",
                        "model": model_id,
                        "choices": [
                            {"message": {"role": "assistant", "content": body["messages"][0]["content"]}}
                        ],
                    },
                )
            finally:
                self.inflight.discard(session_id)

    node = SlowNode()
    app = gateway_app(
        Config(
            data_dir=Path(os.environ["DATA_DIR"]),
            admin_token="soak-dummy-secret-" + "x" * 40,
            public_origin="http://127.0.0.1:8766",
            secure_cookie=False,
            demo=True,
        ),
        node=node,
        helper=FakeHelper(),
    )
    app.state.store.put(
        "settings",
        "policy",
        Policy(
            models=[
                ModelPolicy(
                    id=MODEL, alias="soak", max_sessions=8, duration_seconds=86400, retention="until_expiry"
                )
            ],
            max_sessions=8,
            queue_seconds=2,
            recovery=RecoveryPolicy(enabled=False),
        ).model_dump(),
    )
    if os.getenv("SOAK_PREWARM"):
        # Explicit fixture setup: avoids exercising the separately tested opener.
        for index in range(1, 9):
            sid, local = "0x" + f"{index:064x}", f"warm-{index}"
            now = int(time.time())
            node.sessions[sid] = {
                "Id": sid,
                "User": WALLET,
                "Provider": PROVIDER,
                "BidID": BID,
                "ModelAgentId": MODEL,
                "OpenedAt": now,
                "EndsAt": now + 86400,
                "ClosedAt": 0,
                "Stake": "5000000000000000000",
            }
            app.state.store.put(
                "session",
                local,
                {
                    "id": local,
                    "chain_id": sid,
                    "model": MODEL,
                    "alias": "soak",
                    "provider": PROVIDER,
                    "bid": BID,
                    "state": "open",
                    "created_at": now,
                    "last_used": now,
                    "ends_at": now + 86400,
                    "stake_wei": "5000000000000000000",
                    "revision": 0,
                },
            )
        node.opens = 8
    if os.getenv("SOAK_DEBUG"):
        import traceback

        original_policy = app.state.store.policy
        policy_reads = 0

        def debug_policy():
            nonlocal policy_reads
            policy_reads += 1
            if policy_reads == 10000:
                traceback.print_stack()
            if policy_reads >= 10000:
                raise RuntimeError("Test diagnostic: excessive policy reads")
            return original_policy()

        app.state.store.policy = debug_policy

    @app.get("/test-metrics")
    async def metrics():
        return {
            "calls": node.calls,
            "opens": node.opens,
            "peak": node.peak,
            "duplicate_leases": node.duplicate,
            "active": len(app.state.sessions.active),
            "waiting": app.state.sessions.waiting,
            "events": app.state.store.db.execute(
                "SELECT count(*) FROM records WHERE kind='event'"
            ).fetchone()[0],
        }

    return app


async def run(args):
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="morpheus-soak-") as directory:
        log = output.with_suffix(".log").open("w")
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "devtools.soak:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "8766",
                "--no-access-log",
                "--limit-concurrency",
                "128",
            ],
            env={**os.environ, "DATA_DIR": directory, **({"SOAK_PREWARM": "1"} if args.warm else {})},
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        result = {"mode": "real HTTP, simulated provider, no wallet", "samples": [], "errors": []}
        try:
            async with httpx.AsyncClient(
                base_url="http://127.0.0.1:8766",
                timeout=10,
                limits=httpx.Limits(max_connections=160),
                trust_env=False,
            ) as client:
                for _ in range(100):
                    try:
                        if (await client.get("/healthz")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
                login = await client.post(
                    "/admin/api/login",
                    json={"token": "soak-dummy-secret-" + "x" * 40},
                    headers={"Origin": "http://127.0.0.1:8766"},
                )
                login.raise_for_status()
                key = await client.post(
                    "/admin/api/keys",
                    json={"name": "soak", "concurrency": 32, "requests_per_minute": 10000},
                    headers={"Origin": "http://127.0.0.1:8766", "X-CSRF-Token": login.json()["csrf"]},
                )
                key.raise_for_status()
                auth = {"Authorization": "Bearer " + key.json()["key"]}
                if args.warm:
                    result["fixture_seeded_sessions"] = 8
                    for concurrency in (1, 2, 4, 8):
                        warm = await asyncio.gather(
                            *[
                                client.post(
                                    "/v1/chat/completions",
                                    headers=auth,
                                    json={"model": "soak", "messages": [{"role": "user", "content": "warm"}]},
                                )
                                for _ in range(concurrency)
                            ]
                        )
                        for response in warm:
                            response.raise_for_status()
                    result["warmup_requests"] = 15
                started = time.monotonic()
                counts, latencies, sequence, bad = Counter(), [], 0, 0

                async def request(index):
                    nonlocal bad
                    t = time.monotonic()
                    kind = (
                        "invalid_model" if index % 20 < 3 else "unauthorized" if index % 20 == 3 else "valid"
                    )
                    try:
                        response = await client.post(
                            "/v1/chat/completions",
                            headers=auth if kind != "unauthorized" else {},
                            json={
                                "model": "missing" if kind == "invalid_model" else "soak",
                                "messages": [{"role": "user", "content": str(index)}],
                            },
                        )
                        counts[str(response.status_code)] += 1
                        expected = {"invalid_model": 404, "unauthorized": 401, "valid": 200}[kind]
                        correct = response.status_code == expected
                        if correct and kind == "valid":
                            correct = response.json()["choices"][0]["message"]["content"] == str(index)
                        if not correct:
                            bad += 1
                            if len(result["errors"]) < 20:
                                result["errors"].append(
                                    {"index": index, "kind": kind, "status": response.status_code}
                                )
                    except Exception as exc:
                        counts[type(exc).__name__] += 1
                        bad += 1
                    latencies.append(time.monotonic() - t)

                next_sample = started + 60
                while time.monotonic() - started < args.seconds:
                    wave = time.monotonic()
                    await asyncio.gather(*(request(i) for i in range(sequence, sequence + 32)))
                    sequence += 32
                    if time.monotonic() >= next_sample:
                        ordered = sorted(latencies)
                        rss = int(
                            subprocess.check_output(
                                ["ps", "-o", "rss=", "-p", str(process.pid)], text=True
                            ).strip()
                        )
                        result["samples"].append(
                            {
                                "elapsed": time.monotonic() - started,
                                "requests": sequence,
                                "status_counts": dict(counts),
                                "incorrect": bad,
                                "p50_ms": statistics.median(ordered) * 1000,
                                "p95_ms": ordered[int(len(ordered) * 0.95)] * 1000,
                                "p99_ms": ordered[int(len(ordered) * 0.99)] * 1000,
                                "rss_kib": rss,
                                "sqlite_bytes": sum(
                                    p.stat().st_size for p in Path(directory).glob("gateway.sqlite*")
                                ),
                            }
                        )
                        output.write_text(json.dumps(result, indent=2))
                        result["samples"][-1]["metrics"] = (await client.get("/test-metrics")).json()
                        output.write_text(json.dumps(result, indent=2))
                        print(json.dumps(result["samples"][-1]), flush=True)
                        latencies.clear()
                        next_sample += 60
                    await asyncio.sleep(max(0, 32 / args.rps - (time.monotonic() - wave)))
                result.update(
                    elapsed_seconds=time.monotonic() - started,
                    requests=sequence,
                    status_counts=dict(counts),
                    incorrect=bad,
                    final_metrics=(await client.get("/test-metrics")).json(),
                )
                result["passed"] = (
                    bad == 0
                    and result["final_metrics"]["duplicate_leases"] == 0
                    and result["final_metrics"]["active"] == 0
                )
                output.write_text(json.dumps(result, indent=2))
        except Exception as exc:
            result.update(passed=False, harness_exception=type(exc).__name__, service_exit=process.poll())
            output.write_text(json.dumps(result, indent=2))
            raise
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--rps", type=float, default=60)
    parser.add_argument("--output", default="data/soak.json")
    parser.add_argument(
        "--warm", action="store_true", help="Warm the pool before measurement; excludes cold-start behavior"
    )
    asyncio.run(run(parser.parse_args()))
