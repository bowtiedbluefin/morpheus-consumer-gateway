import secrets

import httpx

from .config import Config
from .models import ADDRESS


class GatewayError(Exception):
    def __init__(self, code: str, message: str, status: int = 503):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class Node:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = httpx.AsyncClient(
            base_url=cfg.node_url.rstrip("/"),
            auth=(cfg.node_username, cfg.node_password),
            timeout=cfg.request_timeout,
            trust_env=False,
        )

    async def request(self, method: str, path: str, **kwargs):
        try:
            res = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise GatewayError("node_unreachable", "The consumer node did not respond") from None
        if res.is_error:
            # These exact prefixes originate before OpenSession in the pinned node's
            # tryOpenSession. Only these proven pre-chain failures permit a bid walk.
            if method == "POST" and path.startswith("/blockchain/bids/") and path.endswith("/session"):
                try:
                    error = res.json().get("error", "")
                except (ValueError, AttributeError):
                    error = ""
                if isinstance(error, str) and error.startswith(
                    (
                        "provider healthcheck ping failed",
                        "provider self-reports model as not serviceable",
                        "failed to initiate session",
                    )
                ):
                    raise GatewayError(
                        "provider_declined", "Provider could not accept a session before submission"
                    )
            # No raw upstream bodies: they may contain internal URLs, tokens or prompts.
            raise GatewayError("node_rejected", f"Consumer node returned HTTP {res.status_code}", 502)
        try:
            return res.json()
        except ValueError:
            raise GatewayError("node_protocol", "Consumer node returned an invalid response", 502) from None

    async def identity(self):
        data = await self.request("GET", "/config")
        derived = data.get("DerivedConfig", {})
        wallet = str(derived.get("WalletAddress", "")).lower()
        if not ADDRESS.fullmatch(wallet) or int(wallet, 16) == 0:
            raise GatewayError("wallet_not_ready", "The node wallet is not ready")
        return {
            "wallet": wallet,
            "chain": str(derived.get("ChainID", "")),
            "version": data.get("Version", "unknown"),
        }

    async def catalog(self):
        result = []
        for offset in range(0, 10000, 100):
            data = await self.request("GET", "/blockchain/models", params={"offset": offset, "limit": 100})
            batch = data.get("models", [])
            result.extend(m for m in batch if not m.get("IsDeleted"))
            if len(batch) < 100:
                return result
        raise GatewayError("catalog_limit", "Catalog exceeds this release's pagination limit")

    async def bids(self, model_id):
        data = await self.request("GET", f"/blockchain/models/{model_id}/bids/rated")
        return data.get("bids", [])

    async def bid(self, bid_id):
        return (await self.request("GET", f"/blockchain/bids/{bid_id}")).get("bid", {})

    async def open(self, bid_id, duration):
        # Absolutely no automatic HTTP retries on this chain mutation.
        return await self.request(
            "POST",
            f"/blockchain/bids/{bid_id}/session",
            json={"sessionDuration": duration},
            timeout=self.cfg.open_timeout,
        )

    async def session(self, session_id):
        return (await self.request("GET", f"/blockchain/sessions/{session_id}")).get("session", {})

    async def close_session(self, session_id):
        return await self.request(
            "POST", f"/blockchain/sessions/{session_id}/close", json={}, timeout=self.cfg.open_timeout
        )

    async def wallet_sessions(self, wallet):
        return await self.request(
            "GET", "/blockchain/sessions/user", params={"user": wallet, "limit": 100, "order": "desc"}
        )

    async def completion(self, session_id, model_id, body, request_id):
        # Deliberately construct internal headers; never forward caller routing/auth headers.
        request = self.client.build_request(
            "POST",
            "/v1/chat/completions",
            json=body,
            headers={
                "session_id": session_id,
                "model_id": model_id,
                "chat_id": "0x" + secrets.token_hex(32),
                "X-Request-ID": request_id,
            },
        )
        try:
            return await self.client.send(request, stream=True)
        except httpx.HTTPError:
            raise GatewayError("inference_transport", "Could not reach the node for inference", 502) from None

    async def aclose(self):
        await self.client.aclose()


class Helper:
    def __init__(self, socket: str):
        self.configured = bool(socket)
        self.client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket),
            base_url="http://helper",
            timeout=240,
            trust_env=False,
        )

    async def request(self, method, path, **kwargs):
        if not self.configured:
            raise GatewayError("helper_missing", "Node management helper is not connected")
        try:
            res = await self.client.request(method, path, **kwargs)
            if res.is_error:
                raise GatewayError("helper_rejected", "Node restart/apply failed; inspect local helper logs")
            return res.json()
        except httpx.HTTPError:
            raise GatewayError(
                "helper_unreachable", "Cannot reach the local node management helper"
            ) from None

    async def aclose(self):
        await self.client.aclose()
