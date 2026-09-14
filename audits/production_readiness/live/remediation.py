"""Funded verification of repaired acceptance gates. Explicit opt-in required."""

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/remediation"
BASE = "http://localhost:8000"
MODEL = "0xc2c4b037ff12e0aa81178deac52aeed902b36189b9e6feae22b72324c9221130"


async def main():
    results = []

    def record(test, passed, **fields):
        results.append({"test": test, "passed": bool(passed), **fields})
        (OUT / "live-results.json").write_text(json.dumps(results, indent=2))
        print(json.dumps(results[-1]), flush=True)

    async with httpx.AsyncClient(base_url=BASE, timeout=260, trust_env=False) as c:
        for _ in range(60):
            if (await c.get("/readyz")).status_code == 200:
                break
            await asyncio.sleep(2)
        r = await c.post(
            "/admin/api/login",
            json={"token": (ROOT / "secrets/admin-token").read_text().strip()},
            headers={"Origin": BASE},
        )
        r.raise_for_status()
        admin = {"Origin": BASE, "X-CSRF-Token": r.json()["csrf"]}

        async def get(route):
            r = await c.get("/admin/api/" + route)
            r.raise_for_status()
            return r.json()

        original = await get("policy")
        baseline = await get("status")
        (OUT / "live-baseline.json").write_text(
            json.dumps({"policy": original, "status": baseline}, indent=2)
        )
        assert baseline["live_sessions"] == baseline["active_requests"] == 0
        policy = json.loads(json.dumps(original))
        policy["models"] = [
            {
                **next(m for m in original["models"] if m["id"] == MODEL),
                "duration_seconds": 300,
                "max_sessions": 2,
                "retention": "until_expiry",
                "warm_until": None,
            }
        ]
        policy.update(max_sessions=2, queue_seconds=60, paused=False)
        policy["providers"] = {"mode": "all", "allow": [], "deny": []}
        policy["budget"]["max_session_stake_wei"] = str(3 * 10**18)
        policy["budget"]["max_total_stake_wei"] = str(10 * 10**18)
        r = await c.put("/admin/api/policy", headers=admin, json=policy)
        r.raise_for_status()
        campaign_revision = r.json()["revision"]
        r = await c.post(
            "/admin/api/keys",
            headers=admin,
            json={
                "name": "Remediation acceptance",
                "models": [MODEL],
                "concurrency": 8,
                "requests_per_minute": 120,
            },
        )
        r.raise_for_status()
        key = r.json()
        auth = {"Authorization": "Bearer " + key["key"]}
        start_ids = {s["id"] for s in (await get("sessions"))["sessions"]}
        try:
            before = await get("sessions")
            body = {
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": "Reply with exactly READY"}],
                "max_tokens": 2048,
            }
            bad = await c.post(
                "/v1/chat/completions", headers=auth, content=json.dumps({**body, "extension": float("nan")})
            )
            wrong = await c.post(
                "/v1/chat/completions", headers=auth, json={**body, "model": "deepseek-v4-flahs"}
            )
            record(
                "invalid_input_before_escrow",
                bad.status_code == 400 and wrong.status_code == 404 and before == await get("sessions"),
                nan_status=bad.status_code,
                wrong_model_status=wrong.status_code,
            )

            peak = 0
            stop = asyncio.Event()

            async def observe():
                nonlocal peak
                while not stop.is_set():
                    s = await get("status")
                    peak = max(peak, s["active_requests"])
                    await asyncio.sleep(0.25)

            observer = asyncio.create_task(observe())

            async def prompt(index):
                marker = f"FIXED_{index}"
                started = time.monotonic()
                r = await c.post(
                    "/v1/chat/completions",
                    headers={**auth, "Idempotency-Key": uuid.uuid4().hex},
                    json={
                        **body,
                        "messages": [
                            {"role": "user", "content": "Reply with exactly " + marker + " and nothing else."}
                        ],
                    },
                )
                value = r.json()
                (OUT / f"live-concurrent-{index}.json").write_text(json.dumps(value, indent=2))
                text = value.get("choices", [{}])[0].get("message", {}).get("content") or ""
                return {
                    "index": index,
                    "status": r.status_code,
                    "seconds": time.monotonic() - started,
                    "marker_present": marker in text,
                }

            try:
                replies = await asyncio.gather(*(prompt(i) for i in range(4)))
            finally:
                stop.set()
                await observer
            record(
                "four_cold_requests",
                all(r["status"] == 200 and r["marker_present"] for r in replies),
                peak_active=peak,
                replies=replies,
            )
            created = [
                s
                for s in (await get("sessions"))["sessions"]
                if s["id"] not in start_ids and s.get("chain_id")
            ]
            record(
                "minimum_300_seconds_on_chain",
                bool(created) and all(s["ends_at"] - s["opened_at"] >= 300 for s in created),
                sessions=[
                    {
                        "chain_id": s["chain_id"],
                        "duration": s["ends_at"] - s["opened_at"],
                        "stake_wei": s["stake_wei"],
                    }
                    for s in created
                ],
            )
            r = await c.post(
                "/v1/chat/completions",
                headers=auth,
                json={
                    **body,
                    "stream": True,
                    "messages": [{"role": "user", "content": "Reply exactly STREAM_FIXED"}],
                },
            )
            record(
                "real_stream",
                r.status_code == 200 and "[DONE]" in r.text and '"error"' not in r.text,
                status=r.status_code,
            )
        finally:
            try:
                # Restore user intent before closing campaign-owned sessions.
                current = await get("policy")
                if current["revision"] == campaign_revision:
                    original["revision"] = current["revision"]
                    r = await c.put("/admin/api/policy", headers=admin, json=original)
                    r.raise_for_status()
                else:
                    record(
                        "user_policy_changed",
                        False,
                        message="Concurrent user edit preserved; original policy not overwritten",
                    )
                deadline = time.monotonic() + 180
                close_errors = []
                while True:
                    rows = [s for s in (await get("sessions"))["sessions"] if s["id"] not in start_ids]
                    pending = [s for s in rows if s["state"] not in ("closed", "failed")]
                    if not pending or time.monotonic() >= deadline:
                        break
                    for s in pending:
                        # Same-ID close is idempotent; never replace an uncertain opening.
                        try:
                            r = await c.post(
                                "/admin/api/sessions/" + s["id"] + "/close", headers=admin, json={}
                            )
                            if r.status_code >= 400:
                                close_errors.append({"id": s["id"], "status": r.status_code})
                        except httpx.RequestError as exc:
                            close_errors.append({"id": s["id"], "error": type(exc).__name__})
                    await asyncio.sleep(2)
                (OUT / "live-final-sessions.json").write_text(json.dumps(rows, indent=2))
                final = await get("status")
                (OUT / "live-final-status.json").write_text(json.dumps(final, indent=2))
                record(
                    "cleanup",
                    not pending and final["active_requests"] == 0,
                    live_sessions=final["live_sessions"],
                    balances=final["balances"],
                    held=final["held"],
                    transient_close_errors=close_errors,
                )
            finally:
                # Even a cleanup/control outage must not skip credential revocation.
                r = await c.delete("/admin/api/keys/" + key["record"]["id"], headers=admin)
                r.raise_for_status()
    assert all(r["passed"] for r in results), (
        "One or more funded acceptance checks failed; inspect retained evidence"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-wallet-transactions", action="store_true", required=True)
    parser.parse_args()
    asyncio.run(main())
