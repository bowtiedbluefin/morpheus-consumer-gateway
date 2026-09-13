import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/production-readiness"
MODEL = "0xc2c4b037ff12e0aa81178deac52aeed902b36189b9e6feae22b72324c9221130"
PROVIDER = "0xbe7462798fc20affa0982d27af1eebef9c270611"
BASE = "http://localhost:8000"


class Campaign:
    def __init__(self):
        self.results = []

    def record(self, name, passed, **detail):
        row = {"test": name, "passed": bool(passed), "at": time.time(), **detail}
        self.results.append(row)
        (OUT / "live-core.json").write_text(json.dumps(self.results, indent=2))
        print(json.dumps(row), flush=True)

    async def run(self):
        async with httpx.AsyncClient(base_url=BASE, timeout=260, trust_env=False) as c:
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

            baseline = await get("/status")
            policy = await get("/policy")
            (OUT / "baseline.json").write_text(json.dumps({"status": baseline, "policy": policy}, indent=2))
            keypath = ROOT / "secrets/production-test-api-key"
            if keypath.exists():
                token = keypath.read_text().strip()
            else:
                r = await c.post(
                    "/admin/api/keys",
                    headers={**admin, "Idempotency-Key": f"production-readiness-key-{uuid.uuid4().hex}"},
                    json={
                        "name": "Production readiness campaign",
                        "models": [MODEL],
                        "concurrency": 16,
                        "requests_per_minute": 600,
                    },
                )
                r.raise_for_status()
                token = r.json()["key"]
                fd = os.open(keypath, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(token + "\n")
            auth = {"Authorization": "Bearer " + token}
            body = {
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": "Reply only with READY."}],
                "max_tokens": 32,
            }
            before = await get("/sessions")
            for label, model, expected in [
                ("misspelled", "deepseek-v4-flahs", 404),
                ("unknown_id", "0x" + "a" * 64, 404),
                ("blank", "", 400),
                ("wrong_type", 42, 400),
                ("whitespace", " deepseek-v4-flash", 404),
                ("homoglyph", "deepseek-v4-flаsh", 404),
            ]:
                t = time.monotonic()
                r = await c.post("/v1/chat/completions", headers=auth, json={**body, "model": model})
                self.record(
                    "model_" + label,
                    r.status_code == expected,
                    status=r.status_code,
                    elapsed_ms=round((time.monotonic() - t) * 1000),
                    error=r.json().get("error"),
                )
            for label, patch in [
                ("messages_scalar", {"messages": [42]}),
                ("missing_content", {"messages": [{"role": "user"}]}),
                ("bad_role", {"messages": [{"role": "alien", "content": "hi"}]}),
                ("negative_tokens", {"max_tokens": -1}),
                ("routing_injection", {"session_id": "evil"}),
                ("stream_string", {"stream": "true"}),
            ]:
                r = await c.post("/v1/chat/completions", headers=auth, json={**body, **patch})
                self.record("invalid_" + label, r.status_code == 400, status=r.status_code)
            after = await get("/sessions")
            self.record(
                "invalid_requests_no_session_mutations",
                before == after,
                rows_before=len(before["sessions"]),
                rows_after=len(after["sessions"]),
            )
            # Full HTTP checks without an administrator cookie.
            async with httpx.AsyncClient(base_url=BASE, timeout=15, trust_env=False) as outsider:
                for path, headers, expected in [
                    ("/admin/api/policy", auth, 401),
                    ("/v1/models", {}, 401),
                    ("/v1/models", {"Authorization": "Bearer invalid"}, 401),
                ]:
                    r = await outsider.get(path, headers=headers)
                    self.record(
                        "auth_" + path + "_" + str(len(headers)),
                        r.status_code == expected,
                        status=r.status_code,
                    )
            r = await c.post(
                "/admin/api/node/restart",
                json={"immediate": False, "apply_rating": False},
                headers={"Origin": "https://untrusted.invalid"},
            )
            self.record("csrf_reject_restart", r.status_code == 403, status=r.status_code)
            policy["max_sessions"] = 4
            policy["models"][0].update(max_sessions=4, duration_seconds=600, retention="until_expiry")
            policy["providers"] = {"mode": "allowlist", "allow": [PROVIDER], "deny": []}
            policy["budget"]["max_session_stake_wei"] = str(3 * 10**18)
            policy["budget"]["max_total_stake_wei"] = str(15 * 10**18)
            r = await c.put("/admin/api/policy", headers=admin, json=policy)
            r.raise_for_status()

            # Long enough responses to keep several independently opened sessions busy.
            async def prompt(i):
                started = time.monotonic()
                r = await c.post(
                    "/v1/chat/completions",
                    headers={**auth, "Idempotency-Key": f"production-concurrent-{uuid.uuid4().hex}"},
                    json={
                        "model": "deepseek-v4-flash",
                        "messages": [
                            {
                                "role": "user",
                                "content": f"Begin your final answer with TEST_{i}. Then write 200 numbered, detailed statements about distributed database design. Do not stop early.",
                            }
                        ],
                        "max_tokens": 2048,
                    },
                )
                d = r.json()
                choices = d.get("choices", [])
                text = choices[0].get("message", {}).get("content", "") if choices else ""
                result = {
                    "index": i,
                    "status": r.status_code,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "request_id": r.headers.get("x-request-id"),
                    "correct_marker": f"TEST_{i}" in text,
                    "response_characters": len(text),
                    "error": d.get("error"),
                }
                return result

            jobs = [asyncio.create_task(prompt(i)) for i in range(4)]
            samples = []
            while not all(j.done() for j in jobs):
                s = await get("/status")
                samples.append(
                    {
                        "at": time.time(),
                        "active": s["active_requests"],
                        "queued": s["queued"],
                        "live_sessions": s["live_sessions"],
                    }
                )
                await asyncio.sleep(0.5)
            results = await asyncio.gather(*jobs)
            (OUT / "concurrency-samples.json").write_text(json.dumps(samples, indent=2))
            peak = max(s["active"] for s in samples)
            self.record(
                "four_live_concurrent_sessions",
                all(x["status"] == 200 and x["correct_marker"] for x in results) and peak == 4,
                peak_active=peak,
                responses=results,
            )
            sessions = await get("/sessions")
            (OUT / "after-concurrency-sessions.json").write_text(json.dumps(sessions, indent=2))
            live = [s for s in sessions["sessions"] if s["state"] not in ["closed", "failed"]]
            self.record(
                "live_session_cap", len(live) <= 4, count=len(live), chain_ids=[s["chain_id"] for s in live]
            )
            # Genuine SSE iteration: record timing for first event and clean completion.
            started = time.monotonic()
            first = None
            done = False
            chunks = 0
            output = ""
            async with c.stream(
                "POST",
                "/v1/chat/completions",
                headers={**auth, "Idempotency-Key": f"production-stream-{uuid.uuid4().hex}"},
                json={
                    **body,
                    "stream": True,
                    "messages": [{"role": "user", "content": "Reply only with STREAM_OK."}],
                    "max_tokens": 64,
                },
            ) as r:
                status = r.status_code
                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        if first is None:
                            first = time.monotonic() - started
                        if line == "data: [DONE]":
                            done = True
                            continue
                        d = json.loads(line[6:])
                        chunks += 1
                        for choice in d.get("choices", []):
                            output += choice.get("delta", {}).get("content") or ""
            self.record(
                "live_sse",
                status == 200 and done and "STREAM_OK" in output,
                status=status,
                ttft_seconds=first,
                chunks=chunks,
                done=done,
                output=output,
            )
            # Duplicate application request must not replay or open another session.
            idem = {**auth, "Idempotency-Key": f"production-idempotency-{uuid.uuid4().hex}"}
            a = await c.post("/v1/chat/completions", headers=idem, json=body)
            b = await c.post("/v1/chat/completions", headers=idem, json=body)
            self.record(
                "live_idempotency",
                a.status_code == 200 and b.status_code == 409,
                statuses=[a.status_code, b.status_code],
            )
            final = await get("/status")
            (OUT / "after-core-status.json").write_text(json.dumps(final, indent=2))
            self.record(
                "live_core_wallet_accounting",
                int(final["balances"]["eth"]) > int(policy["budget"]["min_eth_wei"]),
                balances=final["balances"],
                held=final["held"],
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
    asyncio.run(Campaign().run())
