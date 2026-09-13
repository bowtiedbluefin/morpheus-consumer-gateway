import os
from pathlib import Path

import uvicorn

if __name__ == "__main__":
    os.umask(0o077)
    socket = Path(os.getenv("HELPER_SOCKET", "/control/helper.sock"))
    socket.parent.mkdir(parents=True, exist_ok=True)
    # The container owns this socket and runs exactly one supervisor.
    socket.unlink(missing_ok=True)
    uvicorn.run("node_helper.app:create_app", factory=True, uds=str(socket), workers=1, access_log=False)
