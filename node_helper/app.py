import asyncio
import hashlib
import json
import os
import signal
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import Field

from gateway.config import secret
from gateway.models import Providers, StrictModel, Weights


class Params(StrictModel):
    weights: Weights


class Rating(StrictModel):
    algorithm: str = Field(default="default", pattern="^default$")
    providerAllowlist: list[str] = Field(default_factory=list)
    providerDenylist: list[str] = Field(default_factory=list)
    params: Params

    def validated(self):
        addresses = Providers(allow=self.providerAllowlist, deny=self.providerDenylist)
        return {**self.model_dump(), "providerAllowlist": addresses.allow, "providerDenylist": addresses.deny}


class RestartRequest(StrictModel):
    rating: Rating | None = None
    operation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


def atomic_write(path: Path, content: bytes):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("wb") as file:
        os.chmod(temp, 0o600)
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Supervisor:
    def __init__(
        self,
        directory: Path,
        command: list[str],
        environment: dict,
        node_url="http://127.0.0.1:8082",
        password="",
        readiness_seconds=90,
    ):
        self.directory, self.command, self.environment = directory, command, environment
        self.rating_path = directory / "rating-config.json"
        self.backup = directory / "rating-config.previous.json"
        self.journal = directory / "applying"
        self.process = None
        self.lock = asyncio.Lock()
        self.node_url, self.password = node_url, password
        self.readiness_seconds = readiness_seconds
        self.last_error = None
        self.health = {"ready": False}

    async def start(self):
        # Command comes exclusively from trusted server setup, never the HTTP request.
        self.process = await asyncio.create_subprocess_exec(
            *self.command, cwd=self.directory, env=self.environment, start_new_session=True
        )

    async def stop(self):
        process = self.process
        if process and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(process.wait(), 20)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()

    async def ready(self):
        deadline = asyncio.get_running_loop().time() + self.readiness_seconds
        async with httpx.AsyncClient(timeout=2, auth=("admin", self.password), trust_env=False) as client:
            while asyncio.get_running_loop().time() < deadline:
                if not self.process or self.process.returncode is not None:
                    raise RuntimeError("Node process exited before readiness")
                try:
                    res = await client.get(self.node_url + "/config")
                    if res.status_code == 200:
                        balance = await client.get(self.node_url + "/blockchain/balance")
                        if balance.status_code == 200:
                            self.health = {"ready": True}
                            return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
        raise RuntimeError("Node readiness timed out")

    async def restart(self, rating: dict | None):
        async with self.lock:
            self.health = {"ready": False}
            if rating is not None:
                old = self.rating_path.read_bytes()
                atomic_write(self.backup, old)
                atomic_write(self.journal, b"pending")
                atomic_write(self.rating_path, json.dumps(rating).encode())
            try:
                await self.stop()
                await self.start()
                await self.ready()
                self.last_error = None
            except BaseException:
                self.last_error = "Restart failed"
                if self.journal.exists():
                    atomic_write(self.rating_path, self.backup.read_bytes())
                    # Restore the prior config even if restarting that version also fails.
                    await self.stop()
                    await self.start()
                    try:
                        await self.ready()
                        self.journal.unlink(missing_ok=True)
                    except Exception:
                        self.last_error = "Rollback restored config but node is not ready"
                raise
            self.journal.unlink(missing_ok=True)
            return {"ready": True, "rating_hash": hashlib.sha256(self.rating_path.read_bytes()).hexdigest()}

    async def monitor(self):
        failures = 0
        while True:
            await asyncio.sleep(5)
            try:
                if self.lock.locked():
                    continue
                if self.process and self.process.returncode is not None:
                    async with self.lock:
                        self.last_error = "Node exited; supervisor restarted it"
                        await self.start()
                async with httpx.AsyncClient(
                    timeout=2, auth=("admin", self.password), trust_env=False
                ) as client:
                    config = await client.get(self.node_url + "/config")
                    failures = 0 if config.status_code == 200 else failures + 1
                    balance = await client.get(self.node_url + "/blockchain/balance")
                    self.health = {"ready": config.status_code == 200 and balance.status_code == 200}
            except httpx.HTTPError:
                failures += 1
                self.health = {"ready": False}
            except Exception:
                self.last_error = "Node monitoring needs attention"
            if failures >= 3 and not self.lock.locked():
                try:
                    await self.restart(None)
                    failures = 0
                except Exception:
                    self.last_error = "Unresponsive node could not be restarted"


def create_app(supervisor=None):
    if supervisor is None:
        directory = Path(os.getenv("NODE_DATA_DIR", "/node-data"))
        directory.mkdir(parents=True, exist_ok=True)
        password = secret("NODE_PASSWORD")
        wallet = secret("WALLET_PRIVATE_KEY")
        if len(password) < 32 or not wallet:
            raise RuntimeError("Node password and wallet secret files are required")
        (directory / "storage").mkdir(parents=True, exist_ok=True, mode=0o700)
        env = dict(os.environ)
        # Published v7 network defaults; explicit deployment values take precedence.
        networks = {
            "8453": (
                "https://base.blockscout.com/api",
                "0x6aBE1d282f72B474E54527D93b979A4f64d3030a",
                "0x7431aDa8a591C955a994a21710752EF9b882b8e3",
            ),
            "84532": (
                "https://base-sepolia.blockscout.com/api",
                "0xA328196f2438DADA5ab729E39388D86896c27c85",
                "0x5C80Ddd187054E1E4aBBfFCD750498e81d34FfA3",
            ),
        }
        chain = env.get("ETH_NODE_CHAIN_ID", "8453")
        if chain not in networks:
            raise RuntimeError("Supported node networks are Base and Base Sepolia")
        for name, value in zip(
            ("BLOCKSCOUT_API_URL", "DIAMOND_CONTRACT_ADDRESS", "MOR_TOKEN_ADDRESS"), networks[chain]
        ):
            if not env.get(name):
                env[name] = value
        env.update(
            WALLET_PRIVATE_KEY=wallet,
            COOKIE_CONTENT="admin:" + password,
            COOKIE_FILE_PATH=str(directory / ".cookie"),
            AUTH_CONFIG_FILE_PATH=str(directory / "proxy.conf"),
            RATING_CONFIG_PATH=str(directory / "rating-config.json"),
            PROXY_STORAGE_PATH=str(directory / "storage"),
            GATEWAY_JOURNAL_PATH=str(directory / "gateway-journal"),
            PROXY_STORE_CHAT_CONTEXT="false",
            PROXY_FORWARD_CHAT_CONTEXT="false",
        )
        for name in ("PROVIDER_ALLOW_LIST", "RATING_CONFIG_CONTENT"):
            env.pop(name, None)
        supervisor = Supervisor(directory, ["/usr/local/bin/proxy-router"], env, password=password)

    @asynccontextmanager
    async def lifespan(app):
        supervisor.directory.mkdir(parents=True, exist_ok=True)
        if supervisor.journal.exists():
            atomic_write(supervisor.rating_path, supervisor.backup.read_bytes())
            supervisor.journal.unlink()
        if not supervisor.rating_path.exists():
            from gateway.models import Policy

            atomic_write(supervisor.rating_path, json.dumps(Policy().rating()).encode())
        await supervisor.start()
        monitor = asyncio.create_task(supervisor.monitor())
        try:
            yield
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            await supervisor.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/status")
    async def status():
        return {
            "running": supervisor.process is not None and supervisor.process.returncode is None,
            "last_error": supervisor.last_error,
            "ready": supervisor.health["ready"],
            "rating": json.loads(supervisor.rating_path.read_text())
            if supervisor.rating_path.exists()
            else None,
            "rating_hash": hashlib.sha256(supervisor.rating_path.read_bytes()).hexdigest()
            if supervisor.rating_path.exists()
            else None,
        }

    @app.get("/journal/{operation_id}")
    async def journal(operation_id: str):
        import re

        if not re.fullmatch(r"[a-f0-9]{32}", operation_id):
            raise HTTPException(400, "Invalid operation ID")
        path = supervisor.directory / "gateway-journal" / (operation_id + ".json")
        if not path.exists():
            raise HTTPException(404, "Operation not recorded")
        try:
            return json.loads(path.read_bytes())
        except ValueError:
            raise HTTPException(503, "Operation journal not yet readable") from None

    @app.get("/operations/{operation_id}")
    async def operation(operation_id: str):
        import re

        if not re.fullmatch(r"[a-f0-9]{32}", operation_id):
            raise HTTPException(400, "Invalid operation ID")
        path = supervisor.directory / ("operation-" + operation_id + ".json")
        if not path.exists():
            raise HTTPException(404, "Operation not recorded")
        return json.loads(path.read_bytes())

    @app.post("/restart")
    async def restart(body: RestartRequest):
        # Persist intent before changing files/processes. A retried operation ID
        # returns its status, never repeats a restart.
        path = (
            supervisor.directory / ("operation-" + body.operation_id + ".json") if body.operation_id else None
        )
        fingerprint = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
        if path and path.exists():
            previous = json.loads(path.read_bytes())
            if previous["fingerprint"] != fingerprint:
                raise HTTPException(409, "Operation ID reused with different settings")
            if previous["state"] == "succeeded":
                return previous["result"]
            return JSONResponse(
                {"error": "restart_already_attempted", "operation": previous}, status_code=409
            )
        record = {"state": "running", "fingerprint": fingerprint}
        if path:
            atomic_write(path, json.dumps(record).encode())
        try:
            result = await supervisor.restart(body.rating.validated() if body.rating else None)
            record.update(state="succeeded", result=result)
            if path:
                atomic_write(path, json.dumps(record).encode())
            return result
        except BaseException as exc:
            record.update(
                state="failed" if isinstance(exc, Exception) else "interrupted",
                message=supervisor.last_error or "Restart interrupted",
            )
            if path:
                atomic_write(path, json.dumps(record).encode())
            if not isinstance(exc, Exception):
                raise
            return JSONResponse({"error": "restart_failed", "message": record["message"]}, status_code=503)

    return app
