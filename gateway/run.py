import os

import uvicorn

from .runtime import tcp_listener

if __name__ == "__main__":
    os.umask(0o077)
    with tcp_listener(os.getenv("HOST", "::"), int(os.getenv("PORT", "8000"))) as listener:
        server = uvicorn.Server(
            uvicorn.Config(
                "gateway.app:create_app",
                factory=True,
                workers=1,
                access_log=False,
                limit_concurrency=128,
                timeout_keep_alive=5,
                timeout_graceful_shutdown=170,
            )
        )
        server.run(sockets=[listener])
