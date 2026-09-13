import asyncio
import json
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/production-readiness"
BASE = "http://localhost:8000"


async def main():
    results = []
    async with httpx.AsyncClient(base_url=BASE, timeout=260, trust_env=False) as c:
        r = await c.post(
            "/admin/api/login",
            json={"token": (ROOT / "secrets/admin-token").read_text().strip()},
            headers={"Origin": BASE},
        )
        r.raise_for_status()
        admin = {"Origin": BASE, "X-CSRF-Token": r.json()["csrf"]}
        auth = {"Authorization": "Bearer " + (ROOT / "secrets/production-test-api-key").read_text().strip()}

        async def get(p):
            r = await c.get("/admin/api" + p)
            r.raise_for_status()
            return r.json()

        def record(name, passed, **kw):
            d = {"test": name, "passed": passed, **kw}
            results.append(d)
            print(json.dumps(d), flush=True)
            (OUT / "live-lifecycle.json").write_text(json.dumps(results, indent=2))

        p = await get("/policy")
        p["recovery"].update(enabled=True, auto_withdraw=True, cleanup_untracked_expired=True)
        r = await c.put("/admin/api/policy", headers=admin, json=p)
        r.raise_for_status()
        # A disconnect after receiving the first event exercises real TCP cancellation.
        t = time.monotonic()
        first = None
        async with c.stream(
            "POST",
            "/v1/chat/completions",
            headers=auth,
            json={
                "model": "deepseek-v4-flash",
                "messages": [
                    {
                        "role": "user",
                        "content": "Write 300 detailed numbered sentences about distributed computing.",
                    }
                ],
                "stream": True,
                "max_tokens": 4096,
            },
        ) as r:
            status = r.status_code
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    first = line
                    break
        for _ in range(30):
            s = await get("/status")
            if s["active_requests"] == 0:
                break
            await asyncio.sleep(1)
        record(
            "live_client_disconnect_releases_lease",
            status == 200 and first is not None and s["active_requests"] == 0,
            elapsed=time.monotonic() - t,
            active=s["active_requests"],
        )
        # Request stores the actual response to distinguish truncated reasoning from routing failure.
        body = {
            "model": "deepseek-v4-flash",
            "messages": [
                {
                    "role": "user",
                    "content": "Start your final answer with DRAIN_OK. Write 150 numbered sentences about TCP.",
                }
            ],
            "max_tokens": 4096,
        }
        task = asyncio.create_task(c.post("/v1/chat/completions", headers=auth, json=body))
        for _ in range(120):
            s = await get("/status")
            if s["active_requests"] > 0:
                break
            if task.done():
                break
            await asyncio.sleep(0.25)
        t = time.monotonic()
        rr = await c.post(
            "/admin/api/node/restart", headers=admin, json={"immediate": False, "apply_rating": True}
        )
        op = rr.json()
        health = await c.get("/healthz")
        blocked = await c.post("/v1/chat/completions", headers=auth, json={**body, "max_tokens": 16})
        response = await task
        data = response.json()
        (OUT / "live-drain-response.json").write_text(json.dumps(data, indent=2))
        final = {}
        for _ in range(120):
            ops = await get("/operations")
            final = next((x for x in ops["operations"] if x["id"] == op.get("id")), {})
            if final.get("state") in ("succeeded", "failed", "interrupted"):
                break
            await asyncio.sleep(1)
        record(
            "live_graceful_restart_drains_and_applies_rating",
            rr.status_code == 200
            and response.status_code == 200
            and final.get("state") == "succeeded"
            and health.status_code == 200
            and blocked.status_code == 503,
            seconds=time.monotonic() - t,
            inference_status=response.status_code,
            blocked_status=blocked.status_code,
            operation=final,
        )
        r = await c.post(
            "/v1/chat/completions",
            headers=auth,
            json={
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": "Reply with exactly RESTART_OK."}],
                "max_tokens": 128,
            },
        )
        data = r.json()
        (OUT / "live-after-restart-response.json").write_text(json.dumps(data, indent=2))
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        record(
            "live_inference_after_node_restart",
            r.status_code == 200 and "RESTART_OK" in content,
            status=r.status_code,
            finish_reason=data.get("choices", [{}])[0].get("finish_reason"),
            content=content,
        )
        rows = (await get("/sessions"))["sessions"]
        candidate = next((x for x in rows if x["state"] == "open" and x["ends_at"] > time.time() + 30), None)
        if candidate:
            start = await get("/status")
            r = await c.post("/admin/api/sessions/" + candidate["id"] + "/close", headers=admin)
            r2 = await c.post("/admin/api/sessions/" + candidate["id"] + "/close", headers=admin)
            end = await get("/status")
            record(
                "live_early_close_repeat_is_idempotent",
                r.status_code == 200
                and r2.status_code == 200
                and r.json().get("state") in ("closed", "close_pending")
                and r.json().get("close_operation_id") == r2.json().get("close_operation_id"),
                session=candidate["chain_id"],
                first=r.json(),
                second=r2.json(),
                balances_before=start["balances"],
                balances_after=end["balances"],
                held_after=end["held"],
            )
        (OUT / "after-lifecycle-sessions.json").write_text(json.dumps(await get("/sessions"), indent=2))
        (OUT / "after-lifecycle-status.json").write_text(json.dumps(await get("/status"), indent=2))


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
    if not args.allow_service_interruption:
        parser.error(
            "This test interrupts the local gateway/node; explicitly pass --allow-service-interruption"
        )
    (ROOT / "data/production-readiness").mkdir(parents=True, exist_ok=True)
    asyncio.run(main())
