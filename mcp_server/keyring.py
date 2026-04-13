"""
JWT signing key ring.

Two configuration modes:

- **Legacy single-secret** (backwards compatible). `MCP_JWT_SECRET` holds
  one HMAC secret; the keyring wraps it with `kid="primary"`. Tokens
  signed in this mode carry `kid: primary` in the JWT header so future
  multi-key deployments can still verify them.

- **Multi-key rotation**. `MCP_JWT_KEYS` is a JSON object of
  `{kid: secret}`. `MCP_JWT_ACTIVE_KID` names the one used to sign new
  tokens; every other key is verify-only. Rotation workflow:
    1. Add a new key to `MCP_JWT_KEYS` alongside the current one.
    2. Deploy.
    3. Flip `MCP_JWT_ACTIVE_KID` to the new kid.
    4. Deploy. Old tokens still verify (old kid is still in the ring).
    5. After the longest-lived old token has expired, remove the old
       kid from `MCP_JWT_KEYS` and deploy.

Both modes share `KeyRing.algorithm` (HS256 by default). Asymmetric
(RS256/ES256) support is a later step — the productization plan will move
signing keys into a cloud KMS; the same `KeyRing` shape absorbs that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .config import Settings


@dataclass(frozen=True)
class KeyRing:
    keys: dict[str, str]
    active_kid: str
    algorithm: str = "HS256"

    def get_secret(self, kid: str) -> str | None:
        return self.keys.get(kid)

    @property
    def active_secret(self) -> str:
        return self.keys[self.active_kid]

    @property
    def known_kids(self) -> list[str]:
        return sorted(self.keys.keys())


def build_keyring(settings: Settings) -> KeyRing:
    """Construct a keyring from validated Settings."""
    if settings.jwt_keys_raw:
        try:
            keys = json.loads(settings.jwt_keys_raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"MCP_JWT_KEYS is not valid JSON: {exc}"
            ) from exc
        if not isinstance(keys, dict) or not keys:
            raise RuntimeError(
                "MCP_JWT_KEYS must be a non-empty JSON object {kid: secret}"
            )
        for kid, secret in keys.items():
            if not isinstance(kid, str) or not isinstance(secret, str) or not secret:
                raise RuntimeError(
                    "MCP_JWT_KEYS entries must be {str: non-empty str}"
                )
        active = settings.jwt_active_kid
        if active not in keys:
            raise RuntimeError(
                f"MCP_JWT_ACTIVE_KID={active!r} not present in MCP_JWT_KEYS "
                f"(known: {sorted(keys)})"
            )
        return KeyRing(
            keys=dict(keys),
            active_kid=active,
            algorithm=settings.jwt_algorithm,
        )

    # Legacy mode — single secret wrapped under the default kid.
    return KeyRing(
        keys={"primary": settings.jwt_secret},
        active_kid="primary",
        algorithm=settings.jwt_algorithm,
    )
