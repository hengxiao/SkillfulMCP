"""Skill bundle signing + verification (item J).

Uses Ed25519 because the signatures are compact (64 bytes), the
keys are compact (32 bytes), and the crypto is constant-time by
default — a good fit for signing skill artifacts that the agent
runtime will execute.

Signing is done out-of-band by the skill author with their own
private key; the mcp_server only stores + verifies. The list of
trusted public keys lives in `MCP_BUNDLE_SIGNING_PUBLIC_KEYS` as a
JSON object `{kid: base64url-raw-pubkey}`. The canonical digest
the signature covers is built in `compute_bundle_digest` below.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from sqlalchemy.orm import Session

from .logging_config import get_logger
from .models import Skill, SkillFile

_log = get_logger("mcp.bundle_signing")


# ---------------------------------------------------------------------------
# Canonical digest
# ---------------------------------------------------------------------------

def compute_bundle_digest(db: Session, skill_pk: int) -> bytes:
    """Canonical SHA-256 of a bundle's contents.

    Sorts files by path (so upload order doesn't change the digest)
    and hashes `<path>\\0<content>\\0` for each. An empty bundle
    digests to sha256(b"").

    Returns raw 32-byte bytes; callers typically base64url-encode
    for transport.
    """
    hasher = hashlib.sha256()
    files = (
        db.query(SkillFile)
        .filter(SkillFile.skill_pk == skill_pk)
        .order_by(SkillFile.path)
        .all()
    )
    for f in files:
        hasher.update(f.path.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(f.content)
        hasher.update(b"\x00")
    return hasher.digest()


# ---------------------------------------------------------------------------
# Trust store
# ---------------------------------------------------------------------------

def _b64url_decode(s: str) -> bytes:
    # Allow both padded and unpadded base64url.
    s = s.strip()
    pad = 4 - (len(s) % 4)
    if pad and pad < 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


def _trust_store() -> Mapping[str, Ed25519PublicKey]:
    """Parse MCP_BUNDLE_SIGNING_PUBLIC_KEYS into a dict of kid →
    loaded Ed25519 public keys. Empty dict when unset (verification
    returns False for every signature in that case)."""
    raw = os.environ.get("MCP_BUNDLE_SIGNING_PUBLIC_KEYS", "").strip()
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log.error(
            "MCP_BUNDLE_SIGNING_PUBLIC_KEYS is not valid JSON",
            extra={"error": str(exc)},
        )
        return {}
    out: dict[str, Ed25519PublicKey] = {}
    for kid, b64 in (decoded or {}).items():
        try:
            out[kid] = Ed25519PublicKey.from_public_bytes(_b64url_decode(b64))
        except Exception as exc:  # pragma: no cover - malformed key
            _log.error(
                "bundle signing key failed to load",
                extra={"kid": kid, "error": str(exc)},
            )
    return out


# ---------------------------------------------------------------------------
# Sign + verify
# ---------------------------------------------------------------------------

def sign_digest(digest: bytes, private_key_b64: str) -> str:
    """Sign a 32-byte digest with a base64url-encoded Ed25519
    private key. Returns the signature as base64url.

    Used by the mcp-cli helper + tests; the server itself never
    signs — authors sign out of band.
    """
    key = Ed25519PrivateKey.from_private_bytes(_b64url_decode(private_key_b64))
    sig = key.sign(digest)
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("utf-8")


def verify_signature(
    digest: bytes, signature_b64: str, kid: str
) -> bool:
    """Return True iff `signature_b64` is a valid Ed25519
    signature of `digest` under the public key registered for
    `kid` in MCP_BUNDLE_SIGNING_PUBLIC_KEYS.

    Returns False for unknown kid, malformed signature, or
    empty trust store — all failure modes map to "not verified"
    from the caller's perspective.
    """
    if not signature_b64 or not kid:
        return False
    store = _trust_store()
    pub = store.get(kid)
    if pub is None:
        return False
    try:
        sig = _b64url_decode(signature_b64)
    except Exception:
        return False
    try:
        pub.verify(sig, digest)
    except InvalidSignature:
        return False
    return True


def verify_skill(db: Session, skill: Skill) -> bool:
    """Convenience wrapper: computes the digest + checks the
    stored signature (if any). Returns False for unsigned rows."""
    if not skill.bundle_signature or not skill.bundle_signature_kid:
        return False
    digest = compute_bundle_digest(db, skill.pk)
    return verify_signature(
        digest, skill.bundle_signature, skill.bundle_signature_kid
    )
