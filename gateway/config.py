import os
from dataclasses import dataclass
from pathlib import Path


def secret(name: str, default: str = "") -> str:
    path = os.getenv(f"{name}_FILE")
    return Path(path).read_text().strip() if path else os.getenv(name, default)


@dataclass
class Config:
    data_dir: Path
    admin_token: str
    node_url: str = "http://127.0.0.1:8082"
    node_username: str = "admin"
    node_password: str = ""
    helper_socket: str = ""
    public_origin: str = "http://localhost:8000"
    secure_cookie: bool = True
    static_dir: Path = Path("web/dist")
    request_timeout: float = 120
    open_timeout: float = 180
    drain_timeout: float = 150
    tick_seconds: float = 10
    demo: bool = False

    @classmethod
    def from_env(cls):
        token = secret("ADMIN_TOKEN")
        if len(token) < 32:
            raise RuntimeError("Set ADMIN_TOKEN_FILE to a generated secret of at least 32 characters")
        return cls(
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            admin_token=token,
            node_url=os.getenv("NODE_URL", "http://127.0.0.1:8082"),
            node_username=os.getenv("NODE_USERNAME", "admin"),
            node_password=secret("NODE_PASSWORD"),
            helper_socket=os.getenv("HELPER_SOCKET", ""),
            public_origin=os.getenv("PUBLIC_ORIGIN", "http://localhost:8000").rstrip("/"),
            secure_cookie=os.getenv("COOKIE_SECURE", "true").lower() == "true",
            static_dir=Path(os.getenv("STATIC_DIR", "web/dist")),
        )
