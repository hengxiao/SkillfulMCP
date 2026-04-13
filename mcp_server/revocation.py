"""
Token revocation list.

In-process `jti` deny-list, thread-safe. Each revocation stores an
`expires_at` wall-clock timestamp; entries past that point are lazily
purged on lookup (so a token that was revoked and would have expired
anyway doesn't permanently occupy memory).

Not a substitute for Redis — multi-replica deployments need shared state.
The `RevocationList` interface is small on purpose so swapping the
storage layer later is a localized change (productization §3.1).
"""

from __future__ import annotations

import time
from threading import Lock


class RevocationList:
    def __init__(self) -> None:
        self._entries: dict[str, float] = {}
        self._lock = Lock()

    def revoke(self, jti: str, *, expires_at: float | None = None) -> None:
        """Mark `jti` as revoked. `expires_at` defaults to 24h from now."""
        if not jti:
            return
        with self._lock:
            self._entries[jti] = (
                expires_at if expires_at is not None else time.time() + 86400
            )

    def is_revoked(self, jti: str) -> bool:
        """Return True if `jti` is in the list AND still within its TTL.

        Lazily purges its own entry when expired, so the deny-list
        doesn't grow without bound for revoked-then-expired tokens.
        """
        if not jti:
            return False
        now = time.time()
        with self._lock:
            exp = self._entries.get(jti)
            if exp is None:
                return False
            if now > exp:
                del self._entries[jti]
                return False
            return True

    def purge_expired(self) -> int:
        """Drop all expired entries. Returns the count removed."""
        now = time.time()
        with self._lock:
            expired = [jti for jti, exp in self._entries.items() if now > exp]
            for jti in expired:
                del self._entries[jti]
            return len(expired)

    def reset(self) -> None:
        """Test helper: drop everything."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
