import asyncio
import json
import time

import httpx

from gateway.node import GatewayError

MODEL = "0x" + "1" * 64
BID = "0x" + "2" * 64
PROVIDER = "0x" + "3" * 40
WALLET = "0x" + "4" * 40


class FakeNode:
    def __init__(self):
        self.sessions = {}
        self.opens = 0
        self.closes = 0
        self.open_delay = 0
        self.lose_open_response = False
        self.open_started = asyncio.Event()
        self.completions = []
        self.stream_lines = None

    async def identity(self):
        return {"wallet": WALLET, "chain": "84532", "version": "7.11.0"}

    async def catalog(self):
        return [{"Id": MODEL, "Name": "Demo model", "ModelType": "LLM"}]

    async def bid(self, id):
        return {
            "Id": BID,
            "Provider": PROVIDER,
            "ModelAgentId": MODEL,
            "DeletedAt": 0,
            "PricePerSecond": "10000000000",
        }

    async def bids(self, model):
        return [{"Bid": await self.bid(BID), "Score": 10.0}]

    async def open(self, bid, duration):
        self.opens += 1
        self.open_started.set()
        await asyncio.sleep(self.open_delay)
        id = "0x" + f"{self.opens:064x}"
        self.sessions[id] = {
            "Id": id,
            "User": WALLET,
            "Provider": PROVIDER,
            "BidID": BID,
            "ModelAgentId": MODEL,
            "EndsAt": int(time.time()) + duration,
            "OpenedAt": int(time.time()),
            "ClosedAt": 0,
            "Stake": "5000000000000000000",
        }
        if self.lose_open_response:
            raise GatewayError("node_unreachable", "Lost response after opening")
        return {"sessionID": id}

    async def session(self, id):
        if id not in self.sessions:
            raise GatewayError("node_rejected", "Session not found")
        return dict(self.sessions[id])

    async def close_session(self, id):
        self.closes += 1
        self.sessions[id]["ClosedAt"] = int(time.time())
        return {"tx": "0x" + "5" * 64}

    async def wallet_sessions(self, wallet):
        return {"sessions": list(self.sessions.values())}

    async def completion(self, session_id, model_id, body, request_id):
        self.completions.append((session_id, model_id, body, request_id))
        if body.get("stream"):
            chunk = {
                "id": "chatcmpl-demo",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Hello from the simulated provider."},
                        "finish_reason": None,
                    }
                ],
            }
            content = self.stream_lines or "data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n"
            return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-demo",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Hello from the simulated provider."},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    async def request(self, method, path, **kwargs):
        if path == "/blockchain/balance":
            return {"mor": "125000000000000000000", "eth": "50000000000000000"}
        return {"available": "0", "hold": "0"}

    async def aclose(self):
        pass


class FakeHelper:
    configured = True

    def __init__(self):
        self.restarts = []
        self.fail = False

    async def request(self, method, path, **kwargs):
        self.restarts.append(kwargs)
        if self.fail:
            raise GatewayError("helper_rejected", "Simulated restart failure")
        return {"ready": True, "rating_hash": "demo"}

    async def aclose(self):
        pass
