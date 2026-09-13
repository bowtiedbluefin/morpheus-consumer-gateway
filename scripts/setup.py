"""Generate local secrets without printing or placing them in shell history."""

import getpass
import os
import re
import secrets
from pathlib import Path


def write_new(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(value + "\n")


def main():
    root = Path(__file__).resolve().parents[1]
    directory = root / "secrets"
    directory.mkdir(mode=0o700, exist_ok=True)
    for name in ("admin-token", "node-password"):
        path = directory / name
        if not path.exists():
            write_new(path, secrets.token_urlsafe(48))
            print(f"Created secrets/{name}")
    wallet = directory / "wallet-key"
    if not wallet.exists():
        value = getpass.getpass("Consumer wallet private key (input hidden): ").strip().removeprefix("0x")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", value) or int(value, 16) == 0:
            raise SystemExit("Invalid private key; wallet secret was not written")
        write_new(wallet, value)
        print("Created secrets/wallet-key")
    print(
        "Secrets are never overwritten. On Linux, grant container UID 10001 read access as documented in README."
    )
    print("Edit .env, start Compose, then use secrets/admin-token to sign in.")


if __name__ == "__main__":
    main()
