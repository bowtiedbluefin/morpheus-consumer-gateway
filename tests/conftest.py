import httpx
import pytest_asyncio

from devtools.fakes import MODEL, FakeHelper, FakeNode
from gateway.app import create_app
from gateway.config import Config
from gateway.models import ModelPolicy, Policy, RecoveryPolicy

TOKEN = "test-admin-secret-" + "x" * 32
ORIGIN = "http://testserver"


@pytest_asyncio.fixture
async def env(tmp_path):
    cfg = Config(
        data_dir=tmp_path,
        admin_token=TOKEN,
        public_origin=ORIGIN,
        secure_cookie=False,
        tick_seconds=3600,
        drain_timeout=0.2,
        recovery_interval=3600,
    )
    node, helper = FakeNode(), FakeHelper()
    app = create_app(cfg, node, helper)
    async with app.router.lifespan_context(app):
        store = app.state.store
        store.put(
            "settings",
            "policy",
            Policy(
                models=[ModelPolicy(id=MODEL, alias="demo")],
                queue_seconds=1,
                recovery=RecoveryPolicy(enabled=False),
            ).model_dump(),
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            yield app, client, node, helper, cfg


async def login(client):
    res = await client.post("/admin/api/login", json={"token": TOKEN}, headers={"origin": ORIGIN})
    assert res.status_code == 200, res.text
    return {"origin": ORIGIN, "x-csrf-token": res.json()["csrf"]}


async def key(client, **settings):
    headers = await login(client)
    res = await client.post("/admin/api/keys", json={"name": "test", **settings}, headers=headers)
    assert res.status_code == 200, res.text
    return {"authorization": "Bearer " + res.json()["key"]}, res.json()["record"]
