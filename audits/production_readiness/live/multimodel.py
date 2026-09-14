import asyncio
import json
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/production-readiness"
BASE = "http://localhost:8000"
SECOND = "0x6324881ed8f1322f7c42e98eb8c560ef13f4fc78f54d4d99a1ff1950190b4f1c"


async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=260, trust_env=False) as c:
        r = await c.post(
            "/admin/api/login",
            json={"token": (ROOT / "secrets/admin-token").read_text().strip()},
            headers={"Origin": BASE},
        )
        r.raise_for_status()
        admin = {"Origin": BASE, "X-CSRF-Token": r.json()["csrf"]}
        auth = {"Authorization": "Bearer " + (ROOT / "secrets/production-test-api-key").read_text().strip()}
        p = (await c.get("/admin/api/policy")).json()
        p["providers"] = {"mode": "all", "allow": [], "deny": []}
        p["models"] = [p["models"][0]]
        p["models"].append(
            {
                **p["models"][0],
                "id": SECOND,
                "alias": "llama-test",
                "max_sessions": 1,
                "duration_seconds": 600,
            }
        )
        r = await c.put("/admin/api/policy", headers=admin, json=p)
        r.raise_for_status()
        keys = (await c.get("/admin/api/keys")).json()["keys"]
        key = next(k for k in keys if k["name"] == "Production readiness campaign")
        r = await c.put(
            "/admin/api/keys/" + key["id"],
            headers=admin,
            json={
                k: ([m["id"] for m in p["models"]] if k == "models" else key[k])
                for k in ["name", "models", "concurrency", "requests_per_minute"]
            },
        )
        r.raise_for_status()
        quote = await c.get("/admin/api/models/" + SECOND + "/quote")
        print(json.dumps({"quote_status": quote.status_code, "quote": quote.json()}), flush=True)

        async def prompt(alias, marker):
            t = time.monotonic()
            r = await c.post(
                "/v1/chat/completions",
                headers=auth,
                json={
                    "model": alias,
                    "messages": [
                        {
                            "role": "user",
                            "content": f"Begin with {marker}. Write 200 numbered sentences about distributed systems.",
                        }
                    ],
                    "max_tokens": 4096,
                },
            )
            d = r.json()
            (OUT / (alias + "-multimodel-response.json")).write_text(json.dumps(d, indent=2))
            text = d.get("choices", [{}])[0].get("message", {}).get("content", "")
            return {
                "alias": alias,
                "status": r.status_code,
                "marker": marker in text,
                "returned_model": d.get("model"),
                "seconds": time.monotonic() - t,
                "request_id": r.headers.get("x-request-id"),
                "error": d.get("error"),
            }

        jobs = [
            asyncio.create_task(prompt("deepseek-v4-flash", "DEEPSEEK_ISOLATION")),
            asyncio.create_task(prompt("llama-test", "LLAMA_ISOLATION")),
        ]
        samples = []
        while not all(j.done() for j in jobs):
            rows = (await c.get("/admin/api/sessions")).json()["sessions"]
            samples.append(
                {
                    "at": time.time(),
                    "busy": [
                        {k: r[k] for k in ["id", "model", "provider", "chain_id"]} for r in rows if r["busy"]
                    ],
                }
            )
            await asyncio.sleep(0.5)
        responses = await asyncio.gather(*jobs)
        overlap = any(len({r["model"] for r in s["busy"]}) >= 2 for s in samples)
        result = {
            "test": "two_live_models_concurrently",
            "passed": overlap and all(r["status"] == 200 and r["marker"] for r in responses),
            "overlap": overlap,
            "responses": responses,
            "samples": samples,
        }
        (OUT / "live-multimodel-llama.json").write_text(json.dumps(result, indent=2))
        print(json.dumps({k: v for k, v in result.items() if k != "samples"}), flush=True)


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
