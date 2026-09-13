import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/production-readiness"
BASE = "http://localhost:8000"


async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=260, trust_env=False) as c:
        r = await c.post(
            "/admin/api/login",
            json={"token": (ROOT / "secrets/admin-token").read_text().strip()},
            headers={"Origin": BASE},
        )
        r.raise_for_status()
        admin = {"Origin": BASE, "X-CSRF-Token": r.json()["csrf"]}

        async def get(p):
            r = await c.get("/admin/api" + p)
            r.raise_for_status()
            return r.json()

        p = await get("/policy")
        p["providers"] = {"mode": "all", "allow": [], "deny": []}
        p["queue_seconds"] = 60
        r = await c.put("/admin/api/policy", headers=admin, json=p)
        r.raise_for_status()
        auth = {"Authorization": "Bearer " + (ROOT / "secrets/production-test-api-key").read_text().strip()}

        async def job(i):
            t = time.monotonic()
            r = await c.post(
                "/v1/chat/completions",
                headers={**auth, "Idempotency-Key": uuid.uuid4().hex},
                json={
                    "model": "deepseek-v4-flash",
                    "messages": [
                        {
                            "role": "user",
                            "content": f"Begin with RUN2_{i}. Write 300 numbered detailed sentences describing network failure modes. Continue until all 300 are complete.",
                        }
                    ],
                    "max_tokens": 4096,
                },
            )
            d = r.json()
            text = d.get("choices", [{}])[0].get("message", {}).get("content", "")
            return {
                "index": i,
                "status": r.status_code,
                "marker": f"RUN2_{i}" in text,
                "seconds": time.monotonic() - t,
                "request_id": r.headers.get("x-request-id"),
                "error": d.get("error"),
            }

        tasks = [asyncio.create_task(job(i)) for i in range(4)]
        samples = []
        while not all(t.done() for t in tasks):
            d = await get("/status")
            samples.append(
                {
                    "at": time.time(),
                    "active": d["active_requests"],
                    "queued": d["queued"],
                    "sessions": d["live_sessions"],
                }
            )
            await asyncio.sleep(0.5)
        responses = await asyncio.gather(*tasks)
        peak = max(s["active"] for s in samples)
        result = {
            "test": "four_live_concurrent_multi_provider",
            "passed": peak == 4 and all(r["status"] == 200 and r["marker"] for r in responses),
            "peak": peak,
            "responses": responses,
            "samples": samples,
            "sessions": await get("/sessions"),
            "status": await get("/status"),
        }
        (OUT / "live-concurrency-multiprovider.json").write_text(json.dumps(result, indent=2))
        print(
            json.dumps({k: v for k, v in result.items() if k not in ("samples", "sessions", "status")}),
            flush=True,
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Funded acceptance test; modifies local policy and creates/closes sessions. Read ../README.md first."
    )
    parser.add_argument("--allow-wallet-transactions", action="store_true")
    parser.add_argument("--allow-service-interruption", action="store_true")
    args = parser.parse_args()
    if not args.allow_wallet_transactions:
        parser.error("This test spends gas and locks MOR; explicitly pass --allow-wallet-transactions")
    (ROOT / "data/production-readiness").mkdir(parents=True, exist_ok=True)
    asyncio.run(main())
