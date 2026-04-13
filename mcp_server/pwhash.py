"""Password hashing helpers — shared by the Web UI and the mcp_server.

bcrypt with a 72-byte input cap (the library silently truncates longer
inputs, so we do it explicitly to make the behavior deterministic).

This module exists so that `mcp_server.routers.users` can hash/verify
passwords without importing from `webui`, which would reverse the
dependency direction.
"""

from __future__ import annotations

import bcrypt

_MAX_PASSWORD_BYTES = 72


def _encode(plain: str) -> bytes:
    return plain.encode("utf-8")[:_MAX_PASSWORD_BYTES]


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_encode(plain), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(_encode(plain), hashed.encode("utf-8"))
    except ValueError:
        return False
