import asyncio
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.background import BackgroundTask

from .config import Config
from .models import KeyCreate, Policy, Restart, StrictModel
from .node import GatewayError, Helper, Node
from .sessions import Sessions
from .store import Store
from .transport import ChatRequest, bounded_body, chat_events, strict_json, validate_completion


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
    helper = helper or Helper(cfg.helper_socket, url=cfg.helper_url, token=cfg.helper_token)
    sessions = Sessions(store, node, helper, cfg)
    key_active = defaultdict(int)
    rates = defaultdict(deque)
    logins = defaultdict(deque)
    connections = 0

    @asynccontextmanager
    async def lifespan(app):
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
        version="0.2.0",
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

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, exc):
        sessions.ready = False
        store.error = "Storage unavailable; check free disk space and restore instructions"
        return JSONResponse(
            {"error": {"message": store.error, "code": "storage_unavailable"}}, status_code=503
        )

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        nonlocal connections
        request.state.request_id = secrets.token_hex(16)

        def decorate(response):
            response.headers["X-Request-ID"] = request.state.request_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'"
            )
            response.headers["Cache-Control"] = "no-store"
            return response

        if connections >= cfg.max_connections:
            return decorate(
                JSONResponse(
                    {"error": {"code": "connection_limit", "message": "Gateway is busy"}}, status_code=429
                )
            )
        connections += 1
        try:
            if request.method in ("POST", "PUT", "PATCH"):
                body = bytearray()
                try:
                    async with asyncio.timeout(cfg.body_timeout):
                        async for chunk in request.stream():
                            if len(body) + len(chunk) > 2 * 1024 * 1024:
                                return decorate(
                                    JSONResponse({"error": {"code": "payload_too_large"}}, status_code=413)
                                )
                            body.extend(chunk)
                except TimeoutError:
                    return decorate(
                        JSONResponse(
                            {"error": {"code": "upload_timeout", "message": "Request upload timed out"}},
                            status_code=408,
                        )
                    )
                request._body = bytes(body)
            response = await call_next(request)
            return decorate(response)
        finally:
            connections -= 1

    def auth_epoch():
        return digest(cfg.admin_token + str(store.get("settings", "auth_generation")))

    def check_origin(request):
        if request.headers.get("origin") != cfg.public_origin:
            raise GatewayError(
                "invalid_origin", "Administrative changes require the configured browser origin", 403
            )

    async def admin(request: Request):
        token = request.cookies.get("mg_admin", "")
        auth = store.get("admin", digest(token)) if token else None
        if not auth or auth["expires"] < time.time() or auth.get("epoch") != auth_epoch():
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
        source = request.client.host if request.client else "unknown"
        for old in list(logins):
            if not logins[old] or logins[old][-1] < time.time() - 60:
                del logins[old]
        bucket = logins[source]
        while bucket and bucket[0] < time.time() - 60:
            bucket.popleft()
        # A known correct secret can always recover access; failed callers cannot lock it out.
        if not hmac.compare_digest(digest(body.token), digest(cfg.admin_token)):
            if len(bucket) >= 10 or len(logins) > 10000:
                raise GatewayError(
                    "login_rate_limit", "Too many invalid sign-in attempts; wait one minute", 429
                )
            bucket.append(time.time())
            raise GatewayError("unauthorized", "Invalid administrator secret", 401)
        logins.pop(source, None)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        for old in store.all("admin"):
            if old["expires"] < time.time():
                store.delete("admin", old["id"])
        record = {
            "id": digest(token),
            "csrf": csrf,
            "expires": time.time() + 12 * 3600,
            "epoch": auth_epoch(),
        }
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

    @app.post("/admin/api/logout")
    async def logout(request: Request):
        check_origin(request)
        store.delete("admin", digest(request.cookies.get("mg_admin", "")))
        res = JSONResponse({"ok": True})
        res.delete_cookie("mg_admin", path="/admin")
        return res

    @app.post("/admin/api/logout-all", dependencies=[Depends(admin)])
    async def logout_all():
        store.put("settings", "auth_generation", {"value": secrets.token_hex(16)})
        return {"ok": True}

    @app.get("/readyz")
    async def readiness():
        management_ready = True
        if cfg.helper_url:
            try:
                state = await helper.request("GET", "/status", timeout=min(cfg.control_timeout, 3))
                management_ready = state.get("ready") is True
            except GatewayError:
                management_ready = False
        ready = sessions.ready and management_ready and not sessions.maintenance and not store.error
        return JSONResponse(
            {"ready": bool(ready)},
            status_code=200 if ready else 503,
        )

    @app.get("/admin/api/status", dependencies=[Depends(admin)])
    async def status():
        calls = (
            sessions.verify_identity(),
            node.balances(),
            node.stakes(),
            helper.request("GET", "/status", timeout=min(cfg.control_timeout, 3)),
        )
        results = await asyncio.gather(*calls, return_exceptions=True)
        identity, balances, held, helper_state = [
            None if isinstance(r, BaseException) else r for r in results
        ]
        errors = [
            r.message if isinstance(r, GatewayError) else "Node status unavailable"
            for r in results[:3]
            if isinstance(r, BaseException)
        ]
        sessions.ready = not errors
        policy = store.policy()
        effective = helper_state.get("rating") if helper_state else None
        return {
            "demo": cfg.demo,
            "identity": identity,
            "balances": balances,
            "held": held,
            "node_error": "; ".join(errors) or None,
            "last_error": store.error
            or sessions.last_error
            or (results[3].message if cfg.helper_url and isinstance(results[3], GatewayError) else None),
            "active_requests": len(sessions.active),
            "queued": sessions.waiting,
            "maintenance": sessions.maintenance,
            "paused": policy.paused,
            "helper_connected": helper_state is not None,
            "helper_ready": helper_state.get("ready", False) if helper_state else False,
            "base_url": cfg.public_origin + "/v1",
            "rating_pending": effective != policy.rating(),
            "effective_rating": effective,
            "updated_at": time.time(),
            "policy_revision": policy.revision,
            "live_sessions": len(store.sessions()),
            "recovery": store.get("settings", "recovery_status"),
            "wallet_scan": store.get("settings", "wallet_scan"),
            "treasury": store.get("settings", "treasury"),
            "timeouts": {
                "queue": policy.queue_seconds,
                "opening": cfg.acquisition_timeout,
                "inference": cfg.request_timeout,
            },
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
        sessions.rebalance()
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

    @app.get("/admin/api/models/{model_id}/quote", dependencies=[Depends(admin)])
    async def quote(model_id: str):
        return await sessions.quote(model_id)

    @app.post("/admin/api/recovery/run", dependencies=[Depends(admin)])
    async def recovery_run():
        if sessions.sweep_task and not sessions.sweep_task.done():
            return {"state": "running"}
        sessions.sweep_task = sessions.spawn(sessions.sweep())
        return {"state": "scheduled"}

    @app.post("/admin/api/wallet/withdraw", dependencies=[Depends(admin)])
    async def withdraw():
        if sessions.sweep_task and not sessions.sweep_task.done():
            raise GatewayError("recovery_running", "Wallet recovery is already running", 409)
        sessions.sweep_task = sessions.spawn(sessions.treasury(manual=True))
        return {"state": "scheduled"}

    @app.get("/admin/api/keys", dependencies=[Depends(admin)])
    async def keys():
        return {"keys": store.all("key")}

    @app.post("/admin/api/keys", dependencies=[Depends(admin)])
    async def create_key(body: KeyCreate, request: Request):
        idem = request.headers.get("idempotency-key")
        if idem:
            marker = digest("create-key:" + idem)
            if store.get("request", marker):
                raise GatewayError(
                    "key_already_created",
                    "Key creation was already attempted; revoke the listed key if its value was lost",
                    409,
                )
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
        if idem:
            store.put(
                "request", marker, {"state": "completed", "created_at": time.time(), "key_id": record["id"]}
            )
        store.put("key", digest(token), record)
        store.event("key_created", key=record["id"])
        return {"key": token, "record": record}

    @app.put("/admin/api/keys/{id}", dependencies=[Depends(admin)])
    async def edit_key(id: str, body: KeyCreate):
        if any(mid not in {m.id for m in store.policy().models} for mid in body.models):
            raise GatewayError("unknown_model", "Key scopes must use saved models", 400)
        for dbid, raw in store.db.execute("SELECT id,data FROM records WHERE kind='key'").fetchall():
            record = json.loads(raw)
            if record["id"] == id:
                record.update(body.model_dump())
                store.put("key", dbid, record)
                return record
        raise GatewayError("not_found", "Key not found", 404)

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
    async def list_sessions(offset: int = 0):
        if not 0 <= offset <= 1000000:
            raise GatewayError("invalid_page", "Invalid page", 400)
        rows = store.page("session", 100, offset)
        return {
            "sessions": [{**s, "busy": s["id"] in sessions.active} for s in rows],
            "next_offset": offset + 100 if len(rows) == 100 else None,
        }

    @app.get("/admin/api/wallet/sessions", dependencies=[Depends(admin)])
    async def wallet_sessions(offset: int = 0):
        if not 0 <= offset <= 1000000:
            raise GatewayError("invalid_page", "Invalid page", 400)
        identity = await sessions.verify_identity()
        return await node.wallet_sessions(identity["wallet"], offset=offset)

    @app.post("/admin/api/sessions/{id}/close", dependencies=[Depends(admin)])
    async def close_session(id: str, stop_maintaining: bool = False):
        return await sessions.close(id, stop_maintaining=stop_maintaining)

    @app.post("/admin/api/sessions/{id}/bind", dependencies=[Depends(admin)])
    async def bind(id: str, body: BindSession):
        return await sessions.bind(id, body.session_id)

    @app.post("/admin/api/node/restart", dependencies=[Depends(admin)])
    async def restart(body: Restart):
        return sessions.restart(body.immediate, body.apply_rating)

    @app.get("/admin/api/events", dependencies=[Depends(admin)])
    async def events():
        return {"events": store.page("event", 100)}

    @app.get("/admin/api/operations", dependencies=[Depends(admin)])
    async def operations():
        return {"operations": store.page("operation", 50), "events": store.page("event", 60)}

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
            body = strict_json(await request.body())
        except (ValueError, UnicodeError, RecursionError):
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
        try:
            parsed = ChatRequest.model_validate(body)
        except ValidationError:
            raise GatewayError(
                "invalid_request",
                "Invalid chat messages or options; check roles, content and token limits",
                400,
            ) from None
        body = parsed.model_dump(exclude_none=True, exclude_unset=True)
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
        request_id = request.state.request_id
        outcome = "failed"
        failure_code = None
        idem_record = None
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
                try:
                    if row:
                        sessions.release(row)
                finally:
                    key_active[key["id"]] -= 1
                if idem_record:
                    store.put(
                        "request",
                        idem_record,
                        {
                            "state": "completed",
                            "created_at": time.time(),
                            "request_id": request_id,
                            "outcome": outcome,
                        },
                    )
                store.event(
                    "request_finished",
                    request=request_id,
                    model=model.id,
                    key=key["id"],
                    session=row["id"] if row else None,
                    provider=row["provider"] if row else None,
                    outcome=outcome,
                    code=failure_code,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )

        try:
            idem = request.headers.get("idempotency-key")
            if idem:
                if len(idem) > 200:
                    raise GatewayError("invalid_idempotency_key", "Idempotency-Key is too long", 400)
                idem_record = digest(key["id"] + ":" + idem)
                previous = store.get("request", idem_record)
                if previous:
                    idem_record = None
                    raise GatewayError(
                        "request_already_attempted",
                        "This request was already attempted; responses are not stored or replayed",
                        409,
                    )
                store.put(
                    "request",
                    idem_record,
                    {"state": "started", "created_at": time.time(), "request_id": request_id},
                )
            row = await sessions.acquire(model.id)
            # Recheck revocation after waiting for a cold open or a busy pool.
            fresh_key = store.get("key", digest(request.headers["authorization"][7:]))
            if (
                not fresh_key
                or not fresh_key["enabled"]
                or (fresh_key["models"] and model.id not in fresh_key["models"])
            ):
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
                status = upstream.status_code
                if status >= 500 or status == 429:
                    sessions.invalidate(row, "provider_http_" + str(status))
                raise GatewayError(
                    "provider_request_rejected"
                    if 400 <= status < 500 and status != 429
                    else "provider_error",
                    "Model rejected the request; check messages/options"
                    if 400 <= status < 500 and status != 429
                    else "Provider is unavailable; it has been quarantined for subsequent requests",
                    status if 400 <= status < 500 else 502,
                )
            if body.get("stream"):

                async def stream():
                    nonlocal outcome, failure_code
                    try:
                        async with asyncio.timeout(max(0, inference_deadline - time.monotonic())):
                            async for event in chat_events(upstream, cfg.response_bytes):
                                yield event
                            outcome = "succeeded"
                    except (httpx.HTTPError, GatewayError, ValueError, TimeoutError) as exc:
                        failure_code = getattr(exc, "code", "stream_interrupted")
                        sessions.invalidate(row, failure_code)
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
                raw = await bounded_body(upstream, cfg.response_bytes)
            try:
                result = validate_completion(strict_json(raw))
            except (ValueError, UnicodeError, RecursionError):
                raise GatewayError("provider_protocol", "Provider returned invalid JSON", 502) from None
            response = JSONResponse(result, headers={"X-Request-ID": request_id})
            outcome = "succeeded"
            await cleanup()
            return response
        except GatewayError as exc:
            failure_code = exc.code
            if row and exc.code in (
                "provider_protocol",
                "provider_response_too_large",
                "inference_transport",
            ):
                sessions.invalidate(row, exc.code)
            await asyncio.shield(cleanup())
            raise
        except TimeoutError:
            failure_code = "inference_timeout"
            if row:
                sessions.invalidate(row, failure_code)
            await asyncio.shield(cleanup())
            raise GatewayError("inference_timeout", "Inference exceeded the request deadline", 504) from None
        except httpx.HTTPError:
            failure_code = "inference_transport"
            if row:
                sessions.invalidate(row, failure_code)
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
