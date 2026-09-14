import asyncio
import math
import secrets

import httpx

from .config import Config
from .models import ADDRESS, HASH


class GatewayError(Exception):
    def __init__(self, code: str, message: str, status: int = 503, *, outcome="unknown", transactions=None):
        self.code, self.message, self.status = code, message, status
        self.outcome = outcome
        self.transactions = transactions or []
        super().__init__(message)


def object_response(value):
    if not isinstance(value, dict):
        raise GatewayError("node_protocol", "Node returned an invalid object", 502)
    return value


def amount(value):
    try:
        if (
            isinstance(value, bool)
            or not isinstance(value, (str, int))
            or not str(value).isdigit()
            or len(str(value)) > 78
        ):
            raise ValueError()
        return str(int(value))
    except (TypeError, ValueError):
        raise GatewayError("node_protocol", "Node returned an invalid amount", 502) from None


class Node:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = httpx.AsyncClient(
            base_url=cfg.node_url.rstrip("/"),
            auth=(cfg.node_username, cfg.node_password),
            timeout=cfg.control_timeout,
            trust_env=False,
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
        )
        self.inference = httpx.AsyncClient(
            base_url=cfg.node_url.rstrip("/"),
            auth=(cfg.node_username, cfg.node_password),
            timeout=cfg.request_timeout,
            trust_env=False,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
        self.rpc = httpx.AsyncClient(timeout=cfg.control_timeout, trust_env=False)

    async def request(self, method, path, **kwargs):
        try:
            async with (
                asyncio.timeout(kwargs.get("timeout", self.cfg.control_timeout)),
                self.client.stream(method, path, **kwargs) as res,
            ):
                body = bytearray()
                async for chunk in res.aiter_bytes():
                    if len(body) + len(chunk) > self.cfg.response_bytes:
                        raise GatewayError("node_protocol", "Node response exceeds the size limit", 502)
                    body.extend(chunk)
                import json

                try:
                    data = object_response(json.loads(body))
                except ValueError:
                    raise GatewayError("node_protocol", "Node returned invalid JSON", 502) from None
                if not res.is_error:
                    return data
                opening = (
                    method == "POST" and path.startswith("/blockchain/bids/") and path.endswith("/session")
                )
                if opening and HASH.fullmatch(str(data.get("sessionID", ""))) and int(data["sessionID"], 16):
                    return data  # Open mined; local node registration/visibility failed afterward.
                message = data.get("error", "")
                progress = data.get("progress", {})
                transactions = progress.get("transactions", []) if isinstance(progress, dict) else []
                outcome = (
                    "not_submitted"
                    if isinstance(progress, dict) and progress.get("stage") == "not_submitted"
                    else "unknown"
                )
                if opening and isinstance(message, str):
                    if (
                        "SessionTooShort" in message
                        or "gateway session duration below contract minimum" in message
                    ):
                        raise GatewayError(
                            "session_duration_invalid",
                            "The contract rejected the session duration; check the network minimum and refresh the stake quote",
                            400,
                            outcome=outcome,
                            transactions=transactions,
                        )
                    if message.startswith(
                        (
                            "provider healthcheck ping failed",
                            "provider self-reports model as not serviceable",
                            "failed to initiate session",
                            "TEE attestation failed",
                        )
                    ):
                        raise GatewayError(
                            "provider_declined",
                            "Provider declined or failed verification before submission",
                            outcome="not_submitted",
                            transactions=transactions,
                        )
                    if message.startswith(
                        (
                            "failed to parse token supply",
                            "failed to parse token budget",
                            "failed to get bid",
                            "failed to get provider",
                            "failed to get my address",
                            "cannot open session with own bid",
                            "gateway stake limit exceeded",
                            "invalid token supply",
                            "invalid emissions budget",
                        )
                    ):
                        raise GatewayError(
                            "open_preflight_failed",
                            "Opening preflight failed; check RPC, bid and configured stake limit",
                            503,
                            outcome="not_submitted",
                            transactions=transactions,
                        )
                if (
                    method == "POST"
                    and path.endswith("/close")
                    and isinstance(message, str)
                    and message.startswith(
                        ("cannot get private key", "failed to get session report from user")
                    )
                ):
                    outcome = "not_submitted"
                if method == "GET":
                    outcome = "not_submitted"
                raise GatewayError(
                    "node_rejected",
                    f"Consumer node returned HTTP {res.status_code}; inspect activity for the operation",
                    502,
                    outcome=outcome,
                    transactions=transactions,
                )
        except (httpx.HTTPError, TimeoutError):
            raise GatewayError(
                "node_unreachable",
                "The consumer node did not respond",
                outcome="unknown" if method == "POST" else "not_submitted",
            ) from None

    async def identity(self):
        data = await self.request("GET", "/config")
        derived = object_response(data.get("DerivedConfig"))
        wallet = str(derived.get("WalletAddress", "")).lower()
        if not ADDRESS.fullmatch(wallet) or int(wallet, 16) == 0:
            raise GatewayError("wallet_not_ready", "The node wallet is not ready")
        chain = amount(derived.get("ChainID"))
        if chain == "0" or not isinstance(data.get("Version"), str):
            raise GatewayError("node_protocol", "Node identity is incomplete")
        return {
            "wallet": wallet,
            "chain": chain,
            "version": data["Version"],
            "capabilities": data.get("GatewayCapabilities", []),
        }

    async def catalog(self):
        result = []
        for offset in range(0, 10000, 100):
            data = await self.request("GET", "/blockchain/models", params={"offset": offset, "limit": 100})
            batch = data.get("models")
            if not isinstance(batch, list):
                raise GatewayError("node_protocol", "Node catalog is invalid", 502)
            for m in batch:
                if not isinstance(m, dict) or not HASH.fullmatch(str(m.get("Id", ""))):
                    raise GatewayError("node_protocol", "Node returned an invalid model", 502)
                if not m.get("IsDeleted") and m.get("ModelType") == "LLM":
                    result.append(m)
            if len(batch) < 100:
                return result
        raise GatewayError("catalog_limit", "Catalog exceeds the pagination limit")

    async def model(self, model_id):
        # v7.11 exposes a paginated model catalog, not GET /models/:id.
        for offset in range(0, 10000, 100):
            data = await self.request(
                "GET", "/blockchain/models", params={"offset": offset, "limit": 100, "order": "desc"}
            )
            batch = data.get("models")
            if not isinstance(batch, list):
                raise GatewayError("node_protocol", "Node catalog is invalid", 502)
            for model in batch:
                object_response(model)
                if str(model.get("Id", "")).lower() != model_id.lower():
                    continue
                if model.get("IsDeleted") or model.get("ModelType") != "LLM":
                    raise GatewayError(
                        "unsupported_model",
                        "This model does not support chat completions",
                        400,
                        outcome="not_submitted",
                    )
                return model
            if len(batch) < 100:
                raise GatewayError(
                    "model_not_found", "Model is absent from the node catalog", 404, outcome="not_submitted"
                )
        raise GatewayError("catalog_limit", "Model lookup exceeds the catalog pagination limit")

    async def bids(self, model_id):
        data = (await self.request("GET", f"/blockchain/models/{model_id}/bids/rated")).get("bids")
        if not isinstance(data, list):
            raise GatewayError("node_protocol", "Node returned an invalid bid list", 502)
        for entry in data:
            object_response(entry)
            bid = object_response(entry.get("Bid"))
            if not HASH.fullmatch(str(bid.get("Id", ""))) or not ADDRESS.fullmatch(
                str(bid.get("Provider", ""))
            ):
                raise GatewayError("node_protocol", "Node returned an invalid bid", 502)
            score = entry.get("Score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                raise GatewayError("node_protocol", "Node returned an invalid score", 502)
            amount(bid.get("PricePerSecond"))
        return data

    async def bid(self, bid_id):
        return object_response((await self.request("GET", f"/blockchain/bids/{bid_id}")).get("bid"))

    async def open(self, bid_id, duration, max_stake=None, operation_id=None):
        body = {"sessionDuration": duration}
        if max_stake is not None:
            body["maxStakeWei"] = str(max_stake)
        return await self.request(
            "POST",
            f"/blockchain/bids/{bid_id}/session",
            json=body,
            headers={"X-Gateway-Operation": operation_id} if operation_id else {},
            timeout=self.cfg.open_timeout,
        )

    async def session(self, session_id):
        return object_response(
            (await self.request("GET", f"/blockchain/sessions/{session_id}")).get("session")
        )

    async def close_session(self, session_id, operation_id=None):
        return await self.request(
            "POST",
            f"/blockchain/sessions/{session_id}/close",
            json={},
            headers={"X-Gateway-Operation": operation_id} if operation_id else {},
            timeout=self.cfg.open_timeout,
        )

    async def wallet_sessions(self, wallet, offset=0, limit=100):
        data = await self.request(
            "GET",
            "/blockchain/sessions/user",
            params={"user": wallet, "limit": limit, "offset": offset, "order": "desc"},
        )
        if not isinstance(data.get("sessions"), list) or any(
            not isinstance(s, dict) for s in data["sessions"]
        ):
            raise GatewayError("node_protocol", "Wallet session list is invalid", 502)
        return data

    async def balances(self):
        data = await self.request("GET", "/blockchain/balance")
        return {k: amount(data.get(k)) for k in ("mor", "eth")}

    async def stakes(self):
        data = await self.request("GET", "/blockchain/stakes/onhold", params={"iterations": 255})
        return {k: amount(data.get(k)) for k in ("available", "hold")}

    async def withdraw(self, operation_id=None):
        return await self.request(
            "POST",
            "/blockchain/stakes/withdraw",
            json={"iterations": 255},
            headers={"X-Gateway-Operation": operation_id} if operation_id else {},
            timeout=self.cfg.open_timeout,
        )

    async def estimate(self, bid, duration):
        supply = amount((await self.request("GET", "/blockchain/token/supply")).get("supply"))
        budget = amount((await self.request("GET", "/blockchain/sessions/budget")).get("budget"))
        if not int(budget):
            raise GatewayError(
                "budget_unavailable", "Network emissions budget is unavailable", outcome="not_submitted"
            )
        numerator = int(supply) * int(amount(bid.get("PricePerSecond"))) * duration * 10001
        denominator = int(budget) * 10000
        return (numerator + denominator - 1) // denominator

    async def rpc_call(self, method, params):
        if not self.cfg.rpc_url:
            raise GatewayError(
                "rpc_not_configured", "Configure RPC_URL for transaction receipt reconciliation"
            )
        try:
            res = await self.rpc.post(
                self.cfg.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )
            data = res.json()
            if res.is_error or not isinstance(data, dict) or "error" in data or "result" not in data:
                raise ValueError()
            return data["result"]
        except (httpx.HTTPError, ValueError):
            raise GatewayError("rpc_unavailable", "Recovery RPC did not return a valid result") from None

    async def receipt(self, tx, chain):
        if not HASH.fullmatch(tx):
            raise GatewayError("invalid_transaction", "Invalid transaction ID", 400)
        rpc_chain = await self.rpc_call("eth_chainId", [])
        if int(rpc_chain, 16) != int(chain):
            raise GatewayError("rpc_chain_mismatch", "Recovery RPC is connected to another network")
        result = await self.rpc_call("eth_getTransactionReceipt", [tx])
        if result is not None:
            object_response(result)
        return result

    async def completion(self, session_id, model_id, body, request_id):
        request = self.inference.build_request(
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
            return await self.inference.send(request, stream=True)
        except httpx.HTTPError:
            raise GatewayError("inference_transport", "Could not reach the node for inference", 502) from None

    async def aclose(self):
        await self.client.aclose()
        await self.inference.aclose()
        await self.rpc.aclose()


class Helper:
    def __init__(self, socket):
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
                raise GatewayError("helper_rejected", "Node operation failed; inspect the activity log")
            return object_response(res.json())
        except (httpx.HTTPError, ValueError):
            raise GatewayError(
                "helper_unreachable", "Cannot reach the local node management helper"
            ) from None

    async def aclose(self):
        await self.client.aclose()
