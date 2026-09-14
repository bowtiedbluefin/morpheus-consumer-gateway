import os
from pathlib import Path

import uvicorn

from gateway.config import secret
from gateway.runtime import tcp_listener, validate_helper_token


def transport():
    mode = os.getenv("HELPER_TRANSPORT", "unix")
    if mode not in ("unix", "http"):
        raise RuntimeError("HELPER_TRANSPORT must be unix or http")
    if mode == "http":
        validate_helper_token(secret("HELPER_TOKEN"))
    return mode


if __name__ == "__main__":
    os.umask(0o077)
    if transport() == "http":
        with tcp_listener(os.getenv("HELPER_HOST", "::"), int(os.getenv("HELPER_PORT", "8083"))) as listener:
            server = uvicorn.Server(
                uvicorn.Config(
                    "node_helper.app:create_app",
                    factory=True,
                    workers=1,
                    access_log=False,
                    limit_concurrency=64,
                    timeout_graceful_shutdown=150,
                )
            )
            server.run(sockets=[listener])
    else:
        socket = Path(os.getenv("HELPER_SOCKET", "/control/helper.sock"))
        socket.parent.mkdir(parents=True, exist_ok=True)
        socket.unlink(missing_ok=True)
        uvicorn.run("node_helper.app:create_app", factory=True, uds=str(socket), workers=1, access_log=False)
