import os
from pathlib import Path

from gateway.app import create_app as gateway_app
from gateway.config import Config

from .fakes import FakeHelper, FakeNode


def create_app():
    token = os.environ.get("ADMIN_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("Demo also requires an ADMIN_TOKEN of at least 32 characters")
    cfg = Config(
        data_dir=Path(os.getenv("DATA_DIR", "data/demo")),
        admin_token=token,
        public_origin=os.getenv("PUBLIC_ORIGIN", "http://localhost:8000"),
        secure_cookie=False,
        demo=True,
    )
    return gateway_app(cfg, node=FakeNode(), helper=FakeHelper())
