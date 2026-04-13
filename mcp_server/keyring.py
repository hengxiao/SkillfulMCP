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
    """Signing + verification key material.

    `keys` maps kid → key material. For HMAC (HS*), the value is the
    shared secret string; for asymmetric (RS* / ES*) the value is
    the PEM private key (for the active kid — signing) plus PEM
    public keys (for every non-active kid — verify-only). The
    `algorithm` field tells consumers which family to use.

    `public_keys` is the paired public-PEM map used by the JWKS
    endpoint when the ring is asymmetric. Empty dict in HMAC mode
    because there's no public half.
    """

    keys: dict[str, str]
    active_kid: str
    algorithm: str = "HS256"
    public_keys: dict[str, str] | None = None

    def get_secret(self, kid: str) -> str | None:
        return self.keys.get(kid)

    @property
    def active_secret(self) -> str:
        return self.keys[self.active_kid]

    @property
    def known_kids(self) -> list[str]:
        return sorted(self.keys.keys())

    @property
    def is_asymmetric(self) -> bool:
        return self.algorithm.upper() in {"RS256", "RS384", "RS512",
                                           "ES256", "ES384", "ES512"}


def build_keyring(settings: Settings) -> KeyRing:
    """Construct a keyring from validated Settings."""
    # Wave 9 item I — asymmetric path takes precedence when PEM is set.
    if settings.jwt_private_key_pem:
        algorithm = settings.jwt_algorithm
        if algorithm.upper().startswith("HS"):
            # Caller gave us a private key but left algorithm as HS256
            # by default — flip to RS256 automatically so the common
            # case doesn't require two env changes.
            algorithm = "RS256"
        kid = settings.jwt_asymmetric_kid
        public_pem = (
            settings.jwt_public_key_pem
            or _public_pem_from_private(settings.jwt_private_key_pem)
        )
        return KeyRing(
            keys={kid: settings.jwt_private_key_pem},
            active_kid=kid,
            algorithm=algorithm,
            public_keys={kid: public_pem},
        )

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


def _public_pem_from_private(private_pem: str) -> str:
    """Derive the public PEM from the private one so operators only
    have to configure the private half — the public half is a lossy
    extract of the private key.
    """
    from cryptography.hazmat.primitives import serialization

    private = serialization.load_pem_private_key(
        private_pem.encode("utf-8"), password=None
    )
    return private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")


def public_jwks(keyring: KeyRing) -> dict:
    """Return the JWKS payload for `/.well-known/jwks.json`.

    HMAC keyrings return `{"keys": []}` — there's no public half to
    publish. Callers can treat that as "no JWKS to serve."
    """
    if not keyring.is_asymmetric or not keyring.public_keys:
        return {"keys": []}

    import base64
    from cryptography.hazmat.primitives import serialization

    def b64u(n: int) -> str:
        length = max(1, (n.bit_length() + 7) // 8)
        return base64.urlsafe_b64encode(
            n.to_bytes(length, "big")
        ).rstrip(b"=").decode()

    entries: list[dict] = []
    for kid, pem in keyring.public_keys.items():
        pub = serialization.load_pem_public_key(pem.encode("utf-8"))
        # Only RSA supported in the first cut (ES* to follow).
        numbers = getattr(pub, "public_numbers", None)
        if numbers is None:
            continue
        nums = numbers()
        entries.append({
            "kty": "RSA",
            "use": "sig",
            "alg": keyring.algorithm,
            "kid": kid,
            "n": b64u(nums.n),
            "e": b64u(nums.e),
        })
    return {"keys": entries}
