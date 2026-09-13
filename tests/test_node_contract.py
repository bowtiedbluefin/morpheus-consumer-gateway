import httpx
import pytest

from devtools.fakes import BID, MODEL
from gateway.config import Config
from gateway.node import GatewayError, Node


async def test_real_http_adapter_paths_auth_and_no_mutation_retry(tmp_path):
    requests = []

    async def handle(req):
        requests.append(req)
        if req.url.path.endswith("/session"):
            raise httpx.ReadTimeout("response lost", request=req)
        return httpx.Response(200, json={"choices": []})

    node = Node(Config(data_dir=tmp_path, admin_token="x" * 32, node_password="node-password"))
    await node.client.aclose()
    node.client = httpx.AsyncClient(
        base_url="http://node", transport=httpx.MockTransport(handle), auth=("admin", "node-password")
    )
    with pytest.raises(GatewayError):
        await node.open(BID, 600)
    assert len(requests) == 1
    assert requests[0].url.path == f"/blockchain/bids/{BID}/session"
    assert requests[0].headers["authorization"].startswith("Basic ")
    assert b'"sessionDuration":600' in requests[0].content
    await node.completion("0x" + "a" * 64, MODEL, {"messages": []}, "rid")
    first = requests[-1].headers["chat_id"]
    await node.completion("0x" + "a" * 64, MODEL, {"messages": []}, "rid2")
    assert first != requests[-1].headers["chat_id"]
    assert requests[-1].headers["model_id"] == MODEL
    assert requests[-1].headers["session_id"] == "0x" + "a" * 64
    await node.aclose()


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("provider healthcheck ping failed: dial failed", "provider_declined"),
        ("failed to initiate session: unavailable", "provider_declined"),
        ("provider self-reports model as not serviceable: offline", "provider_declined"),
        ("failed to open session: transaction result unknown", "node_rejected"),
        ("unexpected failure", "node_rejected"),
    ],
)
async def test_only_proven_pre_submission_errors_allow_provider_walk(tmp_path, message, expected):
    node = Node(Config(data_dir=tmp_path, admin_token="x" * 32))
    await node.client.aclose()
    node.client = httpx.AsyncClient(
        base_url="http://node",
        transport=httpx.MockTransport(lambda _: httpx.Response(500, json={"error": message})),
    )
    try:
        with pytest.raises(GatewayError) as error:
            await node.open(BID, 600)
        assert error.value.code == expected
    finally:
        await node.aclose()
