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
    async with httpx.AsyncClient(base_url=BASE, timeout=180, trust_env=False) as c:
        r = await c.post(
            "/admin/api/login",
            json={"token": (ROOT / "secrets/admin-token").read_text().strip()},
            headers={"Origin": BASE},
        )
        r.raise_for_status()
        h = {"Origin": BASE, "X-CSRF-Token": r.json()["csrf"]}
        p = (await c.get("/admin/api/policy")).json()
        assert p["paused"]
        s = (await c.get("/admin/api/status")).json()
        assert s["active_requests"] == 0 and s["live_sessions"] == 0
        # Stop gateway during the external opening to preserve one wallet mutation owner.
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "compose",
            "stop",
            "gateway",
            cwd=ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        op = uuid.uuid4().hex
        script = f"""import json,urllib.request,urllib.error,base64,pathlib
secret=pathlib.Path('/run/secrets/node_password').read_text().strip();auth='Basic '+base64.b64encode(('admin:'+secret).encode()).decode()
req=urllib.request.Request('http://127.0.0.1:8082/blockchain/bids/0xf3ba65dfc17bdb4cd199ce1b76c7267ae2cdd3f8491eb035f407aabc9b0e96ce/session',data=json.dumps({{'sessionDuration':301,'maxStakeWei':'3000000000000000000'}}).encode(),headers={{'Authorization':auth,'Content-Type':'application/json','X-Gateway-Operation':'{op}'}})
try:
 with urllib.request.urlopen(req,timeout=180) as r:status=r.status;data=json.load(r)
except urllib.error.HTTPError as e:status=e.code;data=json.load(e)
print(json.dumps({{'operation':'{op}','status':status,'body':data}}))
"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "compose",
                "exec",
                "-T",
                "node",
                "python",
                "-",
                cwd=ROOT,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate(script.encode())
            opened = json.loads(stdout)
            (OUT / "orphan-native-open.json").write_text(json.dumps(opened, indent=2))
            print(json.dumps(opened), flush=True)
        finally:
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
        sid = opened["body"].get("sessionID")
        assert opened["status"] == 200 and sid and int(sid, 16)
        for _ in range(30):
            try:
                if (await c.get("/healthz")).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        r = await c.post(
            "/admin/api/login",
            json={"token": (ROOT / "secrets/admin-token").read_text().strip()},
            headers={"Origin": BASE},
        )
        r.raise_for_status()
        h = {"Origin": BASE, "X-CSRF-Token": r.json()["csrf"]}
        await c.post("/admin/api/recovery/run", headers=h)
        await asyncio.sleep(3)
        rows = (await c.get("/admin/api/sessions")).json()["sessions"]
        untouched = not any(r.get("chain_id") == sid for r in rows)
        result = {
            "test": "expired_untracked_wallet_session_discovered_and_closed",
            "chain_id": sid,
            "live_orphan_left_untouched": untouched,
            "samples": [],
        }
        deadline = time.time() + 420
        while time.time() < deadline:
            rows = (await c.get("/admin/api/sessions")).json()["sessions"]
            row = next((r for r in rows if r.get("chain_id") == sid), None)
            result["samples"].append({"at": time.time(), "row": row})
            (OUT / "live-orphan-recovery.json").write_text(json.dumps(result, indent=2))
            if row and row["state"] == "closed":
                result["passed"] = untouched
                result["final_row"] = row
                (OUT / "live-orphan-recovery.json").write_text(json.dumps(result, indent=2))
                print(json.dumps({k: v for k, v in result.items() if k != "samples"}), flush=True)
                return
            await asyncio.sleep(10)
        result.update(passed=False, reason="Orphan did not reach closed state before deadline")
        (OUT / "live-orphan-recovery.json").write_text(json.dumps(result, indent=2))


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
