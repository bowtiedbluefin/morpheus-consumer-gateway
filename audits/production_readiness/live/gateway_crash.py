import asyncio
import json
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/production-readiness"
BASE = "http://localhost:8000"


async def main():
    request_identity = "production-crash-" + uuid.uuid4().hex
    results = []
    async with httpx.AsyncClient(base_url=BASE, timeout=260, trust_env=False) as c:

        async def login():
            r = await c.post(
                "/admin/api/login",
                json={"token": (ROOT / "secrets/admin-token").read_text().strip()},
                headers={"Origin": BASE},
            )
            r.raise_for_status()
            return {"Origin": BASE, "X-CSRF-Token": r.json()["csrf"]}

        admin = await login()
        auth = {"Authorization": "Bearer " + (ROOT / "secrets/production-test-api-key").read_text().strip()}

        async def get(path):
            r = await c.get("/admin/api" + path)
            r.raise_for_status()
            return r.json()

        def record(test, passed, **kw):
            d = {"test": test, "passed": passed, **kw}
            results.append(d)
            print(json.dumps(d), flush=True)
            (OUT / "live-crash.json").write_text(json.dumps(results, indent=2))

        before = await get("/sessions")
        ids = {s["id"] for s in before["sessions"]}
        # Use a verified provider to avoid killing during a known pretransaction decline.
        p = await get("/policy")
        p["providers"] = {
            "mode": "allowlist",
            "allow": ["0x23da7e8ce578bfead351caff6b0f4e976ea99676"],
            "deny": [],
        }
        p["models"][0]["duration_seconds"] = 900
        r = await c.put("/admin/api/policy", headers=admin, json=p)
        r.raise_for_status()
        task = asyncio.create_task(
            c.post(
                "/v1/chat/completions",
                headers={**auth, "Idempotency-Key": request_identity},
                json={
                    "model": "deepseek-v4-flash",
                    "messages": [{"role": "user", "content": "Reply with CRASH_OPEN_OK."}],
                    "max_tokens": 64,
                },
            )
        )
        target = None
        journal = None
        for _ in range(200):
            rows = (await get("/sessions"))["sessions"]
            pending = [s for s in rows if s["id"] not in ids and s["state"] == "opening"]
            if pending:
                target = pending[0]
                command = [
                    "docker",
                    "compose",
                    "exec",
                    "-T",
                    "node",
                    "python",
                    "-c",
                    f'import pathlib; p=pathlib.Path("/node-data/gateway-journal/{target["id"]}.json"); print(p.read_text() if p.exists() else "{{}}")',
                ]
                proc = await asyncio.create_subprocess_exec(
                    *command, cwd=ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await proc.communicate()
                journal = json.loads(stdout or b"{}")
                if journal.get("transactions") and not journal.get("completed"):
                    break
            if task.done():
                break
            await asyncio.sleep(0.1)
        if target and journal and journal.get("transactions") and not journal.get("completed"):
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "compose",
                "kill",
                "-s",
                "SIGKILL",
                "gateway",
                cwd=ROOT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            await asyncio.gather(task, return_exceptions=True)
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "compose",
                "up",
                "-d",
                "--no-deps",
                "gateway",
                cwd=ROOT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            for _ in range(100):
                try:
                    if (await c.get("/healthz")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
            admin = await login()
            states = []
            after = None
            for _ in range(120):
                after = await get("/sessions")
                row = next(s for s in after["sessions"] if s["id"] == target["id"])
                states.append(row["state"])
                if row["state"] in ("open", "failed", "closed"):
                    break
                await asyncio.sleep(1)
            created = [s for s in after["sessions"] if s["id"] not in ids and s.get("chain_id")]
            replay = await c.post(
                "/v1/chat/completions",
                headers={**auth, "Idempotency-Key": request_identity},
                json={
                    "model": "deepseek-v4-flash",
                    "messages": [{"role": "user", "content": "Reply with CRASH_OPEN_OK."}],
                    "max_tokens": 64,
                },
            )
            record(
                "gateway_sigkill_during_open_recovers_one_escrow",
                row["state"] == "open" and len(created) == 1 and replay.status_code == 409,
                states=states,
                created=created,
                journal_before_kill=journal,
                duplicate_request_status=replay.status_code,
            )
        else:
            await asyncio.gather(task, return_exceptions=True)
            record(
                "gateway_sigkill_during_open_recovers_one_escrow",
                False,
                reason="Did not catch an in-flight journaled transaction; no kill was performed",
                target=target,
                journal=journal,
            )
        r = await c.post(
            "/v1/chat/completions",
            headers=auth,
            json={
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": "Reply with exactly AFTER_CRASH_OK."}],
                "max_tokens": 256,
            },
        )
        d = r.json()
        (OUT / "after-crash-response.json").write_text(json.dumps(d, indent=2))
        content = d.get("choices", [{}])[0].get("message", {}).get("content", "")
        record(
            "inference_after_gateway_crash",
            r.status_code == 200 and "AFTER_CRASH_OK" in content,
            status=r.status_code,
            content=content,
        )
        (OUT / "after-crash-sessions.json").write_text(json.dumps(await get("/sessions"), indent=2))
        (OUT / "after-crash-status.json").write_text(json.dumps(await get("/status"), indent=2))


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
