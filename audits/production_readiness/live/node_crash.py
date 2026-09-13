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
        auth = {"Authorization": "Bearer " + (ROOT / "secrets/production-test-api-key").read_text().strip()}

        async def get(p):
            r = await c.get("/admin/api" + p)
            r.raise_for_status()
            return r.json()

        async def node_process(kill=False):
            code = (
                'import os,pathlib,json; ps=[int(p.name) for p in pathlib.Path("/proc").iterdir() if p.name.isdigit() and (p/"comm").exists() and (p/"comm").read_text().strip()=="proxy-router"];print(json.dumps(ps));'
                + (" [os.kill(p,9) for p in ps]" if kill else "")
            )
            p = await asyncio.create_subprocess_exec(
                "docker",
                "compose",
                "exec",
                "-T",
                "node",
                "python",
                "-c",
                code,
                cwd=ROOT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            o, e = await p.communicate()
            return json.loads(o or b"[]")

        t = time.monotonic()
        error = False
        done = False
        killed = []
        chunks = 0
        async with c.stream(
            "POST",
            "/v1/chat/completions",
            headers=auth,
            json={
                "model": "deepseek-v4-flash",
                "messages": [
                    {"role": "user", "content": "Write 1000 numbered sentences describing networking."}
                ],
                "stream": True,
                "max_tokens": 8192,
            },
        ) as r:
            status = r.status_code
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    chunks += 1
                    if not killed:
                        killed = await node_process(True)
                    if '"error"' in line:
                        error = True
                    if line == "data: [DONE]":
                        done = True
        health = (await c.get("/healthz")).status_code
        new = []
        state = {}
        for _ in range(40):
            state = await get("/status")
            new = await node_process()
            if new and set(new).isdisjoint(killed) and state["helper_ready"] and not state["node_error"]:
                break
            await asyncio.sleep(1)
        result = {
            "test": "native_process_sigkill_midstream",
            "passed": status == 200
            and bool(killed)
            and error
            and not done
            and bool(new)
            and set(new).isdisjoint(killed)
            and health == 200,
            "status": status,
            "chunks": chunks,
            "structured_stream_error": error,
            "done": done,
            "old_pids": killed,
            "new_pids": new,
            "health_status": health,
            "seconds": time.monotonic() - t,
        }
        results.append(result)
        print(json.dumps(result), flush=True)
        r = await c.post(
            "/v1/chat/completions",
            headers=auth,
            json={
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": "Reply exactly NATIVE_CRASH_RECOVERED."}],
                "max_tokens": 256,
            },
        )
        d = r.json()
        (OUT / "native-crash-recovery-response.json").write_text(json.dumps(d, indent=2))
        content = d.get("choices", [{}])[0].get("message", {}).get("content", "")
        result = {
            "test": "new_inference_after_native_crash",
            "passed": r.status_code == 200 and "NATIVE_CRASH_RECOVERED" in content,
            "status": r.status_code,
            "error": d.get("error"),
            "content": content,
        }
        results.append(result)
        print(json.dumps(result), flush=True)
        (OUT / "live-node-crash.json").write_text(json.dumps(results, indent=2))
        (OUT / "after-node-crash-sessions.json").write_text(json.dumps(await get("/sessions"), indent=2))


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
