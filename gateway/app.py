import asyncio
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from .config import Config
from .models import KeyCreate, Policy, Restart, StrictModel
from .node import GatewayError, Helper, Node
from .sessions import Sessions
from .store import Store


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Login(StrictModel):
    token: str


class BindSession(StrictModel):
    session_id: str


def create_app(cfg: Config | None = None, node=None, helper=None):
    cfg = cfg or Config.from_env()
    store = Store(cfg.data_dir)
    node = node or Node(cfg)
    helper = helper or Helper(cfg.helper_socket)
    sessions = Sessions(store, node, helper, cfg)
    key_active = defaultdict(int)
    rates = defaultdict(deque)
    logins = deque()

    @asynccontextmanager
    async def lifespan(app):
        await sessions.recover()
        worker = asyncio.create_task(sessions.run())
        try:
            yield
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            if sessions.tasks:
                # Operation state is durable if shutdown must interrupt it.
                for task in list(sessions.tasks):
                    task.cancel()
                await asyncio.gather(*sessions.tasks, return_exceptions=True)
            await node.aclose()
            await helper.aclose()
            store.close()

    app = FastAPI(
        title="Morpheus Consumer Gateway",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store, app.state.sessions = store, sessions

    @app.exception_handler(GatewayError)
    async def gateway_error(request, exc):
        return JSONResponse(
            {"error": {"message": exc.message, "type": "gateway_error", "code": exc.code}},
            status_code=exc.status,
            headers={"Retry-After": "5"} if exc.status == 429 else {},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not echo invalid input, which could include credentials or prompts.
        fields = [".".join(str(p) for p in e["loc"]) + ": " + e["msg"] for e in exc.errors()]
        return JSONResponse(
            {"error": {"message": "; ".join(fields), "code": "validation_error"}}, status_code=422
        )

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        # Enforce actual bytes, including chunked bodies (Content-Length alone is insufficient).
        if request.method in ("POST", "PUT", "PATCH"):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    return JSONResponse({"error": {"code": "payload_too_large"}}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        if request.url.path.startswith(("/admin/", "/v1/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    def check_origin(request):
        if request.headers.get("origin") != cfg.public_origin:
            raise GatewayError(
                "invalid_origin", "Administrative changes require the configured browser origin", 403
            )

    async def admin(request: Request):
        token = request.cookies.get("mg_admin", "")
        auth = store.get("admin", digest(token)) if token else None
        if not auth or auth["expires"] < time.time():
            raise GatewayError("unauthorized", "Sign in to administer this installation", 401)
        if request.method not in ("GET", "HEAD"):
            check_origin(request)
            if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), auth["csrf"]):
                raise GatewayError("csrf", "Invalid browser session token", 403)
        return auth

    async def api_key(request: Request):
        value = request.headers.get("authorization", "")
        if not value.startswith("Bearer "):
            raise GatewayError("invalid_api_key", "Supply an application Bearer API key", 401)
        key = store.get("key", digest(value[7:]))
        if not key or not key["enabled"]:
            raise GatewayError("invalid_api_key", "API key is invalid or revoked", 401)
        return key

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.post("/admin/api/login")
    async def login(body: Login, request: Request):
        check_origin(request)
        while logins and logins[0] < time.time() - 60:
            logins.popleft()
        if len(logins) >= 10:
            raise GatewayError("login_rate_limit", "Too many sign-in attempts; wait one minute", 429)
        logins.append(time.time())
        if not hmac.compare_digest(digest(body.token), digest(cfg.admin_token)):
            raise GatewayError("unauthorized", "Invalid administrator secret", 401)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        for old in store.all("admin"):
            if old["expires"] < time.time():
                store.delete("admin", old["id"])
        record = {"id": digest(token), "csrf": csrf, "expires": time.time() + 12 * 3600}
        store.put("admin", record["id"], record)
        response = JSONResponse({"csrf": csrf})
        response.set_cookie(
            "mg_admin",
            token,
            httponly=True,
            secure=cfg.secure_cookie,
            samesite="strict",
            max_age=12 * 3600,
            path="/admin",
        )
        return response

    @app.get("/admin/api/me", dependencies=[Depends(admin)])
    async def me(auth=Depends(admin)):
        return {"csrf": auth["csrf"]}

    @app.post("/admin/api/logout", dependencies=[Depends(admin)])
    async def logout(request: Request):
        store.delete("admin", digest(request.cookies.get("mg_admin", "")))
        res = JSONResponse({"ok": True})
        res.delete_cookie("mg_admin", path="/admin")
        return res

    @app.get("/admin/api/status", dependencies=[Depends(admin)])
    async def status():
        identity, balances, held, error = None, None, None, None
        try:
            identity = await sessions.verify_identity()
            balances = await node.request("GET", "/blockchain/balance")
            held = await node.request("GET", "/blockchain/stakes/onhold")
        except GatewayError as exc:
            error = exc.message
        policy = store.policy()
        applied = store.get("settings", "applied_rating")
        return {
            "demo": cfg.demo,
            "identity": identity,
            "balances": balances,
            "held": held,
            "node_error": error,
            "last_error": sessions.last_error,
            "active_requests": len(sessions.active),
            "queued": sessions.waiting,
            "maintenance": sessions.maintenance,
            "paused": policy.paused,
            "helper_connected": helper.configured,
            "base_url": cfg.public_origin + "/v1",
            "rating_pending": not applied or applied["rating"] != policy.rating(),
            "applied_rating": applied,
        }

    @app.get("/admin/api/policy", dependencies=[Depends(admin)])
    async def get_policy():
        return store.policy()

    @app.put("/admin/api/policy", dependencies=[Depends(admin)])
    async def put_policy(policy: Policy):
        if policy.revision != store.policy().revision:
            raise GatewayError(
                "revision_conflict", "Settings changed in another tab; reload before saving", 409
            )
        policy.revision += 1
        store.put("settings", "policy", policy.model_dump())
        store.event("policy_saved", revision=policy.revision)
        return policy

    @app.get("/admin/api/catalog", dependencies=[Depends(admin)])
    async def catalog():
        return {"models": await node.catalog()}

    @app.get("/admin/api/models/{model_id}/bids", dependencies=[Depends(admin)])
    async def bids(model_id: str):
        sessions.model(model_id)
        return {"bids": await sessions.candidates(model_id)}

    @app.post("/admin/api/models/{model_id}/prewarm", dependencies=[Depends(admin)])
    async def prewarm(model_id: str):
        return await sessions.acquire(model_id, prewarm=True)

    @app.get("/admin/api/keys", dependencies=[Depends(admin)])
    async def keys():
        return {"keys": store.all("key")}

    @app.post("/admin/api/keys", dependencies=[Depends(admin)])
    async def create_key(body: KeyCreate):
        known = {m.id for m in store.policy().models}
        if any(id not in known for id in body.models):
            raise GatewayError("unknown_model", "Key restrictions must reference configured model IDs", 400)
        token = "mg_" + secrets.token_urlsafe(32)
        record = {
            "id": secrets.token_hex(12),
            **body.model_dump(),
            "prefix": token[:12],
            "enabled": True,
            "created_at": time.time(),
            "last_used": None,
        }
        store.put("key", digest(token), record)
        store.event("key_created", key=record["id"])
        return {"key": token, "record": record}

    @app.delete("/admin/api/keys/{id}", dependencies=[Depends(admin)])
    async def revoke(id: str):
        for dbid, data in store.db.execute("SELECT id,data FROM records WHERE kind='key'").fetchall():
            record = json.loads(data)
            if record["id"] == id:
                record["enabled"] = False
                store.put("key", dbid, record)
                store.event("key_revoked", key=id)
                return {"ok": True}
        raise GatewayError("not_found", "API key not found", 404)

    @app.get("/admin/api/sessions", dependencies=[Depends(admin)])
    async def list_sessions():
        return {"sessions": [{**s, "busy": s["id"] in sessions.active} for s in store.all("session")]}

    @app.get("/admin/api/wallet/sessions", dependencies=[Depends(admin)])
    async def wallet_sessions():
        identity = await sessions.verify_identity()
        return await node.wallet_sessions(identity["wallet"])

    @app.post("/admin/api/sessions/{id}/close", dependencies=[Depends(admin)])
    async def close_session(id: str):
        return await sessions.close(id)

    @app.post("/admin/api/sessions/{id}/bind", dependencies=[Depends(admin)])
    async def bind(id: str, body: BindSession):
        return await sessions.bind(id, body.session_id)

    @app.post("/admin/api/node/restart", dependencies=[Depends(admin)])
    async def restart(body: Restart):
        return sessions.restart(body.immediate, body.apply_rating)

    @app.get("/admin/api/operations", dependencies=[Depends(admin)])
    async def operations():
        return {"operations": store.all("operation")[:30], "events": store.all("event")[:60]}

    @app.get("/v1/models")
    async def models(key=Depends(api_key)):
        return {
            "object": "list",
            "data": [
                {"id": m.alias, "object": "model", "created": 0, "owned_by": "consumer"}
                for m in store.policy().models
                if m.enabled and (not key["models"] or m.id in key["models"])
            ],
        }

    @app.post("/v1/chat/completions")
    async def completion(request: Request, key=Depends(api_key)):
        try:
            body = await request.json()
        except ValueError:
            raise GatewayError("invalid_request", "Request must be JSON", 400) from None
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("model"), str)
            or not isinstance(body.get("messages"), list)
            or not body["messages"]
            or not isinstance(body.get("stream", False), bool)
        ):
            raise GatewayError(
                "invalid_request", "Supply model, nonempty messages and an optional boolean stream", 400
            )
        if any(k in body for k in ("session_id", "model_id", "chat_id", "provider_url", "node_url")):
            raise GatewayError("invalid_request", "Internal routing fields are not accepted", 400)
        model = store.policy().resolve(body["model"])
        if not model:
            raise GatewayError("model_not_found", "Unknown or disabled model; use /v1/models", 404)
        if key["models"] and model.id not in key["models"]:
            raise GatewayError("model_forbidden", "This API key cannot use that model", 403)
        bucket = rates[key["id"]]
        while bucket and bucket[0] < time.monotonic() - 60:
            bucket.popleft()
        if len(bucket) >= key["requests_per_minute"] or key_active[key["id"]] >= key["concurrency"]:
            raise GatewayError("rate_limit", "API key rate or concurrency limit reached", 429)
        bucket.append(time.monotonic())
        key_active[key["id"]] += 1
        row, upstream, cleaned = None, None, False
        request_id = secrets.token_hex(16)
        started = time.monotonic()

        async def cleanup():
            nonlocal cleaned
            if cleaned:
                return
            cleaned = True
            try:
                if upstream is not None:
                    await upstream.aclose()
            finally:
                if row:
                    sessions.release(row)
                key_active[key["id"]] -= 1
                store.event(
                    "request_finished",
                    request=request_id,
                    model=model.id,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )

        try:
            row = await sessions.acquire(model.id)
            # Recheck revocation after waiting for a cold open or a busy pool.
            fresh_key = store.get("key", digest(request.headers["authorization"][7:]))
            if not fresh_key or not fresh_key["enabled"]:
                raise GatewayError("invalid_api_key", "API key was revoked while waiting", 401)
            current = store.policy()
            if (
                current.paused
                or sessions.maintenance
                or not current.resolve(model.id)
                or not current.providers.permits(row["provider"])
            ):
                raise GatewayError("policy_changed", "Admission policy changed while waiting; retry later")
            fresh_key["last_used"] = time.time()
            store.put("key", digest(request.headers["authorization"][7:]), fresh_key)
            inference_deadline = time.monotonic() + cfg.request_timeout
            upstream = await asyncio.wait_for(
                node.completion(row["chain_id"], model.id, body, request_id), cfg.request_timeout
            )
            if upstream.is_error:
                raise GatewayError(
                    "provider_error",
                    f"Inference returned HTTP {upstream.status_code}",
                    429 if upstream.status_code == 429 else 502,
                )
            if body.get("stream"):

                async def stream():
                    try:
                        async with asyncio.timeout(max(0, inference_deadline - time.monotonic())):
                            done = False
                            async for line in upstream.aiter_lines():
                                if len(line) > 2 * 1024 * 1024:
                                    raise ValueError("Oversized event")
                                if not line.startswith("data:"):
                                    continue
                                value = line[5:].strip()
                                if value == "[DONE]":
                                    yield b"data: [DONE]\n\n"
                                    done = True
                                    break
                                parsed = json.loads(value)
                                if not isinstance(parsed, dict) or "error" in parsed:
                                    raise ValueError("Invalid or error event")
                                yield ("data: " + value + "\n\n").encode()
                            if not done:
                                raise ValueError("Stream ended before DONE")
                    except (httpx.HTTPError, ValueError, TimeoutError):
                        yield b'data: {"error":{"code":"stream_interrupted","message":"Provider stream interrupted; no automatic replay was attempted"}}\n\n'
                    finally:
                        await asyncio.shield(cleanup())

                return StreamingResponse(
                    stream(),
                    media_type="text/event-stream",
                    headers={"X-Request-ID": request_id, "X-Accel-Buffering": "no"},
                    background=BackgroundTask(cleanup),
                )
            async with asyncio.timeout(max(0, inference_deadline - time.monotonic())):
                await upstream.aread()
            try:
                result = upstream.json()
                if not isinstance(result, dict) or "error" in result:
                    raise ValueError()
            except ValueError:
                raise GatewayError(
                    "provider_protocol", "Provider returned an invalid completion", 502
                ) from None
            await cleanup()
            return JSONResponse(result, headers={"X-Request-ID": request_id})
        except TimeoutError:
            await asyncio.shield(cleanup())
            raise GatewayError("inference_timeout", "Inference exceeded the request deadline", 504) from None
        except httpx.HTTPError:
            await asyncio.shield(cleanup())
            raise GatewayError("inference_transport", "Provider response was interrupted", 502) from None
        except BaseException:
            await asyncio.shield(cleanup())
            raise

    if cfg.static_dir.is_dir():
        assets = cfg.static_dir / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/")
        @app.get("/admin")
        async def index():
            return FileResponse(cfg.static_dir / "index.html", headers={"Cache-Control": "no-cache"})

    return app
