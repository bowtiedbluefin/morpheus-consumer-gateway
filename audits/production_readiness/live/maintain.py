import asyncio
import json
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/production-readiness"
BASE = "http://localhost:8000"
MODEL = "0xc2c4b037ff12e0aa81178deac52aeed902b36189b9e6feae22b72324c9221130"


async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=180, trust_env=False) as c:
        r = await c.post(
            "/admin/api/login",
            json={"token": (ROOT / "secrets/admin-token").read_text().strip()},
            headers={"Origin": BASE},
        )
        r.raise_for_status()
        admin = {"Origin": BASE, "X-CSRF-Token": r.json()["csrf"]}

        async def get(path):
            r = await c.get("/admin/api" + path)
            r.raise_for_status()
            return r.json()

        before = await get("/sessions")
        ids = {s["id"] for s in before["sessions"]}
        p = await get("/policy")
        end = int(time.time()) + 430
        p["models"] = [
            {
                **p["models"][0],
                "max_sessions": 1,
                "duration_seconds": 301,
                "retention": "maintain",
                "warm_until": end,
            }
        ]
        p["max_sessions"] = 1
        p["paused"] = False
        p["providers"] = {
            "mode": "allowlist",
            "allow": ["0x23da7e8ce578bfead351caff6b0f4e976ea99676"],
            "deny": [],
        }
        r = await c.put("/admin/api/policy", headers=admin, json=p)
        r.raise_for_status()
        # Retain maintenance intentionally: manual close should be followed by an automatic replacement.
        for row in before["sessions"]:
            if row["state"] == "open":
                r = await c.post("/admin/api/sessions/" + row["id"] + "/close", headers=admin)
                r.raise_for_status()
        samples = []
        last = None
        while time.time() < end + 75:
            rows = (await get("/sessions"))["sessions"]
            new = [s for s in rows if s["id"] not in ids]
            active = [s for s in rows if s["state"] not in ["closed", "failed"]]
            sample = {"at": time.time(), "new_rows": new, "live_count": len(active)}
            samples.append(sample)
            summary = {
                "at": sample["at"],
                "states": [(s["id"], s["state"]) for s in new],
                "live": len(active),
            }
            sig = json.dumps(summary["states"])
            if sig != last:
                print(json.dumps(summary), flush=True)
                last = sig
            result = {
                "test": "maintain_replaces_then_stops_at_deadline",
                "warm_until": end,
                "samples": samples,
            }
            (OUT / "live-maintain-301.json").write_text(json.dumps(result, indent=2))
            if time.time() > end + 20 and not active:
                break
            await asyncio.sleep(5)
        funded = [s for s in new if s.get("chain_id")]
        result.update(
            passed=len(funded) >= 2 and all(s["state"] == "closed" for s in funded) and not active,
            opened_sessions=len(funded),
            peak_live=max(s["live_count"] for s in samples),
            final_rows=new,
        )
        (OUT / "live-maintain-301.json").write_text(json.dumps(result, indent=2))
        print(json.dumps({k: v for k, v in result.items() if k not in ["samples", "final_rows"]}), flush=True)


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
