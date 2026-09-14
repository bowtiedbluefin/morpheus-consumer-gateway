"""Runtime configuration shared by the packaged gateway and helper."""

import re
import socket


def validate_helper_token(token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise RuntimeError("HELPER_TOKEN must be 32–256 URL-safe random characters")


def tcp_listener(host, port):
    """Explicit dual-stack socket for Railway, including IPv6-only environments."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        listener.bind((host, port))
        listener.listen(128)
        return listener
    except BaseException:
        listener.close()
        raise
