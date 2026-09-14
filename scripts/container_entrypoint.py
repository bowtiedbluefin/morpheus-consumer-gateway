"""Initialize mounted data ownership, then run the application as UID 10001.

Railway starts this entrypoint as root via RAILWAY_RUN_UID=0 because new volumes
are root-owned. Compose continues to run directly as the image's non-root user.
"""

import os
import sys
from pathlib import Path


def main():
    os.umask(0o077)
    if os.getuid() == 0:
        variable, default = (
            ("NODE_DATA_DIR", "/node-data") if os.getenv("APP_ROLE") == "node" else ("DATA_DIR", "/data")
        )
        directory = Path(os.getenv(variable, default))
        if not directory.is_absolute() or directory == Path("/") or directory.is_symlink():
            raise RuntimeError("Data directory must be an absolute, non-root, non-symlink path")
        directory.mkdir(parents=True, exist_ok=True)
        # Do not follow links out of the service's dedicated data volume.
        for root, directories, files in os.walk(directory, followlinks=False):
            os.chown(root, 10001, 10001, follow_symlinks=False)
            for name in directories + files:
                os.chown(Path(root) / name, 10001, 10001, follow_symlinks=False)
        os.chmod(directory, 0o700)
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
